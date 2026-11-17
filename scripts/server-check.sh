#!/usr/bin/env bash
# 只读检查服务器；不下单、不撤单、不发送链上授权。
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONUTF8=1
python_bin=".venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "未找到项目虚拟环境。请先运行：bash scripts/install.sh"
  exit 1
fi
failed=0
echo '=== 1/3 地区、时间及网络 ==='
"$python_bin" - <<'PY' || failed=1
import asyncio
from bot.config import Config
from bot.market_discovery import PublicAPI

async def main():
    api = PublicAPI()
    okay = True
    try:
        try:
            await asyncio.wait_for(api.sync_clock(), 45)
            limit = Config.load().settings['max_clock_skew_seconds']
            bound = abs(api.offset) + api.clock_uncertainty
            print(f'时间接口：成功，时钟偏差 {api.offset:.2f} 秒，误差上限 {bound:.2f} 秒（允许 {limit} 秒）')
            if bound > limit:
                print('时间检查未通过，程序会暂缓新买单')
                okay = False
        except Exception as exc:
            print('时间接口检查失败：' + type(exc).__name__)
            okay = False
        try:
            geo = await asyncio.wait_for(api.geoblock(), 45)
            if geo.get('blocked') is False:
                print('地区检查：通过')
            else:
                print('地区检查：未通过或结果不明确；不要启动实盘')
                okay = False
        except Exception as exc:
            print('地区检查失败：' + type(exc).__name__)
            okay = False
    finally:
        await api.close()
    return 0 if okay else 1

raise SystemExit(asyncio.run(main()))
PY
echo
echo '=== 2/3 交易市场和实时行情 ==='
"$python_bin" main.py check --ws-seconds 8 || failed=1
echo
echo '=== 3/3 账户认证、余额和授权 ==='
"$python_bin" main.py auth-check || failed=1
echo
if [[ "$failed" -eq 0 ]]; then
  echo '自检通过：公开接口、行情和账户检查可用。尚未验证真实订单提交或撤单。'
else
  echo '自检未全部通过：请查看上面的失败项；不要把私钥或令牌发给别人。'
fi
echo '注意：账户认证可能创建或派生 CLOB API 凭据；本脚本不提交订单或资产授权。'
exit "$failed"
