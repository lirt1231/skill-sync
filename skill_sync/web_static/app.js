let token = "";
let state = null;
const WEB_CONTEXT_STORAGE_KEY = "skill-sync:web-context:v1";
const restoredUiContext = readUiContext();
let activeView = restoredUiContext.activeView;
let activeImportAgent = "codex";
const DETAIL_QUERY_PARAM = "detail";
let detailSkill = detailTargetFromLocation();
let detailNeedsFocus = Boolean(detailSkill);
let detailReturnTarget = null;
const inventoryFilters = {...restoredUiContext.filters};
const selected = new Set(restoredUiContext.selected);
const selectedImports = new Set();
const loadedViews = new Set();
const inFlightOperations = new Map();
let mutationInFlight = null;
let stateGeneration = 0;
let toastTimer = null;
let pendingMutation = null;
const $ = selector => document.querySelector(selector);

function viewsFor(view) {
  if (view === "skills") return ["summary", "inventory", "agents"];
  if (view === "agents") return ["agents"];
  return ["import-candidates"];
}

function stateUrl(views) {
  const params = new URLSearchParams();
  views.forEach(view => params.append("view", view));
  return `/api/state?${params}`;
}

function mergeState(partial) {
  if (!state || state.initialized !== partial.initialized) {
    state = {initialized: partial.initialized, status: {skills: []}, doctor: {agents: [], matrix: [], issues: []}, import_candidates: []};
    loadedViews.clear();
  }
  state = {...state, ...partial};
  (partial.loaded_views || []).forEach(view => loadedViews.add(view));
  hydrateSkillAgents();
}

function hydrateSkillAgents() {
  const skills = state?.status?.skills || [];
  const agents = state?.doctor?.agents || [];
  const matrix = new Map((state?.doctor?.matrix || []).map(item => [`${item.skill}\u0000${item.agent}`, item.state]));
  skills.forEach(skill => {
    skill.agents = Object.fromEntries(agents.map(agent => [agent.name, matrix.get(`${skill.name}\u0000${agent.name}`) || "not-detected"]));
  });
}

async function loadViews(views, force = false, generation = stateGeneration) {
  if (mutationInFlight) return false;
  const requested = force ? views : views.filter(view => !loadedViews.has(view));
  if (!requested.length) return true;
  const response = await fetch(stateUrl(requested));
  if (generation !== stateGeneration) return false;
  const partial = await response.json();
  if (generation !== stateGeneration) return false;
  if (!response.ok) throw new Error(partial.error || "状态加载失败");
  mergeState(partial); render();
  return true;
}

async function loadView(view, force = false, generation = stateGeneration) {
  if (view === "skills") {
    // Inventory is intentionally independent so a large Agent diagnosis does
    // not delay the first useful list paint.
    await loadViews(["inventory"], force, generation);
    if (generation === stateGeneration && state?.initialized) await loadViews(["summary", "agents"], force, generation);
    return;
  }
  await loadViews(viewsFor(view), force, generation);
}

async function loadActiveView(force = false) {
  await loadView(activeView, force);
}

async function ensureToken() {
  if (token) return token;
  const response = await fetch("/api/token");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法获取请求令牌");
  token = data.token;
  return token;
}

function operationInfo(path, body) {
  const count = body.skills?.length || 0;
  const info = {
    "/api/init": ["init", "正在连接…", "同步仓库已连接", "连接同步仓库失败"],
    "/api/sync": ["sync", "正在同步…", "同步已完成", "同步失败"],
    "/api/select": ["select", "正在加入同步…", `已将 ${count} 个 Skill 加入同步`, "加入同步失败"],
    "/api/deselect": ["deselect", "正在取消同步…", `已将 ${count} 个 Skill 取消同步`, "取消同步失败"],
    "/api/link": ["link", "正在修复链接…", `已修复 ${count} 个 Skill 的链接`, "修复链接失败"],
    "/api/copy": ["copy", "正在复制…", `已复制 ${count} 个 Skill`, "复制 Skill 失败"],
    "/api/delete": ["delete", "正在删除…", `已删除 ${count} 个全局 Skill`, "删除全局 Skill 失败"],
    "/api/import": [`import:${body.agent}`, "正在导入…", `已从 ${body.agent} 导入 ${count} 个 Skill`, `从 ${body.agent} 导入失败`],
    "/api/agent": [`agent:${body.agent}`, body.enabled ? "正在启用…" : "正在停用…", `${body.agent} 已${body.enabled ? "启用" : "停用"}`, `${body.enabled ? "启用" : "停用"} ${body.agent} 失败`],
    "/api/backup": [`backup:${body.skill}`, "正在备份…", `${body.skill} 的本地备份已创建`, `备份 ${body.skill} 失败`],
  }[path];
  if (!info) return {key:path, running:"正在处理…", success:"操作已完成", failure:"操作失败"};
  return {key:info[0], running:info[1], success:info[2], failure:info[3]};
}

function runOperation(key, task, mutation = false) {
  if (inFlightOperations.has(key)) return inFlightOperations.get(key);
  if (mutation && hasBusyOperation()) { toast("请等待当前操作完成"); return Promise.resolve(false); }
  inFlightOperations.set(key, null);
  setOperationBusy(key, true);
  const promise = (async () => {
    try { return await task(); }
    finally {
      if (mutationInFlight === promise) mutationInFlight = null;
      inFlightOperations.delete(key);
      setOperationBusy(key, false);
    }
  })();
  inFlightOperations.set(key, promise);
  if (mutation) mutationInFlight = promise;
  return promise;
}

function setOperationBusy(key, busy) {
  if (!busy) inFlightOperations.delete(key);
  try {
    if (state?.initialized) {
      renderSkills(state.status?.skills || []);
      renderAgents(state.doctor?.agents || []);
      renderImports(state.import_candidates || []);
      renderDetail();
    }
  } catch (_) {
    // A stale or malformed response must never leave the operation lock set.
  }
  try { renderOperationStates(); } catch (_) {}
}

function isBusy(key) { return inFlightOperations.has(key); }
function hasBusyOperation() { return inFlightOperations.size > 0; }
function selectionOperationBusy() { return ["select","deselect","link","copy","delete"].some(isBusy); }
function importOperationBusy() { return isBusy(`import:${activeImportAgent}`); }

