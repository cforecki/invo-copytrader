import base64
import json
import time

import pytest

import invo_client as IC
from ratelimit import SlidingWindowLimiter


def jwt(expires):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'alg': 'HS256'})}.{b({'user_id': 'u1', 'expires': expires})}.sig"


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)


class FakeHTTP:
    def __init__(self, posts, gets=None):
        self.posts, self.gets, self.calls = list(posts), list(gets or []), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers["Authorization"]))
        return self.posts.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers["Authorization"]))
        return self.gets.pop(0)


def client(posts, gets=None, access=None, refresh=None):
    return IC.InvoClient(refresh_token=refresh, access_token=access,
                         limiter=SlidingWindowLimiter(1000, 300), session=FakeHTTP(posts, gets))


def test_jwt_expiry_decoding():
    assert IC.jwt_expiry(jwt(1234)) == 1234
    assert IC.jwt_user_id(jwt(1)) == "u1"
    assert IC.jwt_expiry("not.a.jwt") is None


def test_no_credentials_fails_loudly(monkeypatch):
    monkeypatch.delenv("INVO_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("INVO_ACCESS_TOKEN", raising=False)
    with pytest.raises(IC.InvoAuthError) as e:
        IC.InvoClient()
    assert "DevTools" in str(e.value)


def test_401_triggers_refresh_then_retries():
    new_access = jwt(time.time() + 600)
    c = client([Resp(401, {}), Resp(200, {"success": True, "investmentsTicker": [{"baseId": "x"}]})],
               gets=[Resp(200, {"accessToken": new_access})],
               access="Bearer " + jwt(time.time() + 600), refresh=jwt(time.time() + 86400))
    assert c.get_open_investments("p") == [{"baseId": "x"}]
    assert c.http.calls[1][0] == "GET" and c.http.calls[2][2] == f"Bearer {new_access}"


def test_expired_refresh_token_raises_with_instructions():
    c = client([], access=None, refresh=jwt(time.time() - 10))
    with pytest.raises(IC.InvoAuthError, match="expired"):
        c.get_open_investments("p")


def test_rejected_refresh_raises():
    c = client([], gets=[Resp(401, {"status": "error"})], refresh=jwt(time.time() + 86400))
    with pytest.raises(IC.InvoAuthError, match="rejected"):
        c.get_portfolio("p")


def test_success_false_is_an_error_not_empty():
    c = client([Resp(200, {"success": False, "error": {"msg": "nope"}})], access=jwt(time.time() + 600))
    with pytest.raises(IC.InvoError):
        c.get_open_investments("p")


def test_base64_body_decoded():
    body = base64.b64encode(json.dumps({"success": True, "portfolio": {"id": "p"}}).encode()).decode()
    c = client([Resp(200, body)], access=jwt(time.time() + 600))
    assert c.get_portfolio("p") == {"id": "p"}


def test_pagination_stops_on_short_page_and_flags_truncation():
    full = {"success": True, "investmentsTicker": [{"baseId": f"a{i}"} for i in range(IC.PAGE_SIZE)]}
    full2 = {"success": True, "investmentsTicker": [{"baseId": f"b{i}"} for i in range(IC.PAGE_SIZE)]}
    short = {"success": True, "investmentsTicker": [{"baseId": "c0"}]}
    c = client([Resp(200, full), Resp(200, short)], access=jwt(time.time() + 600))
    out, trunc = c.get_all_closed_investments("p")
    assert len(out) == IC.PAGE_SIZE + 1 and not trunc
    c = client([Resp(200, full), Resp(200, full2)], access=jwt(time.time() + 600))
    out, trunc = c.get_all_closed_investments("p", max_pages=2)
    assert trunc


def test_rate_limiter_blocks_when_full():
    t = [0.0]
    slept = []
    lim = SlidingWindowLimiter(2, 300, clock=lambda: t[0],
                               sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    lim.acquire(); lim.acquire()  # noqa: E702
    assert lim.acquire() > 299 and slept
