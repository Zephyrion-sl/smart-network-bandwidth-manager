const MBPS = 1_000_000;
const POLL_MS = 2000;

const REASONS = {
  insufficient_samples: "Waiting for a second traffic sample before deciding.",
  not_congested: "Utilization is under the congestion line. No new limits were selected.",
  unchanged: "The selected share already matches the installed share. tc was left unchanged.",
  cooldown: "A new share is ready, but the cooldown has not elapsed. tc was left unchanged.",
  applied: "The allocator selected a new share.",
  cleared: "The clear request removed the lab qdiscs.",
  enforcement_failed: "Linux did not accept the traffic-control update.",
};

const state = {
  clients: [],
  metrics: null,
  policy: null,
  allocations: null,
  events: [],
};
let formDirty = false;

const banner = document.querySelector("#banner");
const result = document.querySelector("#action-result");

function formatBps(bps) {
  if (bps == null || Number.isNaN(Number(bps))) return "—";
  const mbps = Number(bps) / MBPS;
  if (Math.abs(mbps) >= 0.05) return `${trim(mbps)} Mbps`;
  return `${trim(Number(bps) / 1000)} kbps`;
}

function trim(value) {
  return (Math.round(value * 10) / 10).toString();
}

function formatBytes(bytes) {
  const size = Number(bytes) || 0;
  if (size >= MBPS) return `${trim(size / MBPS)} MB`;
  if (size >= 1000) return `${trim(size / 1000)} KB`;
  return `${size} B`;
}

function formatPercent(ratio) {
  if (ratio == null || Number.isNaN(Number(ratio))) return "—";
  return `${trim(Number(ratio) * 100)}%`;
}

function loadBps(sample) {
  if (!sample) return null;
  const rates = [sample.rx_bps, sample.tx_bps].filter((value) => value != null);
  if (!rates.length) return null;
  return Math.max(...rates);
}

function metricById(clientId) {
  const rows = state.metrics ? state.metrics.clients : [];
  return rows.find((row) => row.client_id === clientId) || null;
}

function latestDecision() {
  return state.events.find((event) => event.details && "new_allocation" in event.details) || null;
}

function decisionEvaluated() {
  return state.events.some((event) => event.kind === "control_decision" || event.kind === "qos_cleared");
}

function allocatedBps(clientId) {
  const decision = latestDecision();
  if (!decision) return { known: false, bps: null };
  const share = decision.details.new_allocation || {};
  if (Object.prototype.hasOwnProperty.call(share, clientId)) {
    return { known: true, bps: share[clientId] };
  }
  return { known: true, bps: null };
}

function enforcedBps(client) {
  if (client.allocation_bps == null) return null;
  return client.allocation_bps;
}

function text(id, value) {
  document.querySelector(id).textContent = value;
}

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error ? body.error.message : `HTTP ${response.status}`);
    error.payload = body.error || null;
    error.status = response.status;
    throw error;
  }
  return body;
}

function showBanner(message) {
  banner.hidden = !message;
  banner.textContent = message || "";
}

function showResult(message, ok) {
  result.textContent = message;
  result.classList.toggle("ok", Boolean(ok));
}

function enforcementSentence(flag) {
  if (flag === true) return "Linux accepted the tc rules.";
  return "Not enforced.";
}

function describeAction(body, error) {
  const raw = error && error.details && !Array.isArray(error.details) ? error.details : body;
  const reason = raw && raw.reason ? raw.reason : "";
  const lead = REASONS[reason] || (error ? error.message : "Request finished.");
  if (raw && typeof raw.enforced === "boolean") return `${lead} ${enforcementSentence(raw.enforced === true)}`;
  return lead;
}

