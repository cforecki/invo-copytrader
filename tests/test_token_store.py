"""Token persistence tests. Tokens here are fabricated JWT-shaped strings (TEST FIXTURES)."""

import io
import json
import os
import stat
import time

import pytest

import invo_auth
import invo_client as IC
from ratelimit import SlidingWindowLimiter
from test_invo_client import FakeHTTP, Resp, jwt
from token_store import TokenStore


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def store_path():
    return TokenStore().path  # conftest points INVO_TOKEN_FILE at a tmp dir


def test_save_load_roundtrip_with_0600(tmp_path):
    st = TokenStore(tmp_path / "d" / "t.json")
    st.save("r", "a", 1.0, 2.0)
    d = st.load()
    assert (d["refresh_token"], d["access_token"], d["refresh_expires"], d["access_expires"]) == ("r", "a", 1.0, 2.0)
    assert mode(st.path) == 0o600
    assert mode(st.path.parent) == 0o700
    assert not list(st.path.parent.glob("*.tmp"))


def test_loose_permissions_tightened_on_load(tmp_path):
    st = TokenStore(tmp_path / "t.json")
    st.save("r", "a", None, None)
    os.chmod(st.path, 0o644)
    st.load()
    assert mode(st.path) == 0o600


def test_corrupt_file_ignored(tmp_path):
    p = tmp_path / "t.json"
    p.write_text("{not json")
    os.chmod(p, 0o600)
    assert TokenStore(p).load() == {}


def client(posts=(), gets=(), **kw):
    return IC.InvoClient(limiter=SlidingWindowLimiter(1000, 300), session=FakeHTTP(list(posts), list(gets)), **kw)


def test_env_token_seeds_file_and_refresh_is_saved(monkeypatch):
    r1, a1 = jwt(time.time() + 300 * 86400), jwt(time.time() + 600)
    monkeypatch.setenv("INVO_REFRESH_TOKEN", r1)
    c = client(gets=[Resp(200, {"accessToken": a1})])
    assert json.loads(store_path().read_text())["refresh_token"] == r1   # seeded on startup
    c.refresh()
    saved = json.loads(store_path().read_text())
    assert saved["access_token"] == a1 and saved["refresh_token"] == r1
    assert mode(store_path()) == 0o600


def test_rotated_refresh_token_is_persisted(monkeypatch):
    r1, r2 = jwt(time.time() + 100 * 86400), jwt(time.time() + 365 * 86400)
    monkeypatch.setenv("INVO_REFRESH_TOKEN", r1)
    c = client(gets=[Resp(200, {"accessToken": jwt(time.time() + 600), "refreshToken": r2})])
    c.refresh()
    assert json.loads(store_path().read_text())["refresh_token"] == r2
    # a restart with the OLD env var still picks the newer saved token
    c2 = client()
    assert c2.refresh_token == r2 and c2.refresh_source.startswith("file")


def test_newer_env_beats_older_file(monkeypatch):
    old, new = jwt(time.time() + 10 * 86400), jwt(time.time() + 300 * 86400)
    TokenStore().save(old, None, None, None)
    monkeypatch.setenv("INVO_REFRESH_TOKEN", new)
    c = client()
    assert c.refresh_token == new
    assert json.loads(store_path().read_text())["refresh_token"] == new


def test_expired_file_token_ignored_when_env_valid(monkeypatch):
    expired, good = jwt(time.time() - 10), jwt(time.time() + 50 * 86400)
    TokenStore().save(expired, None, None, None)
    monkeypatch.setenv("INVO_REFRESH_TOKEN", good)
    assert client().refresh_token == good


def test_only_expired_token_reports_expiry_date():
    TokenStore().save(jwt(time.time() - 86400), None, None, None)
    c = client()
    with pytest.raises(IC.InvoAuthError, match="expired on"):
        c.refresh()


def test_persistence_can_be_disabled(monkeypatch):
    monkeypatch.setenv("INVO_PERSIST_TOKENS", "0")
    monkeypatch.setenv("INVO_REFRESH_TOKEN", jwt(time.time() + 86400))
    c = client(gets=[Resp(200, {"accessToken": jwt(time.time() + 600)})])
    c.refresh()
    assert c.store is None and not store_path().exists()


def test_status_never_prints_token_values(monkeypatch, capsys):
    r = jwt(time.time() + 200 * 86400)
    TokenStore().save(r, jwt(time.time() + 600), IC.jwt_expiry(r), None)
    monkeypatch.setenv("INVO_REFRESH_TOKEN", r)
    assert invo_auth.main(["status"]) == 0
    out = capsys.readouterr().out
    for part in r.split("."):
        assert part not in out
    assert "days left" in out


def test_import_rejected_token_saves_nothing(monkeypatch, capsys):
    bad = jwt(time.time() + 86400)
    monkeypatch.setattr("sys.stdin", io.StringIO(bad + "\n"))
    monkeypatch.setattr(IC.requests, "Session",
                        lambda: FakeHTTP([], [Resp(401, {"detail": "Invalid token or expired token."})]))
    assert invo_auth.main(["import", "--stdin"]) == 2
    assert not store_path().exists()
    assert "nothing was saved" in capsys.readouterr().err


def test_import_valid_token_saved(monkeypatch):
    good = jwt(time.time() + 300 * 86400)
    monkeypatch.setattr("sys.stdin", io.StringIO(good + "\n"))
    monkeypatch.setattr(IC.requests, "Session",
                        lambda: FakeHTTP([], [Resp(200, {"accessToken": jwt(time.time() + 600)})]))
    assert invo_auth.main(["import", "--stdin"]) == 0
    assert json.loads(store_path().read_text())["refresh_token"] == good
    assert mode(store_path()) == 0o600
