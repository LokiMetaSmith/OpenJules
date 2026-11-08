let currentTaskId = null;
let activeTasks = [];

document.addEventListener("DOMContentLoaded", () => {
  pollState();
  setInterval(pollState, 3000);
});

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
  const diffEl = document.getElementById("diff-content");
  const statsEl = document.getElementById("diff-stats");
  if (!diffEl) return;

  if (!diffText || diffText.trim() === "") {
    diffEl.innerText = "# No working changes detected in workspace.";
    if (statsEl) statsEl.innerText = "+0 -0";
    return;
  }

  diffEl.innerText = diffText;
  const added = (diffText.match(/^\+[^+]/gm) || []).length;
  const removed = (diffText.match(/^-[^-]/gm) || []).length;
  if (statsEl) statsEl.innerText = `+${added} -${removed}`;
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
