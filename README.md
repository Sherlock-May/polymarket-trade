# Polymarket 多市场预挂机器人

配置 BTC / ETH / SOL 的 5 分钟、15 分钟、1 小时、4 小时 Up / Down 市场。使用官方生产市场与行情接口，Python 3.11+、asyncio、官方 `polymarket-client==0.18.0`、SQLite WAL。

**市场开关和交易参数都在根目录 `markets.json`。密钥与运行模式在本地 `.env`。**

## 已确认的策略

1. 找到所选系列中**最近的、尚未开盘的下一轮**市场，核实真实开盘、结束时间、condition ID 和两个交易资产 ID。中途启动不参与已经开盘的这一轮。
2. 开盘前给 UP 和 DOWN 分别提交一笔固定价格、固定份额的限价买单。默认每边 `0.10 × 10`，合计计划买入金额 2 个报价货币单位，手续费另计。不要求报价先触及 0.10。
3. 任一方向首次撮合即关闭该轮入口；对已确认且可卖的份额挂固定价格 GTC 止盈，撤销两边剩余买单。本轮不补挂买单。
4. 撤单竞态导致两边均成交时，分别保护两个方向的持仓。部分成交按实际可卖数量处理；晚到成交继续补足止盈，禁止重复占用同一份库存。
5. 若一直没有成交，到**实际开盘时间 + `entry_window_seconds`** 撤销剩余买单。默认提前 1 秒发起撤单；实盘买单同时带 GTD 到期保护。网络撤单并不能保证零竞态。
6. 止盈价就是 `take_profit_price` 的绝对价格，例如 `"0.20"`，与实际买入均价、倍数无关。不自动改价追单，不主动止损。
7. 市场结束后检查最终结算，尚未解决则等待；旧仓位与新一轮并行管理。实盘赎回默认关闭，需有可用钱包操作授权才能启用。

“下一轮”来自官方系列及时间，不是取最远的市场，也不通过 UTC 时间取整猜 4 小时窗口。官方 `startDate` 常是创建时间，程序不用它当开盘时间。

## 在哪里配置

默认配置仅开启 **`btc-4h`**：

```json
{
  "id": "btc-4h",
  "enabled": true,
  "symbol": "BTC",
  "timeframe": "4h",
  "entry_price": "0.10",
  "shares": "10",
  "take_profit_price": "0.20",
  "entry_window_seconds": 3600,
  "preopen_seconds": 86400
}
```

| 字段 | 含义 |
| --- | --- |
| `enabled` | `true` 开启、`false` 关闭 |
| `symbol` | `BTC` / `ETH` / `SOL` |
| `timeframe` | `5m` / `15m` / `1h` / `4h` |
| `entry_price` | 两边买单的固定限价 |
| `shares` | **每个方向**的份额，不是投入金额 |
| `take_profit_price` | 固定止盈卖价，不是倍数 |
| `entry_window_seconds` | 从市场实际开盘算起，未成交买单最多保留多久 |
| `preopen_seconds` | 允许提前多久挂单；86400 表示最近下一轮一经发现即可提前挂，最多提前一天 |

金额、价格和份额使用 JSON 字符串，内部全部用 Decimal。文件中预置了 12 种组合，除 BTC 4 小时之外默认均关闭。预置不代表平台一定提供该系列；实际不存在、未上线、不可交易、参数无效都会记录日志。

也可以使用命令，避免手改 JSON：

```powershell
# Windows：在项目根目录执行
.\.venv\Scripts\python.exe main.py market list
.\.venv\Scripts\python.exe main.py market enable btc-4h
.\.venv\Scripts\python.exe main.py market disable btc-4h
.\.venv\Scripts\python.exe main.py market enable sol-15m
.\.venv\Scripts\python.exe main.py market set sol-15m entry_window_seconds 120
.\.venv\Scripts\python.exe main.py market set sol-15m shares 10
.\.venv\Scripts\python.exe main.py market set sol-15m entry_price 0.10
.\.venv\Scripts\python.exe main.py market set sol-15m take_profit_price 0.21
```

Linux 把 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

