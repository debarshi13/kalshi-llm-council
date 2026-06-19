"use strict";

const $ = (sel) => document.querySelector(sel);
const lanes = {
  architect: $("#lane-architect"),
  implementer: $("#lane-implementer"),
  reviewer: $("#lane-reviewer"),
};
const hud = {
  round: $("#hud-round"),
  tokens: $("#hud-tokens"),
  cost: $("#hud-cost"),
  status: $("#hud-status"),
};

let es = null;          // active EventSource
let currentRun = null;
let totalTokens = 0;

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

function addMsg(role, text, cls = "", ts = Date.now() / 1000) {
  const lane = lanes[role];
  if (!lane) return;
  const el = document.createElement("div");
  el.className = `msg ${cls}`.trim();
  el.innerHTML = "";
  el.append(document.createTextNode(text));
  const t = document.createElement("time");
  t.textContent = fmtTime(ts);
  el.append(t);
  lane.append(el);
  lane.scrollTop = lane.scrollHeight;
}

function setStatus(s) {
  hud.status.textContent = s;
  hud.status.dataset.s = s;
}

function resetView() {
  Object.values(lanes).forEach((l) => (l.innerHTML = ""));
  hud.round.textContent = "–";
  hud.tokens.textContent = "0";
  hud.cost.textContent = "0.0000";
  totalTokens = 0;
}

function handleEvent(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "run_started":
      setStatus("running");
      break;
    case "round_started":
      hud.round.textContent = d.round;
      break;
    case "message":
      if (ev.role) addMsg(ev.role, d.text || "", "", ev.ts);
      break;
    case "tool_call":
      if (ev.role) addMsg(ev.role, `▸ ${d.tool}${d.path ? " " + d.path : ""}${d.command ? " · " + d.command : ""}`, "tool", ev.ts);
      break;
    case "test_result": {
      const pass = d.ok;
      const lane = lanes[ev.role || "reviewer"];
      const el = document.createElement("div");
      el.className = `msg test ${pass ? "pass" : "fail"}`;
      const v = document.createElement("span");
      v.className = "verdict";
      v.textContent = pass ? "✓ tests pass" : "✗ tests fail";
      el.append(v, document.createTextNode(`  (exit ${d.returncode})`));
      if (d.stdout) el.append(document.createTextNode("\n" + d.stdout.trim()));
      if (!pass && d.stderr) el.append(document.createTextNode("\n" + d.stderr.trim()));
      lane.append(el);
      lane.scrollTop = lane.scrollHeight;
      break;
    }
    case "usage":
      totalTokens += d.total_tokens || 0;
      hud.tokens.textContent = totalTokens;
      if (typeof d.cost_usd === "number") {
        const cur = parseFloat(hud.cost.textContent) || 0;
        hud.cost.textContent = (cur + d.cost_usd).toFixed(4);
      }
      break;
    case "approval_requested":
      showApproval(d);
      break;
    case "approval_resolved":
      hideApproval();
      break;
    case "run_finished":
      setStatus(d.status);
      hud.cost.textContent = (d.usage?.cost_usd ?? 0).toFixed(4);
      hud.tokens.textContent = d.usage?.total_tokens ?? totalTokens;
      refreshHistory();
      break;
    case "error":
      addMsg("reviewer", `⚠ ${d.error}`, "tool");
      break;
  }
}

// ── Approval modal ──────────────────────────────────────
const modal = $("#approval");
function showApproval(d) {
  $("#approval-detail").textContent = `${d.action} — ${d.detail}`;
  modal.dataset.approvalId = d.approval_id;
  modal.classList.remove("hidden");
}
function hideApproval() { modal.classList.add("hidden"); }

async function resolveApproval(granted) {
  const approvalId = modal.dataset.approvalId;
  hideApproval();
  await fetch(`/api/runs/${currentRun}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, granted }),
  });
}
$("#approve-yes").onclick = () => resolveApproval(true);
$("#approve-no").onclick = () => resolveApproval(false);

// ── Streaming ───────────────────────────────────────────
function connect(runId) {
  if (es) es.close();
  currentRun = runId;
  es = new EventSource(`/api/runs/${runId}/events`);
  es.onmessage = (m) => {
    try { handleEvent(JSON.parse(m.data)); } catch (_) {}
  };
  es.onerror = () => { es.close(); };
}

// ── Start a run ─────────────────────────────────────────
$("#run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const task = $("#task").value.trim();
  if (!task) return;
  const engine = $("#engine").value || null;
  resetView();
  setStatus("starting");
  const r = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, engine }),
  });
  const { run_id } = await r.json();
  connect(run_id);
});

// ── History ─────────────────────────────────────────────
async function refreshHistory() {
  const r = await fetch("/api/runs");
  const { runs } = await r.json();
  const list = $("#run-list");
  list.innerHTML = "";
  for (const run of runs) {
    const li = document.createElement("li");
    const task = document.createElement("span");
    task.className = "task";
    task.textContent = run.task;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${run.status || "?"} · ${run.total_tokens ?? 0} tok`;
    li.append(task, meta);
    li.onclick = () => { resetView(); connect(run.run_id); };
    list.append(li);
  }
}

refreshHistory();
