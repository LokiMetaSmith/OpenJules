let currentTaskId = null;
let activeTasks = [];
let activeWs = null;

document.addEventListener("DOMContentLoaded", () => {
  pollState();
  setInterval(pollState, 3000);
});

function connectTaskWebSocket(taskId) {
  if (activeWs) {
    activeWs.close();
    activeWs = null;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/tasks/${taskId}`;
  try {
    activeWs = new WebSocket(wsUrl);
    activeWs.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "thought") {
          appendThoughtCard(msg.thought);
        } else if (msg.type === "diff") {
          renderDiff(msg.diff);
        }
      } catch (e) {
        console.debug("WS parse error", e);
      }
    };
  } catch (err) {
    console.debug("WebSocket connection fallback to polling", err);
  }
}

function appendThoughtCard(th) {
  const container = document.getElementById("thought-stream");
  if (!container) return;
  const card = document.createElement("div");
  card.className = "thought-card";
  card.innerHTML = `<div class="thought-meta">${th.origin || "Agent"} [${th.status || "executing"}]</div><div class="thought-body">${th.content || ""}</div>`;
  container.appendChild(card);
  container.scrollTop = container.scrollHeight;
}

async function pollState() {
  try {
    const res = await fetch("/api/tasks");
    if (!res.ok) return;
    const data = await res.json();
    renderTasks(data.tasks || []);

    const polyRes = await fetch("/api/polyphony/state");
    if (polyRes.ok) {
      const polyData = await polyRes.json();
      renderPolyphony(polyData);
    }
  } catch (err) {
    console.debug("Poll error", err);
  }
}

function renderTasks(tasks) {
  activeTasks = tasks;
  const countEl = document.getElementById("task-count");
  if (countEl) countEl.innerText = tasks.length;

  const listEl = document.getElementById("task-list");
  if (!listEl) return;

  if (tasks.length === 0) {
    listEl.innerHTML = '<div class="empty-state">No tasks running. Dispatch one above.</div>';
    return;
  }

  listEl.innerHTML = "";
  tasks.forEach((t) => {
    const card = document.createElement("div");
    card.className = `task-card ${t.id === currentTaskId ? "active" : ""}`;
    card.onclick = () => selectTask(t.id);

    const title = document.createElement("div");
    title.className = "task-card-title";
    title.innerText = `#${t.issue_id} ${t.issue_title}`;

    const meta = document.createElement("div");
    meta.className = "task-card-meta";
    meta.innerHTML = `<span>${t.forge.toUpperCase()}</span><span>${t.status || "running"}</span>`;

    card.appendChild(title);
    card.appendChild(meta);
    listEl.appendChild(card);
  });

  if (!currentTaskId && tasks.length > 0) {
    selectTask(tasks[0].id);
  }
}

function renderPolyphony(data) {
  const batonEl = document.getElementById("baton-list");
  if (!batonEl) return;

  const batons = data.batons || {};
  const entries = Object.entries(batons);

  if (entries.length === 0) {
    batonEl.innerHTML = '<div class="empty-state">No active locks held.</div>';
    return;
  }

  batonEl.innerHTML = "";
  entries.forEach(([res, owner]) => {
    const item = document.createElement("div");
    item.className = "tree-item";
    item.innerText = `🔒 ${res} (${owner})`;
    batonEl.appendChild(item);
  });
}

async function selectTask(taskId) {
  currentTaskId = taskId;
  connectTaskWebSocket(taskId);
  document.querySelectorAll(".task-card").forEach((c) => c.classList.remove("active"));

  // Highlight active card
  const cards = document.querySelectorAll(".task-card");
  tasks = activeTasks;
  const idx = tasks.findIndex((t) => t.id === taskId);
  if (idx >= 0 && cards[idx]) {
    cards[idx].classList.add("active");
  }

  // Fetch thoughts and diff for this task
  try {
    const res = await fetch(`/api/tasks/${taskId}`);
    if (res.ok) {
      const details = await res.json();
      renderThoughtStream(details.thoughts || []);
      renderDiff(details.diff || "");
      const ind = document.getElementById("agent-indicator");
      if (ind) ind.innerText = `● ${details.status || "Active"}`;
    }
  } catch (e) {
    console.error(e);
  }
}

function renderThoughtStream(thoughts) {
  const container = document.getElementById("thought-stream");
  if (!container) return;

  if (thoughts.length === 0) {
    container.innerHTML = '<div class="empty-state">No thoughts recorded yet.</div>';
    return;
  }

  container.innerHTML = "";
  thoughts.forEach((th) => {
    const card = document.createElement("div");
    card.className = "thought-card";

    const meta = document.createElement("div");
    meta.className = "thought-meta";
    meta.innerText = `${th.origin || "Agent"} [${th.status || "executing"}]`;

    const body = document.createElement("div");
    body.className = "thought-body";
    body.innerText = th.content || "";

    card.appendChild(meta);
    card.appendChild(body);
    container.appendChild(card);
  });
  container.scrollTop = container.scrollHeight;
}

