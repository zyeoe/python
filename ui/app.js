(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const params = new URLSearchParams(window.location.search);
  const injectedApiBase = typeof window.AGENT_API_BASE === "string" ? window.AGENT_API_BASE : "";
  const API_BASE = (params.get("apiBase") || injectedApiBase || "http://127.0.0.1:8000").replace(/\/+$/, "");
  const WS_BASE = API_BASE.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  const SESSION_ID = localStorage.getItem("desktop-agent-session-id") || makeId();
  localStorage.setItem("desktop-agent-session-id", SESSION_ID);

  const dom = {
    workspaceName: $("#workspaceName"), connectionStatus: $("#connectionStatus"),
    connectionText: $("#connectionText"), fileTree: $("#fileTree"),
    refreshTreeBtn: $("#refreshTreeBtn"), fileTabs: $("#fileTabs"),
    editorEmpty: $("#editorEmpty"), codeScroll: $("#codeScroll"),
    lineNumbers: $("#lineNumbers"), codeContent: $("#codeContent"),
    fileLanguage: $("#fileLanguage"), fileSize: $("#fileSize"),
    filePathStatus: $("#filePathStatus"), refreshFileBtn: $("#refreshFileBtn"),
    copyFileBtn: $("#copyFileBtn"), chatMessages: $("#chatMessages"),
    chatForm: $("#chatForm"), chatInput: $("#chatInput"), sendBtn: $("#sendBtn"),
    interruptBtn: $("#interruptBtn"), composerHint: $("#composerHint"),
    toolTimeline: $("#toolTimeline"), toolCount: $("#toolCount"),
    clearToolsBtn: $("#clearToolsBtn"), toast: $("#toast"),
  };

  const state = {
    socket: null, connected: false, running: false,
    reconnectTimer: null, reconnectAttempt: 0, sessionId: SESSION_ID,
    workspaceRoot: "", currentFile: null, currentContent: "",
    currentLanguage: "text", toolCalls: new Map(), toolNumber: 0,
    thinkingElement: null, refreshTreeTimer: null, refreshFileTimer: null,
  };

  const TOOL_LABELS = { read: "READ", write: "WRITE", edit: "EDIT", bash: "BASH", error: "ERROR", thinking: "THINK" };

  function makeId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  function truncateText(value, max = 12000) {
    const text = String(value ?? "");
    return text.length <= max ? text : `${text.slice(0, max)}\n… [${text.length - max} characters truncated]`;
  }

  function resultText(result) {
    if (typeof result === "string") return result;
    try { return JSON.stringify(result, null, 2); } catch { return String(result); }
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "—";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function timeLabel() {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
  }

  let toastTimer = null;
  function showToast(message, type = "info") {
    dom.toast.textContent = message;
    dom.toast.className = `toast is-visible${type === "error" ? " error" : ""}`;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { dom.toast.className = "toast"; }, 2800);
  }
  function setConnection(mode, text) {
    dom.connectionStatus.className = `connection-status is-${mode}`;
    dom.connectionText.textContent = text;
  }

  function setRunning(running, interruptPending = false) {
    state.running = running;
    dom.sendBtn.disabled = !state.connected || running;
    dom.chatInput.disabled = !state.connected;
    dom.interruptBtn.disabled = !state.connected || !running || interruptPending;
    dom.interruptBtn.textContent = interruptPending ? "中断中…" : "■ 中断";
    dom.interruptBtn.classList.toggle("is-armed", running && !interruptPending);
    dom.composerHint.textContent = running ? "Agent 正在执行，可随时点击中断"
      : state.connected ? "Enter 发送 · Shift+Enter 换行" : "正在等待后端连接";
  }

  function scrollChat() {
    requestAnimationFrame(() => { dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight; });
  }

  function scrollTools() {
    requestAnimationFrame(() => { dom.toolTimeline.scrollTop = dom.toolTimeline.scrollHeight; });
  }

  function addMessage(role, content, options = {}) {
    const article = document.createElement("article");
    article.className = `message ${role}${options.error ? " error" : ""}`;
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const author = role === "user" ? "你" : role === "assistant" ? "Agent" : "系统";
    meta.innerHTML = `<span>${author}</span><span>${escapeHtml(options.meta || timeLabel())}</span>`;
    const body = document.createElement("div");
    body.className = "message-body";
    body.innerHTML = role === "assistant" ? renderMarkdown(content) : escapeHtml(content).replaceAll("\n", "<br>");
    article.append(meta, body);
    dom.chatMessages.append(article);
    scrollChat();
    return article;
  }

  function showThinking(message) {
    if (!state.thinkingElement) {
      const article = document.createElement("article");
      article.className = "message assistant thinking";
      article.innerHTML = `
        <div class="message-meta"><span>Agent</span><span>执行中</span></div>
        <div class="message-body">
          <span class="thinking-dots"><span></span><span></span><span></span></span>
          <span class="thinking-label"></span>
        </div>`;
      dom.chatMessages.append(article);
      state.thinkingElement = article;
    }
    state.thinkingElement.querySelector(".thinking-label").textContent = message || "正在思考…";
    scrollChat();
  }

  function hideThinking() {
    if (state.thinkingElement) {
      state.thinkingElement.remove();
      state.thinkingElement = null;
    }
  }

  function renderMarkdown(text) {
    const source = String(text ?? "");
    const parts = [];
    const fence = /```([\w+-]*)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let match;
    while ((match = fence.exec(source)) !== null) {
      parts.push(renderParagraphs(source.slice(lastIndex, match.index)));
      parts.push(`<pre><code>${escapeHtml(match[2])}</code></pre>`);
      lastIndex = match.index + match[0].length;
    }
    parts.push(renderParagraphs(source.slice(lastIndex)));
    return parts.join("");
  }

  function renderParagraphs(raw) {
    if (!raw) return "";
    const escaped = escapeHtml(raw).replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return escaped.split(/\n{2,}/).map((paragraph) =>
      paragraph.trim() ? `<p>${paragraph.replace(/\n/g, "<br>")}</p>` : ""
    ).join("");
  }

  function connectWebSocket() {
    if (state.socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket.readyState)) state.socket.close();
    window.clearTimeout(state.reconnectTimer);
    setConnection("connecting", "连接中");
    const socket = new WebSocket(`${WS_BASE}/ws/${encodeURIComponent(state.sessionId)}`);
    state.socket = socket;
    socket.addEventListener("open", () => {
      state.reconnectAttempt = 0;
      setConnection("connecting", "等待会话");
    });
    socket.addEventListener("message", (event) => {
      try { handleServerEvent(JSON.parse(event.data)); }
      catch (error) { console.error(error); showToast("收到无法解析的后端事件", "error"); }
    });
    socket.addEventListener("close", (event) => {
      state.connected = false;
      setRunning(false);
      setConnection("offline", "已断开");
      if (event.code === 1011 || event.code === 1008) return;
      if (!state.reconnectTimer) {
        const delay = Math.min(8000, 1000 * (2 ** state.reconnectAttempt));
        state.reconnectAttempt += 1;
        state.reconnectTimer = window.setTimeout(() => {
          state.reconnectTimer = null;
          connectWebSocket();
        }, delay);
      }
    });
    socket.addEventListener("error", () => setConnection("offline", "连接错误"));
  }
  function handleServerEvent(event) {
    switch (event.type) {
      case "session_ready":
        state.connected = true;
        setConnection("online", "已连接");
        setRunning(false);
        break;
      case "thinking":
        appendThinkingEvent(event);
        showThinking(event.message || "正在分析请求…");
        break;
      case "tool_call":
        hideThinking();
        upsertToolCall(event);
        showThinking(event.message || "正在调用工具…");
        break;
      case "tool_result":
        updateToolResult(event);
        scheduleWorkspaceRefresh(event);
        break;
      case "final":
        hideThinking();
        addMessage("assistant", event.content || event.message || "已完成", { meta: `完成 · 第 ${event.step || 1} 步` });
        scheduleWorkspaceRefresh();
        finishRun();
        break;
      case "error":
        handleServerError(event);
        break;
      case "interrupt_requested":
        appendThinkingEvent(event);
        setRunning(true, true);
        break;
      case "interrupted":
        hideThinking();
        appendThinkingEvent(event);
        addMessage("warning", event.message || "Agent 已中断。", { meta: "中断" });
        finishRun();
        break;
      case "pong":
        break;
      default:
        console.debug("Unhandled event", event);
    }
  }

  function finishRun() {
    setRunning(false);
    state.toolCalls.forEach(({ element }) => element.classList.remove("is-running"));
  }

  function handleServerError(event) {
    hideThinking();
    addMessage("error", event.message || "服务端发生错误", { meta: "错误", error: true });
    appendErrorCard(event);
    if (event.error_type !== "AgentBusy" && event.error_type !== "NoActiveRun") finishRun();
  }

  function appendThinkingEvent(event) {
    if (!event.message) return;
    const item = document.createElement("div");
    item.className = `tool-thinking${event.level === "warning" ? " warning" : ""}`;
    item.innerHTML = `<span class="mini-dot"></span><span>${escapeHtml(event.message)}</span>`;
    dom.toolTimeline.append(item);
    scrollTools();
  }

  function ensureToolEmptyRemoved() {
    const empty = $("#toolEmpty");
    if (empty) empty.remove();
  }

  function nextToolId() {
    state.toolNumber += 1;
    return `local-tool-${state.toolNumber}`;
  }

  function toolLabel(name) {
    return TOOL_LABELS[name] || String(name || "TOOL").toUpperCase();
  }

  function toolDescription(event) {
    if (event.message) return event.message;
    const args = event.arguments || {};
    if (["read", "write", "edit"].includes(event.tool)) return `${event.tool} ${args.path || ""}`.trim();
    if (event.tool === "bash") return args.command || "执行命令";
    return event.tool || "工具调用";
  }

  function upsertToolCall(event) {
    ensureToolEmptyRemoved();
    const callId = event.call_id || nextToolId();
    if (state.toolCalls.has(callId)) return state.toolCalls.get(callId);
    const card = document.createElement("article");
    card.className = "tool-card is-running";
    const header = document.createElement("div");
    header.className = "tool-card-header";
    header.innerHTML = `
      <span class="tool-badge ${escapeHtml(event.tool || "")}">${escapeHtml(toolLabel(event.tool))}</span>
      <span class="tool-card-title"><strong>${escapeHtml(toolDescription(event))}</strong></span>
      <span class="tool-status">RUNNING</span>`;
    const body = document.createElement("div");
    body.className = "tool-card-body";
    const args = event.arguments || {};
    if (event.tool === "bash" && args.command) {
      body.innerHTML = `<pre class="tool-command">$ ${escapeHtml(args.command)}</pre>`;
    } else if (args.path) {
      body.innerHTML = `<div class="tool-meta-row"><span class="meta-chip">path: ${escapeHtml(args.path)}</span></div>`;
    }
    header.addEventListener("click", () => body.classList.toggle("is-collapsed"));
    card.append(header, body);
    dom.toolTimeline.append(card);
    const record = { callId, event, element: card, body, header };
    state.toolCalls.set(callId, record);
    dom.toolCount.textContent = String(state.toolCalls.size);
    scrollTools();
    return record;
  }

  function findToolRecord(event) {
    if (event.call_id && state.toolCalls.has(event.call_id)) return state.toolCalls.get(event.call_id);
    return [...state.toolCalls.values()].reverse().find((record) => record.event.tool === event.tool) || null;
  }
  function updateToolResult(event) {
    ensureToolEmptyRemoved();
    let record = findToolRecord(event);
    if (!record) record = upsertToolCall({ ...event, type: "tool_call" });
    const card = record.element;
    const body = record.body;
    card.classList.remove("is-running");
    card.classList.toggle("is-error", Boolean(event.is_error));
    const status = record.header.querySelector(".tool-status");
    status.textContent = event.is_error ? "FAILED" : "DONE";
    status.classList.toggle("error", Boolean(event.is_error));

    const args = event.arguments || record.event.arguments || {};
    const result = event.result;
    const frag = document.createElement("div");

    if (event.tool === "bash") {
      const exitCode = result && typeof result === "object" ? result.exit_code : null;
      const stdout = result && typeof result === "object" ? result.stdout : "";
      const stderr = result && typeof result === "object" ? result.stderr : "";
      frag.innerHTML = `
        <div class="tool-meta-row">
          <span class="meta-chip ${exitCode === 0 ? "success" : "failure"}">exit: ${escapeHtml(exitCode ?? "?")}</span>
          ${args.command ? `<span class="meta-chip">timeout: ${escapeHtml(args.timeout ?? 30)}s</span>` : ""}
        </div>
        <pre class="tool-output">${escapeHtml(truncateText(stdout || "(stdout 为空)"))}</pre>
        ${stderr ? `<pre class="tool-output error">${escapeHtml(truncateText(stderr))}</pre>` : ""}`;
    } else if (event.diff) {
      frag.innerHTML = `<div class="tool-meta-row"><span class="meta-chip success">diff ready</span></div>${renderDiff(event.diff)}`;
    } else {
      const text = resultText(result);
      frag.innerHTML = `
        <div class="tool-meta-row">
          <span class="meta-chip ${event.is_error ? "failure" : "success"}">${event.is_error ? "failed" : "completed"}</span>
        </div>
        <pre class="tool-output${event.is_error ? " error" : ""}">${escapeHtml(truncateText(text || "(无输出)"))}</pre>`;
    }

    if (event.is_error && result && typeof result === "object" && result.error) {
      const error = document.createElement("div");
      error.className = "tool-error-message";
      error.textContent = result.error;
      frag.append(error);
    }
    body.replaceChildren(frag);
    scrollTools();
  }

  function renderDiff(diff) {
    const html = String(diff || "").split("\n").map((line) => {
      let kind = "";
      if (line.startsWith("+++") || line.startsWith("---")) kind = "header";
      else if (line.startsWith("@@")) kind = "hunk";
      else if (line.startsWith("+")) kind = "add";
      else if (line.startsWith("-")) kind = "remove";
      return `<span class="diff-line ${kind}">${escapeHtml(line || " ")}</span>`;
    }).join("");
    return `<div class="diff-view" aria-label="文件 diff">${html}</div>`;
  }

  function appendErrorCard(event) {
    ensureToolEmptyRemoved();
    const card = document.createElement("article");
    card.className = "tool-card is-error";
    card.innerHTML = `
      <div class="tool-card-header">
        <span class="tool-badge error">ERROR</span>
        <span class="tool-card-title"><strong>${escapeHtml(event.message || "服务端错误")}</strong></span>
        <span class="tool-status error">FAILED</span>
      </div>
      <div class="tool-card-body">
        <div class="tool-error-message">${escapeHtml(event.error || event.error_type || "Unknown error")}</div>
      </div>`;
    dom.toolTimeline.append(card);
    scrollTools();
  }

  function scheduleWorkspaceRefresh(event = null) {
    window.clearTimeout(state.refreshTreeTimer);
    state.refreshTreeTimer = window.setTimeout(loadTree, 250);
    if (event && ["write", "edit"].includes(event.tool)) {
      window.clearTimeout(state.refreshFileTimer);
      state.refreshFileTimer = window.setTimeout(() => {
        if (state.currentFile) openWorkspaceFile(state.currentFile, { silent: true });
      }, 280);
    }
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch {
        // Keep the HTTP status if the response is not JSON.
      }
      throw new Error(detail);
    }
    return response.json();
  }
  async function loadWorkspaceInfo() {
    try {
      const info = await fetchJson(`${API_BASE}/api/workspace`);
      state.workspaceRoot = info.root || "";
      dom.workspaceName.textContent = info.name || info.root || "当前工作区";
      dom.workspaceName.title = info.root || "";
      document.title = `${info.name || "Desktop Agent"} · Desktop Agent`;
    } catch (error) {
      dom.workspaceName.textContent = "无法读取工作区";
      showToast(`工作区读取失败：${error.message}`, "error");
    }
  }

  async function loadTree() {
    try {
      const data = await fetchJson(`${API_BASE}/api/workspace/tree`);
      const entries = data.entries || [];
      dom.fileTree.replaceChildren();
      if (!entries.length) {
        dom.fileTree.innerHTML = '<div class="panel-empty">工作区为空</div>';
        return;
      }
      dom.fileTree.append(renderTree(entries));
    } catch (error) {
      dom.fileTree.innerHTML = `<div class="panel-empty">文件树加载失败<br>${escapeHtml(error.message)}</div>`;
      showToast(`文件树加载失败：${error.message}`, "error");
    }
  }

  function renderTree(entries) {
    const fragment = document.createDocumentFragment();
    for (const entry of entries) {
      if (entry.type === "directory") {
        const details = document.createElement("details");
        details.className = "tree-folder";
        details.open = true;
        const summary = document.createElement("summary");
        summary.innerHTML = `<span class="tree-icon">▰</span><span class="tree-name">${escapeHtml(entry.name)}</span>`;
        details.append(summary);
        const children = document.createElement("div");
        children.className = "tree-folder-children";
        children.append(renderTree(entry.children || []));
        details.append(children);
        fragment.append(details);
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tree-file";
        button.dataset.path = entry.path;
        button.title = entry.path;
        button.innerHTML = `<span class="tree-icon">${fileIcon(entry.name)}</span><span class="tree-name">${escapeHtml(entry.name)}</span>`;
        button.addEventListener("click", () => openWorkspaceFile(entry.path));
        fragment.append(button);
      }
    }
    return fragment;
  }

  function fileIcon(name) {
    const extension = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    if (["py", "js", "ts", "html", "css", "json"].includes(extension)) return "◆";
    if (["md", "txt"].includes(extension)) return "▤";
    return "◇";
  }

  function setActiveTreeFile(path) {
    document.querySelectorAll(".tree-file.is-active").forEach((node) => node.classList.remove("is-active"));
    if (!path) return;
    const node = [...document.querySelectorAll(".tree-file")].find((item) => item.dataset.path === path);
    if (node) {
      node.classList.add("is-active");
      node.scrollIntoView({ block: "nearest" });
    }
  }

  async function openWorkspaceFile(path, options = {}) {
    try {
      const data = await fetchJson(`${API_BASE}/api/workspace/file?path=${encodeURIComponent(path)}`);
      state.currentFile = data.path;
      state.currentContent = data.content || "";
      state.currentLanguage = data.language || "text";
      dom.editorEmpty.hidden = true;
      dom.codeScroll.hidden = false;
      dom.fileTabs.innerHTML = `<div class="file-tab" title="${escapeHtml(data.path)}">${escapeHtml(data.path)}</div>`;
      dom.fileLanguage.textContent = data.language || "text";
      dom.fileSize.textContent = formatBytes(data.size);
      dom.filePathStatus.textContent = data.path;
      dom.refreshFileBtn.disabled = false;
      dom.copyFileBtn.disabled = false;
      renderCode(state.currentContent, state.currentLanguage);
      setActiveTreeFile(path);
      if (!options.silent) showToast(`已打开 ${data.path}`);
    } catch (error) {
      showToast(`打开文件失败：${error.message}`, "error");
      if (!options.silent) {
        dom.editorEmpty.hidden = false;
        dom.codeScroll.hidden = true;
      }
    }
  }

  function renderCode(content, language) {
    const lines = String(content ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    dom.lineNumbers.innerHTML = lines.map((_, index) => `<span>${index + 1}</span>`).join("");
    dom.codeContent.innerHTML = lines
      .map((line) => `<span class="code-line">${highlightLine(line || " ", language)}</span>`)
      .join("");
    dom.codeScroll.scrollTop = 0;
    dom.codeScroll.scrollLeft = 0;
  }
  const LANGUAGE_RULES = {
    python: [
      ["comment", "#[^\\n]*"],
      ["string", "(?:[rRuUbBfF]{0,2})(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*')"],
      ["decorator", "@[A-Za-z_][\\w.]*"],
      ["keyword", "\\b(?:and|as|assert|async|await|break|class|continue|def|del|elif|else|except|False|finally|for|from|global|if|import|in|is|lambda|None|nonlocal|not|or|pass|raise|return|True|try|while|with|yield)\\b"],
      ["builtin", "\\b(?:print|len|range|dict|list|set|tuple|str|int|float|bool|open|enumerate|zip|sum|min|max|any|all)\\b"],
      ["number", "\\b(?:0[xX][0-9A-Fa-f]+|\\d+(?:\\.\\d+)?)\\b"],
    ],
    javascript: [
      ["comment", "//[^\\n]*"],
      ["string", "(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`)"],
      ["keyword", "\\b(?:async|await|break|case|catch|class|const|continue|debugger|default|delete|do|else|export|extends|finally|for|from|function|if|import|in|instanceof|let|new|of|return|static|super|switch|this|throw|try|typeof|var|void|while|with|yield)\\b"],
      ["builtin", "\\b(?:console|document|window|Math|JSON|Promise|Array|Object|String|Number|Boolean|Map|Set|Date|RegExp|WebSocket|fetch)\\b"],
      ["number", "\\b(?:0[xX][0-9A-Fa-f]+|\\d+(?:\\.\\d+)?)\\b"],
    ],
    typescript: [
      ["comment", "//[^\\n]*"],
      ["string", "(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`)"],
      ["keyword", "\\b(?:abstract|any|as|async|await|boolean|break|case|catch|class|const|continue|declare|default|delete|do|else|enum|export|extends|finally|for|from|function|if|implements|import|in|infer|instanceof|interface|keyof|let|module|namespace|never|new|null|number|object|of|private|protected|public|readonly|return|static|string|super|switch|this|throw|true|try|type|typeof|undefined|unknown|var|void|while|yield)\\b"],
      ["number", "\\b(?:0[xX][0-9A-Fa-f]+|\\d+(?:\\.\\d+)?)\\b"],
    ],
    json: [
      ["key", "\"(?:\\\\.|[^\"\\\\])*\"(?=\\s*:)"],
      ["string", "\"(?:\\\\.|[^\"\\\\])*\""],
      ["keyword", "\\b(?:true|false|null)\\b"],
      ["number", "-?\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b"],
    ],    html: [
      ["comment", "<!--.*?-->"],
      ["tag", "</?[A-Za-z][^>]*>"],
    ],
    css: [
      ["comment", "/\\*.*?\\*/"],
      ["string", "(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*')"],
      ["key", "[\\w-]+(?=\\s*:)"],
      ["number", "#[0-9A-Fa-f]{3,8}\\b|\\b\\d+(?:\\.\\d+)?(?:px|rem|em|vh|vw|%|s|ms)?\\b"],
    ],
    bash: [
      ["comment", "#[^\\n]*"],
      ["string", "(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*')"],
      ["keyword", "\\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|function|in|select|until|time)\\b"],
      ["number", "\\b\\d+\\b"],
    ],
    powershell: [
      ["comment", "#[^\\n]*"],
      ["string", "(?:\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*')"],
      ["keyword", "\\b(?:if|else|elseif|foreach|for|while|function|param|return|try|catch|finally|throw|switch|in)\\b"],
      ["number", "\\b\\d+\\b"],
    ],
  };

  function highlightLine(line, language) {
    const rules = LANGUAGE_RULES[language] || [];
    if (!rules.length) return escapeHtml(line);
    const pattern = new RegExp(rules.map(([_, source]) => `(${source})`).join("|"), "g");
    let result = "";
    let lastIndex = 0;
    let match;
    while ((match = pattern.exec(line)) !== null) {
      if (!match[0]) {
        pattern.lastIndex += 1;
        continue;
      }
      result += escapeHtml(line.slice(lastIndex, match.index));
      const groupIndex = match.slice(1).findIndex((value) => value !== undefined);
      const tokenType = rules[groupIndex] ? rules[groupIndex][0] : "text";
      result += `<span class="tok-${tokenType}">${escapeHtml(match[0])}</span>`;
      lastIndex = match.index + match[0].length;
    }
    result += escapeHtml(line.slice(lastIndex));
    return result || " ";
  }
  function sendMessage() {
    const content = dom.chatInput.value.trim();
    if (!content) return;
    if (!state.connected || !state.socket || state.socket.readyState !== WebSocket.OPEN) {
      showToast("后端尚未连接，无法发送", "error");
      return;
    }
    if (state.running) {
      showToast("Agent 正在运行，请先等待或中断", "error");
      return;
    }
    addMessage("user", content, { meta: timeLabel() });
    dom.chatInput.value = "";
    setRunning(true);
    state.socket.send(JSON.stringify({ type: "user_message", content }));
  }

  async function interruptAgent() {
    if (!state.running) return;
    setRunning(true, true);
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify({ type: "interrupt" }));
      return;
    }
    try {
      await fetchJson(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/interrupt`, { method: "POST" });
    } catch (error) {
      showToast(`中断请求失败：${error.message}`, "error");
      setRunning(true, false);
    }
  }

  async function refreshCurrentFile() {
    if (!state.currentFile) return;
    await openWorkspaceFile(state.currentFile, { silent: true });
    showToast(`已刷新 ${state.currentFile}`);
  }

  async function copyCurrentFile() {
    if (!state.currentFile) return;
    try {
      await navigator.clipboard.writeText(state.currentContent);
      showToast("文件内容已复制");
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = state.currentContent;
      document.body.append(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
      showToast("文件内容已复制");
    }
  }

  function clearTools() {
    state.toolCalls.clear();
    state.toolNumber = 0;
    dom.toolCount.textContent = "0";
    dom.toolTimeline.innerHTML = `
      <div id="toolEmpty" class="tool-empty">
        <div class="tool-empty-icon">⌁</div>
        <p>还没有工具调用</p>
        <span>读取文件、命令输出、文件 diff 和错误都会显示在这里。</span>
      </div>`;
  }

  function bindEvents() {
    dom.chatForm.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage();
    });
    dom.chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    dom.interruptBtn.addEventListener("click", interruptAgent);
    dom.refreshTreeBtn.addEventListener("click", loadTree);
    dom.refreshFileBtn.addEventListener("click", refreshCurrentFile);
    dom.copyFileBtn.addEventListener("click", copyCurrentFile);
    dom.clearToolsBtn.addEventListener("click", clearTools);
    window.addEventListener("beforeunload", () => {
      if (state.socket && state.socket.readyState === WebSocket.OPEN) state.socket.close();
    });
  }

  async function init() {
    bindEvents();
    setRunning(false);
    await loadWorkspaceInfo();
    await loadTree();
    const initialFile = params.get("file");
    if (initialFile) await openWorkspaceFile(initialFile, { silent: true });
    connectWebSocket();
  }

  init();
})();