# Architecture

## 当前策略与证据边界

`main.py` 通过 `Config` 和 CLI 进入唯一 maker 循环：只买 Up / Down 限价单，
没有 SELL、主动吃 ask 补仓或到期前主动卖出路径，已成交库存持有至官方结算。
默认 paper，显式 `--live` 才会真实签名与发送委托；不自动发出赎回、合并、拆分或审批交易。

研究参考钱包为 `0x5E498154448608C826E005c93533B827Cbb6377e`。
用户确认关注的最新版是 maker、只买不卖；这是当前设计约束，不是源码一致性证明。
2026-11-14 的历史公开样本有 3,089 个目标 fill，约 93.3% maker / 6.7% taker，全部 Bid。
这些历史 taker 记录保留用于说明数据，不能当作最新版仍有主动补仓的证明。
LIMIT 订单可以成为 maker 或 taker，不能由 taker 身份推断是 MARKET 单。

100 / 40 股与 1 美分网格有历史成交样本依据，但不能恢复未成交挂单、撤单、报价模型与阈值。
本实现是自研风险策略，不是目标私有源码完整复刻。所有默认风险参数均为显式设计假设。
样本范围和人工案例见 `README.md`。

## 行情与市场对象

`PredictMarketFeed` 从 `/v1/series/{slug}/rounds` 发现当前开放、带起始价格的 crypto up/down 轮次，
60 / 1440 分钟转换为官方 `1h` / `1d` slug。市场对象保留 outcomes、按 indexSet 1/2 映射的
outcome token IDs、精度、`isNegRisk` / `isYieldBearing`、奖励门槛与书时间戳。

`PredictWebSocketFeed` 订阅 `predictOrderbook/{marketId}`、`predictTradingStatus/{marketId}`、
`predictMarketStatus/{marketId}`、`predictMarketChanged/{marketId}`，处理心跳与重连重订阅。
书更新唤醒扫描，经 debounce / `min_scan_interval` 节流；默认扫描等待上限 12 秒、最短间隔 1 秒。
暂停、只允许撤单、关闭或已结算市场退出报价流程。

优先使用新鲜 socket 完整书；缺失时 `/v1/markets/orderbooks` 按最多 100 IDs 分批读取，
确实缺少该市场时才回退 `/v1/markets/quotes`。报价、深度、时间戳同源，REST 不混入 stale socket。
Down 书由 Yes 书交换 bid/ask 并取互补价格，按市场 tick 计算，shares 不变。
quote-only 快照不能产生模拟深度成交。

行情新鲜度默认上限 15 秒。缺失/过期/无法解析行情、市场状态不允许或扫描失败时撤回报价；
实盘撤单失败或移除锁未到期时只保留诊断和资金预留，不把撤单请求当成已经撤回。

## 报价、风险裁剪与全局配资

`quote_ladder` 创建两个 outcome 的 BUY 梯队。默认 mid 下方 1 美分、间隔 1 美分、
每侧最多 5 档，范围 0.01–0.99，5m / 15m 每档基准 100 / 40 股。
`level_notional=0` 使用股数基准，正值启用可选金额定仓。
报价低于当时可见对手 ask，但这不保证订单到达时仍不成交。

`risk_adjusted_ladder` 在 paper / live 都执行自研规则：

- 最坏单侧全部成交后的净库存默认不超过 200 股；按各侧容量裁剪，不能假设对侧同时成交。
- 拟议双边配对的净股成本至多 `1 - pair_margin`，默认 margin 为 0.02 USDT；
  补缺少侧还要结合未配对库存成本，且补单股数不超过当前净缺口。
- 单市场已付持仓成本加全部买单预留默认不超过 250 USDT。此步骤可能缩小报价股数。
- 剩余 60 秒只允许满足配对条件的缺少侧买单；已平衡时停止新增。
- 剩余 10 秒撤全部未成交买单；不卖掉已成交库存。
- 相邻扫描 mid 变动达到 0.12 时撤单，冷却 10 秒；陈旧/缺失行情也触发撤单及冷却。

实盘 `isPostOnly=false`，所以风险价格需按市场 `fee_rate_bps` 预留潜在 taker 手续费，
以净到账 shares 评估配对成本。maker 实际成交费用为零时，真实净量按 match 记录确认。
这些约束只控制拟议新增风险，不能撤销已经成交的逆向选择或保证缺少侧成交。

`fund_ladders` 接收已经过风险裁剪的梯队，按较近深度优先、遍历市场分配同一账户资金。
它只选择或省略档位，不再次 resize 股数；保留订单按剩余量预留，而非原始完整数量重复占款。
因此 100 / 40 是基准，最终尺寸可能已经由风险阶段裁剪；300 USDT 也不能挂满所有基准档位。

