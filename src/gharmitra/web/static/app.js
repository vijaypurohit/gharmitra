// GharMitra web app: Chat (with live trace over SSE), Bookings, Memory and Settings. No build step.

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};
const rupees = (paise) => `₹${(paise / 100).toLocaleString("en-IN")}`;

const state = { sessionId: null, source: null, filter: "all", user: "u-demo", busy: false, page: "chat" };
const HINTS = [
  "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500",
  "Move my AC servicing to Saturday",
  "Book the same electrician as last time, but not before 10 a.m.",
  "Cancel my cleaning booking",
  "What do you remember about me?",
];
const FILTERS = {
  all: () => true,
  tools: (t) => ["tool_call", "injection_flagged"].includes(t),
  approvals: (t) => ["approval", "gate", "operation", "receipt", "refund", "exit"].includes(t),
  model: (t) => ["model_call", "intent"].includes(t),
};

async function api(path, body, method) {
  const opts = body === undefined && !method ? {} : {
    method: method || "POST", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = await res.text();
    try { detail = JSON.parse(detail).detail || detail; } catch { /* plain text */ }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

function showDialog(text) {
  $("#dialog-body").textContent = text;
  $("#dialog").showModal();
}

// --- pages -----------------------------------------------------------------------------------------

const PAGES = { chat: () => {}, bookings: loadBookings, memory: loadMemory, settings: loadSettings };

function route() {
  const page = (location.hash || "#chat").slice(1);
  state.page = PAGES[page] ? page : "chat";
  $$(".page").forEach((p) => (p.hidden = p.id !== `page-${state.page}`));
  $$(".pages a").forEach((a) => a.classList.toggle("active", a.dataset.page === state.page));
  PAGES[state.page]().catch((e) => showDialog(`Couldn't load ${state.page}: ${e.message}`));
}

// --- chat ----------------------------------------------------------------------------------------

function addMessage(role, text) {
  const n = el("div", `msg ${role}`, text);
  $("#messages").append(n);
  n.scrollIntoView({ block: "end" });
}

function closeCards() {
  $$(".card:not(.closed)").forEach((c) => {
    c.classList.add("closed");
    $$("button", c).forEach((b) => (b.disabled = true));
  });
}

function addCard(a) {
  const card = el("div", "card");
  card.append(el("h3", "", `Approve ${a.action}`), el("p", "", a.summary));
  const expires = new Date(a.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  card.append(el("div", "meta mono", `${a.approval_id} · expires ${expires}`));
  const actions = el("div", "actions");
  const yes = el("button", "", "Confirm");
  const no = el("button", "ghost", "Decline");
  yes.onclick = () => send("Confirm", a.approval_id);
  no.onclick = () => send("Decline", a.approval_id);
  actions.append(yes, no);
  card.append(actions);
  $("#messages").append(card);
  card.scrollIntoView({ block: "end" });
}

async function send(text, approvalId = null) {
  if (!text.trim() || state.busy || !state.sessionId) return;
  state.busy = true;
  $("#composer button").disabled = true;
  addMessage("user", text);
  closeCards();
  try {
    const r = await api("/api/chat", { session_id: state.sessionId, text, approval_id: approvalId });
    r.messages.forEach((m) => addMessage("agent", m));
    if (r.approval) addCard(r.approval);
  } catch (e) {
    addMessage("notice", `Request failed: ${e.message}`);
  } finally {
    state.busy = false;
    $("#composer button").disabled = false;
    $("#text").focus();
  }
}

// Bookings page actions start a chat flow, so the change still needs an approval.
async function startChat(text) {
  location.hash = "#chat";
  await newChat();
  await send(text);
}

// --- trace -----------------------------------------------------------------------------------------

function summarise(e) {
  const p = e.payload;
  switch (e.type) {
    case "user_msg": case "agent_msg": return p.text.split("\n")[0];
    case "intent": return `${p.act}${p.goal ? "/" + p.goal : ""} ${JSON.stringify(p.entities)}${p.fallback ? " (fallback)" : ""}`;
    case "model_call": return `${p.adapter} ${p.latency_ms}ms${p.error ? " error=" + p.error : ""}${p.fallback ? " fallback" : ""}`;
    case "state_transition": return `${p.src} → ${p.dst} (${p.reason})`;
    case "tool_call": {
      const args = Object.entries(p.args).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("..") : v}`).join(", ");
      const out = p.error ? `✗ ${p.error.code}` : summariseResult(p.name, p.result);
      return `${p.name}(${args}) → ${out}  ${Math.round(p.latency_ms)}ms`;
    }
    case "approval": return `${p.status} ${p.approval_id} ${p.action} ${p.provider_id} ${rupees(p.amount)}${p.reason ? " (" + p.reason + ")" : ""}`;
    case "gate": return `${p.allowed ? "pass" : "refuse"} ${p.approval_id} ${p.action}${p.reason ? " " + p.reason : ""}`;
    case "operation": return `${p.status} ${p.op_key}${p.booking_id ? " " + p.booking_id : ""}${p.error ? " " + p.error : ""}`;
    case "receipt": return `${p.receipt_no} ${p.op_key}`;
    case "injection_flagged": return `⚠ ${p.field} from ${p.tool} ${p.ref}`;
    case "exit": return p.reason;
    case "budget": return `model ${p.model_calls} · tools ${p.tool_calls} · ${p.elapsed_s}s`;
    case "memory": return `${p.op} ${p.key} → ${p.status}`;
    case "followup": return `${p.event} ${p.booking_id}`;
    case "followup_ignored": return `${p.booking_id} still ${p.provider_status} at the provider`;
    case "refund": return `${p.booking_id} ${rupees(p.amount)} + credit ${rupees(p.credit)} (${p.reason})`;
    default: return JSON.stringify(p);
  }
}

function summariseResult(name, r) {
  if (r === null || r === undefined) return "ok";
  if (name === "search_providers") return `${r.length} providers`;
  if (name === "get_provider") return `${r.name}, ${r.reviews.length} reviews`;
  if (name === "list_bookings") return `${r.length} bookings`;
  if (name === "hold_slot") return `${r.hold_id} ₹${r.quoted_price}`;
  if (name === "get_booking_status") return r.status + (r.booking_id ? " " + r.booking_id : "");
  return r.booking_id || r.status || "ok";
}

function addEvent(e) {
  const li = el("li", `k-${e.type}`);
  if (e.type === "exit" && e.payload.reason !== "completed") li.classList.add("bad");
  li.dataset.type = e.type;
  li.hidden = !FILTERS[state.filter](e.type);
  const ts = new Date(e.ts).toLocaleTimeString([], { hour12: false });
  const details = el("details");
  const summary = el("summary", e.type === "tool_call" && e.payload.error ? "err" : "", summarise(e));
  details.append(summary, el("pre", "", JSON.stringify(e.payload, null, 2)));
  const body = el("span", "body");
  body.append(details);
  li.append(el("span", "seq", String(e.seq)), el("span", "ts", ts), el("span", "type", e.type), body);
  const list = $("#events");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.append(li);
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function watch(sessionId) {
  if (state.source) state.source.close();
  $("#events").replaceChildren();
  $("#session").textContent = sessionId;
  const src = new EventSource(`/api/sessions/${sessionId}/events`);
  src.onmessage = (m) => addEvent(JSON.parse(m.data));
  state.source = src;
}

function remember(sessionId) {
  localStorage.setItem("gm-session", JSON.stringify({ id: sessionId, user: state.user }));
}

async function newChat() {
  const { session_id } = await api("/api/sessions", { user_id: state.user });
  state.sessionId = session_id;
  $("#messages").replaceChildren();
  addMessage("notice", `New chat as ${$("#user").selectedOptions[0].textContent}. Session ${session_id}.`);
  watch(session_id);
  remember(session_id);
}

async function resume(saved) {
  try {
    const msgs = await api(`/api/sessions/${saved.id}/messages`);
    state.sessionId = saved.id;
    state.user = saved.user;
    $("#user").value = saved.user;
    msgs.forEach((m) => addMessage(m.role, m.text));
    watch(saved.id);
    return true;
  } catch {
    return false;
  }
}

async function providerCancels() {
  const now = new Date();
  const upcoming = (await api(`/api/bookings?user_id=${state.user}`))
    .filter((b) => b.status === "booked" && new Date(b.slot_start) > now);
  if (!upcoming.length) {
    addMessage("notice", "This user has no upcoming bookings to cancel.");
    return;
  }
  const list = upcoming.map((b) => `${b.id}  ${b.provider_name}, ${new Date(b.slot_start).toLocaleString()}`).join("\n");
  const id = prompt(`Which booking does the provider cancel?\n\n${list}`, upcoming[0].id);
  if (!id) return;
  try {
    const r = await api("/api/demo/provider-cancels", { booking_id: id.trim() });
    state.sessionId = r.session_id;
    $("#messages").replaceChildren();
    addMessage("notice", `Provider cancelled ${id.trim()}. Follow-up session ${r.session_id}.`);
    watch(r.session_id);
    remember(r.session_id);
    r.messages.forEach((m) => addMessage("agent", m));
  } catch (e) {
    addMessage("notice", `Request failed: ${e.message}`);
  }
}

// --- bookings --------------------------------------------------------------------------------------

async function loadBookings() {
  const rows = await api(`/api/bookings?user_id=${state.user}`);
  const tbody = $("#bookings-table tbody");
  tbody.replaceChildren();
  $("#bookings-empty").hidden = rows.length > 0;
  $("#bookings-table").hidden = rows.length === 0;
  const now = new Date();
  for (const b of rows) {
    const tr = el("tr");
    const slot = new Date(b.slot_start).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    const status = el("span", `badge ${b.status}`, b.status.replace("_", " "));
    const receipts = el("td");
    b.receipts.forEach((no, i) => {
      const a = el("a", "mono", no);
      a.href = `/api/receipts/${no}`;
      a.target = "_blank";
      a.rel = "noopener";
      if (i) receipts.append(" ");
      receipts.append(a);
    });
    const actions = el("td");
    if (b.status === "booked" && new Date(b.slot_start) > now) {
      const wrap = el("div", "row-actions");
      const move = el("button", "ghost", "Reschedule");
      move.onclick = () => startChat(`Reschedule booking ${b.id}`);
      const cancel = el("button", "ghost danger", "Cancel");
      cancel.onclick = () => startChat(`Cancel booking ${b.id}`);
      wrap.append(move, cancel);
      actions.append(wrap);
    }
    const statusCell = el("td");
    statusCell.append(status);
    tr.append(el("td", "mono", b.id), el("td", "", b.service_label), el("td", "", b.provider_name), el("td", "", slot),
      el("td", "num", b.price), statusCell, el("td", "num", b.fee ?? "—"),
      el("td", "num", b.refund ? b.refund + (b.credit ? ` + ${b.credit} credit` : "") : "—"), receipts, actions);
    tbody.append(tr);
  }
}

// --- memory ------------------------------------------------------------------------------------------

async function loadMemory() {
  const facts = await api(`/api/memory?user_id=${state.user}`);
  const list = $("#facts");
  list.replaceChildren();
  $("#facts-empty").hidden = facts.length > 0;
  for (const f of facts) {
    const li = el("li");
    const value = el("span", "value", f.display);
    const badge = el("span", `badge ${f.status}`, f.status);
    const correct = el("button", "ghost", "Correct");
    const forget = el("button", "ghost danger", "Forget");
    correct.onclick = () => {
      const form = el("form");
      const input = el("input");
      input.value = f.display;
      input.setAttribute("aria-label", `New ${f.label}`);
      form.append(input, el("button", "", "Save"));
      form.onsubmit = async (ev) => {
        ev.preventDefault();
        try {
          await api(`/api/memory/${encodeURIComponent(f.key)}?user_id=${state.user}`, { value: input.value }, "PUT");
          loadMemory();
        } catch (e) { showDialog(e.message); }
      };
      value.replaceWith(form);
      input.focus();
    };
    forget.onclick = async () => {
      if (!confirm(`Forget your ${f.label}?`)) return;
      await api(`/api/memory/${encodeURIComponent(f.key)}?user_id=${state.user}`, undefined, "DELETE");
      loadMemory();
    };
    li.append(el("span", "label", f.label), value, badge,
      el("span", "when muted", new Date(f.updated_at).toLocaleDateString()), correct, forget);
    list.append(li);
  }
}

// --- settings ----------------------------------------------------------------------------------------

const FIELD_LABELS = {
  model_adapter: "Adapter", model_base_url: "Base URL", model_name: "Model name",
  model_timeout_s: "Timeout (s)", model_min_interval_s: "Min gap between calls (s)",
  trace_retention_days: "Keep traces (days, 0 = forever)",
  "limits.model_calls_per_turn": "Model calls per message", "limits.tool_calls_per_turn": "Tool calls per message",
  "limits.seconds_per_turn": "Seconds per message", "limits.turns_per_session": "Messages per session",
  "limits.retries_per_operation": "Retries per operation", "limits.min_confidence": "Min model confidence",
};
const POLICY_FIELDS = {
  free_hours: ["Free if at least (h before)", 1], late_hours: ["Flat fee if at least (h before)", 1],
  late_fee: ["Flat fee (₹)", 100], very_late_percent: ["Fee when later (%)", 1],
  provider_cancel_credit: ["Credit when provider cancels (₹)", 100], reschedule_free_hours: ["Reschedule allowed until (h before)", 1],
};
let loaded = null;

function field(key, value, source, adapters) {
  const wrap = el("label", "field");
  const name = el("span", "name");
  name.append(el("span", "", FIELD_LABELS[key] || key));
  if (source) name.append(el("span", `src ${source}`, source));
  let input;
  if (key === "model_adapter") {
    input = el("select");
    for (const a of adapters) input.append(new Option(a, a));
    if (!adapters.includes(value)) input.append(new Option(value, value));
    input.value = value;
  } else {
    input = el("input");
    input.value = value ?? "";
    if (typeof value === "number") input.inputMode = "decimal";
  }
  input.name = key;
  wrap.append(name, input);
  return wrap;
}

async function loadSettings() {
  const [s, policy] = await Promise.all([api("/api/settings"), api("/api/policy")]);
  loaded = s;
  const groups = { "model-fields": [], "limit-fields": [], "trace-fields": [] };
  for (const [key, value] of Object.entries(s.values)) {
    const group = key.startsWith("limits.") ? "limit-fields" : key.startsWith("trace") ? "trace-fields" : "model-fields";
    groups[group].push(field(key, value, s.sources[key], s.adapters));
  }
  for (const [id, nodes] of Object.entries(groups)) $(`#${id}`).replaceChildren(...nodes);
  $("#secrets").replaceChildren(...Object.entries(s.secrets).map(([k, v]) => el("span", "pill", `${k}: ${v}`)));

  $("#policy-fields").replaceChildren(...Object.entries(POLICY_FIELDS).map(([key, [labelText, scale]]) => {
    const wrap = el("label", "field");
    wrap.append(el("span", "name", labelText));
    const input = el("input");
    input.name = key;
    input.dataset.scale = scale;
    input.value = policy.platform[key] / scale;
    wrap.append(input);
    return wrap;
  }));
  const overrides = Object.entries(policy.overrides);
  $("#overrides").textContent = overrides.length
    ? `Provider overrides: ${overrides.map(([p, o]) => `${p} ${JSON.stringify(o)}`).join("; ")}`
    : "No provider overrides (set them with PUT /api/policy).";

  const dl = $("#locale");
  dl.replaceChildren();
  for (const [k, v] of Object.entries({ ...s.locale, scenario: s.scenario, seed: s.seed })) dl.append(el("dt", "", k), el("dd", "", String(v)));
}

async function saveSettings(ev) {
  ev.preventDefault();
  const changes = {};
  for (const input of $$("#settings-form [name]")) {
    const before = loaded.values[input.name];
    const raw = input.value.trim();
    if (String(before ?? "") === raw) continue;
    changes[input.name] = raw === "" ? null : raw;
  }
  const status = $("#settings-status");
  if (!Object.keys(changes).length) { status.textContent = "Nothing changed."; return; }
  try {
    await api("/api/settings", { changes }, "PUT");
    status.textContent = `Saved ${Object.keys(changes).length} change(s); applied to new messages.`;
    await loadSettings();
    const info = await api("/api/info");
    $("#model").textContent = `model: ${info.model}`;
  } catch (e) { status.textContent = e.message; }
}

async function savePolicy(ev) {
  ev.preventDefault();
  const platform = {};
  for (const input of $$("#policy-form [name]")) platform[input.name] = Number(input.value) * Number(input.dataset.scale);
  try {
    await api("/api/policy", { platform }, "PUT");
    $("#policy-status").textContent = "Saved. New bookings use this policy.";
  } catch (e) { $("#policy-status").textContent = e.message; }
}

async function testModel() {
  const out = $("#test-result");
  out.hidden = false;
  out.textContent = "Calling the model…";
  try {
    out.textContent = JSON.stringify(await api("/api/settings/test-model", { text: $("#test-text").value }), null, 2);
  } catch (e) { out.textContent = e.message; }
}

// --- setup -----------------------------------------------------------------------------------------------

const THEMES = ["auto", "light", "dark"];
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("gm-theme", theme);
  $("#theme").title = `Theme: ${theme}`;
  $("#theme").textContent = { auto: "◐", light: "☀", dark: "☾" }[theme];
}

async function init() {
  setTheme(document.documentElement.dataset.theme || "auto");
  $("#theme").onclick = () => setTheme(THEMES[(THEMES.indexOf(document.documentElement.dataset.theme) + 1) % 3]);
  const info = await api("/api/info");
  $("#model").textContent = `model: ${info.model}`;
  for (const u of info.users) $("#user").append(new Option(u.name, u.id));
  for (const s of info.scenarios) {
    const o = new Option(s.name, s.name);
    o.title = s.note;
    $("#scenario").append(o);
  }
  $("#scenario").value = info.scenario;
  HINTS.forEach((h) => {
    const b = el("button", "", h);
    b.type = "button";
    b.onclick = () => send(h);
    $("#hints").append(b);
  });

  $("#composer").onsubmit = (ev) => {
    ev.preventDefault();
    const text = $("#text").value;
    $("#text").value = "";
    send(text);
  };
  $("#new").onclick = newChat;
  $("#cancel-demo").onclick = providerCancels;
  $("#report").onclick = async () => {
    if (!state.sessionId) return;
    try { showDialog((await api(`/api/sessions/${state.sessionId}/report`)).text); } catch (e) { showDialog(e.message); }
  };
  $("#user").onchange = () => { state.user = $("#user").value; newChat(); if (state.page !== "chat") route(); };
  $("#reset").onclick = async () => {
    const scenario = $("#scenario").value;
    if (!confirm(`Reset the demo world with scenario "${scenario}"? Bookings, memory and chats are wiped; settings stay.`)) return;
    await api("/api/reset", { scenario });
    await newChat();
    addMessage("notice", `World reset with scenario "${scenario}".`);
  };
  $$(".filters button").forEach((b) => (b.onclick = () => {
    state.filter = b.dataset.filter;
    $$(".filters button").forEach((x) => x.classList.toggle("active", x === b));
    $$("#events li").forEach((li) => (li.hidden = !FILTERS[state.filter](li.dataset.type)));
  }));
  $$(".tabs button").forEach((b) => (b.onclick = () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === b.dataset.tab));
  }));
  $("#settings-form").onsubmit = saveSettings;
  $("#policy-form").onsubmit = savePolicy;
  $("#test-model").onclick = testModel;

  window.addEventListener("hashchange", route);
  const saved = JSON.parse(localStorage.getItem("gm-session") || "null");
  if (!(saved && (await resume(saved)))) await newChat();
  route();
}

init();
