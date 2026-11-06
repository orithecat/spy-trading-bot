SPY Trading Bot — Strategy 1

本项目目标是通过 Webull US 账户进行真实 SPY 期权交易。程序没有用户可切换的模拟/实盘模式。离线 Mock 只用于自动化测试，不代表账户中的交易。

安全提示：启动 app.py 只启动 Dashboard；按 Strategy 1 的 START 后，在所有信号、账户、预算和风险门控通过时，程序会尝试提交真实 Webull 期权订单。不要把 START 当作模拟运行按钮。

Strategy 1 当前代码包含 Webull 期权限价买单、订单状态轮询、成交记录持久化、从成交重建持仓、止盈/止损 SELL_TO_CLOSE、普通 STOP 继续管理已知持仓，以及账户级 EMERGENCY STOP 的退出尝试。策略和风险设置由现有配置控制。

仍需完成完整发布前验证：使用官方 SDK 3.0.4 参数核验真实订单与成交响应；验证部分成交、订单重启恢复、真实账户持仓对账、卖单拒单/部分成交恢复、紧急停止及 Dashboard 控制的端到端故障处理。当前仅有离线 Mock 测试，开发期间不会连接 Webull 或发送订单。不要将本次开发测试视为真实账户实盘验收。

运行离线测试：
  source .venv/bin/activate
  python -m unittest discover -s tests -v

启动 Dashboard：
  python app.py
  浏览器访问 http://127.0.0.1:8765

凭据保存在本机 .env 与 conf/token.txt 中。不要分享这些文件或其内容。测试不得读取真实账户、发送订单或暴露凭据。
