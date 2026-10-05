import json

import screener
from conftest import NOW, inv


def test_parse_targets_urls_csv_and_dedupe(tmp_path):
    u = "6053206f-bd17-4fda-ae27-9cf318aa9a2a"
    f = tmp_path / "p.csv"
    f.write_text(f"id,label,hl_address\nhttps://app.invoapp.com/portfolio/{u},Vanta,0xabc\n")
    t = screener.parse_targets([f"https://app.invoapp.com/portfolio/{u.upper()}", "nonsense"], str(f))
    assert len(t) == 1 and t[0]["id"] == u
    txt = tmp_path / "p.txt"
    txt.write_text(f"# comment\n{u}, Vanta\n")
    assert screener.parse_targets([], str(txt))[0]["label"] == "Vanta"


def raw_for(n, days, win_every=3):
    closed = []
    for i in range(n):
        ex = 104 if i % win_every else 98
        closed.append(inv(i, coin=["BTC", "ETH", "SOL"][i % 3], entry=100, exit_=ex, lev=3, size=5,
                          opened_days_ago=days - i * (days - 2) / n, hold_days=0.5))
    return {"portfolio": {"title": "T", "owner": {"username": "u"}}, "open": [], "closed": closed,
            "truncated": False}


def test_end_to_end_offline_writes_reports_and_approved(tmp_path, monkeypatch):
    cache = tmp_path / "raw"
    cache.mkdir()
    good, young = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    (cache / f"{good}.json").write_text(json.dumps(raw_for(120, 200)))
    (cache / f"{young}.json").write_text(json.dumps(raw_for(20, 40)))
    monkeypatch.setattr(screener, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    rc = screener.main(["--offline", "--ids", good, young, "--cache-dir", str(cache),
                        "--out-dir", str(tmp_path / "rep"), "--approved-out", str(tmp_path / "ap.json")])
    assert rc == 0
    md = next((tmp_path / "rep").glob("*.md")).read_text()
    assert "Past performance does not guarantee future results" in md
    approved = json.loads((tmp_path / "ap.json").read_text())["portfolios"]
    ids = [p["id"] for p in approved]
    assert young not in ids
    csv_text = next((tmp_path / "rep").glob("*.csv")).read_text()
    assert "RED" in csv_text  # the 40-day portfolio is RED
    # the good synthetic one should rank first
    first_data_row = csv_text.splitlines()[1]
    assert good in first_data_row
