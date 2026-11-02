// GharMitra web chat: chat on the left, live trace (SSE) on the right.

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const state = { sessionId: null, source: null, filter: "all", user: "u-demo", busy: false };
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
  approvals: (t) => ["approval", "gate", "operation", "exit"].includes(t),
  model: (t) => ["model_call", "intent"].includes(t),
};

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// --- chat ------------------------------------------------------------------------------------

function addMessage(role, text) {
  const n = el("div", `msg ${role}`, text);
  $("#messages").append(n);
  n.scrollIntoView({ block: "end" });
}

function closeCards() {
  document.querySelectorAll(".card:not(.closed)").forEach((c) => {
    c.classList.add("closed");
    c.querySelectorAll("button").forEach((b) => (b.disabled = true));
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
    case "approval": return `${p.status} ${p.approval_id} ${p.action} ${p.provider_id} ₹${(p.amount / 100).toLocaleString("en-IN")}${p.reason ? " (" + p.reason + ")" : ""}`;
    case "gate": return `${p.allowed ? "pass" : "refuse"} ${p.approval_id} ${p.action}${p.reason ? " " + p.reason : ""}`;
    case "operation": return `${p.status} ${p.op_key}${p.booking_id ? " " + p.booking_id : ""}${p.error ? " " + p.error : ""}`;
    case "injection_flagged": return `⚠ ${p.field} from ${p.tool} ${p.ref}`;
    case "exit": return p.reason;
    case "budget": return `model ${p.model_calls} · tools ${p.tool_calls} · ${p.elapsed_s}s`;
    case "memory": return `${p.op} ${p.key} → ${p.status}`;
    case "followup": return `${p.event} ${p.booking_id}`;
    case "followup_ignored": return `${p.booking_id} still ${p.provider_status} at the provider`;
    case "refund": return `${p.booking_id} ₹${(p.amount / 100).toLocaleString("en-IN")} + credit ₹${(p.credit / 100).toLocaleString("en-IN")} (${p.reason})`;
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

// --- setup -----------------------------------------------------------------------------------------------

async function newChat() {
  const { session_id } = await api("/api/sessions", { user_id: state.user });
  state.sessionId = session_id;
  $("#messages").replaceChildren();
  addMessage("notice", `New chat as ${$("#user").selectedOptions[0].textContent}. Session ${session_id}.`);
  watch(session_id);
  localStorage.setItem("gm-session", JSON.stringify({ id: session_id, user: state.user }));
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
    localStorage.setItem("gm-session", JSON.stringify({ id: r.session_id, user: state.user }));
    r.messages.forEach((m) => addMessage("agent", m));
  } catch (e) {
    addMessage("notice", `Request failed: ${e.message}`);
  }
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

async function init() {
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
  $("#user").onchange = () => { state.user = $("#user").value; newChat(); };
  $("#reset").onclick = async () => {
    const scenario = $("#scenario").value;
    if (!confirm(`Reset the demo world with scenario "${scenario}"? Bookings, memory and chats are wiped.`)) return;
    await api("/api/reset", { scenario });
    await newChat();
    addMessage("notice", `World reset with scenario "${scenario}".`);
  };
  document.querySelectorAll(".filters button").forEach((b) => (b.onclick = () => {
    state.filter = b.dataset.filter;
    document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll("#events li").forEach((li) => (li.hidden = !FILTERS[state.filter](li.dataset.type)));
  }));
  document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === b.dataset.tab));
  }));

  const saved = JSON.parse(localStorage.getItem("gm-session") || "null");
  if (!(saved && (await resume(saved)))) await newChat();
}

init();
