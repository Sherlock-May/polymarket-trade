from dataclasses import asdict
from types import SimpleNamespace as NS
from bot.models import Fill


async def settled_live_position(engine):
    r = engine.rounds["test-round"]
    buy = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    await engine.accept_fills([asdict(Fill("buy", buy["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
    engine.api.value = r["market"]["end"] + 1
    await engine.manage()
    engine.mode = "live"
    async def market(slug):
        return {"closed":True, "umaResolutionStatus":"resolved", "outcomes":'["Up","Down"]', "outcomePrices":'["1","0"]'}
    engine.api.market = market
    async def receipt(tx_hash):
        return None
    engine.api.receipt = receipt
    return r


async def test_redemption_polls_until_confirmed_without_resubmission(engine, monkeypatch):
    r = await settled_live_position(engine)
    monkeypatch.setenv("AUTO_REDEEM", "true")
    calls = []
    class Client:
        async def redeem_positions(self, **kwargs):
            calls.append(kwargs)
            return NS(transaction_id="relayer-id", transaction_hash=None)
    engine.broker.client = Client()
    states = iter(["STATE_MINED", "STATE_CONFIRMED"])
    async def status(ident):
        assert ident == "relayer-id"
        return {"state":next(states), "tx_hash":"0xhash"}
    engine.api.redemption_status = status
    await engine.settle()
    assert r["redeem"]["status"] == "SUBMITTED"
    await engine.settle()
    assert r["status"] == "FINAL" and r["redeem"]["status"] == "CONFIRMED"
    assert len(calls) == 1


async def test_losing_round_final_without_redemption(engine, monkeypatch):
    r = await settled_live_position(engine)
    async def losing_market(slug):
        return {"closed":True, "umaResolutionStatus":"resolved", "outcomes":'["Up","Down"]', "outcomePrices":'["0","1"]'}
    engine.api.market = losing_market
    monkeypatch.setenv("AUTO_REDEEM", "true")
    await engine.settle()
    assert r["status"] == "FINAL"
    assert r.get("redeem") is None


async def test_lost_redemption_response_does_not_resubmit(engine, monkeypatch):
    r = await settled_live_position(engine)
    monkeypatch.setenv("AUTO_REDEEM", "true")
    calls = []
    class Client:
        async def redeem_positions(self, **kwargs):
            calls.append(kwargs)
            raise TimeoutError()
    engine.broker.client = Client()
    await engine.settle()
    await engine.settle()
    assert r["redeem"]["status"] == "UNKNOWN"
    assert len(calls) == 1


async def test_redemption_receipt_records_gas_without_double_charge(engine, monkeypatch):
    r = await settled_live_position(engine)
    monkeypatch.setenv("AUTO_REDEEM", "false")
    r["redeem"] = {"status":"UNKNOWN", "tx_hash":"0xhash"}
    async def receipt(tx_hash):
        return {"status":"0x1", "gasUsed":hex(21000), "effectiveGasPrice":hex(1000000000)}
    engine.api.receipt = receipt
    await engine.settle()
    assert r["status"] == "FINAL"
    assert r["redeem"]["chain_gas_cost_POL"] == "0.000021"
    assert r["redeem"]["gas_paid_by_account"] == "unverified"
