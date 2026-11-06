
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8765

STRATEGIES = {
    "strategy1": {
        "name": "Strategy 1",
        "subtitle": "EMA + Trailing Buy",
        "description": (
            "EMA 9/21/50 trend checks, option low tracking, "
            "and rebound entry."
        ),
        "icon": "📈",
        "parameters": {
            "EMA Periods": "9 / 21 / 50",
            "Rebound Trigger": "+4%",
            "Expiry": "0DTE / 1DTE",
        },
    },
    "strategy2": {
        "name": "Strategy 2",
        "subtitle": "Option Price Tracking",
        "description": (
            "Track option prices and evaluate entry conditions."
        ),
        "icon": "🎯",
        "parameters": {
            "Entry Rule": "To be finalized",
            "Expiry": "0DTE / 1DTE",
            "Position Limits": "To be finalized",
        },
    },
    "strategy3": {
        "name": "Strategy 3",
        "subtitle": "Trend Direct Entry",
        "description": (
            "Trend-based direct entry without necessarily "
            "waiting for a rebound."
        ),
        "icon": "📊",
        "parameters": {
            "Trend Rule": "To be finalized",
            "Entry Confirmation": "To be finalized",
            "Expiry": "0DTE / 1DTE",
        },
    },
}

state_lock = threading.Lock()

state = {
    "active_strategy": None,
    "strategies": {
        key: {
            "running": False,
            "emergency_stop": False,
            "message": "Ready. Trading engine is not connected.",
            "signal": "Waiting",
            "spy_price": None,
            "candidate_option": None,
            "ema21": None,
            "ema50": None,
        }
        for key in STRATEGIES
    },
}


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport"
 content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#10141d">