`starting_cash` 默认 300，是两种模式的全账户已部署成本上限：

```text
剩余成本空间 = max(0, starting_cash - 全账户未结算持仓成本)
实盘买单总预算 = min(剩余成本空间, 链上 USDT 余额)
模拟买单总预算 = min(剩余成本空间, 模拟可用现金)
```

全部挂单、未确认撤单、未知提交与尚未入库存快照的成交保持 reservation；
单市场限额与全账户限额共同生效。当前不报价市场的持仓和仍存续的委托也不能遗漏。
配对仓位仍持有时不提前释放资金；没有自动 merge，也没有把预估结算收益当作可用现金。

`--rebalance-trigger` 保留为兼容参数，但非零值在 CLI 直接拒绝。
旧 helper 即使留在项目中，也不代表主循环启用它；当前主循环没有主动吃 ask 再平衡调用。
`--skew-cents` 是可选库存价格偏移，两种模式均可用，默认 0。

## 模拟订单、成交与结算账本

`PaperPortfolio.sync_maker_quotes` 保留价格/目标尺寸未变化的委托和部分成交的剩余股数，
完全成交后才按 `replenish_delay` 再补；风险改变目标量时重新协调。

`collect_maker_fills` 使用既有 ask-crossing 模型：只处理挂单之后的新完整书，
以 Up asks 与 Up bids 互补得到的 Down asks 作为可用深度，按限价、剩余量、现金生成部分成交。
同次快照不重复分配可见深度，相同或旧时间戳、初始书、过期书、quote-only 均不伪造成交。
跳价时先处理旧委托可能发生的成交，再执行 shock 保护，避免回看后删除不利 fills。

该模型没有真实队列和完整主动成交逐笔，书变化也可能是撤单，因此不能称为真实回测。
主循环按市场逐一记账，避免批量模拟重复使用同一现金。
`PaperTrader` / `PaperPortfolio` 沿用既有 JSON / CSV / TXT 账本；已有现金不会被 `starting_cash` 重置，
但本次配置的全局成本上限仍生效。

奖励只累计满足 `spreadThreshold` / `shareThreshold` 的时间和股数时间，不增加现金或利润。
`rewards.schedule.hourlyRate` 是积分速度，不是可直接入账的 USDT。
模拟仅在 `/v1/markets/{id}` 确认 `RESOLVED` 后结算，胜方每股 1、败方 0、50/50 每边 0.5。
实盘不经过模拟成交、现金或结算路径。

## 真实库存与终态成交对账

每轮在新增委托前完整分页读取 `/v1/positions/{accountAddress}?isResolved=false&first=100`，
持续传回 cursor 直到结束，拒绝坏页面和重复 cursor。Predict account 使用 account address。
风险快照按 `market.id + outcome.onChainId` 去重，token ID 保持字符串，按已发现 token 映射 UP/DOWN。
`amount` 为 18 位整数单位，除 `10^18` 得 shares；`averageBuyPriceUsd` 是十进制每股均价，
不再除 `10^18`，其与 shares 相乘得到当前持仓成本。全账户全部未结算市场都计入总成本。

`LiveBook` 首次完整快照作为库存基线，本进程的已确认净买入形成索引追赶下界。
持仓快照是库存来源，成交计数只验证其已追上，不能再把本地成交重复加到 API 库存。
额外 shares（如 maker rebate）也作为真实风险库存接纳；这不证明它们是策略交易盈利。

`refresh` 检查 OPEN 与 FILLED / CANCELLED / EXPIRED / INVALIDATED 等终态，
终态仍需拉取完整成交，避免漏掉最后一次 fill。成交按 settlement 身份去重，核对订单 hash、
market、outcome 和 BUY 身份；毛成交减 `SHARES` 费用得到净到账量，`COLLATERAL` 费用记入现金成本。
成交总量必须与订单已结算量相符。

索引尚未追上、净量倒退、身份不符、订单/成交状态未知、坏字段、资金读取失败等情况 fail closed：
撤掉本进程能撤的单并停止新增，保留预留及 hash 后续核对。只有完整库存、订单、成交与资金全部校验通过后才允许尝试恢复；无法确认时继续暂停。
市场官方结算后才允许释放对应库存下界；这不等于自动赎回或直接增加链上余额。

取消 ACK 或部分成交终态不证明链上 pending 已结束。`MarketSnapshot.settlements_pending`
是三态观察：只有显式、新鲜的 WS 完整书提供合法 `settlementsPending.asks/bids` 才记为 true / false，
REST-only、缺字段或无法确认来源的状态保持 unknown，不能默认为 false。
`main.py` 将有效观察送入 `LiveBook.observe_pending`，其 pending-clear 书时间戳必须严格晚于
`cancelAckAt` 与首次 `terminalObservedAt`，再 GET 确认 terminal、校验完整成交，并取得通过核对的
新一轮完整 positions，才允许释放 `terminalHoldWei` 等剩余 reservation。
当前依赖全市场 pending-clear，不能识别其中哪笔属于本订单；未实现 wallet events 的逐笔结算跟踪。