async function getState(announce = true) {
  if (mutationInFlight) return false;
  const view = activeView;
  return runOperation("refresh", async () => {
    const generation = ++stateGeneration;
    if (announce) toast(`正在刷新${viewName(view)}…`);
    try {
      await ensureToken();
      await loadView(view, true, generation);
      if (announce) toast(`${viewName(view)}状态已刷新`);
      return true;
    } catch (error) {
      if (!state) $("#load-failure").classList.remove("hidden");
      toast(`刷新${viewName(view)}失败：${error.message}`, true);
      return false;
    }
  });
}

async function action(path, body = {}, options = {}) {
  const operation = operationInfo(path, body);
  const requestView = activeView;
  return runOperation(operation.key, async () => {
    stateGeneration += 1;
    toast(operation.running);
    try {
      let response;
      try {
        response = await fetch(path, {method:"POST",headers:{"Content-Type":"application/json","X-Skill-Sync-Token":token},body:JSON.stringify({...body, views: viewsFor(requestView)})});
      } catch (_) {
        throw new Error("操作结果未知，请刷新核验");
      }
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error("操作结果未知，请刷新核验"); }
      if (!response.ok) {
        if (data.mutation_applied) throw new Error(`${operation.success}，但状态刷新失败，请刷新核验`);
        if (response.status >= 500) throw new Error("操作结果未知，请刷新核验");
        throw new Error(data.error || operation.failure);
      }
      loadedViews.clear();
      try { mergeState(data.state); render(); }
      catch (_) { throw new Error("操作已执行，但界面更新失败，请刷新核验"); }
      const success = data.result?.backup_path ? `${operation.success}：${data.result.backup_path}` : operation.success;
      const outcome={ok:true,message:success,result:data.result||{},unknown:false};
      if(typeof options.captureResult==="function")options.captureResult(outcome);
      toast(success); return true;
    } catch (error) {
      const unknown = error.message.includes("刷新核验") ? error.message : `${operation.failure}：${error.message}`;
      if(typeof options.captureResult==="function")options.captureResult({ok:false,message:unknown,error:error.message,unknown:error.message.includes("刷新核验")});
      toast(unknown, true);
      return false;
    }
  }, true);
}

function mutationPlanRequest(operation, body) {
  if (operation === "sync") return body.skills ? {skills:[...body.skills]} : {};
  if (operation === "import") return {skills:[...(body.skills||[])],agent:body.agent};
  if (operation === "agent") return {agent:body.agent,enabled:body.enabled};
  if (operation === "link-repair") return {skills:[...(body.skills||[])],...(body.agents?{agents:[...body.agents]}:{})};
  if (operation === "delete") return {skills:[...(body.skills||[])]};
  throw new Error(`不支持的计划操作：${operation}`);
}

async function fetchMutationPlan(operation, request) {
  await ensureToken();
  let response;
  try {
    response=await fetch("/api/plan",{method:"POST",headers:{"Content-Type":"application/json","X-Skill-Sync-Token":token},body:JSON.stringify({operation,request})});
  } catch (_) {
    throw new Error("无法连接 Skill Sync，请稍后重新规划");
  }
  let data;
  try { data=await response.json(); }
  catch (_) { throw new Error("规划结果无法解析，请稍后重试"); }
  if(!response.ok)throw new Error(data.error||"无法安全规划此操作");
  if(data?.schema_version!==1||data.operation!==operation||typeof data.can_execute!=="boolean"||!data.request||typeof data.request!=="object")throw new Error("规划结果格式不兼容，请升级 Skill Sync");
  return data;
}

function mutationOrigin(origin,operation,body){
  if(origin?.id)return {kind:"id",value:origin.id};
  if(origin?.dataset?.agentAction)return {kind:"agent",value:origin.dataset.agentAction};
  if(operation==="agent"&&body.agent)return {kind:"agent",value:body.agent};
  return {kind:"fallback",value:"search"};
}

function restoreMutationFocus(origin){
  let target=null;
  if(origin?.kind==="id")target=$(`#${origin.value}`);
  if(origin?.kind==="agent")target=[...document.querySelectorAll("[data-agent-action]")].find(item=>item.dataset.agentAction===origin.value)||null;
  (target||$(activeView==="imports"?"#import-selected":"#search")||$("#refresh"))?.focus();
}

function setMutationLayer(open){
  const layer=$("#mutation-layer");if(!layer)return;
  layer.classList.toggle("hidden",!open);layer.setAttribute("aria-hidden",String(!open));
  if($("#app"))$("#app").inert=open;
  if($("#setup"))$("#setup").inert=open;
}

function mutationFingerprint(plan){
  const keys=["schema_version","operation","request","status","can_execute","summary","targets","steps","conflicts","blockers","warnings","effects","backup","recovery","details"];
  return JSON.stringify(Object.fromEntries(keys.map(key=>[key,plan?.[key]])));
}

function mutationTitle(operation,request){
  if(operation==="sync")return "确认同步";
  if(operation==="import")return `确认从 ${request.agent||"Agent"} 导入`;
  if(operation==="agent")return request.enabled?`启用 ${request.agent}`:`停用 ${request.agent}`;
  if(operation==="link-repair")return "确认修复 Agent 链接";
  if(operation==="delete")return "确认永久删除";
  return "确认操作";
}

function mutationConfirmLabel(operation,request){
  if(operation==="sync")return "确认同步";
  if(operation==="import")return "确认导入";
  if(operation==="agent")return request.enabled?"确认启用":"确认停用";
  if(operation==="link-repair")return "确认修复";
  if(operation==="delete")return "永久删除";
  return "确认执行";
}

function mutationStatusLabel(value){return({ready:"可以执行",blocked:"已阻止",conflict:"存在冲突"})[value]||value||"正在规划";}

function planTargetText(plan){
  const skills=(plan?.targets?.skills||[]).map(item=>item.name).filter(Boolean);
  const clients=(plan?.targets?.clients||[]).map(item=>`${item.client||item.agent||"Agent"}${item.skill?` / ${item.skill}`:""}`);
  return `<div class="mutation-target-group"><strong>Skill (${skills.length})</strong><span>${skills.length?skills.map(escapeHtml).join("、"):"无直接 Skill 变更"}</span></div><div class="mutation-target-group"><strong>Client (${clients.length})</strong><span>${clients.length?clients.map(escapeHtml).join("、"):"无 Agent 链接变更"}</span></div>`;
}

