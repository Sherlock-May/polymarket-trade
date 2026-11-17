#!/usr/bin/env bash
# 只读检查服务器；不下单、不撤单、不发送链上授权。
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONUTF8=1
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--account" ) ]]; then
  echo '用法：bash scripts/server-check.sh [--account]'
  exit 2
fi
account_check="${1:-}"
python_bin=".venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "未找到项目虚拟环境。请先运行：bash scripts/install.sh"
  exit 1
fi
failed=0
echo '=== 1/2 地区、时间及网络 ==='
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
echo '=== 2/2 交易市场和实时行情 ==='
"$python_bin" main.py check --ws-seconds 8 || failed=1
echo
if [[ "$account_check" == "--account" ]]; then
  echo '=== 可选：账户认证、余额和授权 ==='
  "$python_bin" main.py auth-check || failed=1
  echo '注意：账户认证可能创建或派生 CLOB API 凭据。'
  echo
else
  echo '未进行账户检查；无需填写私钥或钱包地址。'
fi
if [[ "$failed" -eq 0 ]]; then
  echo '公开接口和行情检查通过；尚未验证真实账户、订单提交或撤单。'
else
  echo '自检未全部通过：请查看上面的失败项；不要把私钥或令牌发给别人。'
fi
echo '本脚本不提交订单、撤单或资产授权。地区检查通过不代表账户具备交易资格。'
exit "$failed"