function renderDiff(diffText) {
  const diffContainer = document.getElementById("diff-viewport");
  const statsEl = document.getElementById("diff-stats");
  if (!diffContainer) return;

  if (!diffText || diffText.trim() === "") {
    diffContainer.innerHTML = '<pre class="diff-code" id="diff-content"><code class="language-diff"># No working changes detected in workspace.\n# When the agent edits files and executes tools, diffs appear here.</code></pre>';
    if (statsEl) statsEl.innerText = "+0 -0";
    return;
  }

  const added = (diffText.match(/^\+[^+]/gm) || []).length;
  const removed = (diffText.match(/^-[^-]/gm) || []).length;
  if (statsEl) statsEl.innerText = `+${added} -${removed}`;

  // Multi-file split parser
  const fileChunks = diffText.split(/^diff --git /m).filter(Boolean);
  if (fileChunks.length === 0) {
    diffContainer.innerHTML = `<pre class="diff-code"><code>${escapeHtml(diffText)}</code></pre>`;
    return;
  }

  diffContainer.innerHTML = "";
  fileChunks.forEach((chunk, idx) => {
    const lines = chunk.split("\n");
    const headerLine = lines[0] || "unknown file";
    const fileName = headerLine.split(" ")[0].replace(/^a\//, "");

    const fileCard = document.createElement("div");
    fileCard.className = "diff-file-card";

    const fileHeader = document.createElement("div");
    fileHeader.className = "diff-file-header";
    fileHeader.innerHTML = `<span>📄 <strong>${escapeHtml(fileName)}</strong></span><span class="file-action-hint">Click line to review</span>`;

    const codeBlock = document.createElement("div");
    codeBlock.className = "diff-code-table";

    lines.slice(1).forEach((line, lineIdx) => {
      const row = document.createElement("div");
      let lineClass = "diff-line-normal";
      if (line.startsWith("+")) lineClass = "diff-line-add";
      else if (line.startsWith("-")) lineClass = "diff-line-del";
      else if (line.startsWith("@@")) lineClass = "diff-line-meta";

      row.className = `diff-line ${lineClass}`;
      row.onclick = () => promptInlineComment(fileName, lineIdx + 1, line);

      row.innerHTML = `<span class="line-num">${lineIdx + 1}</span><span class="line-text">${escapeHtml(line)}</span>`;
      codeBlock.appendChild(row);
    });

    fileCard.appendChild(fileHeader);
    fileCard.appendChild(codeBlock);
    diffContainer.appendChild(fileCard);
  });
}

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function promptInlineComment(fileName, lineNo, lineText) {
  if (!currentTaskId) return;
  const comment = prompt(`Add inline review comment on ${fileName}:${lineNo}\nCode: ${lineText.substring(0, 50)}...`);
  if (!comment || !comment.trim()) return;

  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        comment: comment,
        file_path: fileName,
        line_number: lineNo,
      }),
    });
    if (res.ok) {
      const data = await res.json();
      appendThoughtCard(data.comment);
    }
  } catch (e) {
    alert("Failed to submit comment: " + e.message);
  }
}

async function fetchCurrentDiff() {
  if (!currentTaskId) return;
  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/diff`);
    if (res.ok) {
      const data = await res.json();
      renderDiff(data.diff || "");
    }
  } catch (e) {
    console.error(e);
  }
}

async function runPaletteCommand(action) {
  const statusEl = document.getElementById("palette-status");
  if (statusEl) statusEl.innerText = `Executing: ${action}...`;

  if (!currentTaskId) {
    if (statusEl) statusEl.innerText = "No active task selected.";
    return;
  }

  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/command`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: jsonStringify({ command: action }),
    });
    const data = await res.json();
    if (statusEl) statusEl.innerText = data.result || "Command executed.";
    fetchCurrentDiff();
  } catch (err) {
    if (statusEl) statusEl.innerText = `Error: ${err.message}`;
  }
}

function jsonStringify(obj) {
  return JSON.stringify(obj);
}

function openNewTaskModal() {
  document.getElementById("task-modal").style.display = "flex";
}

function closeNewTaskModal() {
  document.getElementById("task-modal").style.display = "none";
}

async function submitNewTask() {
  const forge = document.getElementById("task-forge").value;
  const repo = document.getElementById("task-repo").value;
  const title = document.getElementById("task-title").value;
  const body = document.getElementById("task-body").value;

  if (!title) {
    alert("Please provide an issue title");
    return;
  }

  const payload = {
    forge: forge,
    issue_id: Math.floor(Math.random() * 900) + 100,
    issue_title: title,
    issue_body: body,
    repo_full_name: repo,
    repo_clone_url: repo.startsWith("rad:") ? repo : `https://github.com/${repo}.git`,
    default_branch: forge === "radicle" ? "master" : "main",
  };

  try {
    const res = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      closeNewTaskModal();
      pollState();
    }
  } catch (e) {
    alert("Failed to dispatch task: " + e.message);
  }
}

function handleInputKey(e) {
  if (e.key === "Enter") {
    sendPromptCommand();
  }
}

function sendPromptCommand() {
  const input = document.getElementById("agent-command-input");
  if (!input || !input.value.trim()) return;

  const thoughtContainer = document.getElementById("thought-stream");
  const card = document.createElement("div");
  card.className = "thought-card";
  card.innerHTML = `<div class="thought-meta">Operator Instruction</div><div class="thought-body">${input.value}</div>`;
  thoughtContainer.appendChild(card);
  thoughtContainer.scrollTop = thoughtContainer.scrollHeight;

  input.value = "";
}