function planStepsHtml(plan){
  const steps=plan?.steps||[];
  return steps.length?steps.map(step=>`<li><strong>${escapeHtml(step.action||"执行")}</strong>${step.skill?` · ${escapeHtml(step.skill)}`:""}${step.client?` · ${escapeHtml(step.client)}`:""}${step.reason?` — ${escapeHtml(step.reason)}`:""}</li>`).join(""):'<li>此操作只更新配置，不直接修改 Skill 或 Agent 链接。</li>';
}

function planFindingsHtml(plan){
  return [...(plan?.conflicts||[]),...(plan?.blockers||[]),...(plan?.warnings||[]).map(item=>({...item,_warning:true}))].map(item=>`<div class="mutation-finding ${item._warning?"warning":""}"><strong>${escapeHtml(item.code||"提示")}</strong>${item.detail?`：${escapeHtml(item.detail)}`:""}</div>`).join("");
}

function planEffectsHtml(plan){
  const effects=plan?.effects||{},writes=effects.writes||{},writeLabels={config:"配置",registry:"Registry",canonical:"全局 Skill",rendered:"Rendered deployment",agent_links:"Agent 链接"};
  const changed=Object.entries(writes).filter(([,value])=>value).map(([key])=>writeLabels[key]||key);
  const git=Object.entries(effects.git||{}).filter(([,value])=>value).map(([key])=>`Git ${key}`);
  return `<p>${changed.length?`将写入：${changed.map(escapeHtml).join("、")}`:"不会写入本地 Skill 状态"}</p><p>${effects.network?"正式执行会访问网络":"正式执行不访问网络"}${git.length?`；${git.map(escapeHtml).join("、")}`:"；无 Git 写操作"}</p>`;
}

function planRecoveryHtml(plan){
  const backup=plan?.backup||{},recovery=plan?.recovery||{},operations=recovery.operations||[];
  return `<p>${escapeHtml(backup.hint||"确认前不会创建备份。")}</p><p>${escapeHtml(recovery.hint||"失败时停止并保留诊断信息。")}</p>${operations.length?`<p>待恢复操作：${operations.map(item=>escapeHtml(item.path||item.status||"unknown")).join("、")}</p>`:""}`;
}

function deleteConfirmationPhrase(pending=pendingMutation){
  const count=pending?.plan?.targets?.skills?.length||0;
  return pending?.operation==="delete"&&count>1?`DELETE ${count}`:"";
}

function updateMutationConfirmState(){
  const button=$("#mutation-confirm");if(!button)return;
  const phrase=deleteConfirmationPhrase();
  const phraseReady=!phrase||$("#mutation-delete-input").value===phrase;
  button.disabled=!pendingMutation||pendingMutation.phase!=="confirm"||!pendingMutation.plan?.can_execute||!phraseReady;
}

function focusMutationDialog(){
  if(!pendingMutation?.focusRequested)return;
  pendingMutation.focusRequested=false;
  const phrase=deleteConfirmationPhrase();
  (phrase?$("#mutation-delete-input"):pendingMutation.phase==="confirm"?$("#mutation-confirm"):$("#mutation-dialog"))?.focus();
}

function renderMutationDialog(){
  if(!$("#mutation-layer"))return;
  if(!pendingMutation){setMutationLayer(false);return;}
  setMutationLayer(true);
  const pending=pendingMutation,plan=pending.plan,phase=pending.phase;
  $("#mutation-title").textContent=mutationTitle(pending.operation,pending.request);
  const status=$("#mutation-status");status.className=`mutation-status ${phase==="result"&&!pending.result?.ok?"result-error":phase==="confirm"?(plan?.status||"ready"):phase}`;
  status.textContent=phase==="planning"?"正在生成只读计划…":phase==="revalidating"?"正在重新规划…":phase==="running"?"正在执行…":phase==="result"?(pending.result?.ok?"操作已完成":"操作未完成"):mutationStatusLabel(plan?.status);
  $("#mutation-summary").textContent=plan?.summary||"Skill Sync 会先计算影响范围，确认前不会写入任何内容。";
  $("#mutation-findings").innerHTML=planFindingsHtml(plan);
  $("#mutation-targets").innerHTML=planTargetText(plan);
  $("#mutation-steps").innerHTML=planStepsHtml(plan);
  $("#mutation-effects").innerHTML=planEffectsHtml(plan);
  $("#mutation-recovery").innerHTML=planRecoveryHtml(plan);
  const phrase=deleteConfirmationPhrase(pending),deleteBox=$("#mutation-delete-confirmation");
  deleteBox.classList.toggle("hidden",!phrase||phase!=="confirm");$("#mutation-delete-phrase").textContent=phrase;
  const result=$("#mutation-result");result.className=`mutation-result ${phase==="result"?(pending.result?.ok?"success":"error"):"hidden"}`;result.textContent=phase==="result"?(pending.result?.message||"操作结果未知"):"";
  const working=["planning","revalidating","running"].includes(phase),mutating=phase==="running";
  $("#mutation-close").disabled=mutating;$("#mutation-cancel").disabled=mutating;$("#mutation-cancel").textContent=phase==="result"?"关闭":"取消";
  $("#mutation-retry").classList.toggle("hidden",phase!=="result"||pending.result?.ok);$("#mutation-retry").textContent=pending.result?.unknown?"关闭并刷新":"重新规划";
  $("#mutation-confirm").classList.toggle("hidden",phase==="result");$("#mutation-confirm").setAttribute("aria-busy",String(working));$("#mutation-confirm").textContent=working?(phase==="running"?"正在执行…":"正在重新规划…"):mutationConfirmLabel(pending.operation,pending.request);
  updateMutationConfirmState();focusMutationDialog();
}

async function refreshPendingPlan({requireUnchanged=false}={}){
  const pending=pendingMutation;if(!pending)return false;
  const previous=mutationFingerprint(pending.plan);pending.phase=requireUnchanged?"revalidating":"planning";pending.focusRequested=true;if(!requireUnchanged)$("#mutation-delete-input").value="";renderMutationDialog();
  try{
    const fresh=await fetchMutationPlan(pending.operation,pending.request);
    if(pendingMutation!==pending)return false;
    pending.plan=fresh;
    if(requireUnchanged&&previous!==mutationFingerprint(fresh)){
      pending.phase="confirm";pending.result=null;$("#mutation-delete-input").value="";toast("影响范围已变化，请检查后再次确认");renderMutationDialog();return false;
    }
    pending.phase="confirm";pending.result=null;renderMutationDialog();return true;
  }catch(error){
    if(pendingMutation!==pending)return false;
    pending.phase="result";pending.result={ok:false,message:`规划失败：${error.message}`,unknown:false};renderMutationDialog();return false;
  }
}