**运行时无需重启即可读入 `markets.json` 的有效修改。**

- 关闭某个市场：停止新开仓，撤销本程序在该市场剩余的买单；已有持仓继续挂止盈与等待结算。
- 开启某个市场：下次发现周期内开始查找下一轮，默认最长约 30 秒。
- 同时开启多个市场：分别管理，受合计总敞口和资金限额约束。
- 修改价格、份额、止盈价、挂单时间：用于**新发现的轮次**；已发现轮次使用持久化的参数快照，重启也不会改写。关闭后重新打开不重置本轮进场次数。
- 修改 `.env`、密钥、模拟/实盘模式：需要重启。模式禁止热切换。
- JSON 无效：保留上一份有效配置，记录错误，修正文件后自动重读。

`settings` 包含总敞口、日亏损限额、模拟初始资金、行情有效期、对账频率和模拟参数。日界统一为 UTC；日亏损暂停采用已实现盈亏与全部未结库存成本的保守口径，不自动卖出止损。暂停也继续管理已有仓位。

## 安装及启动

### Windows

本次开发已在项目的 `.venv` 安装依赖。直接执行：

```powershell
.\.venv\Scripts\python.exe main.py validate
.\.venv\Scripts\python.exe main.py check --market btc-4h --ws-seconds 10
.\scripts\start.ps1 -Record
```

