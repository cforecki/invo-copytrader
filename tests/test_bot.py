import json

import pytest

from bot import MirrorBot
from config import Config, ConfigError
from executors import PaperExecutor
from invo_client import InvoError
from state import StateStore


class FakeInvo:
    def __init__(self):
        self.open = {}      # pid -> list of investments
        self.fail = set()

    def get_open_investments(self, pid):
        if pid in self.fail:
            raise InvoError("boom")
        return list(self.open.get(pid, []))


def oinv(bid, coin="BTC", long=True, entry=100.0, lev=10):
    return {"baseId": bid, "ticker": coin, "directionLong": long, "entryPrice": entry,
            "currentPrice": entry, "leverage": lev, "isOpen": True}


@pytest.fixture
def env(tmp_path):
    cfg = Config(state_file=str(tmp_path / "s.json"), fills_log=str(tmp_path / "f.jsonl"),
                 paper_starting_balance=1000, trade_allocation_pct=0.05, max_trade_amount_usdt=40,
                 min_trade_amount_usdt=10, max_leverage=5, close_confirm_polls=2)
    ex = PaperExecutor(cfg.paper_fee_per_side, cfg.paper_starting_balance)
    prices = {"BTC": 100.0, "ETH": 50.0}
    ex._mids, ex._mids_at = prices, float("inf")
    invo = FakeInvo()

    def make(**over):
        for k, v in over.items():
            setattr(cfg, k, v)
        return MirrorBot(cfg, invo, ex, StateStore(cfg.state_file, cfg.fills_log), [{"id": "P1"}], live=False,
                         price_fn=lambda c, fb=None: prices.get(c))
    return cfg, invo, prices, make, tmp_path


def fills(tmp_path):
    p = tmp_path / "f.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_preexisting_trades_not_mirrored_new_ones_are(env):
    cfg, invo, prices, make, tmp = env
    invo.open["P1"] = [oinv("old")]
    bot = make()
    bot.cycle()
    assert bot.st["positions"] == {}
    invo.open["P1"].append(oinv("new", coin="ETH", entry=50))
    bot.cycle()
    pos = bot.st["positions"]["new"]
    assert pos["status"] == "open" and pos["coin"] == "ETH"
    assert pos["leverage"] == 5            # capped from trader's 10x
    assert pos["margin"] == 40             # min(1000*5%, 40)
    assert pos["size"] == pytest.approx(40 * 5 / 50)


def test_close_requires_confirmation_and_logs_pnl(env):
    cfg, invo, prices, make, tmp = env
    bot = make()
    bot.cycle()
    invo.open["P1"] = [oinv("t1")]
    bot.cycle()
    assert "t1" in bot.st["positions"]
    invo.open["P1"] = []
    prices["BTC"] = 110.0
    bot.cycle()
    assert "t1" in bot.st["positions"]     # 1 missing poll: not yet
    bot.cycle()
    assert "t1" not in bot.st["positions"]
    close = [f for f in fills(tmp) if f["event"] == "close"][0]
    size = 40 * 5 / 100
    expected = size * 10 - (size * 100 + size * 110) * cfg.paper_fee_per_side
    assert close["pnl_usd"] == pytest.approx(expected, abs=1e-3)


def test_failed_fetch_never_closes(env):
    cfg, invo, prices, make, tmp = env
    bot = make()
    bot.cycle()
    invo.open["P1"] = [oinv("t1")]
    bot.cycle()
    invo.fail.add("P1")
    for _ in range(5):
        bot.cycle()
    assert bot.st["positions"]["t1"]["status"] == "open"


def test_long_only_drift_and_coin_dedupe(env):
    cfg, invo, prices, make, tmp = env
    bot = make(long_only=True)
    bot.cycle()
    invo.open["P1"] = [oinv("s", long=False), oinv("d", coin="ETH", entry=40), oinv("a"), oinv("b")]
    bot.cycle()
    sk = bot.st["skipped"]
    assert "LONG_ONLY" in sk["s"]["reason"]
    assert "drifted" in sk["d"]["reason"]
    assert "a" in bot.st["positions"] and "already held" in sk["b"]["reason"]


def test_max_open_positions(env):
    cfg, invo, prices, make, tmp = env
    prices.update({"SOL": 10.0})
    bot = make(max_open_positions=1)
    bot.cycle()
    invo.open["P1"] = [oinv("a"), oinv("c", coin="SOL", entry=10)]
    bot.cycle()
    assert len(bot.st["positions"]) == 1 and "MAX_OPEN" in bot.st["skipped"]["c"]["reason"]


def test_circuit_breaker_flattens_and_halts(env):
    cfg, invo, prices, make, tmp = env
    bot = make(trade_allocation_pct=0.5, max_trade_amount_usdt=1000, circuit_breaker_pct=0.3)
    bot.cycle()
    invo.open["P1"] = [oinv("t1")]
    bot.cycle()                       # 500 margin * 5x = 2500 notional
    prices["BTC"] = 85.0              # -15% * 5 = -375 -> equity ~625, a 37.5% loss
    bot.cycle()
    assert bot.st["halted"] and bot.st["positions"] == {}
    assert any(f["reason"] == "circuit_breaker" for f in fills(tmp) if f["event"] == "close")
    invo.open["P1"] = [oinv("t1"), oinv("t2", coin="ETH", entry=50)]
    bot.cycle()
    assert bot.st["positions"] == {}  # halted: nothing new


def test_state_survives_restart_and_reconciles_opening(env):
    cfg, invo, prices, make, tmp = env
    bot = make()
    bot.cycle()
    invo.open["P1"] = [oinv("t1")]
    bot.cycle()
    bot.st["positions"]["ghost"] = {"status": "opening", "coin": "ETH", "portfolio_id": "P1",
                                    "is_long": True, "leverage": 1, "margin": 10}
    bot.store.save(bot.st)
    bot2 = make()
    bot2.reconcile_after_restart()
    assert "ghost" not in bot2.st["positions"] and bot2.st["positions"]["t1"]["status"] == "open"


def test_paper_and_live_state_never_mix(env):
    cfg, invo, prices, make, tmp = env
    make().store.save(make().st)
    with pytest.raises(RuntimeError, match="must never mix"):
        StateStore(cfg.state_file, cfg.fills_log).load("live", "hyperliquid")


def test_config_validation_and_rate_budget(monkeypatch):
    with pytest.raises(ConfigError):
        Config(venue="binance", long_only=False).validate()
    with pytest.raises(ConfigError):
        Config(circuit_breaker_pct=1.5).validate()
    monkeypatch.setenv("INVO_RATE_LIMIT", "220")
    Config(poll_interval=60).check_rate_budget(40)          # 200 + 2 calls
    with pytest.raises(ConfigError):
        Config(poll_interval=60).check_rate_budget(50)


def test_watched_list_from_screener_output(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"portfolios": [{"id": "A", "name": "x"}, {"id": "B", "enabled": False}]}))
    c = Config(watched_portfolios_file=str(f), watched_portfolios_extra=["C", "A"])
    assert [p["id"] for p in c.load_watched()] == ["A", "C"]


def test_live_gate_requires_both(monkeypatch, tmp_path, capsys):
    import bot
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADING_MODE", "live")
    assert bot.main([]) == 2
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert bot.main(["--live"]) == 2
    assert "BOTH" in capsys.readouterr().err