async function requestPlannedMutation(operation,path,body={},origin=null,onSuccess=null){
  if(pendingMutation||hasBusyOperation()){toast("请等待当前操作完成");return false;}
  let request;
  try{request=mutationPlanRequest(operation,body);}catch(error){toast(error.message,true);return false;}
  pendingMutation={operation,path,body:{...body},request,origin:mutationOrigin(origin,operation,body),onSuccess,phase:"planning",plan:null,result:null,focusRequested:true};
  $("#mutation-delete-input").value="";
  renderMutationDialog();
  return refreshPendingPlan();
}

async function confirmPendingMutation(){
  const pending=pendingMutation;
  if(!pending||pending.phase!=="confirm"||!pending.plan?.can_execute){return false;}
  updateMutationConfirmState();if($("#mutation-confirm").disabled)return false;
  if(!await refreshPendingPlan({requireUnchanged:true}))return false;
  if(pendingMutation!==pending||pending.phase!=="confirm")return false;
  pending.phase="running";pending.focusRequested=true;renderMutationDialog();
  let result=null;
  const ok=await action(pending.path,pending.body,{captureResult:value=>{result=value;}});
  if(pendingMutation!==pending)return ok;
  if(ok&&typeof pending.onSuccess==="function")pending.onSuccess();
  pending.phase="result";pending.result=result||{ok,message:ok?"操作已完成":"操作未完成",unknown:!ok};pending.focusRequested=true;renderMutationDialog();
  return ok;
}

function cancelPendingMutation(){
  if(!pendingMutation||pendingMutation.phase==="running")return false;
  let origin=pendingMutation.origin;
  if(pendingMutation.result?.ok&&pendingMutation.operation==="delete")origin={kind:"id",value:"search"};
  if(pendingMutation.result?.ok&&pendingMutation.operation==="import")origin={kind:"id",value:"select-all-imports"};
  pendingMutation=null;setMutationLayer(false);restoreMutationFocus(origin);return true;
}

async function retryPendingMutation(){
  if(!pendingMutation||pendingMutation.phase!=="result"||pendingMutation.result?.ok)return false;
  if(pendingMutation.result?.unknown){const origin=pendingMutation.origin;pendingMutation=null;setMutationLayer(false);restoreMutationFocus(origin);return getState(true);}
  return refreshPendingPlan();
}