<title>SPY Trading Bot</title>
<style>
:root {
  color-scheme: dark;
  --bg: #10141d;
  --panel: #1a202c;
  --border: #30394a;
  --text: #f4f6fb;
  --muted: #9ba7ba;
  --green: #42d392;
  --red: #ff727f;
  --blue: #81aaff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  width: min(100%, 620px);
  margin: auto;
  padding: 22px 16px calc(32px + env(safe-area-inset-bottom));
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;
}
h1 { font-size: 24px; margin: 0 0 5px; }
h2 { font-size: 17px; margin: 24px 0 12px; }
h3 { font-size: 15px; margin: 0; }
p { line-height: 1.55; }
.subtitle, .muted { color: var(--muted); font-size: 13px; }
.pill {
  padding: 7px 10px;
  border-radius: 20px;
  background: #293345;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.pill.live { color: var(--green); }
.pill.danger { color: #ffd0d5; background: #55252d; }
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 15px;
  padding: 16px;
  margin-bottom: 12px;
}
.pnl-label { color: var(--muted); font-size: 13px; }
.pnl { font-size: 34px; font-weight: 750; margin: 8px 0; }
.notice {
  border-radius: 10px;
  background: #292536;
  color: #e5c8ff;
  font-size: 12px;
  line-height: 1.55;
  padding: 11px 12px;
  margin-top: 14px;
}
.strategy-card {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 13px;
  text-align: left;
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 16px;
  border-radius: 14px;
  margin-bottom: 12px;
}
.strategy-card:active { border-color: var(--blue); }
.strategy-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  background: #293345;
  border-radius: 12px;
  font-size: 23px;
  flex-shrink: 0;
}
.strategy-content { flex: 1; min-width: 0; }
.strategy-title { font-size: 15px; font-weight: 700; }
.strategy-desc {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
  margin-top: 5px;
}
.arrow { color: var(--muted); font-size: 24px; }
.row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.back {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 9px 12px;
  border-radius: 10px;
  margin-bottom: 18px;
  font-size: 13px;
}
.metrics {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.metric {
  background: #141a24;
  border-radius: 10px;
  padding: 13px;
  min-width: 0;
}
.metric-label { color: var(--muted); font-size: 12px; }
.metric-value {
  font-size: 17px;
  font-weight: 650;
  margin-top: 8px;
  overflow-wrap: anywhere;
}
button {
  font: inherit;
  font-weight: 650;
  border: 0;
  border-radius: 11px;
  padding: 14px 10px;
  min-height: 48px;
  cursor: pointer;
}
button:disabled { opacity: .4; cursor: not-allowed; }
.controls {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.start { background: #18794e; color: white; }
.stop { background: #30394a; color: white; }
.emergency {
  width: 100%;
  margin-top: 10px;
  background: #55252d;
  border: 1px solid #a84956;
  color: #ffd9dd;
}
.param-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.param-row:last-child { border-bottom: 0; }
.param-value { color: var(--muted); text-align: right; }
.message {
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.55;
}
.hidden { display: none !important; }
footer {
  text-align: center;
  margin-top: 26px;
  color: var(--muted);
  font-size: 11px;
}
@media (min-width: 520px) {
  main { padding-top: 32px; }
}
</style>
</head>
<body>
<main>
  <section id="homePage">
    <header>
      <div>
        <h1>SPY Trading Bot</h1>
        <div class="subtitle">Local Dashboard</div>
      </div>
      <div class="pill">PROTOTYPE</div>
    </header>

    <section class="card">
      <div class="pnl-label">Today's Total P&amp;L</div>
      <div class="pnl">—</div>
      <div class="muted">Waiting for account data</div>
      <div class="notice">
        Legacy prototype only. This page does not connect START/STOP to the
        Strategy 1 trading runner. Use app.py for the current controls.
      </div>
    </section>

    <h2>Choose a Strategy</h2>
    <p class="muted">
      Select a strategy to open its own dashboard and controls.
      Only one strategy can run at a time.
    </p>
    <div id="strategyList"></div>

    <section class="card">
      <div class="row">
        <div>
          <h3>Strategy Status</h3>
          <div class="muted" style="margin-top:6px">
            Strategy status
          </div>
        </div>
      </div>
      <div id="homeStatuses" style="margin-top:12px"></div>
    </section>
  </section>

  <section id="detailPage" class="hidden">
    <button id="backButton" class="back">← Back to Home</button>

    <header>
      <div>
        <h1 id="detailTitle">Strategy</h1>
        <div id="detailSubtitle" class="subtitle"></div>
      </div>
      <div id="detailStatus" class="pill">STOPPED</div>
    </header>

    <section class="card">
      <h3>Strategy Overview</h3>
      <p id="detailDescription" class="muted"></p>
      <div class="notice">
        This page currently controls demo state only.
        It does not start a trading algorithm or submit orders.
      </div>
    </section>

    <h2>Market &amp; Signal</h2>
    <section class="card">
      <div class="metrics">
        <div class="metric">
          <div class="metric-label">SPY Price</div>
          <div id="spyPrice" class="metric-value">—</div>
        </div>
        <div class="metric">
          <div class="metric-label">Signal</div>
          <div id="signal" class="metric-value">Waiting</div>
        </div>
        <div class="metric">
          <div class="metric-label">EMA 21</div>
          <div id="ema21" class="metric-value">—</div>
        </div>
        <div class="metric">
          <div class="metric-label">EMA 50</div>
          <div id="ema50" class="metric-value">—</div>
        </div>
        <div class="metric" style="grid-column:1 / -1">
          <div class="metric-label">Candidate Option</div>
          <div id="candidateOption" class="metric-value">—</div>
        </div>
      </div>
    </section>

    <h2>Strategy Parameters</h2>
    <section id="parameters" class="card"></section>

    <h2>Strategy Controls</h2>
    <section class="card">
      <div class="controls">
        <button id="startButton" class="start">▶ START</button>
        <button id="stopButton" class="stop">■ STOP</button>
      </div>
      <button id="emergencyButton" class="emergency">
        ⚠ EMERGENCY STOP
      </button>
      <p id="detailMessage" class="message muted" aria-live="polite"></p>
    </section>
  </section>

  <footer>
    Legacy prototype dashboard · not connected to real trading controls
  </footer>
</main>

<script>
const definitions = {
  strategy1: {
    name: "Strategy 1",
    subtitle: "EMA + Trailing Buy",
    icon: "📈",
    description:
      "EMA 9/21/50 trend checks, option low tracking, and rebound entry."
  },
  strategy2: {
    name: "Strategy 2",
    subtitle: "Option Price Tracking",
    icon: "🎯",
    description:
      "Track option prices and evaluate entry conditions. Detailed rules remain to be finalized."
  },
  strategy3: {
    name: "Strategy 3",
    subtitle: "Trend Direct Entry",
    icon: "📊",
    description:
      "Trend-based direct entry. Exact trigger and confirmation rules remain to be finalized."
  }
};

let currentPage = "home";
let currentStrategy = null;
let serverState = null;
let busy = false;

const $ = id => document.getElementById(id);

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.error || "Request failed");
  }

  return data;
}

function statusLabel(s) {
  if (s.emergency_stop) return "EMERGENCY STOP";
  if (s.running) return "RUNNING · DEMO";
  return "STOPPED";
}

function renderHome() {
  $("strategyList").innerHTML = Object.entries(definitions).map(
    ([key, def]) => {
      const s = serverState.strategies[key];
      const status = statusLabel(s);

      return `
        <button class="strategy-card" data-open="${key}">
          <div class="strategy-icon">${def.icon}</div>
          <div class="strategy-content">
            <div class="strategy-title">${def.name}</div>
            <div class="strategy-desc">${def.subtitle}</div>
            <div class="strategy-desc">${status}</div>
          </div>
          <div class="arrow">›</div>
        </button>
      `;
    }
  ).join("");

  document.querySelectorAll("[data-open]").forEach(button => {
    button.addEventListener("click", () => {
      openStrategy(button.dataset.open);
    });
  });

  $("homeStatuses").innerHTML = Object.entries(definitions).map(
    ([key, def]) => {
      const s = serverState.strategies[key];
      return `
        <div class="param-row">
          <span>${def.name}</span>
          <span class="param-value">${statusLabel(s)}</span>
        </div>
      `;
    }
  ).join("");
}

function renderDetail() {
  if (!currentStrategy || !serverState) return;

  const def = definitions[currentStrategy];
  const s = serverState.strategies[currentStrategy];
  const params = serverState.parameters[currentStrategy];

  $("detailTitle").textContent = def.name;
  $("detailSubtitle").textContent = def.subtitle;
  $("detailDescription").textContent = def.description;
  $("detailStatus").textContent = statusLabel(s);
  $("detailStatus").className =
    "pill" + (s.running ? " live" : "") +
    (s.emergency_stop ? " danger" : "");

  $("spyPrice").textContent = s.spy_price ?? "—";
  $("signal").textContent = s.signal ?? "Waiting";
  $("ema21").textContent = s.ema21 ?? "—";
  $("ema50").textContent = s.ema50 ?? "—";
  $("candidateOption").textContent = s.candidate_option ?? "—";
  $("detailMessage").textContent = s.message;

  $("parameters").innerHTML = Object.entries(params).map(
    ([key, value]) => `
      <div class="param-row">
        <span>${key}</span>
        <span class="param-value">${value}</span>
      </div>
    `
  ).join("");

  $("startButton").disabled =
    busy || s.running || s.emergency_stop ||
    Boolean(serverState.active_strategy &&
      serverState.active_strategy !== currentStrategy);

  $("stopButton").disabled = busy || !s.running;
  $("emergencyButton").disabled = busy || s.emergency_stop;
}

function render() {
  $("homePage").classList.toggle("hidden", currentPage !== "home");
  $("detailPage").classList.toggle("hidden", currentPage !== "detail");

  if (!serverState) return;

  if (currentPage === "home") renderHome();
  else renderDetail();
}

async function refresh() {
  try {
    serverState = await api("/api/status");
    render();
  } catch (err) {
    $("detailMessage").textContent =
      "Cannot connect to local server: " + err.message;
  }
}

function openStrategy(key) {
  currentStrategy = key;
  currentPage = "detail";
  render();
}

$("backButton").addEventListener("click", () => {
  currentPage = "home";
  render();
});

async function control(action) {
  if (busy || !currentStrategy) return;

  if (action === "emergency_stop") {
    const confirmed = confirm(
      "Activate emergency stop for this strategy? " +
      "This demo will latch the stop and block restarting it."
    );
    if (!confirmed) return;
  }

  busy = true;
  render();

  try {
    serverState = await api("/api/control", {
      action,
      strategy: currentStrategy
    });
  } catch (err) {
    alert(err.message);
  } finally {
    busy = false;
    await refresh();
  }
}

$("startButton").addEventListener("click", () => control("start"));
$("stopButton").addEventListener("click", () => control("stop"));
$("emergencyButton").addEventListener(
  "click", () => control("emergency_stop")
);

refresh();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type", "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/":
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/html; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/api/status":
            with state_lock:
                snapshot = {
                    "active_strategy": state["active_strategy"],
                    "strategies": {
                        key: dict(value)
                        for key, value in state["strategies"].items()
                    },
                    "parameters": {
                        key: dict(value["parameters"])
                        for key, value in STRATEGIES.items()
                    },
                }
            self.send_json(snapshot)
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path != "/api/control":
            self.send_json({"error": "Not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                self.send_json({"error": "Request too large"}, 413)
                return

            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self.send_json({"error": "Invalid JSON"}, 400)
            return

        action = payload.get("action")
        strategy = payload.get("strategy")

        if strategy not in STRATEGIES:
            self.send_json({"error": "Invalid strategy"}, 400)
            return

        with state_lock:
            selected = state["strategies"][strategy]

            if action == "start":
                if selected["emergency_stop"]:
                    self.send_json(
                        {"error": "Emergency stop is active for this strategy"},
                        409,
                    )
                    return

                other_running = [
                    key for key, value in state["strategies"].items()
                    if value["running"] and key != strategy
                ]

                if other_running:
                    self.send_json(
                        {
                            "error":
                            "Another strategy is running. Stop it first."
                        },
                        409,
                    )
                    return

                selected["running"] = True
                state["active_strategy"] = strategy
                selected["message"] = "Strategy 1 started. Qualifying signals submit real Webull orders."

            elif action == "stop":
                selected["running"] = False

                if state["active_strategy"] == strategy:
                    state["active_strategy"] = None

                selected["message"] = "Strategy stopped accepting new entries. Existing positions remain managed."

            elif action == "emergency_stop":
                selected["running"] = False
                selected["emergency_stop"] = True

                if state["active_strategy"] == strategy:
                    state["active_strategy"] = None

                selected["message"] = "Emergency stop latched. New entries are blocked and positions are being closed."

            else:
                self.send_json({"error": "Unknown action"}, 400)
                return

            snapshot = {
                "active_strategy": state["active_strategy"],
                "strategies": {
                    key: dict(value)
                    for key, value in state["strategies"].items()
                },
                "parameters": {
                    key: dict(value["parameters"])
                    for key, value in STRATEGIES.items()
                },
            }

        self.send_json(snapshot)

    def log_message(self, fmt, *args):
        print(f"[Dashboard] {self.address_string()} - {fmt % args}")


def main():
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

    print("=" * 52)
    print("SPY Trading Bot - Local Dashboard")
    print("=" * 52)
    print(f"Mac browser: http://127.0.0.1:{PORT}")
    print("Phone: use your Mac's local IP on the same Wi-Fi.")
    print("Legacy prototype only; this server is not connected to the trading runner.")
    print("Press Ctrl+C to stop the server.")
    print("=" * 52)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