在新机器上先安装 Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
Copy-Item .env.example .env
```

停止：终端按 Ctrl+C，程序会停止开仓、对账并撤销剩余买单，保留止盈单。重启使用相同命令和数据库。

### Ubuntu / Linux

需要 Python 3.11+、`venv`、CA 证书及准确的系统时钟。安装命令随 Ubuntu 版本调整；Ubuntu 24.04 的系统 Python 满足版本要求。

```bash
bash scripts/install.sh
.venv/bin/python main.py validate
.venv/bin/python main.py check --market btc-4h --ws-seconds 10
bash scripts/start.sh --record
```

默认读取生产 Gamma、CLOB 和官方 WebSocket，**默认模式仍是模拟**，不需要私钥。生产环境和模拟模式并不冲突：模拟使用真实生产行情，本地模拟订单执行。

限定运行后安全退出：

```bash
bash scripts/start.sh --seconds 60 --record
```

### 配置实盘

自行在本地将 `.env.example` 复制为 `.env`，填写账户信息，不要把密钥发到聊天、提交到 Git 或写进市场配置。

```dotenv
DRY_RUN=false
ENABLE_LIVE_TRADING=true
LIVE_TRADING_ACK=I_UNDERSTAND_REAL_ORDERS
DATABASE_PATH=data/live.db
POLYMARKET_PRIVATE_KEY=你的本地签名私钥
POLYMARKET_WALLET_ADDRESS=Polymarket个人资料菜单中的账户钱包地址
AUTO_REDEEM=false
```

然后先读取账户状态：

```bash
.venv/bin/python main.py auth-check
```

迁移电脑后不要直接复用旧 `.venv`：它可能仍指向旧电脑的 Python。保留旧环境备份，使用当前机器的 Python 重新创建 `.venv`，再安装 `requirements.lock`。

旧单详情返回 `null`、对账报 `UnexpectedResponseError` 时，先停止运行实例，再执行（Windows）：

```powershell
.\.venv\Scripts\python.exe main.py reconcile
```

此命令自动备份现有数据库，只查询账户并更新本地记录，不提交买卖单、撤单或赎回。详情为空时先按订单 ID 交叉查询；只有已结束轮次的已知 GTD 买单，在到期后额外等待 60 秒、无挂单、无相关成交、无本地成交记录且该资产持仓余额为零时，才标记 `EXPIRED`，同时保留核实来源。其余不明确的订单继续阻止新开仓。它不会删除历史订单或把空响应当作撤单确认。

`auth-check` 不下订单、不执行资产授权；SDK 连接可能创建或派生 CLOB API 凭据。它显示余额、已有订单数、钱包类型与保守的授权检查结果。

只有你自行运行 `main.py run` 且上述开关全部满足时，程序才进入实盘订单循环。私钥存在本身绝不自动启用实盘。启动前和运行中定期执行官方地区检查，失败即禁止新增买单，不绕过地区限制。实盘买单可能在开盘前成交；0.10 的小单也不是保证不会成交的连通测试。

认证使用官方新 SDK，根据账户钱包识别 EOA / Proxy / Safe / Deposit Wallet，不沿用旧 SDK 的手填签名类型。使用 `create_limit_order` 与 `post_order` 分开签名、提交，避免 `place_limit_order` 的自动授权恢复流程。程序不发送资产授权交易；余额和授权不足时暂停。

**模拟和实盘使用不同数据库。** 已有模拟数据库不能用于实盘，反之亦然。建议使用专用账户/钱包运行，不要与人工同市场同参数订单混用，以免不确定提交的恢复匹配产生歧义。未知订单只查询最终订单和成交记录，不自动再签一笔；无法唯一核实时保留未知状态并告警。

### 赎回

配置 `AUTO_REDEEM=true` 前，需要当前钱包支持的合法交易路由及 Relayer API 授权：

```dotenv
POLYMARKET_RELAYER_API_KEY=
POLYMARKET_RELAYER_API_KEY_ADDRESS=
```

关闭自动赎回时，获胜金额记录为**结算应收**，不伪装成已到账。赎回请求和确认分开记录；已取得 transaction ID 时定期查询官方 Relayer 状态，重启后继续补查。可选 `POLYGON_RPC_URL` 用于只读查询交易回执，记录链上 POL gas 成本；Relayer 可能代付，因此不会自动把链上 gas 成本再扣为账户支出。提交响应完全丢失、既无 ID 也无哈希时保留 UNKNOWN，需要人工核实，禁止重复赎回。

## 日志、统计与数据库

独立成交日志：`logs/live-trades.log`（模拟为 `paper-trades.log`）。记录轮次、方向、买单本地 ID、实际撮合时间（区别于日志记录时间）、确认成交、对应止盈订单 ID、止盈接受/拒绝/未知结果、止盈卖出成交及结算。部分卖出会明确标注可能为部分成交，不把止盈单被接受当作止盈成功。市场和时钟任务不会替代账户确认：已认证账户推送中的 `CONFIRMED` 成交可直接触发止盈保护，REST 对账继续兜底；可卖余额、最小份额和平台响应仍会影响提交结果。

启动窗口显示简洁业务日志，并同时写入 `logs/live-simple.log` 或 `logs/paper-simple.log`：启用市场、每轮时间、买入及止盈挂单结果、成交、撤单确认是否按时、结算或卖出后的扣费净收益。撤单按收到确认的本地时间与开盘后截止时间比较；无法确认时不会标为按时。费用未取得实际数据时标为估算，结算获胜金额未赎回时标为应收。原有详细日志继续保存在 `logs/live.log` 或 `logs/paper.log`。

- 控制台和 `logs/paper.log` / `logs/live.log`：中文业务事件，每文件 10 MB，保留 10 个轮转文件。密钥、认证消息和 SDK 异常原文不写日志。
- `data/paper.db` / `data/live.db`：轮次、订单、成交、持仓、快照及事件；WAL、事务、唯一成交 ID 和单实例进程锁。
- `reports/paper/` / `reports/live/`：`summary.json`、`rounds.csv`、`trades.csv`、`daily_YYYYMMDD.csv`。
- `reports/api-check.json`：最近一次真实生产公共接口及 WebSocket 验证结果。
- 每 30 秒输出统计，并采样盘口和持仓估值。CSV 可查看每轮状态、未交易原因、固定止盈价、结算与赎回状态。

```bash
.venv/bin/python main.py status
.venv/bin/python main.py report
.venv/bin/python scripts/healthcheck.py --mode paper
.venv/bin/python scripts/migrate.py data/paper.db
tail -f logs/paper.log
```

离线重新导出的报告没有新鲜盘口，浮盈采用保守的零估值并标记不完整；不能把它当最新市值。最大回撤是报告采样得到的策略权益回撤，不包含采样间的极值、充值或提现。

费用依据市场真实 `feeSchedule` 计算估算。Maker 零费用只有在当前规则可核实时记为 actual；实盘 Taker 费用没有净到账凭据时标记 estimated。报表分开列实际费用、估算费用及结算应收，不将 estimated 净收益当 actual。尚未接入 Maker rebate 到账来源，返佣不预估进收益。

## 模拟、回放与验收

模拟买卖使用真实盘口深度，首次提交考虑模拟延迟；挂单排队采用可见同价队列乘保守系数，只用后续真实成交量推进。报价触及或盘口撤单不直接当成排到你的订单。公共行情无法完整重建真实优先级，因此模拟成交和费用始终为估计，不能证明实盘必然成交。

`--record` 只保存公开市场消息到 `data/recordings/*.jsonl`，不保存用户认证消息。回放示例：

```bash
.venv/bin/python main.py replay data/recordings/实际文件名.jsonl --source-db data/paper.db --output-db data/replay-01.db
```

源数据库只读，用于恢复市场和策略参数；不复制源成交作为回放结果。不访问网络。回放输出数据库必须不存在；回放不会凭空补齐录制以外的行情、断线期间成交或最终结算。

测试：

```bash
.venv/bin/python -m pytest -q
```

测试中的假服务器和行情只用于状态机单元/集成测试。真实生产验收单独通过 `main.py check` 执行，记录实际时间、token、盘口和 WS 消息，不用测试数据伪造接口验收。

部分买入不足市场最小卖单时，程序会撤剩余买单并记录待处理的小额库存，无法保证立即挂止盈。实际可卖余额不足或精度余量也会单独记录。未知状态、超时、拒单和最终成交分别处理。

本次具体测试结果与仍未验证的账户行为见 `docs/ACCEPTANCE.md`。未提供账户凭据，未执行任何真实资金订单；代码交付不等于所有实盘行为已验收。

## systemd 后台服务

示例服务文件：`deploy/polymarket-bot.service`。将项目放在 `/opt/polymarket-bot`，使用专用用户 `polymarket`，由该用户运行 `scripts/install.sh`。确保 `data`、`logs`、`reports` 由服务用户可写，`.env` 仅服务用户可读；配置文件由你维护。服务使用工作目录下 `.env`，不需要把密钥写进 service。

```bash
sudo install -m 644 deploy/polymarket-bot.service /etc/systemd/system/polymarket-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot
sudo systemctl stop polymarket-bot
sudo systemctl restart polymarket-bot
sudo systemctl status polymarket-bot
journalctl -u polymarket-bot -f
```

修改服务用户或安装目录时，同时修改 service 中相关路径。异常退出自动重启；正常终止先停止开仓、确认撤单。SIGKILL/断电无法执行清理，实盘买单依靠 GTD 到期和重启对账恢复；卖单使用 GTC。

可选 Docker：`python:3.11-slim-bookworm`，`docker compose up -d --build`。`.env` 不进入镜像。首次创建挂载目录需要让 UID 10001 可写。实盘健康检查请在 `.env` 增加 `BOT_HEALTH_MODE=live`。Docker 单文件 bind mount 在宿主机原子替换配置时可能保留旧 inode，修改后运行 `docker compose up -d --force-recreate`；原生运行和 systemd 支持实时配置更新。

## 官方资料

- 新 Python SDK：https://github.com/Polymarket/py-sdk
- SDK 认证：https://docs.polymarket.com/trading/wallets-auth
- 订单：https://docs.polymarket.com/trading/place-orders
- 订单管理：https://docs.polymarket.com/trading/manage-orders
- 费用：https://docs.polymarket.com/trading/fees
- 实时数据：https://docs.polymarket.com/market-data/realtime-data

核实日期：2026-11-16。旧 `py-clob-client` 已归档并明确提示不可用于新集成。当前 SDK/文档使用 pUSD 报价资产，文档中传统“USDC / 1u”口语不应被误解为程序自动桥接或兑换 USDC；实际余额以 SDK 的 COLLATERAL 返回为准。
