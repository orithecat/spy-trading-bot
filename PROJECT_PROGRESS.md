# SPY Trading Bot — Project Progress

最后整理日期：2026-11-06

## 项目开发管理原则

- 项目按以下九个功能区域逐个完成。
- 每次集中处理一个区域，完成验收后再进入下一个区域。
- 每个区域完成后，必须更新本文件，记录完成内容、测试结果、未解决问题和下一步。
- 不随意移动现有文件或重构目录。
- 不得擅自修改交易策略参数、资金分配、入场条件、退出条件或风险限制。
- 不得使用真实 BUY 订单进行测试，未经用户明确授权不得开启真实 BUY。
- 所有自动平仓必须使用 MARKET SELL_TO_CLOSE，并核实真实成交。
- 账户只允许使用唯一匹配的 MARGIN 账户，不得使用 CASH。
- 本文件是项目进度记录的主要依据；每次开始新区域前先读取本文件和相关代码。

## 九个功能区域及当前进度

以下百分比是初步源码审查估算，不代表真实环境验收通过率。

| 编号 | 功能区域 | 当前进度 | 状态 |
|---|---|---:|---|
| 1 | 账户连接与账户数据 | 75% | 部分完成 |
| 2 | 行情数据与期权合约 | 80% | 部分完成 |
| 3 | Strategy 1 信号与入场 | 85% | 部分完成 |
| 4 | 真实下单与订单管理 | 75% | 部分完成 |
| 5 | 持仓管理与账户对账 | 80% | 部分完成 |
| 6 | 账户级风险控制 | 60% | 部分完成，优先处理 |
| 7 | START / STOP / Emergency Stop | 75% | 部分完成 |
| 8 | Dashboard 与设置 | 65% | 部分完成 |
| 9 | Strategy 2 和 Strategy 3 | 0% | 尚未实现 |

## 各区域工作范围

### 区域 1：账户连接与账户数据（75%）
主要文件：
- account_data.py
- account_monitor.py

工作范围：
- Webull US API 账户连接和账户数据读取。
- 唯一 MARGIN 账户验证。
- Equity、Buying Power、持仓数据读取。
- 数据有效性、新鲜度和异常处理。

待完成：真实 SDK 响应、权限、超时和恢复流程验收。

### 区域 2：行情数据与期权合约（80%）
主要文件：
- market_data.py
- option_contracts.py
- option_history.py

工作范围：
- SPY 行情和 EMA 计算。
- 1 分钟及 5 分钟趋势数据。
- 期权合约筛选。
- Bid/Ask、Spread、报价延迟和新鲜度检查。

待完成：真实行情连续运行和异常恢复验收。

### 区域 3：Strategy 1 信号与入场（85%）
主要文件：
- strategies/strategy1.py
- strategy1_runner.py

工作范围：
- CALL / PUT 信号判断。
- EMA 趋势和 5 分钟确认。
- 期权反弹触发。
- 报价质量过滤。
- 0DTE / 1DTE 和交易时间限制。

待完成：核对实际配置与已确认策略参数，完成端到端入场验收。
注意：不得擅自改变用户已确认的策略参数。

### 区域 4：真实下单与订单管理（75%）
主要文件：
- order_execution.py

工作范围：
- BUY_TO_OPEN 下单。
- MARKET SELL_TO_CLOSE 自动退出。
- Order ID 和 Client Order ID。
- 防止重复下单。
- Partial Fill、Reject、Timeout、Unknown 状态处理。
- 订单持久化和重启恢复。

待完成：真实 Webull SDK 请求及响应格式核验，完成订单异常场景验收。
禁止使用真实 BUY 订单测试。

### 区域 5：持仓管理与账户对账（80%）
主要文件：
- strategy1_runner.py
- order_execution.py

工作范围：
- 根据实际成交数量维护持仓。
- 部分平仓和剩余数量管理。
- 期权合约身份和数量核对。
- 防止重复卖出和未记录持仓。
- 重启后恢复持仓状态。

待完成：真实账户持仓、成交和更新时间差异验收。
金额舍入或更新时间差异不能单独认定为持仓冲突。

### 区域 6：账户级风险控制（60%）
主要文件：
- account_risk_monitor.py
- account_monitor.py
- risk.py
- strategy1_runner.py
- order_execution.py

工作范围：
- 每日初始 Equity。
- 当日盈利 +30% 上限。
- 账户总 Equity 峰值，包括未实现盈亏。
- 从峰值回撤 -10% 的全局风险限制。
- 风险锁定和禁止新入场。
- 触发风险后账户级清仓。
- MARKET SELL_TO_CLOSE。
- 核实全部成交和账户确实清仓。
- 未确认清仓时持续锁定。

当前主要缺口：核实风险触发、全局停止新入场、账户全部平仓、成交核实和风险锁定之间是否形成完整闭环。

### 区域 7：START / STOP / Emergency Stop（75%）
主要文件：
- strategy1_runner.py
- app.py

工作范围：
- START 启动策略。
- 普通 STOP 仅阻止新入场，继续管理已有持仓。
- Emergency Stop 是账户级操作。
- 取消未成交入场订单。
- 对账户持仓执行 MARKET 清仓。
- 确认全部清仓后才能解除 Emergency Stop。
- START 不得自动提交 BUY。

待完成：真实订单异常、账户数据异常和重启恢复场景验收。

### 区域 8：Dashboard 与设置（65%）
主要文件：
- app.py
- templates/
- static/

工作范围：
- 本地 Dashboard。
- 策略状态和交易状态展示。
- 参数设置和保存。
- START / STOP / Emergency Stop。
- 日志展示和错误反馈。

待完成：页面与真实 Runner 的完整集成、状态同步和设置生效时机验收。
注意：旧版 dashboard.py 可能是独立原型，不能误认为当前实际控制入口。

### 区域 9：Strategy 2 和 Strategy 3（0%）
主要文件：
- strategies/strategy2.py
- strategies/strategy3.py

工作范围：
- Strategy 2：期权价格 Trailing Buy。
- Strategy 3：后续确定和实现。

当前尚未进入正式开发。先完成 Strategy 1 和账户级安全功能。

## 建议完成顺序

1. 区域 6：账户级风险控制。
2. 区域 4：真实下单与订单管理。
3. 区域 5：持仓管理与账户对账。
4. 区域 3：Strategy 1 信号与入场。
5. 区域 7：START / STOP / Emergency Stop。
6. 区域 1：账户连接与账户数据验收。
7. 区域 2：行情数据与期权合约验收。
8. 区域 8：Dashboard 与设置。
9. 区域 9：Strategy 2 和 Strategy 3。

## 当前工作位置

当前优先开发：区域 6 — 账户级风险控制。

当前阶段：需要先完整审查现有实现，确认风险触发到全账户清仓的调用链和缺口，再实施修改。

## 区域验收记录

每个区域完成后，在这里追加记录：

- 区域编号：
- 完成日期：
- 修改文件：
- 实现内容：
- 自动化测试结果：
- 实际环境验收结果：
- 未解决问题：
- 最终进度：
- GitHub Commit SHA：

目前尚无区域被正式标记为 100% 完成。以上进度均为初步估算，不能视为实盘验收通过。