function mutationFocusableElements(){return $("#mutation-dialog")?[...$("#mutation-dialog").querySelectorAll('button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(item=>!item.closest?.(".hidden")):[];}
function handleMutationKeydown(event){
  if(!pendingMutation)return false;
  if(event.key==="Escape"){event.preventDefault();event.stopPropagation();cancelPendingMutation();return true;}
  if(event.key!=="Tab")return false;
  const focusable=mutationFocusableElements();if(!focusable.length){event.preventDefault();$("#mutation-dialog").focus();return true;}
  const first=focusable[0],last=focusable[focusable.length-1],active=document.activeElement;
  if(event.shiftKey&&(active===first||!$("#mutation-dialog").contains(active))){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&(active===last||!$("#mutation-dialog").contains(active))){event.preventDefault();first.focus();}
  return true;
}

function render() {
  $("#load-failure").classList.add("hidden");
  $("#setup").classList.toggle("hidden", state.initialized); $("#app").classList.toggle("hidden", !state.initialized);
  if (!state.initialized) return;
  const preview = state.preview;
  if (preview) {
    $("#sync-label").textContent = label(preview.action);
    $("#sync-summary").textContent = previewSummary(preview.action);
    $("#sync").disabled = ["blocked","conflict"].includes(preview.action);
  }
  renderIssues(preview?.issues || []);
  renderSkills(state.status?.skills || []);
  renderAgents(state.doctor?.agents || []);
  renderImports(state.import_candidates || []);
  renderDetail();
  showView(activeView);
  renderOperationStates();
}

function setButtonState(button, running, runningHtml, idleHtml, disabled = false) {
  if (!button) return;
  button.disabled = running || disabled;
  button.setAttribute("aria-busy", String(running));
  button.innerHTML = running ? runningHtml : idleHtml;
}

function renderOperationStates() {
  const locked = hasBusyOperation();
  const refreshBusy = isBusy("refresh");
  setButtonState($("#refresh"), refreshBusy, '<i class="ri-refresh-line"></i><span>正在刷新…</span>', '<i class="ri-refresh-line"></i><span>刷新状态</span>', locked && !refreshBusy);
  setButtonState($("#retry-load"), refreshBusy, "正在重新加载…", "重新加载", locked && !refreshBusy);
  setButtonState($("#sync"), isBusy("sync"), '<i class="ri-loop-right-line"></i><span>正在同步…</span>', '<i class="ri-loop-right-line"></i><span>立即同步</span>', locked && !isBusy("sync"));
  setButtonState($("#setup-submit"), isBusy("init"), "正在连接…", "连接并开始使用", locked && !isBusy("init"));
  const selectionBusy = selectionOperationBusy();
  ["#select-all-checkbox","#select-selected","#deselect-selected","#link-selected","#copy-selected","#copy-agent","#delete-selected","#clear-selection"].forEach(selector => {
    const control = $(selector); if (control) control.disabled = selectionBusy || (locked && !selectionBusy);
  });
  if (isBusy("select")) $("#select-selected").innerHTML = '<i class="ri-add-circle-line"></i><span>正在加入…</span>';
  if (isBusy("deselect")) $("#select-selected").innerHTML = '<i class="ri-subtract-line"></i><span>正在取消…</span>';
  $("#link-selected").textContent = isBusy("link") ? "正在修复…" : "修复链接";
  $("#copy-selected").innerHTML = isBusy("copy") ? '<i class="ri-file-copy-line"></i>正在复制…' : '<i class="ri-file-copy-line"></i>复制到';
  $("#delete-selected").textContent = isBusy("delete") ? "正在删除…" : "删除所选";
  const importBusy = importOperationBusy();
  ["#select-all-imports","#clear-imports","#import-selected"].forEach(selector => {
    const control = $(selector); if (control) control.disabled = importBusy || (locked && !importBusy);
  });
  if ($("#import-selected")) {
    $("#import-selected").setAttribute("aria-busy", String(importBusy));
    $("#import-selected").textContent = importBusy ? "正在导入…" : "导入到全局";
  }
  const backupBusy = detailSkill ? isBusy(`backup:${detailSkill}`) : false;
  setButtonState($("#detail-backup"), backupBusy, "正在备份…", "创建本地备份", locked && !backupBusy);
  if ($("#app")) $("#app").setAttribute("aria-busy", String(locked));
  if ($("#setup-form")) $("#setup-form").setAttribute("aria-busy", String(isBusy("init")));
}

function showView(view) {
  document.querySelectorAll(".view").forEach(item => item.classList.toggle("active", item.id === `view-${view}`));
  document.querySelectorAll(".nav-item[data-view]").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  $("#detail-drawer").classList.toggle("hidden", view !== "skills" || !detailSkill);
}

function switchView(view) {
  if (mutationInFlight) { toast("请等待当前操作完成"); return; }
  activeView = view; persistUiContext(); showView(view);
  loadActiveView().catch(error => toast(error.message, true));
}

function renderIssues(issues) {
  const box = $("#issue-list"); box.classList.toggle("hidden", !issues.length);
  box.innerHTML = issues.map(issue => `<div><i class="ri-error-warning-line"></i><strong>${escapeHtml(issueTitle(issue))}</strong><span>${escapeHtml(issue.detail || issue.skill || "需要检查")}</span></div>`).join("");
}

function skillSyncState(skill){return skill.changed_local?"changed":skill.selected?"synced":"local";}
function skillSource(skill){return String(skill.platform||"global");}
function sourceLabel(source){return ({global:"全局库",codex:"Codex 导入",claude:"Claude Code 导入","claude-code":"Claude Code 导入",workbuddy:"WorkBuddy 导入",kimi:"Kimi 导入","kimi-code":"Kimi 导入","kimi-desktop":"Kimi 导入"})[source]||source;}
function inventoryAgents(){return (state?.doctor?.agents||[]).filter(agent=>agent.detected);}
function agentCoversSkill(skill,agent){return ["linked","copied"].includes(skill.agents?.[agent]);}
function renderInventoryControls(skills){
  const statuses=new Set(["all","synced","changed","local"]);
  const sources=[...new Set(skills.map(skillSource))].sort((a,b)=>sourceLabel(a).localeCompare(sourceLabel(b)));
  const agents=inventoryAgents();
  const readyInventory=inventoryDataReady(),readyAgents=agentDataReady();
  const previous=JSON.stringify(inventoryFilters);
  if(!statuses.has(inventoryFilters.status))inventoryFilters.status="all";
  if(readyInventory&&inventoryFilters.source!=="all"&&!sources.includes(inventoryFilters.source))inventoryFilters.source="all";
  if(readyAgents&&inventoryFilters.agent!=="all"&&!agents.some(agent=>agent.name===inventoryFilters.agent))inventoryFilters.agent="all";
  const agentOptions=[...agents];
  if(!readyAgents&&inventoryFilters.agent!=="all"&&!agentOptions.some(agent=>agent.name===inventoryFilters.agent))agentOptions.push({name:inventoryFilters.agent,display_name:`${inventoryFilters.agent}（加载中）`});
  $("#sync-filter").innerHTML='<option value="all">全部状态</option><option value="synced">已同步</option><option value="changed">本地待推送</option><option value="local">未加入同步</option>';
  $("#source-filter").innerHTML='<option value="all">全部来源</option>'+sources.map(source=>`<option value="${escapeHtml(source)}">${escapeHtml(sourceLabel(source))}</option>`).join("");
  $("#agent-filter").innerHTML='<option value="all">全部 Agent</option>'+agentOptions.map(agent=>`<option value="${escapeHtml(agent.name)}">${escapeHtml(agent.display_name)}</option>`).join("");
  $("#sync-filter").value=inventoryFilters.status;$("#source-filter").value=inventoryFilters.source;$("#agent-filter").value=inventoryFilters.agent;
  $("#clear-filters").disabled=!$("#search").value&&Object.values(inventoryFilters).every(value=>value==="all");
  if(previous!==JSON.stringify(inventoryFilters))persistUiContext();
}
function visibleSkills(skills) {
  const query=$("#search").value.trim().toLowerCase();
  return skills.filter(skill => {
    const matchesQuery=!query||[skill.name,skill.description].some(value=>String(value||"").toLowerCase().includes(query));
    const matchesStatus=inventoryFilters.status==="all"||skillSyncState(skill)===inventoryFilters.status;
    const matchesSource=inventoryFilters.source==="all"||skillSource(skill)===inventoryFilters.source;
    const matchesAgent=inventoryFilters.agent==="all"||!agentDataReady()||agentCoversSkill(skill,inventoryFilters.agent);
    return matchesQuery&&matchesStatus&&matchesSource&&matchesAgent;
  });
}

function renderSkills(skills) {
  let selectionChanged=false;
  if(inventoryDataReady())for (const name of [...selected]) if (!skills.some(skill => skill.name === name)){selected.delete(name);selectionChanged=true;}
  if(selectionChanged)persistUiContext();
  renderInventoryControls(skills);
  const shown = visibleSkills(skills);
  const agents = inventoryAgents();
  $("#skill-list").innerHTML = shown.map(skill => {
    const coverage = agents.length?agents.map(agent=>{const agentState=skill.agents?.[agent.name];return `<span class="agent-coverage ${agentStateClass(agentState)}" aria-label="${escapeHtml(agent.display_name)}：${escapeHtml(agentStateLabel(agentState))}"><i aria-hidden="true"></i><span>${escapeHtml(agent.display_name)}</span><small>${escapeHtml(agentStateLabel(agentState))}</small></span>`;}).join(""):'<span class="coverage-empty">暂无已检测 Agent</span>';
    return `<article class="skill-row ${detailSkill === skill.name ? "focused" : ""}" role="button" tabindex="0" data-skill-name="${escapeHtml(skill.name)}" data-detail-trigger="row" aria-label="查看 ${escapeHtml(skill.name)} 详情" onclick="openDetail('${escapeJs(skill.name)}',this)" onkeydown="handleSkillRowKeydown(event,'${escapeJs(skill.name)}')"><input type="checkbox" aria-label="选择 ${escapeHtml(skill.name)}" ${selected.has(skill.name)?"checked":""} ${hasBusyOperation()?"disabled":""} onclick="event.stopPropagation()" onchange="toggle('${escapeJs(skill.name)}',this.checked)"><span class="file-icon"><i class="ri-file-text-line"></i></span><strong>${escapeHtml(skill.name)}</strong><span class="coverage">${coverage}</span><span class="row-status ${skill.changed_local ? "pending" : ""}">${skill.changed_local ? "本地待推送" : (skill.selected ? "已同步" : "未加入同步")}</span><button class="icon-button" data-skill-name="${escapeHtml(skill.name)}" data-detail-trigger="button" aria-label="查看详情" onclick="event.stopPropagation();openDetail('${escapeJs(skill.name)}',this)"><i class="ri-more-2-fill"></i></button></article>`;
  }).join("") || `<p class="empty">没有符合条件的 Skill</p>`;
  const allSelected = shown.length > 0 && shown.every(skill => selected.has(skill.name));
  const someSelected = shown.some(skill => selected.has(skill.name));
  $("#select-all-checkbox").checked = allSelected;
  $("#select-all-checkbox").indeterminate = someSelected&&!allSelected;
  $("#select-all").textContent = allSelected ? "取消选择可见项" : "全选可见项";
  $("#visible-count").textContent=`显示 ${shown.length} / ${skills.length}`;
  const picked=skills.filter(skill=>selected.has(skill.name));
  const pickedAllSynced=picked.length>0&&picked.every(skill=>skill.selected);
  $("#select-selected").innerHTML=pickedAllSynced?'<i class="ri-subtract-line"></i><span>取消同步</span>':'<i class="ri-add-circle-line"></i><span>加入同步</span>';
  $("#select-selected").dataset.mode=pickedAllSynced?"deselect":"select";
  updateSelectionBar();
}

function renderAgents(agents) {
  $("#agent-list").innerHTML = agents.map(agent => {
    const busy = isBusy(`agent:${agent.name}`);
    const unavailable=(!agent.detected&&agent.enabled)||hasBusyOperation();
    return `<article class="agent-row"><span class="agent-icon"><i class="${agentIcon(agent.name)}"></i></span><div><strong>${escapeHtml(agent.display_name)}</strong><p>${agent.detected ? escapeHtml(agent.skills_dir) : "未检测到此 Agent"}</p></div><span class="connection-state ${agent.detected && agent.enabled ? "online" : ""}"><i></i>${agent.enabled ? (agent.detected ? "已连接" : "等待检测") : "已停用"}</span><button ${unavailable ? "disabled" : ""} aria-busy="${busy}" data-agent-action="${escapeHtml(agent.name)}" onclick="requestPlannedMutation('agent','/api/agent',{agent:'${escapeJs(agent.name)}',enabled:${!agent.enabled}},this)">${busy ? (agent.enabled ? "正在停用…" : "正在启用…") : (agent.enabled ? "停用" : "启用")}</button></article>`;
  }).join("");
}

function renderImports(items) {
  const sources=["codex","claude","workbuddy"], titles={codex:"Codex",claude:"Claude Code",workbuddy:"WorkBuddy"};
  for (const key of [...selectedImports]) if (!items.some(item => importKey(item) === key)) selectedImports.delete(key);
  $("#import-tabs").innerHTML = sources.map(agent => `<button class="source-tab ${activeImportAgent===agent?"active":""}" ${hasBusyOperation()?"disabled":""} onclick="setImportAgent('${agent}')"><i class="${agentIcon(agent)}"></i><span>${titles[agent]}</span><b>${items.filter(item=>item.agent===agent).length}</b></button>`).join("");
  const group=items.filter(item=>item.agent===activeImportAgent);
  $("#imports").innerHTML = group.map(item => `<article class="import-row"><input type="checkbox" aria-label="选择 ${escapeHtml(item.name)}" ${selectedImports.has(importKey(item))?"checked":""} ${item.state==="conflict" || hasBusyOperation()?"disabled":""} onchange="toggleImport('${escapeJs(item.agent)}','${escapeJs(item.name)}',this.checked)"><span class="file-icon"><i class="ri-file-text-line"></i></span><strong>${escapeHtml(item.name)}</strong><code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code>${item.state==="conflict"?'<span class="conflict-label"><i class="ri-error-warning-line"></i>名称冲突</span>':""}</article>`).join("") || `<p class="empty">这个 Agent 中没有可导入的 Skill</p>`;
  const selectable=group.filter(item=>item.state!=="conflict");
  $("#select-all-imports").checked=selectable.length>0 && selectable.every(item=>selectedImports.has(importKey(item)));
  updateImportBar();
}

function renderDetail() {
  const skill=(state.status.skills||[]).find(item=>item.name===detailSkill);
  if (!skill) {
    if(detailSkill&&!inventoryDataReady()){$("#detail-drawer").classList.add("hidden");return;}
    if (detailSkill) updateDetailLocation(null, "replace");
    detailSkill=null; detailNeedsFocus=false;
    $("#detail-drawer").classList.add("hidden");
    return;
  }
  $("#detail-drawer").classList.toggle("hidden", activeView!=="skills");
  $("#detail-name").textContent=skill.name;
  $("#detail-status").innerHTML=`<i></i>${skill.changed_local?"本地有修改":skill.selected?"已同步到 Agent":"未加入同步"}`;
  $("#detail-description").textContent=skill.description||"暂无 description";
  $("#detail-sync").textContent=skill.selected?"已加入同步":"仅保存在本地";
  $("#detail-path").textContent=skill.local_path;
  $("#detail-agents").innerHTML=(state.doctor?.agents||[]).map(agent=>`<div><span><i class="agent-dot ${agentStateClass(skill.agents?.[agent.name])}"></i>${escapeHtml(agent.display_name)}</span><b>${agentStateLabel(skill.agents?.[agent.name])}</b></div>`).join("");
  if (detailNeedsFocus && activeView==="skills") {
    detailNeedsFocus=false;
    $("#close-detail").focus();
  }
}

function toggle(name,checked){checked?selected.add(name):selected.delete(name);persistUiContext();renderSkills(state.status.skills||[]);}
function toggleVisibleSkills(){const shown=visibleSkills(state.status.skills||[]);const allSelected=shown.length>0&&shown.every(skill=>selected.has(skill.name));shown.forEach(skill=>allSelected ? selected.delete(skill.name) : selected.add(skill.name));persistUiContext();renderSkills(state.status.skills||[]);}
function clearSelection(){selected.clear();persistUiContext();renderSkills(state.status.skills||[]);}
function updateSelectionBar(){$("#selection-count").textContent=selected.size;$("#selection-bar").classList.toggle("hidden",!selected.size);}
function selectedNames(){if(!selected.size){toast("请先选择至少一个 Skill",true);return null;}return [...selected];}
function bulk(path){const skills=selectedNames();return skills&&action(path,{skills});}
function toggleSelectedSync(){return bulk($("#select-selected").dataset.mode==="deselect"?"/api/deselect":"/api/select");}
function handleSkillRowKeydown(event,name){
  if(event.target!==event.currentTarget||!["Enter"," "].includes(event.key))return;
  event.preventDefault();openDetail(name,event.currentTarget);
}
function openDetail(name,origin=null,updateHistory=true){
  if(!(state?.status?.skills||[]).some(skill=>skill.name===name))return;
  if(origin?.dataset?.skillName===name)detailReturnTarget={skill:name,trigger:origin.dataset.detailTrigger||"row"};
  detailSkill=name;detailNeedsFocus=true;
  if(updateHistory)updateDetailLocation(name,"push");
  renderSkills(state.status.skills||[]);renderDetail();showView(activeView);
}
function closeDetail(updateHistory=true,restoreFocus=true){
  if(!detailSkill)return;
  detailSkill=null;detailNeedsFocus=false;
  if(updateHistory)updateDetailLocation(null,"push");
  $("#detail-drawer").classList.add("hidden");renderSkills(state.status.skills||[]);
  if(restoreFocus)focusDetailReturnTarget();
}
function focusDetailReturnTarget(){
  const target=detailReturnTarget&&[...document.querySelectorAll("[data-skill-name]")].find(item=>item.dataset.skillName===detailReturnTarget.skill&&item.dataset.detailTrigger===detailReturnTarget.trigger);
  (target||$("#search")).focus();
}
function detailFocusableElements(){
  return [...$("#detail-drawer").querySelectorAll('button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')];
}
function handleDetailKeydown(event){
  if(!detailSkill)return;
  if(event.key==="Escape"){
    event.preventDefault();event.stopPropagation();closeDetail();return;
  }
  if(event.key!=="Tab")return;
  const focusable=detailFocusableElements();
  if(!focusable.length){event.preventDefault();$("#detail-drawer").focus();return;}
  const first=focusable[0],last=focusable[focusable.length-1],active=document.activeElement;
  if(event.shiftKey&&(active===first||!$("#detail-drawer").contains(active))){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&(active===last||!$("#detail-drawer").contains(active))){event.preventDefault();first.focus();}
}
function detailTargetFromLocation(){
  try{return new URL(globalThis.location?.href||"http://skill-sync.local/").searchParams.get(DETAIL_QUERY_PARAM)||null;}
  catch(_){return null;}
}
function updateDetailLocation(target,mode){
  if(!globalThis.history?.[`${mode}State`]||!globalThis.location?.href)return;
  const url=new URL(globalThis.location.href);
  target?url.searchParams.set(DETAIL_QUERY_PARAM,target):url.searchParams.delete(DETAIL_QUERY_PARAM);
  const previous=globalThis.history.state&&typeof globalThis.history.state==="object"?globalThis.history.state:{};
  globalThis.history[`${mode}State`]({...previous,detail:target||null},"",`${url.pathname}${url.search}${url.hash}`);
}
function restoreDetailFromLocation(){
  const previous=detailSkill,target=detailTargetFromLocation();
  if(target&&(state?.status?.skills||[]).some(skill=>skill.name===target)){
    detailSkill=target;detailNeedsFocus=true;renderSkills(state.status.skills||[]);renderDetail();showView(activeView);return;
  }
  if(target)updateDetailLocation(null,"replace");
  detailSkill=null;detailNeedsFocus=false;$("#detail-drawer").classList.add("hidden");
  if(state?.initialized)renderSkills(state.status?.skills||[]);
  if(previous)focusDetailReturnTarget();
}
function inventoryDataReady(){return loadedViews.has("inventory")||Boolean(state?.status?.skills?.length);}
function agentDataReady(){return loadedViews.has("agents")||Boolean(state?.doctor?.agents?.length);}
function readUiContext(){
  const fallback={activeView:"skills",search:"",filters:{status:"all",source:"all",agent:"all"},selected:[]};
  try{
    const parsed=JSON.parse(globalThis.sessionStorage?.getItem(WEB_CONTEXT_STORAGE_KEY)||"null");
    if(!parsed||typeof parsed!=="object")return fallback;
    const filters=parsed.filters&&typeof parsed.filters==="object"?parsed.filters:{};
    return {
      activeView:["skills","agents","imports"].includes(parsed.activeView)?parsed.activeView:"skills",
      search:typeof parsed.search==="string"?parsed.search:"",
      filters:{status:typeof filters.status==="string"?filters.status:"all",source:typeof filters.source==="string"?filters.source:"all",agent:typeof filters.agent==="string"?filters.agent:"all"},
      selected:Array.isArray(parsed.selected)?[...new Set(parsed.selected.filter(name=>typeof name==="string"&&name))]:[],
    };
  }catch(_){return fallback;}
}
function persistUiContext(){
  try{globalThis.sessionStorage?.setItem(WEB_CONTEXT_STORAGE_KEY,JSON.stringify({activeView,search:$("#search")?.value||"",filters:inventoryFilters,selected:[...selected]}));}
  catch(_){}
}
function setImportAgent(agent){activeImportAgent=agent;renderImports(state.import_candidates||[]);}
function setInventoryFilter(key,value){inventoryFilters[key]=value;persistUiContext();renderSkills(state.status.skills||[]);}
function clearInventoryFilters(){inventoryFilters.status="all";inventoryFilters.source="all";inventoryFilters.agent="all";$("#search").value="";persistUiContext();renderSkills(state.status.skills||[]);$("#search").focus();}
function importKey(item){return `${item.agent}\u0000${item.name}`;}
function toggleImport(agent,name,checked){const key=`${agent}\u0000${name}`;checked?selectedImports.add(key):selectedImports.delete(key);renderImports(state.import_candidates||[]);}
function toggleAllImports(){const group=(state.import_candidates||[]).filter(item=>item.agent===activeImportAgent&&item.state!=="conflict");const all=group.length>0&&group.every(item=>selectedImports.has(importKey(item)));group.forEach(item=>all?selectedImports.delete(importKey(item)):selectedImports.add(importKey(item)));renderImports(state.import_candidates||[]);}
function updateImportBar(){const count=[...selectedImports].filter(key=>key.startsWith(`${activeImportAgent}\u0000`)).length;$("#import-count").textContent=count;$("#import-bar").classList.toggle("hidden",!count);}
async function importSelected(){const skills=[...selectedImports].filter(key=>key.startsWith(`${activeImportAgent}\u0000`)).map(key=>key.split("\u0000")[1]);if(!skills.length)return false;return requestPlannedMutation("import","/api/import",{skills,agent:activeImportAgent},$("#import-selected"),()=>{selectedImports.clear();renderImports(state.import_candidates||[]);});}
async function backupSkill(skill){await action("/api/backup", {skill});}
async function deleteSelected(){const skills=selectedNames();if(!skills)return false;return requestPlannedMutation("delete","/api/delete",{skills},$("#delete-selected"),()=>{selected.clear();persistUiContext();renderSkills(state.status.skills||[]);});}
function label(value){return({pull:"需要拉取",push:"需要推送","repair-links":"需要修复链接",conflict:"需要手动合并",blocked:"需要先处理",noop:"所有 Agent 已同步"})[value]||value;}
function previewSummary(value){return({pull:"远端有新的 Skill 变更可拉取。",push:"本地变更等待推送到远端。","repair-links":"Skill 内容一致，部分 Agent 链接需要修复。",conflict:"本地和远端均有修改，需要手动合并。",blocked:"同步仓库需要先处理。",noop:"所有受管 Skill 状态一致。"})[value]||"当前状态已更新。";}
function viewName(view){return({skills:"技能库",agents:"连接",imports:"导入"})[view]||"当前页面";}
function issueTitle(issue){return({"content-conflict":"本地与远端同时有修改","dirty-repository":"同步仓库存在额外改动",missing:"链接缺失",conflict:"链接冲突",partial:"链接不完整"})[issue.type]||issue.type;}
function agentStateClass(value){return["linked","copied"].includes(value)?"ok":value==="disabled"?"disabled":value==="missing"?"missing":"warn";}
function agentStateLabel(value){return value==="linked"?"已同步":value==="copied"?"本地副本":value==="disabled"?"已停用":value==="missing"?"未连接":"需检查";}
function agentIcon(name){return({codex:"ri-code-box-line",claude:"ri-command-line",workbuddy:"ri-robot-2-line",kimi:"ri-sparkling-line"})[name]||"ri-apps-line";}
function toast(message,error=false){const el=$("#toast");if(toastTimer!==null)clearTimeout(toastTimer);el.textContent=message;el.className=`show ${error?"error":""}`;el.setAttribute("role",error?"alert":"status");el.setAttribute("aria-live",error?"assertive":"polite");toastTimer=setTimeout(()=>{el.className="";toastTimer=null;},3500);}
function escapeHtml(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
function escapeJs(value){return String(value).replace(/['\\]/g,"\\$&");}

document.querySelectorAll(".nav-item[data-view]").forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$("#search").value=restoredUiContext.search;
showView(activeView);
$("#setup-form").onsubmit=async event=>{event.preventDefault();await action("/api/init",Object.fromEntries(new FormData(event.currentTarget)));};
$("#sync").onclick=()=>requestPlannedMutation("sync","/api/sync",{},$("#sync"));$("#refresh").onclick=()=>getState(true);
$("#retry-load").onclick=()=>getState(true);
$("#search").oninput=()=>{persistUiContext();renderSkills(state.status.skills||[]);};
$("#sync-filter").onchange=event=>setInventoryFilter("status",event.currentTarget.value);
$("#source-filter").onchange=event=>setInventoryFilter("source",event.currentTarget.value);
$("#agent-filter").onchange=event=>setInventoryFilter("agent",event.currentTarget.value);
$("#clear-filters").onclick=clearInventoryFilters;
$("#select-all-checkbox").onchange=toggleVisibleSkills;$("#select-all").onclick=toggleVisibleSkills;$("#clear-selection").onclick=clearSelection;
$("#select-selected").onclick=toggleSelectedSync;$("#deselect-selected").onclick=()=>bulk("/api/deselect");$("#link-selected").onclick=()=>{const skills=selectedNames();return skills&&requestPlannedMutation("link-repair","/api/link",{skills},$("#link-selected"));};
$("#copy-selected").onclick=()=>{const skills=selectedNames();return skills&&action("/api/copy",{skills,agents:[$("#copy-agent").value]});};$("#delete-selected").onclick=deleteSelected;
$("#close-detail").onclick=()=>closeDetail();$("#detail-backup").onclick=()=>detailSkill&&backupSkill(detailSkill);
$("#detail-drawer").onkeydown=handleDetailKeydown;
if(globalThis.addEventListener){
  globalThis.addEventListener("popstate",restoreDetailFromLocation);
  globalThis.addEventListener("keydown",event=>{if(pendingMutation){handleMutationKeydown(event);return;}if(event.key==="Escape"&&detailSkill)handleDetailKeydown(event);});
}
$("#select-all-imports").onchange=toggleAllImports;$("#clear-imports").onclick=()=>{selectedImports.clear();renderImports(state.import_candidates||[]);};$("#import-selected").onclick=importSelected;
if($("#mutation-confirm"))$("#mutation-confirm").onclick=confirmPendingMutation;
if($("#mutation-cancel"))$("#mutation-cancel").onclick=cancelPendingMutation;
if($("#mutation-close"))$("#mutation-close").onclick=cancelPendingMutation;
if($("#mutation-retry"))$("#mutation-retry").onclick=retryPendingMutation;
if($("#mutation-delete-input"))$("#mutation-delete-input").oninput=updateMutationConfirmState;
if($("#mutation-dialog"))$("#mutation-dialog").onkeydown=handleMutationKeydown;
if (!globalThis.SKILL_SYNC_TEST) getState(false).catch(error=>toast(error.message,true));
