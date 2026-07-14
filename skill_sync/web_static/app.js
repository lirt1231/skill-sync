let token = "";
let state = null;
let activeView = "skills";
let activeImportAgent = "codex";
let detailSkill = null;
const selected = new Set();
const selectedImports = new Set();
const $ = selector => document.querySelector(selector);

async function getState() {
  const [tokenResult, stateResult] = await Promise.all([fetch("/api/token").then(r => r.json()), fetch("/api/state").then(r => r.json())]);
  token = tokenResult.token; state = stateResult; render();
}

async function action(path, body = {}) {
  toast("正在处理…");
  const response = await fetch(path, {method:"POST",headers:{"Content-Type":"application/json","X-Skill-Sync-Token":token},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { toast(data.error || "操作失败", true); return false; }
  state = data.state; render(); toast(data.result?.backup_path ? `已备份到 ${data.result.backup_path}` : "操作完成"); return true;
}

function render() {
  $("#setup").classList.toggle("hidden", state.initialized); $("#app").classList.toggle("hidden", !state.initialized);
  if (!state.initialized) return;
  const preview = state.preview;
  $("#sync-label").textContent = label(preview.action);
  $("#sync-summary").textContent = previewSummary(preview.action);
  $("#sync").disabled = ["blocked","conflict"].includes(preview.action);
  renderIssues(preview.issues || []);
  renderSkills(state.status.skills || []);
  renderAgents(state.doctor.agents || []);
  renderImports(state.import_candidates || []);
  renderDetail();
  switchView(activeView);
}

function switchView(view) {
  activeView = view;
  document.querySelectorAll(".view").forEach(item => item.classList.toggle("active", item.id === `view-${view}`));
  document.querySelectorAll(".nav-item[data-view]").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  $("#detail-drawer").classList.toggle("hidden", view !== "skills" || !detailSkill);
}

function renderIssues(issues) {
  const box = $("#issue-list"); box.classList.toggle("hidden", !issues.length);
  box.innerHTML = issues.map(issue => `<div><i class="ri-error-warning-line"></i><strong>${escapeHtml(issueTitle(issue))}</strong><span>${escapeHtml(issue.detail || issue.skill || "需要检查")}</span></div>`).join("");
}

function visibleSkills(skills) { const query=$("#search").value.trim().toLowerCase(); return skills.filter(skill => skill.name.toLowerCase().includes(query)); }

function renderSkills(skills) {
  for (const name of [...selected]) if (!skills.some(skill => skill.name === name)) selected.delete(name);
  const shown = visibleSkills(skills);
  $("#skill-list").innerHTML = shown.map(skill => {
    const agents = state.doctor.agents || [];
    const coverage = agents.map(agent => `<i class="agent-dot ${agentStateClass(skill.agents?.[agent.name])}" title="${escapeHtml(agent.display_name)}: ${escapeHtml(skill.agents?.[agent.name] || "未知")}"></i>`).join("");
    return `<article class="skill-row ${detailSkill === skill.name ? "focused" : ""}" onclick="openDetail('${escapeJs(skill.name)}')"><input type="checkbox" aria-label="选择 ${escapeHtml(skill.name)}" ${selected.has(skill.name)?"checked":""} onclick="event.stopPropagation()" onchange="toggle('${escapeJs(skill.name)}',this.checked)"><span class="file-icon"><i class="ri-file-text-line"></i></span><strong>${escapeHtml(skill.name)}</strong><span class="coverage">${coverage}</span><span class="row-status ${skill.changed_local ? "pending" : ""}">${skill.changed_local ? "本地待推送" : (skill.selected ? "已同步" : "未加入同步")}</span><button class="icon-button" aria-label="查看详情" onclick="event.stopPropagation();openDetail('${escapeJs(skill.name)}')"><i class="ri-more-2-fill"></i></button></article>`;
  }).join("") || `<p class="empty">没有符合条件的 Skill</p>`;
  const allSelected = shown.length > 0 && shown.every(skill => selected.has(skill.name));
  $("#select-all-checkbox").checked = allSelected;
  $("#select-all").textContent = allSelected ? "取消选择可见项" : "全选可见项";
  const picked=skills.filter(skill=>selected.has(skill.name));
  const pickedAllSynced=picked.length>0&&picked.every(skill=>skill.selected);
  $("#select-selected").innerHTML=pickedAllSynced?'<i class="ri-subtract-line"></i><span>取消同步</span>':'<i class="ri-add-circle-line"></i><span>加入同步</span>';
  $("#select-selected").dataset.mode=pickedAllSynced?"deselect":"select";
  updateSelectionBar();
}

function renderAgents(agents) {
  $("#agent-list").innerHTML = agents.map(agent => `<article class="agent-row"><span class="agent-icon"><i class="${agentIcon(agent.name)}"></i></span><div><strong>${escapeHtml(agent.display_name)}</strong><p>${agent.detected ? escapeHtml(agent.skills_dir) : "未检测到此 Agent"}</p></div><span class="connection-state ${agent.detected && agent.enabled ? "online" : ""}"><i></i>${agent.enabled ? (agent.detected ? "已连接" : "等待检测") : "已停用"}</span><button ${!agent.detected ? "disabled" : ""} onclick="action('/api/agent',{agent:'${escapeJs(agent.name)}',enabled:${!agent.enabled}})">${agent.enabled ? "停用" : "启用"}</button></article>`).join("");
}

function renderImports(items) {
  const sources=["codex","claude","workbuddy"], titles={codex:"Codex",claude:"Claude Code",workbuddy:"WorkBuddy"};
  for (const key of [...selectedImports]) if (!items.some(item => importKey(item) === key)) selectedImports.delete(key);
  $("#import-tabs").innerHTML = sources.map(agent => `<button class="source-tab ${activeImportAgent===agent?"active":""}" onclick="setImportAgent('${agent}')"><i class="${agentIcon(agent)}"></i><span>${titles[agent]}</span><b>${items.filter(item=>item.agent===agent).length}</b></button>`).join("");
  const group=items.filter(item=>item.agent===activeImportAgent);
  $("#imports").innerHTML = group.map(item => `<article class="import-row"><input type="checkbox" aria-label="选择 ${escapeHtml(item.name)}" ${selectedImports.has(importKey(item))?"checked":""} ${item.state==="conflict"?"disabled":""} onchange="toggleImport('${escapeJs(item.agent)}','${escapeJs(item.name)}',this.checked)"><span class="file-icon"><i class="ri-file-text-line"></i></span><strong>${escapeHtml(item.name)}</strong><code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code>${item.state==="conflict"?'<span class="conflict-label"><i class="ri-error-warning-line"></i>名称冲突</span>':""}</article>`).join("") || `<p class="empty">这个 Agent 中没有可导入的 Skill</p>`;
  const selectable=group.filter(item=>item.state!=="conflict");
  $("#select-all-imports").checked=selectable.length>0 && selectable.every(item=>selectedImports.has(importKey(item)));
  updateImportBar();
}

function renderDetail() {
  const skill=(state.status.skills||[]).find(item=>item.name===detailSkill);
  if (!skill) { detailSkill=null; $("#detail-drawer").classList.add("hidden"); return; }
  $("#detail-drawer").classList.toggle("hidden", activeView!=="skills");
  $("#detail-name").textContent=skill.name;
  $("#detail-status").innerHTML=`<i></i>${skill.changed_local?"本地有修改":skill.selected?"已同步到 Agent":"未加入同步"}`;
  $("#detail-description").textContent=skill.description||"暂无 description";
  $("#detail-sync").textContent=skill.selected?"已加入同步":"仅保存在本地";
  $("#detail-path").textContent=skill.local_path;
  $("#detail-agents").innerHTML=(state.doctor.agents||[]).map(agent=>`<div><span><i class="agent-dot ${agentStateClass(skill.agents?.[agent.name])}"></i>${escapeHtml(agent.display_name)}</span><b>${agentStateLabel(skill.agents?.[agent.name])}</b></div>`).join("");
}

function toggle(name,checked){checked?selected.add(name):selected.delete(name);renderSkills(state.status.skills||[]);}
function toggleVisibleSkills(){const shown=visibleSkills(state.status.skills||[]);const allSelected=shown.length>0&&shown.every(skill=>selected.has(skill.name));shown.forEach(skill=>allSelected ? selected.delete(skill.name) : selected.add(skill.name));renderSkills(state.status.skills||[]);}
function clearSelection(){selected.clear();renderSkills(state.status.skills||[]);}
function updateSelectionBar(){$("#selection-count").textContent=selected.size;$("#selection-bar").classList.toggle("hidden",!selected.size);}
function selectedNames(){if(!selected.size){toast("请先选择至少一个 Skill",true);return null;}return [...selected];}
function bulk(path){const skills=selectedNames();return skills&&action(path,{skills});}
function toggleSelectedSync(){return bulk($("#select-selected").dataset.mode==="deselect"?"/api/deselect":"/api/select");}
function openDetail(name){detailSkill=name;renderSkills(state.status.skills||[]);renderDetail();}
function closeDetail(){detailSkill=null;$("#detail-drawer").classList.add("hidden");renderSkills(state.status.skills||[]);}
function setImportAgent(agent){activeImportAgent=agent;renderImports(state.import_candidates||[]);}
function importKey(item){return `${item.agent}\u0000${item.name}`;}
function toggleImport(agent,name,checked){const key=`${agent}\u0000${name}`;checked?selectedImports.add(key):selectedImports.delete(key);renderImports(state.import_candidates||[]);}
function toggleAllImports(){const group=(state.import_candidates||[]).filter(item=>item.agent===activeImportAgent&&item.state!=="conflict");const all=group.length>0&&group.every(item=>selectedImports.has(importKey(item)));group.forEach(item=>all?selectedImports.delete(importKey(item)):selectedImports.add(importKey(item)));renderImports(state.import_candidates||[]);}
function updateImportBar(){const count=[...selectedImports].filter(key=>key.startsWith(`${activeImportAgent}\u0000`)).length;$("#import-count").textContent=count;$("#import-bar").classList.toggle("hidden",!count);}
async function importSelected(){const skills=[...selectedImports].filter(key=>key.startsWith(`${activeImportAgent}\u0000`)).map(key=>key.split("\u0000")[1]);if(!skills.length)return; if(confirm(`将 ${skills.length} 个 Skill 从 ${activeImportAgent} 导入全局库？`)){if(await action("/api/import",{skills,agent:activeImportAgent}))selectedImports.clear();}}
async function backupSkill(skill){await action("/api/backup", {skill});}
async function deleteSelected(){const skills=selectedNames();if(skills&&confirm(`永久删除以下全局 Skill？\n\n${skills.join("\n")}`)&&await action("/api/delete",{skills}))selected.clear();}
function label(value){return({pull:"需要拉取",push:"需要推送","repair-links":"需要修复链接",conflict:"需要手动合并",blocked:"需要先处理",noop:"所有 Agent 已同步"})[value]||value;}
function previewSummary(value){return({pull:"远端有新的 Skill 变更可拉取。",push:"本地变更等待推送到远端。","repair-links":"Skill 内容一致，部分 Agent 链接需要修复。",conflict:"本地和远端均有修改，需要手动合并。",blocked:"同步仓库需要先处理。",noop:"所有受管 Skill 状态一致。"})[value]||"当前状态已更新。";}
function issueTitle(issue){return({"content-conflict":"本地与远端同时有修改","dirty-repository":"同步仓库存在额外改动",missing:"链接缺失",conflict:"链接冲突",partial:"链接不完整"})[issue.type]||issue.type;}
function agentStateClass(value){return["linked","copied"].includes(value)?"ok":value==="disabled"?"disabled":value==="missing"?"missing":"warn";}
function agentStateLabel(value){return value==="linked"?"已同步":value==="copied"?"本地副本":value==="disabled"?"已停用":value==="missing"?"未连接":"需检查";}
function agentIcon(name){return({codex:"ri-code-box-line",claude:"ri-command-line",workbuddy:"ri-robot-2-line",kimi:"ri-sparkling-line"})[name]||"ri-apps-line";}
function toast(message,error=false){const el=$("#toast");el.textContent=message;el.className=`show ${error?"error":""}`;setTimeout(()=>el.className="",3500);}
function escapeHtml(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
function escapeJs(value){return String(value).replace(/['\\]/g,"\\$&");}

document.querySelectorAll(".nav-item[data-view]").forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$("#setup-form").onsubmit=async event=>{event.preventDefault();await action("/api/init",Object.fromEntries(new FormData(event.currentTarget)));};
$("#sync").onclick=()=>action("/api/sync");$("#refresh").onclick=getState;
$("#search-toggle").onclick=()=>{$("#search-wrap").classList.toggle("collapsed");if(!$("#search-wrap").classList.contains("collapsed"))$("#search").focus();};
$("#search").oninput=()=>renderSkills(state.status.skills||[]);
$("#select-all-checkbox").onchange=toggleVisibleSkills;$("#select-all").onclick=toggleVisibleSkills;$("#clear-selection").onclick=clearSelection;
$("#select-selected").onclick=toggleSelectedSync;$("#deselect-selected").onclick=()=>bulk("/api/deselect");$("#link-selected").onclick=()=>bulk("/api/link");
$("#copy-selected").onclick=()=>{const skills=selectedNames();return skills&&action("/api/copy",{skills,agents:[$("#copy-agent").value]});};$("#delete-selected").onclick=deleteSelected;
$("#close-detail").onclick=closeDetail;$("#detail-backup").onclick=()=>detailSkill&&backupSkill(detailSkill);
$("#select-all-imports").onchange=toggleAllImports;$("#clear-imports").onclick=()=>{selectedImports.clear();renderImports(state.import_candidates||[]);};$("#import-selected").onclick=importSelected;
getState().catch(error=>toast(error.message,true));
