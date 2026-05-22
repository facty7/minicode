let state = {
  sessionId: localStorage.getItem("minicode.sessionId") || null,
  sessions: [],
  workspaceRoot: "",
  codeIndex: {},
  roleTrace: [],
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function pretty(data) {
  return JSON.stringify(data, null, 2);
}

function displayWorkspace(root) {
  const parts = String(root || "").split(/[\\/]+/).filter(Boolean);
  return parts.at(-1) || "workspace";
}

function showOutput(data, kind = "json") {
  $("outputKind").textContent = kind;
  $("toolOutput").textContent = typeof data === "string" ? data : pretty(data);
}

function renderEmptyState() {
  $("chatLog").innerHTML = `
    <div class="empty-state">
      <h3>问我这个仓库里的任何事</h3>
      <p>我会在后台检索文件、读取上下文、预览修改，并在危险操作前请求确认。</p>
    </div>
  `;
}

function appendBubble(role, content, meta = "") {
  const chatLog = $("chatLog");
  const empty = chatLog.querySelector(".empty-state");
  if (empty) empty.remove();
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  const label = role === "assistant" ? "助手" : role === "user" ? "用户" : meta || role;
  node.innerHTML = `<span class="meta">${escapeHtml(label)}</span>${escapeHtml(content)}`;
  chatLog.appendChild(node);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function renderSessions(items) {
  const list = $("sessionList");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = `<div class="session-item"><span>暂无对话</span></div>`;
    return;
  }
  items.forEach((item) => {
    const node = document.createElement("div");
    node.className = "session-item" + (item.id === state.sessionId ? " active" : "");
    node.innerHTML = `<strong>${escapeHtml(item.title || "新对话")}</strong><span>${escapeHtml(item.summary || "")}</span>`;
    node.onclick = async () => {
      state.sessionId = item.id;
      localStorage.setItem("minicode.sessionId", item.id);
      await loadSession(item.id);
      await bootstrap(false);
    };
    list.appendChild(node);
  });
}

function renderRepoMap(data) {
  const files = data.files || [];
  if (!files.length) {
    $("repoMapView").textContent = "暂无索引，点击重建索引。";
    return;
  }
  $("repoMapView").textContent = files
    .map((item) => `${item.path}  chunks=${item.chunk_count}  lines=${item.first_line}-${item.last_line}`)
    .join("\n");
}

function renderActivity(items) {
  const box = $("activityList");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = `<div class="activity-card"><strong>等待任务<span>idle</span></strong></div>`;
    return;
  }
  items.slice(-5).reverse().forEach((item) => {
    const node = document.createElement("div");
    node.className = "activity-card";
    node.innerHTML = `<strong>${escapeHtml(item.role)}<span>${escapeHtml(item.status)}</span></strong><pre>${escapeHtml(pretty(item.detail || {}))}</pre>`;
    box.appendChild(node);
  });
}

async function fetchRepoMap() {
  const res = await fetch("/api/repo-map?limit=14");
  const data = await res.json();
  renderRepoMap(data);
  return data;
}

function renderBootstrap(data) {
  state.sessions = data.sessions || [];
  state.workspaceRoot = data.workspace_root || "";
  state.codeIndex = data.code_index || {};
  $("workspaceLabel").textContent = displayWorkspace(state.workspaceRoot);
  $("modelStatus").textContent = data.model_enabled ? "在线" : "离线规则";
  $("shellStatus").textContent = data.allow_shell ? "已开启" : "需确认";
  $("indexStatus").textContent = `${state.codeIndex.chunk_count || 0} 片段`;
  renderSessions(state.sessions);
}

function renderHistory(messages) {
  const chatLog = $("chatLog");
  chatLog.innerHTML = "";
  if (!messages.length) {
    renderEmptyState();
    return;
  }
  messages.forEach((message) => appendBubble(message.role, message.content, message.role));
}

async function bootstrap(loadMap = true) {
  const res = await fetch("/api/bootstrap");
  const data = await res.json();
  renderBootstrap(data);
  if (loadMap) await fetchRepoMap();
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
  renderActivity(state.roleTrace);
  showOutput({
    session: data.session,
    recent_tool_runs: data.tool_runs || [],
    recent_trace: data.role_traces || [],
  });
}

async function createSession() {
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "新对话" }),
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
  appendBubble("assistant", data.reply || pretty(data), "assistant");
  showOutput(data);
  await bootstrap(false);
  await loadSession(state.sessionId);
}

async function callTool(name, args, confirm = false) {
  const res = await fetch("/api/tool", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, args, session_id: state.sessionId, confirm }),
  });
  const data = await res.json();
  showOutput(data);
  if (name === "rebuild_index") await fetchRepoMap();
  if (state.sessionId) await loadSession(state.sessionId);
  await bootstrap(false);
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
  const message = input.value.trim();
  input.value = "";
  await sendChat(message, $("confirmRisky").checked);
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    $("chatInput").value = button.dataset.prompt || "";
    $("chatInput").focus();
  });
});

$("newSessionBtn").addEventListener("click", createSession);
$("refreshBtn").addEventListener("click", async () => {
  await bootstrap();
  if (state.sessionId) await loadSession(state.sessionId);
});
$("settingsBtn").addEventListener("click", () => $("settingsPanel").classList.add("open"));
$("closeSettingsBtn").addEventListener("click", () => $("settingsPanel").classList.remove("open"));
$("repoMapBtn").addEventListener("click", async () => showOutput(await fetchRepoMap()));
$("reindexBtn").addEventListener("click", async () => callTool("rebuild_index", {}, false));

(async function init() {
  renderEmptyState();
  await bootstrap();
  await loadInitialSession();
})();
