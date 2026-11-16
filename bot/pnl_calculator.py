from .config import D


def estimate_fee(market, price, shares, liquidity):
    if liquidity == "MAKER" and market.fee_taker_only:
        return D(0)
    if not market.fee_known:
        raise ValueError("市场未返回可核实的费率参数")
    p, size = D(price), D(shares)
    # 读取市场 feeSchedule；不能把 rate 当成交易额固定百分比。
    return (size * D(market.fee_rate) * (p * (1 - p)) ** D(market.fee_exponent)).quantize(D("0.000001"))


def position(fills, order_map, slug, token):
    qty, cost, proceeds, fees, realized = D(0), D(0), D(0), D(0), D(0)
    estimated = False
    for f in sorted(fills, key=lambda x: (x["timestamp"], x["id"])):
        o = order_map[f["order_id"]]
        if o["slug"] != slug or o["asset_id"] != token or f.get("status") != "CONFIRMED":
            continue
        size, price, fee = D(f["shares"]), D(f["price"]), D(f["fee"])
        estimated |= f["fee_kind"] != "actual"
        fees += fee
        if o["side"] == "BUY":
            qty += size
            cost += size * price + fee
        else:
            if size > qty:
                raise ValueError("卖出成交超过已确认买入，需对账")
            basis = cost * size / qty if qty else D(0)
            cash = size * price - fee
            realized += cash - basis
            proceeds += cash
            qty -= size
            cost -= basis
    return {"shares": str(qty), "cost": str(cost), "sell_proceeds": str(proceeds),
            "fees": str(fees), "realized_pnl": str(realized), "accounting": "estimated" if estimated else "actual"}
