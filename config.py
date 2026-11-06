from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent

# Keep real Strategy 1 entries disabled until the user authorizes the fully
# tested live workflow. This is a code-level safety gate, not a runtime mode.
REAL_TRADING_AUTHORIZED = False

STRATEGIES = {
    "strategy1": {
        "name": "Strategy 1",
        "subtitle": "EMA + Confirmed Trailing Buy",
        "icon": "📈",
    },
    "strategy2": {
        "name": "Strategy 2",
        "subtitle": "Not implemented yet",
        "icon": "🎯",
    },
    "strategy3": {
        "name": "Strategy 3",
        "subtitle": "Not implemented yet",
        "icon": "📊",
    },
}

DEFAULT_SETTINGS = {
    "global": {
        "daily_profit_cap_pct": 30,
        "peak_drawdown_limit_pct": 10,
    },
    "strategy1": {
        "expiry": "0DTE",
        "max_trade_amount": 500,
        "take_profit_pct": 4,
        "stop_loss_pct": 5,
        "rebound_trigger_pct": 4,
        "entry_filters": {
            "ema_gap_not_shrinking": True,
        },
    },
    "strategy2": {"expiry": "0DTE"},
    "strategy3": {"expiry": "0DTE"},
}
