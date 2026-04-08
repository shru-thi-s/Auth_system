const samplePayload = {
  device_id: "esp32-01",
  session_id: "dashboard-demo",
  features: {
    mean_interval: 148.3,
    std_interval: 41.8,
    wpm: 43.9,
    pause_ratio: 0.13,
  },
};

const $ = (id) => document.getElementById(id);

function pct(num, den) {
  if (!den) return 0;
  return Math.max(4, Math.min(100, (num / den) * 100));
}

function statusClass(status) {
  return status === "anomaly" ? "bad" : "good";
}

function renderSessions(items) {
  const root = $("sessionsList");
  root.innerHTML = "";

  if (!items.length) {
    root.innerHTML = '<div class="session-item"><div><div class="value">No sessions yet</div><div class="label">Trigger a prediction to populate the log.</div></div></div>';
    return;
  }

  for (const item of items) {
    const last = item.events[item.events.length - 1];
    const anomalies = item.events.filter((e) => e.status === "anomaly").length;
    const normals = item.events.length - anomalies;
    const el = document.createElement("div");
    el.className = "session-item";
    el.innerHTML = `
      <div>
        <div class="label">Session</div>
        <div class="value">${item.device_id} · ${item.session_id}</div>
        <div style="margin-top:10px"><span class="badge ${statusClass(last?.status ?? "normal")}">${last?.status ?? "normal"}</span></div>
      </div>
      <div>
        <div class="label">Events</div>
        <div class="value">${item.events.length}</div>
        <div class="label">Normals: ${normals}</div>
      </div>
      <div>
        <div class="label">Anomalies</div>
        <div class="value">${anomalies}</div>
        <div class="label">Score: ${(last?.score ?? 0).toFixed(4)}</div>
      </div>
      <div>
        <div class="label">Last Seen</div>
        <div class="value">${(last?.timestamp ?? "—").replace("T", " ").replace("Z", "")}</div>
        <div class="label">WPM: ${(last?.wpm ?? 0).toFixed(1)}</div>
      </div>
    `;
    root.appendChild(el);
  }
}

async function refresh() {
  try {
    const [healthRes, overviewRes, sessionsRes] = await Promise.all([
      fetch("/health"),
      fetch("/overview"),
      fetch("/sessions"),
    ]);

    const health = await healthRes.json();
    const overview = await overviewRes.json();
    const sessions = await sessionsRes.json();

    $("apiStatus").textContent = health.ok
      ? `Online · ${health.mode}`
      : "Offline";

    $("totalEvents").textContent = overview.total_events;
    $("normalEvents").textContent = overview.normal_events;
    $("anomalyEvents").textContent = overview.anomaly_events;
    $("sessionsTracked").textContent = overview.sessions_tracked;
    $("latestTimestamp").textContent = overview.latest_timestamp
      ? `Latest update: ${overview.latest_timestamp.replace("T", " ").replace("Z", "")}`
      : "No events yet";

    const total = overview.total_events || 1;
    $("normalBar").style.width = `${pct(overview.normal_events, total)}%`;
    $("anomalyBar").style.width = `${pct(overview.anomaly_events, total)}%`;

    renderSessions(sessions);
  } catch (error) {
    $("apiStatus").textContent = "Connection issue";
    console.error(error);
  }
}

async function runSample() {
  $("sampleOutput").textContent = "Sending sample payload…";
  const response = await fetch("/predict", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(samplePayload),
  });
  const data = await response.json();
  $("sampleOutput").textContent = JSON.stringify(data, null, 2);
  await refresh();
}

document.addEventListener("DOMContentLoaded", () => {
  $("sendSample").addEventListener("click", runSample);
  refresh();
  setInterval(refresh, 5000);
});
