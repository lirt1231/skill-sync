let token = "";
let state = null;
const selectedForDelete = new Set();
const $ = (selector) => document.querySelector(selector);

async function getState() {
  const [tokenResult, stateResult] = await Promise.all([
    fetch("/api/token").then((response) => response.json()),
    fetch("/api/state").then((response) => response.json()),
  ]);
  token = tokenResult.token;
  state = stateResult;
  render();
}

async function action(path, body = {}) {
  toast("正在处理…");
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Skill-Sync-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    toast(data.error || "操作失败");
    return false;
  }
  state = data.state;
  render();
  toast("操作完成");
  return true;
}

function render() {
  const diagnosis = state.doctor;
  const syncState = state.status;
  const skills = syncState.skills || [];
  const agents = diagnosis.agents.filter((agent) => agent.detected);
  const selected = skills.filter((skill) => skill.selected).length;
  const imports = state.import_candidates || [];
  for (const name of [...selectedForDelete]) {
    if (!skills.some((skill) => skill.name === name)) selectedForDelete.delete(name);
  }

  $("#cards").innerHTML = [
    ["全局 Skills", skills.length],
    ["已选择同步", selected],
    ["已检测 Agent", agents.length],
    ["待处理问题", diagnosis.issues.length],
  ].map(([label, value]) => `<article class="card"><strong>${value}</strong><span>${label}</span></article>`).join("");

  $("#imports").innerHTML = imports.map((item) =>
    `<div class="issue"><strong>${escapeHtml(item.name)}</strong> · ${escapeHtml(item.agent)} · ` +
    `<span class="badge ${item.state === "conflict" ? "warn" : ""}">${escapeHtml(item.state)}</span>` +
    (item.state !== "conflict" ? `<button class="link-action" onclick="action('/api/import',{skills:['${escapeJs(item.name)}'],agent:'${escapeJs(item.agent)}'})">一键导入</button>` : "") +
    `</div>`
  ).join("") || "<p>Codex 和 Claude Code 中没有待导入的本地 Skill。</p>";

  $("#head").innerHTML = `<tr><th></th><th>Skill</th><th>同步状态</th>${agents.map((agent) => `<th>${agent.display_name}</th>`).join("")}</tr>`;
  $("#body").innerHTML = skills.map((skill) =>
    `<tr><td><input type="checkbox" aria-label="选择 ${escapeHtml(skill.name)}" ${selectedForDelete.has(skill.name) ? "checked" : ""} onchange="toggleDelete('${escapeJs(skill.name)}',this.checked)"></td>` +
    `<td><span class="skill-name">${escapeHtml(skill.name)}</span><br><small>${escapeHtml(skill.local_path)}</small></td>` +
    `<td>${syncCell(skill)}</td>${agents.map((agent) => agentCell(skill, agent)).join("")}</tr>`
  ).join("") || '<tr><td colspan="9">~/.agents/skills 中还没有 Skill</td></tr>';

  $("#issues").innerHTML = diagnosis.issues.map((issue) =>
    `<div class="issue">${escapeHtml(issue.type)} · ${escapeHtml(issue.skill || "")} ${escapeHtml(issue.agent || issue.path || "")}</div>`
  ).join("") || "<p>没有发现冲突。</p>";
}

function syncCell(skill) {
  if (!skill.selected) return `<span class="badge off">未选择</span><button class="link-action" onclick="action('/api/select',{skills:['${escapeJs(skill.name)}']})">选择</button>`;
  return `<span class="badge ${skill.changed_local ? "warn" : ""}">${skill.changed_local ? "本地修改" : "已同步"}</span><button class="link-action" onclick="action('/api/deselect',{skills:['${escapeJs(skill.name)}']})">取消</button>`;
}

function agentCell(skill, agent) {
  if (!skill.selected) return '<td><span class="badge off">—</span></td>';
  const value = skill.agents[agent.name];
  const linked = value === "linked";
  return `<td><span class="badge ${linked ? "" : "off"}">${linked ? "已链接" : value}</span><button class="link-action" onclick="action('/api/${linked ? "unlink" : "link"}',{skills:['${escapeJs(skill.name)}'],agents:['${escapeJs(agent.name)}']})">${linked ? "移除" : "链接"}</button></td>`;
}

function toggleDelete(name, checked) {
  if (checked) selectedForDelete.add(name); else selectedForDelete.delete(name);
}

async function deleteSelected() {
  const names = [...selectedForDelete];
  if (!names.length) return toast("请先选择要删除的全局 Skill");
  if (!confirm(`永久删除以下全局 Skill 及其 Agent 链接？\n\n${names.join("\n")}`)) return;
  if (await action("/api/delete", { skills: names })) selectedForDelete.clear();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  setTimeout(() => element.classList.remove("show"), 2500);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

function escapeJs(value) {
  return String(value).replace(/['\\]/g, "\\$&");
}

$("#sync").onclick = () => action("/api/sync");
$("#link-all").onclick = () => action("/api/link");
$("#refresh").onclick = getState;
$("#delete-selected").onclick = deleteSelected;
getState().catch((error) => toast(error.message));
