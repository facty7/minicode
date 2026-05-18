let state = {
  sessionId: localStorage.getItem("minicode.sessionId") || null,
  sessions: [],
  memories: [],
  tree: "",
  workspaceRoot: "",
  codeIndex: {},
  roleTrace: [],
};

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function appendBubble(role, content, meta = "") {
  const chatLog = $("chatLog");
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  node.innerHTML = `<span class="meta">${escapeHtml(meta || role)}</span>${escapeHtml(content)}`;
  chatLog.appendChild(node);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function renderSessions(items) {
  const list = $("sessionList");
  list.innerHTML = "";
  items.forEach((item) => {
    const node = document.createElement("div");
    node.className = "item" + (item.id === state.sessionId ? " active" : "");
    node.innerHTML = `<strong>${escapeHtml(item.title || "Session")}</strong><br/><span>${escapeHtml(item.summary || "")}</span>`;
    node.onclick = async () => {
      state.sessionId = item.id;
      localStorage.setItem("minicode.sessionId", item.id);
      await loadSession(item.id);
      await bootstrap();
    };
    list.appendChild(node);
  });
}

function renderMemories(items) {
  const list = $("memoryList");
  list.innerHTML = "";
  items.slice(0, 10).forEach((item) => {
    const node = document.createElement("div");
    node.className = "item";
    node.innerHTML = `<strong>${escapeHtml(item.category)}</strong><br/>${escapeHtml(item.content)}`;
    list.appendChild(node);
  });
}

function renderBootstrap(data) {
  state.sessions = data.sessions || [];
  state.memories = data.memories || [];
  state.tree = data.tree?.text || "";
  state.workspaceRoot = data.workspace_root || "";
  state.codeIndex = data.code_index || {};
  $("workspaceLabel").textContent = `${state.workspaceRoot}  |  model ${data.model_enabled ? "on" : "off"}  |  shell ${data.allow_shell ? "on" : "confirm-only"}`;
  $("treeView").textContent = state.tree;
  $("codeIndexView").textContent = JSON.stringify(state.codeIndex, null, 2);
  renderSessions(state.sessions);
  renderMemories(state.memories);
}

function renderHistory(messages) {
  const chatLog = $("chatLog");
  chatLog.innerHTML = "";
  messages.forEach((message) => appendBubble(message.role, message.content, message.role));
}

async function bootstrap() {
  const res = await fetch("/api/bootstrap");
  const data = await res.json();
  renderBootstrap(data);
  if (!state.sessionId && state.sessions.length) {
    state.sessionId = state.sessions[0].id;
    localStorage.setItem("minicode.sessionId", state.sessionId);
  }
}

async function loadSession(sessionId) {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
  const data = await res.json();
  renderHistory(data.messages || []);
  state.roleTrace = data.role_traces || [];
  $("roleTraceView").textContent = JSON.stringify(state.roleTrace, null, 2);
  $("toolOutput").textContent = JSON.stringify(
    {
      session: data.session,
      tool_runs: data.tool_runs || [],
      role_traces: data.role_traces || [],
      memories: data.memories || [],
    },
    null,
    2
  );
}

async function createSession() {
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "New MiniCode session" }),
  });
  const data = await res.json();
  state.sessionId = data.session_id;
  localStorage.setItem("minicode.sessionId", state.sessionId);
  await bootstrap();
  await loadSession(state.sessionId);
}

async function sendChat(message, confirmRisky = false) {
  if (!message.trim()) return;
  appendBubble("user", message, "user");
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: state.sessionId,
      message,
      confirm_risky: confirmRisky,
    }),
  });
  const data = await res.json();
  state.sessionId = data.session_id;
  localStorage.setItem("minicode.sessionId", state.sessionId);
  appendBubble("assistant", data.reply, "assistant");
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
  await bootstrap();
  await loadSession(state.sessionId);
}

async function callTool(name, args, confirm = false) {
  const res = await fetch("/api/tool", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      args,
      session_id: state.sessionId,
      confirm,
    }),
  });
  const data = await res.json();
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
  await bootstrap();
  if (state.sessionId) {
    await loadSession(state.sessionId);
  }
  return data;
}

async function loadInitialSession() {
  if (state.sessionId) {
    try {
      await loadSession(state.sessionId);
      return;
    } catch (e) {
      localStorage.removeItem("minicode.sessionId");
      state.sessionId = null;
    }
  }
  if (state.sessions.length) {
    state.sessionId = state.sessions[0].id;
    localStorage.setItem("minicode.sessionId", state.sessionId);
    await loadSession(state.sessionId);
  } else {
    await createSession();
  }
}

$("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("chatInput");
  const confirmRisky = $("confirmRisky").checked;
  const message = input.value.trim();
  input.value = "";
  await sendChat(message, confirmRisky);
});

$("newSessionBtn").addEventListener("click", createSession);
$("refreshBtn").addEventListener("click", bootstrap);

$("searchBtn").addEventListener("click", async () => {
  const q = $("searchInput").value.trim();
  if (!q) return;
  const data = await callTool("search", { query: q, limit: 8 });
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
});

$("ragBtn").addEventListener("click", async () => {
  const q = $("ragInput").value.trim();
  if (!q) return;
  const data = await callTool("rag_search", { query: q, limit: 6 });
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
});

$("reindexBtn").addEventListener("click", async () => {
  const data = await callTool("rebuild_index", {}, false);
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
  await bootstrap();
});

$("readBtn").addEventListener("click", async () => {
  const path = $("readPath").value.trim();
  if (!path) return;
  const start = Number($("readStart").value || 1);
  const end = Number($("readEnd").value || 120);
  const data = await callTool("read", { path, start, end });
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
});

$("runBtn").addEventListener("click", async () => {
  const command = $("runCommand").value.trim();
  if (!command) return;
  const confirm = $("runConfirm").checked;
  const data = await callTool("run", { command, confirm }, confirm);
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
});

$("memoryBtn").addEventListener("click", async () => {
  const content = $("memoryInput").value.trim();
  if (!content) return;
  const data = await callTool("remember", { category: "note", content, score: 0.7 }, true);
  $("toolOutput").textContent = JSON.stringify(data, null, 2);
  $("memoryInput").value = "";
});

(async function init() {
  await bootstrap();
  await loadInitialSession();
})();
