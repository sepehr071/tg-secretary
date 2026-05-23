"use strict";

const state = {
  uploadId: null,
  chatId: null,
  msgs: [],
  oldestLoadedId: null,
  startId: null,
  endId: null,
};

// Map from user_msg_id -> { card: HTMLElement, columns: { [columnIdx]: HTMLElement } }
const turnEls = new Map();

const $ = (id) => document.getElementById(id);
const COL_LETTERS = ["A", "B", "C", "D", "E"];

function setEnabled(sectionId, enabled) {
  $(sectionId).classList.toggle("disabled", !enabled);
}

function fmtTs(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return d.toLocaleString();
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------

$("upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = $("db-file").files[0];
  if (!f) return;
  $("upload-status").textContent = `uploading ${f.name}…`;
  const body = new FormData();
  body.append("file", f);
  let resp;
  try {
    resp = await fetch("/upload", { method: "POST", body });
  } catch (e) {
    $("upload-status").textContent = `upload failed: ${e}`;
    return;
  }
  if (!resp.ok) {
    const text = await resp.text();
    $("upload-status").textContent = `error: ${text}`;
    return;
  }
  const data = await resp.json();
  state.uploadId = data.upload_id;
  $("upload-status").textContent = `uploaded · ${data.upload_id.slice(0, 8)}… · ${data.size_bytes} bytes`;
  setEnabled("sec-chat", true);
  await loadChats();
});

// ---------------------------------------------------------------------------
// Chats
// ---------------------------------------------------------------------------

async function loadChats() {
  const picker = $("chat-picker");
  picker.disabled = true;
  picker.innerHTML = `<option value="">loading…</option>`;
  const resp = await fetch(`/uploads/${state.uploadId}/chats`);
  if (!resp.ok) {
    picker.innerHTML = `<option value="">error</option>`;
    return;
  }
  const data = await resp.json();
  picker.innerHTML = `<option value="">— pick a chat —</option>`;
  for (const c of data.chats) {
    const opt = document.createElement("option");
    opt.value = c.chat_id;
    opt.dataset.connId = c.conn_id;
    opt.textContent = `${c.display} · [${c.relationship}] · ${c.chat_id} · ${c.msg_count} msgs`;
    picker.appendChild(opt);
  }
  picker.disabled = false;
}

$("chat-picker").addEventListener("change", async (ev) => {
  const chatId = ev.target.value;
  if (!chatId) return;
  state.chatId = parseInt(chatId, 10);
  state.msgs = [];
  state.oldestLoadedId = null;
  state.startId = null;
  state.endId = null;
  setEnabled("sec-slice", true);
  setEnabled("sec-replay", false);
  $("replay-button").disabled = true;
  $("results").innerHTML = "";
  turnEls.clear();
  await loadMessages({ initial: true });
});

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

async function loadMessages({ initial = false } = {}) {
  const url = new URL(
    `/uploads/${state.uploadId}/chats/${state.chatId}/messages`,
    window.location.origin,
  );
  url.searchParams.set("limit", "200");
  if (state.oldestLoadedId != null) {
    url.searchParams.set("before_id", state.oldestLoadedId);
  }
  const resp = await fetch(url);
  if (!resp.ok) return;
  const data = await resp.json();
  const newer = data.messages;
  if (initial) {
    state.msgs = newer;
  } else {
    state.msgs = newer.concat(state.msgs);
  }
  if (newer.length) state.oldestLoadedId = newer[0].id;
  $("load-older").disabled = !data.has_more;

  if (initial && state.msgs.length) {
    const tail = state.msgs.slice(-20);
    state.startId = tail[0].id;
    state.endId = state.msgs[state.msgs.length - 1].id;
  }
  renderMessages();
  updateSliceBanner();
}

$("load-older").addEventListener("click", () => loadMessages({ initial: false }));

function renderMessages() {
  const list = $("message-list");
  list.innerHTML = "";
  const startId = state.startId;
  const endId = state.endId;
  for (const m of state.msgs) {
    const li = document.createElement("li");
    li.className = `role-${m.role}`;
    if (startId != null && endId != null && m.id >= startId && m.id <= endId) {
      li.classList.add("in-slice");
    }
    if (m.id === startId) li.classList.add("is-start");
    if (m.id === endId) li.classList.add("is-end");

    const idEl = document.createElement("span");
    idEl.className = "msg-id";
    idEl.textContent = `#${m.id}`;

    const roleEl = document.createElement("span");
    roleEl.className = "msg-role";
    roleEl.textContent = m.role === "user" ? "them" : (m.via_bot ? "bot" : "you");

    const contentEl = document.createElement("span");
    contentEl.className = "msg-content";
    contentEl.textContent = m.content || "";

    const actions = document.createElement("span");
    actions.className = "msg-actions";
    const startBtn = document.createElement("button");
    startBtn.textContent = "start";
    startBtn.addEventListener("click", () => {
      state.startId = m.id;
      if (state.endId == null || state.endId < state.startId) state.endId = state.startId;
      renderMessages(); updateSliceBanner();
    });
    const endBtn = document.createElement("button");
    endBtn.textContent = "end";
    endBtn.addEventListener("click", () => {
      state.endId = m.id;
      if (state.startId == null || state.startId > state.endId) state.startId = state.endId;
      renderMessages(); updateSliceBanner();
    });
    actions.append(startBtn, endBtn);

    li.append(idEl, roleEl, contentEl, actions);
    list.appendChild(li);
  }
  const endLi = list.querySelector(".is-end");
  if (endLi) endLi.scrollIntoView({ block: "center", behavior: "instant" });
}