function renderSummary() {
  const policy = state.policy;
  text("#capacity", policy ? formatBps(policy.total_capacity) : "—");
  if (policy) {
    const clearAt = Math.max(0, policy.congestion_threshold - policy.clear_margin);
    text(
      "#threshold",
      `Enter at ${trim(policy.congestion_threshold * 100)}%, clear below ${trim(clearAt * 100)}%. Cooldown ${policy.cooldown}s.`,
    );
  }

  const loads = state.clients.map((client) => loadBps(metricById(client.client_id)));
  const ready = policy && loads.length > 0 && loads.every((value) => value != null);
  text(
    "#utilization",
    ready ? formatPercent(loads.reduce((sum, value) => sum + value, 0) / policy.total_capacity) : "Waiting for sample",
  );

  const congestion = document.querySelector("#congestion");
  congestion.className = "figure status";
  if (!decisionEvaluated()) {
    congestion.textContent = "Not evaluated";
  } else if (state.allocations && state.allocations.congested) {
    congestion.textContent = "Congested";
    congestion.classList.add("congested");
  } else {
    congestion.textContent = "Clear";
    congestion.classList.add("clear");
  }

  const online = state.clients.filter((client) => client.connection_status === "online").length;
  const offline = state.clients.filter((client) => client.connection_status === "offline").length;
  const unknown = state.clients.filter((client) => client.connection_status === "unknown").length;
  text("#clients-count", String(state.clients.length));
  text("#clients-detail", `${online} online · ${offline} offline · ${unknown} unknown`);
  text("#clock", state.metrics ? `Sample ${state.metrics.sampled_at}` : "No sample yet");
}

function policyText(client) {
  const policy = client.policy;
  const sharedMin = state.policy && state.policy.minimum_bandwidth
    ? formatBps(state.policy.minimum_bandwidth)
    : "none";
  const sharedMax = state.policy && state.policy.maximum_bandwidth != null
    ? formatBps(state.policy.maximum_bandwidth)
    : "none";
  const min = policy.min_bandwidth == null ? `min ${sharedMin} shared` : `min ${formatBps(policy.min_bandwidth)}`;
  const max = policy.max_bandwidth == null ? `limit ${sharedMax} shared` : `limit ${formatBps(policy.max_bandwidth)}`;
  return `weight ${policy.weight}, ${min}, ${max}`;
}

function throughputText(client) {
  const sample = metricById(client.client_id) || {};
  const rx = sample.rx_bps != null ? sample.rx_bps : client.current_bandwidth.rx_bps;
  const tx = sample.tx_bps != null ? sample.tx_bps : client.current_bandwidth.tx_bps;
  if (rx == null && tx == null) return "—";
  return `down ${formatBps(rx)} · up ${formatBps(tx)}`;
}

function shareText(info) {
  if (!info.known) return "—";
  if (info.bps == null) return "no limit";
  return formatBps(info.bps);
}