这个保守门槛可能使取消后长期暂停重挂：REST 价格恢复、余额未减少或 cancel ACK 都不足以放开。
需等待有效新鲜 WS pending-clear，或在订单已知 terminal、成交明确时由官方 resolved 路径结束保留。
`resolve_market` 遇仍 OPEN、订单未知或成交未知的订单仍返回拒绝，不释放责任；
官方结算不会覆盖这些未知状态。这里描述的是执行限制，不承诺索引追上后必然立即恢复报价。

## 实盘委托生命周期

`LiveTrader` 按用户配置的 EOA / Predict account 认证签名，不在失败时自动换账户。
只发送 BUY LIMIT，按用户要求 `isPostOnly=false`，自成交防护为 `CANCEL_TAKER`，
使用正确 outcome token 与 `isNegRisk` / `isYieldBearing`。
价格变动可能使 LIMIT 到达时吃单，故执行不能保证纯 maker，也没有另启 ask-taking 策略。

启动发现账户已有 OPEN 委托时拒绝部署，不接管或撤销无法归属的旧单。
`LiveBook` 管理本进程 hash / ID；未变化订单保留队列，部分成交保留剩余量。
改价/改量先撤旧单，逐项核实官方返回的移除 IDs；未知提交保留原 hash 查询，不重发第二笔。
未确认或锁定的移除不释放 reservation；重启后仍须人工处理遗留委托。
退出、行情失效与风控触发时尝试撤单，网络和锁定意味着无法保证即时全部撤回。

本次验证范围为离线检查、mock 回归及公开 GET 查询，没有发送真实委托，没有完成实盘验证。

## 收益口径与场景验证

交易结算收益用实际净 shares 与支付成本，积分/预估奖励不计收入。
若 `P=min(qU,qD)`，按固定平均成本口径分配 paired / unpaired cost：

```text
PnL = P - paired_cost + 未配对结算支付 - unpaired_cost
settlement_floor = min(qU,qD) - total_cost
settlement_ceiling = max(qU,qD) - total_cost
```

终端 `settlePnL` 是全库存区间，不能用仅有的 `paired_profit` 替代。
`maker_pair_stats` 在 2 秒窗口内按时间配对，只是局部诊断，并非整个账户的结算收益。
真实库存含 rebate / 外部转入时，风险区间可能包含这些来源，不是已剔除奖励的策略 alpha。

人工例：U100@.47 + D100@.47 的结算收益为 +6；U20@.47 + D100@.47 且 Up 赢为 -36.4；
U100@.48 + D20@.48 且 Down 赢为 -37.6。昂贵补对 U100@.48 + D100@.70 固定亏 -18，
属于应阻止的计划配对，不能宣传为锁利。低流动、单腿尾盘、50/50、断流、部分成交、
撤单延迟、持仓索引滞后及跨市场同时占款都需覆盖。这些是会计/风控人工案例，不是历史回测，
不支持策略正收益或目标钱包复刻准确率的结论。

已取得公开数据另覆盖 38 个完整已结算轮次。按毛股数乘实体成交价重建成本、
按扣股份手续费后的净股数计算胜方支付，20 轮为正、18 轮为负，合计 -479.6321844 USDT。
4532206 的 Up / Down 净流量为 4032.7689816 / 3921.62301976、成本 4030.95611332，
Down 胜对应 -109.33309356；4532584 的净流量为 2676.5854465 / 2676.5798807、
成本 2878.15458834，Up 胜对应 -201.56914184。
这些只是成交重建反例，未核奖励、返佣、转账等非成交流量，不是实际账户净收益；
不能由此断言账户整体盈亏，也不能预设参考钱包每种行情都盈利。

## 官方参考

- [公开 match](https://dev.predict.fun/api/orders/get-order-match-events)
- [本人订单与 JWT](https://dev.predict.fun/api/orders/get-orders)
- [按地址查询持仓](https://dev.predict.fun/api/positions/get-positions-by-address)
- [持仓单位与结算支付](https://dev.predict.fun/concepts/positions)
- [共享订单簿与互补价格](https://dev.predict.fun/concepts/order-book)
- [做市、post-only 与自成交防护](https://dev.predict.fun/trading/market-making)
- [手续费与 rebates](https://dev.predict.fun/trading/fees)
- [结算](https://dev.predict.fun/concepts/resolution)
