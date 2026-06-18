const state = {
  context: localStorage.getItem("k8sai.context") || "",
  tab: "pods",
  cluster: { namespaces: [], pods: [], deployments: [], services: [] },
};

const els = {
  statusDot: document.getElementById("statusDot"),
  contextSelect: document.getElementById("contextSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  statRow: document.getElementById("statRow"),
  tabs: document.getElementById("resourceTabs"),
  table: document.getElementById("resourceTable"),
  clusterEmpty: document.getElementById("clusterEmpty"),
  clusterError: document.getElementById("clusterError"),
  chatScroll: document.getElementById("chatScroll"),
  chatForm: document.getElementById("chatForm"),
  chatInput: document.getElementById("chatInput"),
  manifestsBtn: document.getElementById("manifestsBtn"),
  drawer: document.getElementById("manifestDrawer"),
  drawerOverlay: document.getElementById("drawerOverlay"),
  closeDrawer: document.getElementById("closeDrawer"),
  manifestList: document.getElementById("manifestList"),
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : res.statusText;
    throw new Error(detail);
  }
  return body;
}

function ctxParam() {
  return state.context ? `context=${encodeURIComponent(state.context)}` : "";
}

/* ----------------------------------------------------- contexts -------- */

async function loadContexts() {
  try {
    const data = await api("/api/contexts");
    els.contextSelect.innerHTML = "";
    data.contexts.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      els.contextSelect.appendChild(opt);
    });
    if (!state.context || !data.contexts.includes(state.context)) {
      state.context = data.current || data.contexts[0] || "";
    }
    els.contextSelect.value = state.context;
    localStorage.setItem("k8sai.context", state.context);
    setStatus("ok");
  } catch (e) {
    setStatus("bad");
    els.contextSelect.innerHTML = `<option>no contexts found</option>`;
  }
}

els.contextSelect.addEventListener("change", () => {
  state.context = els.contextSelect.value;
  localStorage.setItem("k8sai.context", state.context);
  loadCluster();
});

function setStatus(kind) {
  els.statusDot.classList.remove("ok", "bad", "checking");
  els.statusDot.classList.add(kind);
}

/* ------------------------------------------------------- cluster view -- */

async function loadCluster() {
  if (!state.context) return;
  setStatus("checking");
  try {
    const data = await api(`/api/cluster?${ctxParam()}`);
    state.cluster = data;
    setStatus(data.errors && data.errors.length ? "bad" : "ok");
    renderStats();
    renderTable();
    els.clusterError.hidden = !(data.errors && data.errors.length);
    if (data.errors && data.errors.length) {
      els.clusterError.textContent = data.errors[0];
    }
  } catch (e) {
    setStatus("bad");
    els.clusterError.hidden = false;
    els.clusterError.textContent = e.message;
  }
}

function renderStats() {
  const tiles = [
    ["Namespaces", state.cluster.namespaces.length],
    ["Pods", state.cluster.pods.length],
    ["Deployments", state.cluster.deployments.length],
    ["Services", state.cluster.services.length],
  ];
  els.statRow.innerHTML = tiles
    .map(([label, num]) => `<div class="stat-tile"><div class="num">${num}</div><div class="label">${label}</div></div>`)
    .join("");
}

const TAB_COLUMNS = {
  pods: ["Name", "Namespace", "Phase", "Ready", "Restarts"],
  deployments: ["Name", "Namespace", "Desired", "Ready"],
  services: ["Name", "Namespace", "Type", "Ports"],
  namespaces: ["Name", "Status"],
};

function rowsFor(tab) {
  switch (tab) {
    case "pods":
      return state.cluster.pods.map((p) => [p.name, p.namespace, p.phase, p.ready, p.restarts]);
    case "deployments":
      return state.cluster.deployments.map((d) => [d.name, d.namespace, d.desired, d.ready]);
    case "services":
      return state.cluster.services.map((s) => [s.name, s.namespace, s.type, (s.ports || []).join(", ")]);
    case "namespaces":
      return state.cluster.namespaces.map((n) => [n.name, n.status]);
    default:
      return [];
  }
}

function renderTable() {
  const cols = TAB_COLUMNS[state.tab];
  const rows = rowsFor(state.tab);

  els.table.querySelector("thead").innerHTML = `<tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>`;
  els.table.querySelector("tbody").innerHTML = rows
    .map((r) => `<tr>${r.map((v) => `<td>${escapeHtml(String(v))}</td>`).join("")}</tr>`)
    .join("");

  els.clusterEmpty.hidden = rows.length !== 0;
}

els.tabs.addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  state.tab = btn.dataset.tab;
  [...els.tabs.children].forEach((t) => t.classList.toggle("active", t === btn));
  renderTable();
});

els.refreshBtn.addEventListener("click", loadCluster);

/* ------------------------------------------------------------- chat ---- */