function renderClients() {
  const body = document.querySelector("#client-rows");
  body.replaceChildren();
  state.clients.forEach((client) => {
    const row = document.createElement("tr");
    const enforced = enforcedBps(client);
    const cells = [
      client.client_id,
      client.ip,
      client.connection_status,
      formatBytes(client.usage_bytes),
      throughputText(client),
      shareText(allocatedBps(client.client_id)),
      enforced == null ? "not enforced" : formatBps(enforced),
      policyText(client),
    ];
    cells.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  fillClientSelect();
}

function fillClientSelect() {
  const select = document.querySelector("#client-select");
  const current = select.value;
  select.replaceChildren();
  state.clients.forEach((client) => {
    const option = document.createElement("option");
    option.value = client.client_id;
    option.textContent = `${client.client_id} (${client.ip})`;
    select.appendChild(option);
  });
  if (current && [...select.options].some((option) => option.value === current)) {
    select.value = current;
  }
  if (!formDirty) loadForm();
}

function loadForm() {
  const select = document.querySelector("#client-select");
  const client = state.clients.find((item) => item.client_id === select.value);
  if (!client) return;
  document.querySelector("#weight").value = client.policy.weight;
  document.querySelector("#min-mbps").value = client.policy.min_bandwidth == null
    ? ""
    : client.policy.min_bandwidth / MBPS;
  document.querySelector("#max-mbps").value = client.policy.max_bandwidth == null
    ? ""
    : client.policy.max_bandwidth / MBPS;
}

function renderChart() {
  const chart = document.querySelector("#chart");
  chart.replaceChildren();
  const capacity = state.policy ? state.policy.total_capacity : 0;
  if (!state.clients.length) {
    chart.textContent = "No clients configured.";
    return;
  }
  state.clients.forEach((client) => {
    const observed = loadBps(metricById(client.client_id));
    const allocated = allocatedBps(client.client_id);
    const enforced = enforcedBps(client);
    const row = document.createElement("div");
    row.className = "chart-row";
    const name = document.createElement("div");
    name.textContent = client.client_id;
    const bars = document.createElement("div");
    bars.className = "bars";
    bars.append(
      bar("Observed", "observed", observed, capacity, observed == null ? "—" : formatBps(observed)),
      bar("Allocated", "allocated", allocated.known ? allocated.bps : null, capacity, shareText(allocated)),
      bar("Enforced", "enforced", enforced, capacity, enforced == null ? "not enforced" : formatBps(enforced)),
    );
    row.append(name, bars);
    chart.appendChild(row);
  });
}

function bar(label, kind, bps, capacity, valueText) {
  const wrap = document.createElement("div");
  wrap.className = "bar";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = label;
  const track = document.createElement("div");
  track.className = "track";
  const fill = document.createElement("div");
  fill.className = `fill ${kind}`;
  const width = bps == null || !capacity ? 0 : Math.min(100, (bps / capacity) * 100);
  fill.style.width = `${width}%`;
  track.appendChild(fill);
  const val = document.createElement("span");
  val.className = "val";
  val.textContent = valueText;
  wrap.append(tag, track, val);
  return wrap;
}

function renderEvents() {
  const list = document.querySelector("#events");
  list.replaceChildren();
  if (!state.events.length) {
    const item = document.createElement("li");
    item.textContent = "No control events yet.";
    list.appendChild(item);
    return;
  }
  state.events.forEach((event) => {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = event.timestamp;
    time.textContent = event.timestamp;
    const details = event.details || {};
    const reason = details.reason && details.reason !== event.message ? ` · ${details.reason}` : "";
    const enforced = typeof details.enforced === "boolean"
      ? (details.enforced === true ? " · enforced" : " · not enforced")
      : "";
    item.append(time, document.createTextNode(`${event.message}${reason}${enforced}`));
    list.appendChild(item);
  });
}

function render() {
  renderSummary();
  renderClients();
  renderChart();
  renderEvents();
}

async function refresh() {
  try {
    const [clients, metrics, policy, allocations, events] = await Promise.all([
      request("/clients"),
      request("/metrics"),
      request("/policies"),
      request("/allocations"),
      request("/events?limit=12"),
    ]);
    state.clients = clients.clients;
    state.metrics = metrics;
    state.policy = policy;
    state.allocations = allocations;
    state.events = events.events;
    showBanner("");
    render();
  } catch (error) {
    showBanner(error.message || "The API could not be read.");
  }
}

function mbpsOrNull(input) {
  if (input.value.trim() === "") return null;
  const value = Number(input.value);
  if (!Number.isFinite(value) || value < 0) {
    throw new Error("Bandwidth values must be zero or a positive number of Mbps.");
  }
  return Math.round(value * MBPS);
}

async function savePolicy(event) {
  event.preventDefault();
  const clientId = document.querySelector("#client-select").value;
  const weight = Number(document.querySelector("#weight").value);
  if (!Number.isInteger(weight) || weight < 1) {
    showResult("Weight must be a whole number of at least 1.", false);
    return;
  }
  let minimum;
  let maximum;
  try {
    minimum = mbpsOrNull(document.querySelector("#min-mbps"));
    maximum = mbpsOrNull(document.querySelector("#max-mbps"));
  } catch (error) {
    showResult(error.message, false);
    return;
  }
  if (minimum != null && maximum != null && minimum > maximum) {
    showResult("Minimum bandwidth is above the maximum.", false);
    return;
  }
  if (maximum === 0) {
    showResult("A maximum of 0 Mbps is not a usable limit. Leave it blank for no client cap.", false);
    return;
  }
  await runAction(() => request(`/policies/${encodeURIComponent(clientId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      weight,
      min_bandwidth: minimum,
      max_bandwidth: maximum,
    }),
  }), "Policy saved. tc was not changed.");
}

async function runAction(call, successText) {
  const buttons = document.querySelectorAll("button");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    const body = await call();
    formDirty = false;
    showResult(successText || describeAction(body, null), true);
    await refresh();
  } catch (error) {
    showResult(describeAction(null, error.payload || { message: error.message }), false);
  } finally {
    buttons.forEach((button) => {
      button.disabled = false;
    });
  }
}

document.querySelector("#policy-form").addEventListener("submit", savePolicy);
document.querySelector("#client-select").addEventListener("change", () => {
  formDirty = false;
  loadForm();
});
["#weight", "#min-mbps", "#max-mbps"].forEach((selector) => {
  document.querySelector(selector).addEventListener("input", () => {
    formDirty = true;
  });
});
document.querySelector("#apply-btn").addEventListener("click", () => {
  runAction(() => request("/qos/apply", { method: "POST" }));
});
document.querySelector("#clear-btn").addEventListener("click", () => {
  runAction(() => request("/qos/clear", { method: "POST" }));
});

refresh();
setInterval(refresh, POLL_MS);
