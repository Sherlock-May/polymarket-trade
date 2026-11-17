#!/usr/bin/env bash
# 只读取版本和服务诊断信息，不启动或停止机器人，不读取 .env。
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export SYSTEMD_COLORS=0
echo '=== 当前时间及代码版本 ==='
date --iso-8601=seconds
git rev-parse --short HEAD
echo '=== 停机诊断代码是否已更新 ==='
grep -n 'stop_debug_version\|stop_reason\|except BaseException' main.py bot/engine.py
echo '=== 服务状态及退出原因 ==='
systemctl show polymarket-bot \
  -p ActiveState -p SubState -p MainPID -p Result \
  -p ExecMainCode -p ExecMainStatus -p NRestarts -p Restart \
  -p RuntimeMaxUSec -p WatchdogUSec -p TimeoutStopUSec \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
  -p PartOf -p BindsTo -p TriggeredBy
echo '=== 当前待执行的服务任务 ==='
systemctl list-jobs --no-pager
echo '=== 最近20分钟服务日志（最多200行） ==='
journalctl -u polymarket-bot --since '-20 minutes' -n 200 --no-pager -a -o short-iso
echo '=== 最近详细日志（可能包含停机原因） ==='
if [[ -f logs/live.log ]]; then
  tail -n 80 logs/live.log | grep -E '程序启动|安全停机|运行异常|操作未完成|停机原因'
fi
echo '=== 诊断结束：本脚本未执行 restart / stop / start ==='