function updateSliceBanner() {
  const banner = $("slice-banner");
  if (state.startId == null || state.endId == null) {
    banner.textContent = "no slice selected";
    setEnabled("sec-replay", false);
    $("replay-button").disabled = true;
    return;
  }
  const userMsgs = state.msgs.filter(
    (m) => m.role === "user" && m.id >= state.startId && m.id <= state.endId,
  );
  banner.textContent = `slice #${state.startId} → #${state.endId} · ${userMsgs.length} contact messages will be replayed`;
  setEnabled("sec-replay", true);
  $("replay-button").disabled = userMsgs.length === 0;
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

function collectColumns() {
  const rows = document.querySelectorAll(".col-row");
  const out = [];
  rows.forEach((row) => {
    const model = row.querySelector(".model-input").value.trim();
    if (!model) return;
    let effort = row.querySelector(".reasoning-select").value;
    if (effort === "default") effort = null;
    out.push({ model, reasoning_effort: effort });
  });
  return out;
}

$("replay-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (state.startId == null || state.endId == null) return;
  const cols = collectColumns();
  if (!cols.length) {
    $("replay-status").textContent = "fill at least one model slug";
    return;
  }
  $("replay-button").disabled = true;
  $("replay-status").textContent = `running · ${cols.length} column(s)`;
  $("results").innerHTML = "";
  turnEls.clear();

  let resp;
  try {
    resp = await fetch(`/uploads/${state.uploadId}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: state.chatId,
        start_msg_id: state.startId,
        end_msg_id: state.endId,
        columns: cols,
      }),
    });
  } catch (e) {
    $("replay-status").textContent = `fetch failed: ${e}`;
    $("replay-button").disabled = false;
    return;
  }
  if (!resp.ok || !resp.body) {
    const t = await resp.text();
    $("replay-status").textContent = `error: ${t}`;
    $("replay-button").disabled = false;
    return;
  }

  const reader = resp.body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  let turnCount = 0;
  let colCount = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const ev = parseSSE(raw);
      if (!ev) continue;
      if (ev.event === "done") {
        $("replay-status").textContent = `done · ${turnCount} turns · ${colCount} replies`;
      } else if (ev.event === "fatal") {
        const data = safeJson(ev.data);
        renderFatal(data && data.fatal);
      } else if (ev.event === "turn_start") {
        const data = safeJson(ev.data);
        if (data) {
          renderTurnStart(data);
          turnCount++;
        }
      } else if (ev.event === "column_done") {
        const data = safeJson(ev.data);
        if (data) {
          renderColumnDone(data);
          colCount++;
          $("replay-status").textContent = `running · ${turnCount} turns · ${colCount} replies`;
        }
      }
    }
  }
  $("replay-button").disabled = false;
});

function parseSSE(raw) {
  const out = { event: "message", data: "" };
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) out.event = line.slice(6).trim();
    else if (line.startsWith("data:")) out.data += line.slice(5).trim();
  }
  return out;
}

function safeJson(s) {
  try { return JSON.parse(s); } catch { return null; }
}

function renderTurnStart(t) {
  const card = document.createElement("div");
  card.className = "result-card";

  const userLabel = document.createElement("div");
  userLabel.className = "label";
  userLabel.textContent = `contact #${t.user_msg_id} · ${fmtTs(t.user_msg_ts)}`;
  card.appendChild(userLabel);

  const userBody = document.createElement("div");
  userBody.className = "user";
  userBody.textContent = t.user_msg;
  card.appendChild(userBody);

  const origLabel = document.createElement("div");
  origLabel.className = "label";
  origLabel.textContent = `original bot reply${t.original_bot_msg_id ? " #" + t.original_bot_msg_id : " — none"}`;
  card.appendChild(origLabel);

  const origBody = document.createElement("div");
  origBody.className = "orig";
  origBody.textContent = t.original_bot_reply || "(no original reply in DB)";
  card.appendChild(origBody);

  const grid = document.createElement("div");
  grid.className = "cols-grid";
  grid.style.setProperty("--cols", String(t.columns.length));

  const colsMap = {};
  for (const c of t.columns) {
    const cell = document.createElement("div");
    cell.className = "col-cell";

    const head = document.createElement("div");
    head.className = "col-head";
    const tag = document.createElement("span");
    tag.className = "col-tag";
    tag.textContent = COL_LETTERS[c.idx] || String(c.idx);
    head.appendChild(tag);
    const headText = document.createElement("span");
    headText.textContent = `${c.model} · reasoning: ${c.reasoning_effort || "default"}`;
    head.appendChild(headText);
    cell.appendChild(head);

    const body = document.createElement("div");
    body.className = "col-body pending";
    body.textContent = "generating…";
    cell.appendChild(body);

    const meta = document.createElement("div");
    meta.className = "col-meta";
    cell.appendChild(meta);

    grid.appendChild(cell);
    colsMap[c.idx] = cell;
  }
  card.appendChild(grid);

  $("results").appendChild(card);
  turnEls.set(t.user_msg_id, { card, columns: colsMap });
}

function renderColumnDone(c) {
  const turn = turnEls.get(c.user_msg_id);
  if (!turn) return;
  const cell = turn.columns[c.column_idx];
  if (!cell) return;
  const body = cell.querySelector(".col-body");
  const meta = cell.querySelector(".col-meta");
  body.classList.remove("pending");
  if (c.error) {
    cell.classList.add("err");
    body.textContent = c.error;
  } else {
    body.textContent = c.new_reply || "(empty reply)";
  }
  meta.textContent = `${c.latency_ms} ms`;
}

function renderFatal(msg) {
  const card = document.createElement("div");
  card.className = "result-card";
  card.innerHTML = `<div class="label">fatal stream error</div><div class="orig" style="color:#ffb3b3">${msg || "unknown"}</div>`;
  $("results").appendChild(card);
}
