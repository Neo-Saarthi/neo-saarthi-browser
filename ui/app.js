const $ = selector => document.querySelector(selector);
let session = "";
let selected = "";
let latest = null;
let busy = false;

function notice(message) {
  $("#notice").textContent = message;
  $("#notice").hidden = !message;
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", "X-Companion-Session": session, ...(options.headers || {}) }, cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `Request failed (${response.status}).`);
  }
  return response.status === 204 ? null : response.json();
}

async function perform(operation) {
  try { notice(""); await operation(); } catch (error) { notice(error.message); }
}

function settingsVisible(visible) {
  $("#settings").hidden = !visible;
  $("#settings-toggle").setAttribute("aria-expanded", String(visible));
}

async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const state = await api("/api/companion/status");
    const clients = state.browser.clients;
    if (!clients.some(client => client.client_id === selected)) selected = (clients.find(client => client.preferred) || clients[0] || {}).client_id || "";
    $("#browser").replaceChildren();
    if (!clients.length) $("#browser").add(new Option("No paired browser", ""));
    for (const client of clients) $("#browser").add(new Option(`${client.label} · ${client.paused ? "paused" : client.connected ? "connected" : "offline"}`, client.client_id));
    $("#browser").value = selected;
    const client = clients.find(item => item.client_id === selected);
    const paused = Boolean(client?.paused || state.cursor.native_paused);
    $("#connection-state").textContent = paused ? "Paused" : client?.connected ? "Connected" : "Not connected";
    const tasks = state.tasks;
    latest = tasks[tasks.length - 1] || null;
    const active = latest?.status === "running";
    $("#run").disabled = active || paused || !client?.connected || !state.settings.model_key_available;
    $("#pause").disabled = !client || paused;
    $("#resume").disabled = !client || !paused;
    $("#revoke").disabled = !client;
    $("#task-state").textContent = latest ? (active && paused ? "Paused — context retained" : latest.status.replaceAll("_", " ")) : "No task yet";
    if (latest) $("#result").textContent = latest.error || (latest.result ? JSON.stringify(latest.result, null, 2) : `Working on: ${latest.goal}`);
    $("#key-state").textContent = state.settings.model_key_available ? " · configured" : " · not configured";
    for (const label of document.querySelectorAll("[data-configured]")) label.textContent = state.settings.configured[label.dataset.configured] ? " · saved" : "";
    const logs = await api("/api/companion/logs");
    $("#logs").textContent = logs.lines.join("\n") || "No activity yet.";
    $("#logs").scrollTop = $("#logs").scrollHeight;
  } catch (error) {
    notice(`Companion unavailable: ${error.message}`);
  } finally { busy = false; }
}

$("#settings-toggle").onclick = () => settingsVisible($("#settings").hidden);
$("#settings-close").onclick = () => settingsVisible(false);
$("#settings-form").onsubmit = event => {
  event.preventDefault();
  void perform(async () => {
    const values = Object.fromEntries([...new FormData(event.target)].filter(([, value]) => value !== ""));
    await api("/api/companion/settings", { method: "PUT", body: JSON.stringify({ values }) });
    event.target.reset(); notice("Settings saved on this computer."); await refresh();
  });
};
$("#clear-settings").onclick = () => void perform(async () => {
  const values = Object.fromEntries([...$("#settings-form").elements].filter(element => element.name).map(element => [element.name, ""]));
  await api("/api/companion/settings", { method: "PUT", body: JSON.stringify({ values }) });
  $("#settings-form").reset(); notice("Saved values cleared."); await refresh();
});
$("#pair").onclick = () => void perform(async () => {
  const pairing = await api("/api/v1/browser-use/pairing", { method: "POST" });
  $("#pairing-code").textContent = pairing.pairing_code;
  $("#pairing-expiry").textContent = `Expires at ${new Date(pairing.expires_at).toLocaleTimeString()}. Each code can be used once.`;
  $("#pairing").hidden = false;
});
$("#copy-code").onclick = () => void perform(async () => { await navigator.clipboard.writeText($("#pairing-code").textContent); notice("Pairing code copied."); });
$("#browser").onchange = event => void perform(async () => {
  selected = event.target.value;
  await api("/api/v1/browser-use/connections/preferred", { method: "POST", body: JSON.stringify({ client_id: selected }) });
  await refresh();
});
$("#revoke").onclick = () => void perform(async () => {
  await api("/api/v1/browser-use/connections/revoke", { method: "POST", body: JSON.stringify({ client_id: selected }) });
  selected = ""; await refresh();
});
$("#task-form").onsubmit = event => {
  event.preventDefault();
  void perform(async () => {
    $("#run").disabled = true;
    await api("/api/companion/tasks", { method: "POST", body: JSON.stringify({ goal: $("#goal").value.trim(), budget: Number($("#budget").value), browser_id: selected, credential_profile: $("#profile").value }) });
    await refresh();
  });
};
for (const [id, paused] of [["pause", true], ["resume", false]]) $("#" + id).onclick = () => void perform(async () => {
  await api("/api/companion/control", { method: "POST", body: JSON.stringify({ client_id: selected, paused }) }); await refresh();
});
void perform(async () => {
  const response = await fetch("/api/companion/session", { cache: "no-store" });
  if (!response.ok) throw new Error("Cannot open local session.");
  session = (await response.json()).session;
  await refresh();
  setInterval(() => void refresh(), 1500);
});
