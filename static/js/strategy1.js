const $ = id => document.getElementById(id);

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined
  });

  const data = await response.json();

  if (!response.ok) {
    throw Error(data.error || "Request failed");
  }

  return data;
}

function displayValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  return `${value}${suffix}`;
}

function showCandidate(direction, candidate) {
  const prefix = direction.toLowerCase() + "Candidate";
  const fields = {
    Symbol: candidate?.symbol,
    Strike: candidate?.strike,
    Expiry: candidate?.expiry,
    Low: candidate?.tracking_low,
    Trigger: candidate?.trigger_price,
    Status: candidate?.status
  };

  for (const [field, value] of Object.entries(fields)) {
    $(prefix + field).textContent = displayValue(value);
  }

  const statusElement = $(prefix + "Status");
  statusElement.textContent = candidate
    ? (candidate.eligible ? "ELIGIBLE" : "WAITING")
    : "No candidate";
}

function showPosition(direction, position) {
  const prefix = direction.toLowerCase();
  const fields = {
    Symbol: position?.symbol,
    Strike: position?.strike,
    Expiry: position?.expiry,
    EntryPrice: position?.entry_price,
    CurrentBid: position?.current_bid,
    HighestBid: position?.highest_bid,
    Quantity: position?.quantity,
    Pnl: position?.unrealized_pnl,
    Status: position?.status
  };

  $(prefix + "PositionStatus").textContent =
    position ? "Open live position" : "No open position";

  for (const [field, value] of Object.entries(fields)) {
    $(prefix + field).textContent = displayValue(value);
  }

  const pnlElement = $(prefix + "Pnl");
  pnlElement.classList.toggle(
    "positive",
    Number(position?.unrealized_pnl) > 0
  );
  pnlElement.classList.toggle(
    "negative",
    Number(position?.unrealized_pnl) < 0
  );
}

function show(data) {
  const strategy = data.strategies.strategy1;
  const parameters = data.parameters.strategy1;

  $("detailStatus").textContent =
    strategy.emergency_stop
      ? "EMERGENCY STOP"
      : strategy.running
        ? strategy.message?.toLowerCase().includes("not authorized")
          ? "RUNNING · BUY LOCKED"
          : "RUNNING · REAL ORDERS ARMED"
        : "STOPPED";

  $("detailStatus").className =
    "pill" +
    (strategy.emergency_stop
      ? " danger"
      : strategy.running
        ? " live"
        : "");

  const fields = [
    ["spyPrice", "spy_price"],
    ["signal", "signal"],
    ["ema9", "ema9"],
    ["ema21", "ema21"],
    ["ema50", "ema50"],
    ["emaGap", "ema_gap"],
    ["fiveMinTrend", "five_min_trend"],
    ["candidateOption", "candidate_option"],
    ["detailMessage", "message"]
  ];

  for (const [id, key] of fields) {
    $(id).textContent = strategy[key] ?? "—";
  }

  const candidates = strategy.candidates || {};
  showCandidate("CALL", candidates.CALL || null);
  showCandidate("PUT", candidates.PUT || null);

  const positions = strategy.positions || {};
  showPosition("CALL", positions.CALL || null);
  showPosition("PUT", positions.PUT || null);

  const form = $("settingsForm");

  for (const [key, value] of Object.entries(parameters)) {
    if (key !== "entry_filters" && form.elements[key]) {
      form.elements[key].value = value;
    }
  }

  form.elements.filter_ema_gap_not_shrinking.checked =
    parameters.entry_filters?.ema_gap_not_shrinking !== false;

  $("startButton").disabled =
    strategy.running || data.risk_latched;

  $("stopButton").disabled = !strategy.running;
  $("emergencyButton").disabled = strategy.emergency_stop;
}

async function refresh() {
  try {
    show(await api("/api/status"));
  } catch (error) {
    $("detailMessage").textContent = error.message;
  }
}

$("settingsForm").onsubmit = async event => {
  event.preventDefault();

  const form = event.target;
  const parameters = Object.fromEntries(new FormData(form));

  for (const key of [
    "max_trade_amount",
    "take_profit_pct",
    "stop_loss_pct",
    "rebound_trigger_pct"
  ]) {
    parameters[key] = Number(parameters[key]);
  }

  parameters.entry_filters = {
    ema_gap_not_shrinking:
      form.elements.filter_ema_gap_not_shrinking.checked
  };

  try {
    const data = await api("/api/strategy1/settings", parameters);
    show(data);
    $("settingsMessage").textContent = "Settings saved.";
  } catch (error) {
    $("settingsMessage").textContent = error.message;
  }
};

async function control(action) {
  if (
    action === "emergency_stop" &&
    !confirm("Stop ALL strategies? Click START later to clear the emergency stop and resume.")
  ) {
    return;
  }

  try {
    await api("/api/control", {
      action,
      strategy: "strategy1"
    });
    await refresh();
  } catch (error) {
    $("detailMessage").textContent = error.message;
  }
}

$("startButton").onclick = () => control("start");
$("stopButton").onclick = () => control("stop");
$("emergencyButton").onclick = () => control("emergency_stop");

document.querySelectorAll("[data-home]").forEach(button => {
  button.onclick = () => {
    location.href = "/";
  };
});

refresh();
setInterval(refresh, 3000);
