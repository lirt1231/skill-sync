let token = "";
let state = null;
const selected = new Set();
const selectedImports = new Set();
const $ = (selector) => document.querySelector(selector);

async function getState() {
  const [tokenResult, stateResult] = await Promise.all([fetch("/api/token").then(r => r.json()), fetch("/api/state").then(r => r.json())]);
  token = tokenResult.token; state = stateResult; render();
}
async function action(path, body = {}) {
  toast("正在处理…");
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json", "X-Skill-Sync-Token":token}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { toast(data.error || "操作失败", true); return false; }
  state = data.state; render(); toast(resultMessage(data.result)); return true;
}
function resultMessage(result) { return result?.backup_path ? `已创建备份：${result.backup_path}` : "操作完成"; }
function render() {
  $("#setup").classList.toggle("hidden", state.initialized); $("#app").classList.toggle("hidden", !state.initialized);
  if (!state.initialized) return;
  const {doctor, status, preview} = state, skills = status.skills || [], agents = doctor.agents || [];
  const selectedCount = skills.filter(s => s.selected).length;
  $("#sync-label").textContent = label(preview.action); $("#sync-label").className = `status-pill ${preview.action}`;
  $("#sync-summary").textContent = preview.summary; $("#sync-note").textContent = preview.repo?.remote_checked ? "已检查远端状态" : "概览使用本地缓存；同步时会检查远端。";
  $("#sync").disabled = ["blocked", "conflict"].includes(preview.action); $("#sync").textContent = preview.action === "noop" ? "检查并修复链接" : "立即同步";
  $("#cards").innerHTML = [["全部 Skills", skills.length],["正在同步", selectedCount],["已检测 Agent", agents.filter(a=>a.detected).length],["待处理", preview.issues.length]].map(([n,v]) => `<article class="card"><strong>${v}</strong><span>${n}</span></article>`).join("");
  renderIssues(preview.issues); renderSkills(skills); renderAgents(agents); renderImports(state.import_candidates || []);
}
function renderIssues(issues) {
  $("#issue-list").innerHTML = issues.length ? issues.map(issue => `<article class="issue"><span class="issue-icon">!</span><div><strong>${escapeHtml(issueTitle(issue))}</strong><p>${escapeHtml(issue.detail || issue.skill || "需要手动检查")}</p></div>${issue.skill ? `<button onclick="backupSkill('${escapeJs(issue.skill)}')">创建本地备份</button>` : ""}</article>`).join("") : `<p class="empty">没有需要处理的问题。</p>`;
}
function renderSkills(skills) {
  const query = $("#search").value.toLowerCase(); const shown = skills.filter(s => s.name.toLowerCase().includes(query));
  for (const name of [...selected]) if (!skills.some(s => s.name === name)) selected.delete(name);
  $("#select-all").textContent = shown.length && shown.every(s => selected.has(s.name)) ? "取消选择可见项" : "全选可见项";
  $("#skill-list").innerHTML = shown.map(skill => `<article class="skill-row"><input type="checkbox" aria-label="选择 ${escapeHtml(skill.name)}" ${selected.has(skill.name)?"checked":""} onchange="toggle('${escapeJs(skill.name)}',this.checked)"><div class="skill-main"><strong>${escapeHtml(skill.name)}</strong><span class="badge ${skill.selected ? "good" : "muted"}">${skill.selected ? (skill.changed_local ? "本地待推送" : "已选择") : "未同步"}</span><details><summary>查看位置与详情</summary><code>${escapeHtml(skill.local_path)}</code></details></div><div class="row-actions">${skill.selected ? `<button onclick="action('/api/deselect',{skills:['${escapeJs(skill.name)}']})">取消同步</button>` : `<button onclick="action('/api/select',{skills:['${escapeJs(skill.name)}']})">加入同步</button>`}<button onclick="backupSkill('${escapeJs(skill.name)}')">备份</button></div></article>`).join("") || `<p class="empty">没有符合条件的 Skill。</p>`;
}
function renderAgents(agents) {
  $("#agent-list").innerHTML = agents.map(agent => `<article class="agent-card"><div><strong>${escapeHtml(agent.display_name)}</strong><p>${agent.detected ? escapeHtml(agent.skills_dir) : "未检测到此 Agent"}</p></div><span class="badge ${agent.detected && agent.enabled ? "good" : "muted"}">${agent.enabled ? (agent.detected ? "已启用" : "等待检测") : "已停用"}</span><button ${!agent.detected ? "disabled" : ""} onclick="action('/api/agent',{agent:'${escapeJs(agent.name)}',enabled:${!agent.enabled}})">${agent.enabled ? "停用" : "启用"}</button></article>`).join("");
}
function renderImports(items) {
  const sources = ["codex", "claude", "workbuddy"];
  for (const key of [...selectedImports]) if (!items.some(item => importKey(item) === key)) selectedImports.delete(key);
  $("#imports").innerHTML = sources.map(agent => {
    const group = items.filter(item => item.agent === agent);
    const importable = group.filter(item => item.state === "importable" || item.state === "same");
    const title = {codex:"Codex", claude:"Claude Code", workbuddy:"WorkBuddy"}[agent];
    return `<section class="import-group"><div class="import-group-head"><strong>${title}</strong><button ${importable.length ? "" : "disabled"} onclick="importSelected('${agent}')">导入所选</button></div>${group.map(item => `<article class="import-row"><input type="checkbox" aria-label="选择 ${escapeHtml(item.name)}" ${selectedImports.has(importKey(item)) ? "checked" : ""} ${item.state === "conflict" ? "disabled" : ""} onchange="toggleImport('${escapeJs(item.agent)}','${escapeJs(item.name)}',this.checked)"><div><strong>${escapeHtml(item.name)}</strong><p>${escapeHtml(item.path)}</p></div><span class="badge ${item.state === "conflict" ? "warn" : "muted"}">${escapeHtml(item.state)}</span></article>`).join("") || `<p class="empty">没有可导入的 Skill。</p>`}</section>`;
  }).join("");
}
function toggle(name, checked) { checked ? selected.add(name) : selected.delete(name); }
function importKey(item) { return `${item.agent}\u0000${item.name}`; }
function toggleImport(agent, name, checked) { const key = `${agent}\u0000${name}`; checked ? selectedImports.add(key) : selectedImports.delete(key); }
function selectedNames() { if (!selected.size) { toast("请先选择至少一个 Skill", true); return null; } return [...selected]; }
function bulk(path) { const skills = selectedNames(); return skills && action(path, {skills}); }
async function importSelected(agent) { const skills = [...selectedImports].filter(key => key.startsWith(`${agent}\u0000`)).map(key => key.split("\u0000")[1]); if (!skills.length) return toast("请先选择要导入的 Skill", true); if (confirm(`从 ${agent} 导入以下 ${skills.length} 个 Skill？\n\n导入后，原目录将被全局 Skill 的安全链接替换。`)) { if (await action("/api/import", {skills, agent})) skills.forEach(name => selectedImports.delete(`${agent}\u0000${name}`)); } }
async function backupSkill(skill) { await action("/api/backup", {skill}); }
async function deleteSelected() { const skills = selectedNames(); if (skills && confirm(`永久删除以下全局 Skill 及其安全链接？\n\n${skills.join("\n")}`) && await action("/api/delete", {skills})) selected.clear(); }
function label(value) { return ({pull:"需要拉取", push:"需要推送", "repair-links":"需要修复链接", conflict:"需要手动合并", blocked:"需要先处理", noop:"一切正常"})[value] || value; }
function issueTitle(issue) { return ({"content-conflict":"本地与远端同时有修改", "dirty-repository":"同步仓库存在额外改动", missing:"链接缺失", conflict:"链接冲突", partial:"链接不完整"})[issue.type] || issue.type; }
function toast(message, error=false) { const el=$("#toast"); el.textContent=message; el.className=`show ${error?"error":""}`; setTimeout(()=>el.className="",3500); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]); }
function escapeJs(value) { return String(value).replace(/['\\]/g,"\\$&"); }
$("#setup-form").onsubmit = async event => { event.preventDefault(); const data=Object.fromEntries(new FormData(event.currentTarget)); await action("/api/init", data); };
$("#sync").onclick = () => action("/api/sync"); $("#refresh").onclick = getState; $("#search").oninput = () => renderSkills(state.status.skills || []);
$("#select-all").onclick = () => { const shown = state.status.skills.filter(s=>s.name.toLowerCase().includes($("#search").value.toLowerCase())); const allSelected = shown.length > 0 && shown.every(s => selected.has(s.name)); shown.forEach(s => allSelected ? selected.delete(s.name) : selected.add(s.name)); $("#select-all").textContent = allSelected ? "全选可见项" : "取消选择可见项"; renderSkills(state.status.skills); };
$("#select-selected").onclick = () => bulk("/api/select"); $("#deselect-selected").onclick = () => bulk("/api/deselect"); $("#link-selected").onclick = () => bulk("/api/link"); $("#copy-selected").onclick = () => { const skills = selectedNames(); return skills && action("/api/copy", {skills, agents:[$("#copy-agent").value]}); }; $("#delete-selected").onclick = deleteSelected;
getState().catch(error => toast(error.message, true));
