"""不访问账户，不发订单；检查运行统计心跳。"""
import argparse
import json
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--mode", choices=["paper", "live"], default="paper")
p.add_argument("--max-age", type=int, default=120)
args = p.parse_args()
try:
    stats = json.loads(Path(f"reports/{args.mode}/summary.json").read_text(encoding="utf-8"))
    age = time.time() - stats["updated_at"]
    okay = age < args.max_age and stats["unknown_orders"] == 0 and stats.get("running") is True
    print(json.dumps({"healthy": okay, "report_age_seconds": int(age), "unknown_orders": stats["unknown_orders"]}))
    raise SystemExit(0 if okay else 1)
except (OSError, KeyError, ValueError):
    print('{"healthy":false,"reason":"missing_or_invalid_report"}')
    raise SystemExit(1)
