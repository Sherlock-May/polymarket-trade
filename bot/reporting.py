from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from .config import D
from .database import dumps
from .polymarket_client import ACTIVE


def csv_file(path, rows, fields):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def report(engine):
    root = Path(getattr(engine, "report_directory", Path("reports") / engine.mode))
    root.mkdir(parents=True, exist_ok=True)
    rounds, realized, unrealized, receivable, fees, exposure = [], D(0), D(0), D(0), D(0), engine.exposure()
    total_buy = total_sell = D(0)
    unknown_valuation = 0
    position_status = []
    for f in engine.fills:
        value = D(f["shares"]) * D(f["price"])
        if engine.orders[f["order_id"]]["side"] == "BUY":
            total_buy += value
        else:
            total_sell += value
        fees += D(f["fee"])
    snapshots = []
    for slug, r in engine.rounds.items():
        cost, pnl, floating, payable = D(0), D(0), D(0), D(0)
        incomplete = False
        for token in r["market"]["tokens"].values():
            p = engine.position(slug, token)
            if D(p["shares"]) > 0:
                book_for_status = engine.feed.books.get(token)
                position_status.append({"market": slug, "asset_id": token, "shares": p["shares"],
                                        "average_cost_with_fees": str(D(p["cost"]) / D(p["shares"])),
                                        "best_bid": str(max(book_for_status.bids)) if book_for_status and book_for_status.bids else None,
                                        "take_profit_price": r["rule"]["take_profit_price"],
                                        "take_profit_orders": [o.get("exchange_id") or o["local_id"] for o in engine.orders.values() if o["slug"] == slug and o["asset_id"] == token and o["side"] == "SELL" and o["status"] in ACTIVE]})
            pnl += D(p["realized_pnl"])
            if r.get("settlement"):
                pnl += D(p.get("settlement_pnl", "0"))
                payable += D(p.get("settlement_receivable", "0"))
            else:
                cost += D(p["cost"])
                book = engine.feed.books.get(token)
                if D(p["shares"]) > 0:
                    if book and time.time() - book.received_at < engine.config.settings["book_max_age_seconds"]:
                        value, unpriced = book.liquidation(p["shares"])
                        floating += value - D(p["cost"])
                        incomplete |= unpriced > 0
                    else:
                        incomplete = True
                        floating -= D(p["cost"])  # 无可核实买盘部分按 0 保守估值
                if book:
                    snapshots.append((time.time(), token, dumps({"slug": slug, "bids": [[str(x), str(y)] for x, y in sorted(book.bids.items(), reverse=True)[:20]],
                                                                  "asks": [[str(x), str(y)] for x, y in sorted(book.asks.items())[:20]], "position": p,
                                                                  "book_timestamp": book.timestamp, "received_at": book.received_at})))
        realized += pnl
        unrealized += floating
        receivable += payable
        unknown_valuation += int(incomplete)
        local = [o for o in engine.orders.values() if o["slug"] == slug]
        bought = any(D(o.get("matched", "0")) > 0 for o in local if o["side"] == "BUY")
        rounds.append({"market": slug, "market_config": r["rule"]["id"], "mode": engine.mode,
                       "start_utc": datetime.fromtimestamp(r["market"]["start"], timezone.utc).isoformat(),
                       "end_utc": datetime.fromtimestamp(r["market"]["end"], timezone.utc).isoformat(),
                       "status": r["status"], "entry_attempted": r["entry_attempted"], "entry_filled": bought,
                       "position_cost": str(cost), "realized_pnl": str(pnl), "unrealized_pnl": str(floating),
                       "settlement_receivable": str(payable), "valuation_incomplete": incomplete,
                       "take_profit_price": r["rule"]["take_profit_price"], "skip_reason": r["skip_reason"],
                       "settlement": dumps(r.get("settlement")), "redeem": dumps(r.get("redeem"))})
    final = [r for r in rounds if r["status"] == "FINAL" and r["entry_filled"]]
    wins = sum(D(r["realized_pnl"]) > 0 for r in final)
    losses = sum(D(r["realized_pnl"]) < 0 for r in final)
    marked_equity = D(engine.config.settings["paper_initial_balance"]) + realized + unrealized
    peak = max(D(engine.db.meta("equity_peak", str(marked_equity))), marked_equity)
    drawdown = max(D(engine.db.meta("max_drawdown", "0")), peak - marked_equity)
    with engine.db.transaction():
        engine.db.set_meta("equity_peak", str(peak))
        engine.db.set_meta("max_drawdown", str(drawdown))
        engine.db.conn.executemany("INSERT INTO market_snapshots(timestamp,asset_id,data) VALUES(?,?,?)", snapshots)
    stats = {"mode": engine.mode, "accounting": "estimated" if engine.mode == "paper" or any(f["fee_kind"] != "actual" for f in engine.fills) else "actual",
             "updated_at": time.time(),
             "running": not engine.stopping,
             "uptime_seconds": int(time.time() - engine.started_at), "monitored_rounds": len(rounds),
             "submitted_rounds": sum(r["entry_attempted"] for r in rounds), "filled_rounds": sum(r["entry_filled"] for r in rounds),
             "unfilled_rounds": sum(r["entry_attempted"] and not r["entry_filled"] for r in rounds),
             "take_profit_filled_rounds": sum(any(o["slug"] == r["market"] and o["side"] == "SELL" and D(o.get("matched", "0")) > 0 for o in engine.orders.values()) for r in rounds),
             "settled_rounds": sum(bool(r.get("settlement")) for r in engine.rounds.values()),
             "waiting_settlement_rounds": sum(r["status"] == "WAIT_SETTLEMENT" for r in rounds),
             "winning_rounds": wins, "losing_rounds": losses,
             "win_rate": str(D(wins) / len(final)) if final else None,
             "buy_total": str(total_buy), "sell_total": str(total_sell), "open_exposure": str(exposure),
             "realized_pnl": str(realized), "unrealized_pnl": str(unrealized),
             "settlement_receivable": str(receivable), "fees": str(fees),
             "fees_actual": str(sum((D(f["fee"]) for f in engine.fills if f["fee_kind"] == "actual"), D(0))),
             "fees_estimated": str(sum((D(f["fee"]) for f in engine.fills if f["fee_kind"] != "actual"), D(0))),
             "maker_rebates_received": "0", "rebate_note": "未接入实际到账来源，不预估返佣为收益",
             "average_closed_round_pnl": str(sum((D(r["realized_pnl"]) for r in final), D(0)) / len(final)) if final else None,
             "max_drawdown_sampled": str(drawdown), "valuation_incomplete_rounds": unknown_valuation,
             "valuation_basis": "盘口买盘深度；不足深度或过期部分按0保守估值，未扣未来卖出估算费用",
             "paper_available_cash": str(engine.paper_cash() - engine.reserved()) if engine.mode == "paper" else None,
             "live_cash_balance_last_checked": getattr(engine, "last_cash_balance", None),
             "live_cash_balance_checked_at": getattr(engine, "last_cash_checked_at", None),
             "cancel_confirmed": sum(o["status"] == "CANCELED" for o in engine.orders.values()),
             "cancel_pending": sum(o["status"] == "CANCEL_PENDING" for o in engine.orders.values()),
             "unknown_orders": sum(o["status"] in {"UNKNOWN", "INTENT"} for o in engine.orders.values())}
    stats["positions"] = position_status
    (root / "summary.json.tmp").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "summary.json.tmp").replace(root / "summary.json")
    fields = list(rounds[0]) if rounds else ["market", "mode", "status"]
    csv_file(root / "rounds.csv", rounds, fields)
    for day in {r["start_utc"][:10] for r in rounds}:
        csv_file(root / f"daily_{day.replace('-', '')}.csv", [r for r in rounds if r["start_utc"].startswith(day)], fields)
    fills = [{**f, "side": engine.orders[f["order_id"]]["side"], "market": engine.orders[f["order_id"]]["slug"], "mode": engine.mode} for f in engine.fills]
    csv_file(root / "trades.csv", fills, list(fills[0]) if fills else ["id", "market", "mode", "side", "price", "shares", "fee", "fee_kind"])
    return stats