function escapeHtml(str) {
  return str.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function addUserMessage(text) {
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<p>${escapeHtml(text)}</p>`;
  els.chatScroll.appendChild(div);
  scrollChatToBottom();
}

function addSystemNote(text) {
  const div = document.createElement("div");
  div.className = "msg system";
  div.innerHTML = `<p>${escapeHtml(text)}</p>`;
  els.chatScroll.appendChild(div);
  scrollChatToBottom();
}

function scrollChatToBottom() {
  els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
}

function addManifestCard({ namespace, yamlText, explanation }) {
  const wrap = document.createElement("div");
  wrap.className = "card";
  wrap.innerHTML = `
    <div class="card-body">
      <span class="ns-badge">ns/${escapeHtml(namespace)}</span>
      <p class="explanation">${escapeHtml(explanation || "Generated manifest:")}</p>
      <button class="yaml-toggle">show yaml</button>
      <pre class="yaml-block" hidden></pre>
      <div class="card-actions">
        <button class="btn-apply">Apply</button>
        <button class="btn-discard">Discard</button>
      </div>
      <div class="result-line"></div>
    </div>
  `;

  const pre = wrap.querySelector(".yaml-block");
  pre.textContent = yamlText;
  const toggle = wrap.querySelector(".yaml-toggle");
  toggle.addEventListener("click", () => {
    pre.hidden = !pre.hidden;
    toggle.textContent = pre.hidden ? "show yaml" : "hide yaml";
  });

  const applyBtn = wrap.querySelector(".btn-apply");
  const discardBtn = wrap.querySelector(".btn-discard");
  const resultLine = wrap.querySelector(".result-line");

  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    discardBtn.disabled = true;
    resultLine.textContent = "applying...";
    resultLine.className = "result-line";
    try {
      const res = await api("/api/apply", {
        method: "POST",
        body: JSON.stringify({
          yaml: yamlText,
          namespace,
          context: state.context,
          description: explanation,
        }),
      });
      if (res.ok) {
        wrap.classList.add("success");
        resultLine.className = "result-line ok";
        resultLine.textContent = `✓ applied — saved as ${res.file}`;
        loadCluster();
      } else {
        wrap.classList.add("error");
        resultLine.className = "result-line bad";
        resultLine.textContent = `✗ ${res.message}${res.stderr ? "\n" + res.stderr : ""}`;
        applyBtn.disabled = false;
        discardBtn.disabled = false;
      }
    } catch (e) {
      wrap.classList.add("error");
      resultLine.className = "result-line bad";
      resultLine.textContent = `✗ ${e.message}`;
      applyBtn.disabled = false;
      discardBtn.disabled = false;
    }
  });

  discardBtn.addEventListener("click", () => {
    applyBtn.disabled = true;
    discardBtn.disabled = true;
    resultLine.textContent = "discarded — nothing was sent to the cluster.";
  });

  els.chatScroll.appendChild(wrap);
  scrollChatToBottom();
}

function describeAction({ action, resourceKind, name, namespace, replicas }) {
  if (action === "delete") return `Delete ${resourceKind} "${name}" in namespace "${namespace}"`;
  if (action === "scale") return `Scale ${resourceKind} "${name}" in namespace "${namespace}" to ${replicas} replica${replicas === 1 ? "" : "s"}`;
  if (action === "restart") return `Restart ${resourceKind} "${name}" in namespace "${namespace}"`;
  return `${action} ${resourceKind} "${name}" in namespace "${namespace}"`;
}

function addActionCard({ action, resourceKind, name, namespace, replicas, explanation }) {
  const wrap = document.createElement("div");
  wrap.className = "card";
  const summary = describeAction({ action, resourceKind, name, namespace, replicas });
  const buttonLabel = action === "delete" ? "Delete" : action === "scale" ? "Scale" : "Restart";

  wrap.innerHTML = `
    <div class="card-body">
      <span class="ns-badge">ns/${escapeHtml(namespace)}</span>
      <span class="action-badge action-${escapeHtml(action)}">${escapeHtml(action)}</span>
      <p class="explanation">${escapeHtml(explanation || summary)}</p>
      <p class="action-summary">${escapeHtml(summary)}</p>
      <div class="card-actions">
        <button class="btn-apply ${action === "delete" ? "btn-danger" : ""}">${buttonLabel}</button>
        <button class="btn-discard">Discard</button>
      </div>
      <div class="result-line"></div>
    </div>
  `;

  const applyBtn = wrap.querySelector(".btn-apply");
  const discardBtn = wrap.querySelector(".btn-discard");
  const resultLine = wrap.querySelector(".result-line");

  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    discardBtn.disabled = true;
    resultLine.textContent = "running...";
    resultLine.className = "result-line";
    try {
      const res = await api("/api/action", {
        method: "POST",
        body: JSON.stringify({ action, kind: resourceKind, name, namespace, replicas, context: state.context }),
      });
      if (res.ok) {
        wrap.classList.add("success");
        resultLine.className = "result-line ok";
        resultLine.textContent = `✓ ${res.message || "done"}`;
        loadCluster();
      } else {
        wrap.classList.add("error");
        resultLine.className = "result-line bad";
        resultLine.textContent = `✗ ${res.message}${res.stderr ? "\n" + res.stderr : ""}`;
        applyBtn.disabled = false;
        discardBtn.disabled = false;
      }
    } catch (e) {
      wrap.classList.add("error");
      resultLine.className = "result-line bad";
      resultLine.textContent = `✗ ${e.message}`;
      applyBtn.disabled = false;
      discardBtn.disabled = false;
    }
  });

  discardBtn.addEventListener("click", () => {
    applyBtn.disabled = true;
    discardBtn.disabled = true;
    resultLine.textContent = "discarded — nothing was sent to the cluster.";
  });

  els.chatScroll.appendChild(wrap);
  scrollChatToBottom();
}

els.chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = els.chatInput.value.trim();
  if (!text) return;

  addUserMessage(text);
  els.chatInput.value = "";
  const submitBtn = els.chatForm.querySelector("button");
  submitBtn.disabled = true;
  addSystemNote("thinking...");
  const thinkingNote = els.chatScroll.lastElementChild;

  try {
    const res = await api("/api/generate", {
      method: "POST",
      body: JSON.stringify({ message: text, context: state.context }),
    });
    thinkingNote.remove();
    if (res.ok) {
      if (res.kind === "action") {
        addActionCard({
          action: res.action,
          resourceKind: res.resource_kind,
          name: res.name,
          namespace: res.namespace,
          replicas: res.replicas,
          explanation: res.explanation,
        });
      } else {
        addManifestCard({ namespace: res.namespace, yamlText: res.yaml, explanation: res.explanation });
      }
    } else {
      addSystemNote(`Couldn't interpret that request: ${res.error}`);
    }
  } catch (err) {
    thinkingNote.remove();
    addSystemNote(`Request failed: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
  }
});

els.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    els.chatForm.requestSubmit();
  }
});

/* ----------------------------------------------------- manifest drawer - */

function openDrawer() {
  els.drawer.classList.add("open");
  els.drawerOverlay.hidden = false;
  loadManifests();
}

function closeDrawerFn() {
  els.drawer.classList.remove("open");
  els.drawerOverlay.hidden = true;
}

els.manifestsBtn.addEventListener("click", openDrawer);
els.closeDrawer.addEventListener("click", closeDrawerFn);
els.drawerOverlay.addEventListener("click", closeDrawerFn);

async function loadManifests() {
  els.manifestList.innerHTML = `<p class="empty-drawer">loading...</p>`;
  try {
    const data = await api("/api/manifests");
    if (!data.manifests.length) {
      els.manifestList.innerHTML = `<p class="empty-drawer">No saved manifests yet.</p>`;
      return;
    }
    els.manifestList.innerHTML = "";
    data.manifests.forEach((m) => els.manifestList.appendChild(renderManifestItem(m)));
  } catch (e) {
    els.manifestList.innerHTML = `<p class="empty-drawer">Failed to load manifests: ${escapeHtml(e.message)}</p>`;
  }
}

function renderManifestItem(m) {
  const item = document.createElement("div");
  item.className = "manifest-item";
  item.innerHTML = `
    <div class="fname">${escapeHtml(m.filename)}</div>
    <div class="meta">${(m.size / 1024).toFixed(1)} KB · ${new Date(m.modified).toLocaleString()}</div>
    <div class="row">
      <button class="btn-view">View</button>
      <button class="btn-delete">Delete from cluster</button>
    </div>
    <pre hidden></pre>
  `;

  const pre = item.querySelector("pre");
  item.querySelector(".btn-view").addEventListener("click", async () => {
    if (!pre.hidden) { pre.hidden = true; return; }
    try {
      const data = await api(`/api/manifests/${encodeURIComponent(m.filename)}`);
      pre.textContent = data.yaml;
      pre.hidden = false;
    } catch (e) {
      pre.textContent = `Failed to load: ${e.message}`;
      pre.hidden = false;
    }
  });

  item.querySelector(".btn-delete").addEventListener("click", async () => {
    const btn = item.querySelector(".btn-delete");
    btn.disabled = true;
    btn.textContent = "deleting...";
    try {
      const data = await api(`/api/manifests/${encodeURIComponent(m.filename)}?${ctxParam()}`, { method: "DELETE" });
      if (data.ok) {
        item.remove();
        loadCluster();
      } else {
        btn.disabled = false;
        btn.textContent = "Delete from cluster";
        alert(`kubectl delete failed:\n${data.stderr || data.message}`);
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Delete from cluster";
      alert(`Request failed: ${e.message}`);
    }
  });

  return item;
}

/* ------------------------------------------------------------- init ---- */

(async function init() {
  await loadContexts();
  await loadCluster();
  setInterval(loadCluster, 6000);
})();
