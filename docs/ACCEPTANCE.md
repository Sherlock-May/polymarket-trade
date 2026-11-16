# 验收记录

日期：2026-11-16。时间显示使用北京时间，程序内部市场边界使用 UTC。

## 真实生产公共接口

已执行项目命令：

```text
main.py check --market btc-4h --ws-seconds 12
```

- 查询时间：2026-11-16 16:03:30 起。
- 官方 SDK：`polymarket-client==0.18.0`，已安装并核对真实方法签名。
- CLOB `/time`：成功。
- Gamma `/series`、`/events`：成功，按真实系列及时间定位下一轮。
- 下一轮：`btc-updown-4h-1794819600`。
- 真实开盘/结束：北京时间 17:00–21:00。
- 策略买单撤销截止：北京时间 18:00（开盘后 3600 秒）。
- `acceptingOrders=true`，最小份额 5，价格步长 0.01。
- 已核实 condition ID 与两个方向不同的 token ID，REST 盘口返回的 condition ID 一致。
- 两边 REST 盘口查询成功；返回快照 best bid 0.48、best ask 0.52。快照不等于之后仍可成交的报价。
- 官方 SDK WebSocket 订阅成功，收到 5 条事件，包含 `book` 和 `new_market`；收到 UP、DOWN 两个资产的消息。
- 原始验证结果：`reports/api-check.json`。

首次排查时 UTC 直接取整未定位市场的问题已修正；实现不使用候选 slug 推算 token 或开盘时间。

## 完整生产行情模拟运行

最终运行：北京时间 16:12:10–16:12:49 左右，使用独立数据库 `data/final-smoke-paper.db`。

命令：

```text
DRY_RUN=true
ENABLE_LIVE_TRADING=false
DATABASE_PATH=data/final-smoke-paper.db
main.py run --seconds 40 --record
```

结果：

- 发现最近的未来 BTC 4 小时轮次。
- 订阅两个真实资产的订单簿。
- 建立两笔本地模拟限价买单：每边价格 0.10、份额 10。
- 中途统计记录预留金额 2.00；成交次数 0，未虚构成交和收益。
- 运行时持久化市场、订单、事件及市场快照，导出 CSV/JSON。
- 安全停机对两笔模拟买单请求撤单，模拟撤单延迟后确认最终状态。
- 最终报告：`running=false`、`cancel_confirmed=2`、`cancel_pending=0`、`unknown_orders=0`、`filled_rounds=0`、净收益 0。
- 正常退出码 0。

先前开发试运行发现的统计日志重复 mode 参数错误已修复，再次运行没有该错误。开发试运行数据保留在单独文件中，未作为真实账户记录。

默认的 `data/paper.db` 未预置开发订单；首次正式启动会新建。已有验收数据库不影响默认运行的本轮进场唯一性。

## 回放

用已经录下的真实公开盘口消息与 `data/acceptance-paper.db` 的市场元数据离线回放：

- 输入实际录制消息 2 条（两个方向 book）；不复制原数据库成交。
- 输出 `data/replay-final.db`。
- 两笔模拟预挂，0 成交，2 笔取消确认，最终剩余敞口 0。
- 无网络访问，未伪造录制外行情或结算。
- 回放报告单独保存到 `reports/replay/replay-final/`，不会覆盖运行中的 paper 报告。

## 自动化测试

最终命令：

```text
python -m pytest -q --basetemp data/test-run-5 -p no:cacheprovider
```

结果：**61 passed in 3.34s**。

覆盖：配置校验/开关命令、固定份额与固定止盈、真实时间边界、下一轮发现、双向预挂、截止撤单、部分成交、双边成交、撤单成交竞态、未知提交不重发、库存预留防重复卖出、模拟排队、模拟延迟、盘口过期、时钟偏差、敞口限额、幂等成交、重启恢复、网络中断、到期撤单、未决结算、最终结算、赎回轮询、未知赎回不重发、链上费用不重复扣除、报表与单实例锁。

测试中的账户和成交均为隔离的测试对象，仅验证逻辑，不是实盘联通或真实成交证据。

另已检查：Python 编译、市场配置 CLI、SQLite 初始化/迁移脚本、Bash 安装/启动脚本语法。

## 尚未实际验证的部分

- 未提供私钥/钱包凭据，账户认证、账户授权额度及实际余额尚未验证。
- 没有提交任何真实资金订单，实盘买入、真实部分成交、实盘撤单及 GTC 止盈尚未完成资金验收。
- 本次市场没有发生模拟成交，真实行情运行尚未覆盖完整 4 小时轮次；止盈与结算分支通过隔离测试验证。
- 自动赎回的官方路由与 SDK 方法已核对并实现；真实链上提交、回执和 Relayer 授权尚未验证。
- Taker 手续费没有净到账凭据时明确标记 estimated；Maker rebate 到账未接入，未计入收益。
- 公开盘口无法证明真实排队优先级；小于最小卖单的部分成交不能立即挂出合法止盈单。
- 本机 Docker daemon 未运行，未进行 Docker 构建；本机为 Windows，Ubuntu/systemd 部署未实机验收。

这些项目不得标记为“实盘已验证”或“可保证盈利/成交”。具体启用方式和配置入口见根目录 README。
