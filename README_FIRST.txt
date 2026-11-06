更新方式（Mac）

这个项目的目标是 Webull 真实交易。程序没有模拟/实盘选择器。Dashboard 的 Strategy 1 START 在风险门控通过时可以提交真实订单；自动测试里的 Mock 不会在 Webull 账户产生交易。

运行自动化测试：
  cd ~/spy-trading-bot
  source .venv/bin/activate
  python -m unittest discover -s tests -v

启动 Dashboard：
  python app.py
  浏览器访问 http://127.0.0.1:8765

仅启动 Dashboard 不会开始交易；按 START 才会启动 Strategy 1。完整实盘验收尚未完成，开发和测试期间不要按 START。详见 README.txt。
