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
let activeToast = null;
let searchRenderTimer = null;
let pendingMutation = null;
let pendingEdit = null;
const editInspections = new Map();
const $ = selector => document.querySelector(selector);
document.querySelectorAll('i[class^="ri-"],i[class*=" ri-"]').forEach(icon=>icon.setAttribute("aria-hidden","true"));

function viewsFor(view) {
  if (view === "skills") return ["summary", "inventory", "agents", "managed"];
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
    state = {initialized: partial.initialized, status: {skills: []}, doctor: {agents: [], matrix: [], issues: []}, managed: {variants: {variants: []}, deployments: {skills: []}, sessions: {sessions: []}}, import_candidates: []};
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
  const partial = await readJsonResponse(response, response.ok ? "状态响应格式错误" : "状态加载失败");
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
    if (generation === stateGeneration && state?.initialized) await loadViews(["summary", "agents", "managed"], force, generation);
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
  const data = await readJsonResponse(response, "无法获取请求令牌");
  if (!response.ok) throw new Error(data.error || "无法获取请求令牌");
  token = data.token;
  return token;
}

async function readJsonResponse(response, fallback) {
  try { return await response.json(); }
  catch (_) { throw new Error(fallback); }
}

async function postWithToken(path, body) {
  if (!token) await ensureToken();
  const request = () => fetch(path, {method:"POST",headers:{"Content-Type":"application/json","X-Skill-Sync-Token":token},body:JSON.stringify(body)});
  let response = await request();
  if (response.status === 403) {
    token = "";
    await ensureToken();
    response = await request();
  }
  return response;
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
    "/api/edit/begin": [`edit-begin:${body.skill}`, "正在创建编辑会话…", "编辑会话已创建", "创建编辑会话失败"],
    "/api/edit/apply": [`edit-apply:${body.session_id}`, "正在应用更改…", "编辑更改已安全应用", "应用编辑更改失败"],
    "/api/edit/abort": [`edit-abort:${body.session_id}`, "正在中止编辑会话…", "编辑会话已中止", "中止编辑会话失败"],
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
    if (announce && activeToast?.error) dismissToast();
    if (announce) toast(`正在刷新${viewName(view)}…`);
    try {
      await ensureToken();
      await loadView(view, true, generation);
      if (announce) toast(`${viewName(view)}状态已刷新`);
      return true;
    } catch (error) {
      if (!state) { $("#initial-loading")?.classList.add("hidden"); $("#load-failure").classList.remove("hidden"); }
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
    if (activeToast?.error) dismissToast();
    toast(operation.running);
    let responseData=null;
    try {
      let response;
      try {
        response = await postWithToken(path, {...body, views: viewsFor(requestView)});
      } catch (_) {
        throw new Error("操作结果未知，请刷新核验");
      }
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error("操作结果未知，请刷新核验"); }
      responseData=data;
      if (!response.ok) {
        if (data.mutation_applied) throw new Error(`${operation.success}，但状态刷新失败，请刷新核验`);
        if (response.status >= 500) throw new Error("操作结果未知，请刷新核验");
        throw new Error(data.error || operation.failure);
      }
      loadedViews.clear();
      try { mergeState(data.state); render(); }
      catch (_) { throw new Error("操作已执行，但界面更新失败，请刷新核验"); }
      const success = data.result?.backup_path ? `${operation.success}：${data.result.backup_path}` : operation.success;
      const outcome={ok:true,message:success,result:data.result||{},data,unknown:false};
      if(typeof options.captureResult==="function")options.captureResult(outcome);
      toast(success); return true;
    } catch (error) {
      const unknown = error.message.includes("刷新核验") ? error.message : `${operation.failure}：${error.message}`;
      if(typeof options.captureResult==="function")options.captureResult({ok:false,message:unknown,error:error.message,data:responseData,unknown:error.message.includes("刷新核验")});
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
  let response;
  try {
    response=await postWithToken("/api/plan",{operation,request});
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
  const viewFallback=activeView==="skills"?$("#search"):document.querySelector(`.nav-item[data-view="${activeView}"]`);
  focusVisibleControl(target,viewFallback,$("#refresh"));
}

function focusVisibleControl(...controls){
  const target=controls.find(control=>control&&!control.disabled&&!control.closest?.(".hidden")&&!control.closest?.(".view:not(.active)"));
  target?.focus();
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

function mutationStatusLabel(value){return({ready:"可以执行",blocked:"暂时无法执行",conflict:"部分位置需要先处理"})[value]||value||"正在规划";}

function planClientName(item){return clientDisplayName(item?.client||item?.agent||"Agent");}
function repairablePlanClients(plan){return (plan?.targets?.clients||[]).filter(item=>item.client&&!["blocked","noop"].includes(item.effect));}
function blockedPlanClients(plan){return (plan?.targets?.clients||[]).filter(item=>item.effect==="blocked");}
function repairPlanSummary(plan){
  const repairable=repairablePlanClients(plan),blocked=blockedPlanClients(plan);
  if(plan?.can_execute)return `将修复 ${repairable.length||plan?.targets?.clients?.length||0} 个客户端的部署链接。确认前不会写入任何内容。`;
  if(repairable.length&&blocked.length)return `${repairable.map(planClientName).join("、")} 可以自动修复；${blocked.map(planClientName).join("、")} 的目标位置已有其他内容，为避免覆盖，需要先单独处理。`;
  if(blocked.length)return `${blocked.map(planClientName).join("、")} 的目标位置已有其他内容。Skill Sync 不会自动覆盖，请先备份或移走冲突内容。`;
  return "当前状态无法安全自动修复，请查看下面的处理说明。";
}
function planSummaryText(pending){
  const plan=pending?.plan,skills=plan?.targets?.skills?.length||0;
  if(pending?.operation==="link-repair"&&pending.oneClickContext?.skipped?.length){
    return `将修复 ${pending.oneClickContext.repairable.join("、")}；${pending.oneClickContext.skipped.join("、")} 因目标位置已有其他内容不会改动。确认前不会写入任何内容。`;
  }
  if(pending?.operation==="link-repair"&&plan)return repairPlanSummary(plan);
  if(pending?.operation==="import")return `将导入 ${skills} 个 Skill。确认前不会写入任何内容。`;
  if(pending?.operation==="agent")return `${pending.request?.enabled?"启用":"停用"}后将更新客户端连接。确认前不会写入任何内容。`;
  if(pending?.operation==="delete")return `将永久删除 ${skills} 个全局 Skill。请确认下面的影响范围。`;
  if(pending?.operation==="sync")return "将按下面的计划同步 Skill。确认前不会写入任何内容。";
  return "Skill Sync 会先计算影响范围，确认前不会写入任何内容。";
}

function planTargetText(plan){
  const skills=(plan?.targets?.skills||[]).map(item=>item.name).filter(Boolean);
  const clients=(plan?.targets?.clients||[]).map(item=>`${planClientName(item)}${item.skill?` / ${item.skill}`:""}`);
  return `<div class="mutation-target-group"><strong>技能 (${skills.length})</strong><span>${skills.length?skills.map(escapeHtml).join("、"):"无直接 Skill 变更"}</span></div><div class="mutation-target-group"><strong>客户端 (${clients.length})</strong><span>${clients.length?clients.map(escapeHtml).join("、"):"无 Agent 链接变更"}</span></div>`;
}

function planActionLabel(value){return ({"repair-links":"检查部署","build-and-swap":"重新构建并更新链接","build-and-create":"构建并创建链接",swap:"更新链接",create:"创建链接",blocked:"需要先处理目标位置",noop:"无需处理",sync:"同步",import:"导入",agent:"更新 Agent 设置",delete:"删除"})[value]||value||"执行";}
function planStateLabel(value){return ({"linked-render":"已连接当前版本","stale-render":"链接仍指向旧版本",conflict:"目标位置已有其他内容","wrong-link":"链接指向其他位置","broken-link":"链接已失效","tampered-render":"部署内容被修改","missing-render":"部署文件已丢失",missing:"尚未创建链接"})[value]||value||"";}
function planStepsHtml(plan){
  const steps=plan?.steps||[];
  return steps.length?steps.map(step=>`<li><strong>${escapeHtml(planActionLabel(step.action))}</strong>${step.skill?` · ${escapeHtml(step.skill)}`:""}${step.client?` · ${escapeHtml(clientDisplayName(step.client))}`:""}${step.state?` — ${escapeHtml(planStateLabel(step.state))}`:""}</li>`).join(""):'<li>此操作只更新配置，不直接修改 Skill 或 Agent 链接。</li>';
}

function findingLabel(item){return ({conflict:"目标位置存在冲突","unsafe-agent-path":"已保护现有内容","wrong-link":"链接指向其他位置","broken-link":"链接已失效","tampered-render":"部署内容被修改","missing-render":"部署文件已丢失","agent-disabled":"Agent 已停用","deployment-recovery-required":"存在未完成的修复操作",blocked:"暂时无法执行"})[item?.code]||"需要处理";}
function findingDetail(item){
  if(item?.code==="conflict"||item?.code==="unsafe-agent-path")return `${clientDisplayName(item.client||item.agent)} 的目标位置已有其他内容，Skill Sync 不会自动覆盖。请先备份或移走这个目录，再重新检查${item.destination?`：${item.destination}`:"。"}`;
  const details={"Agent synchronization is disabled; enable it before repairing links.":"请先在“连接”页面启用这个 Agent。","An incomplete deployment operation must be recovered before repairing links.":"请先完成或恢复上一次中断的部署操作。"};
  return details[item?.detail]||item?.detail||"";
}
function planFindingsHtml(plan){
  const findings=[...(plan?.conflicts||[]),...(plan?.blockers||[]),...(plan?.warnings||[]).map(item=>({...item,_warning:true}))],seen=new Set();
  return findings.filter(item=>{const detail=findingDetail(item),key=`${item.client||item.agent||""}\0${detail}`;if(seen.has(key))return false;seen.add(key);return true;}).map(item=>`<div class="mutation-finding ${item._warning?"warning":""}"><strong>${escapeHtml(findingLabel(item))}</strong>${findingDetail(item)?`：${escapeHtml(findingDetail(item))}`:""}</div>`).join("");
}

function planEffectsHtml(plan){
  const effects=plan?.effects||{},writes=effects.writes||{},writeLabels={config:"配置",registry:"同步记录",canonical:"全局 Skill",rendered:"客户端部署内容",agent_links:"客户端链接"};
  const changed=Object.entries(writes).filter(([,value])=>value).map(([key])=>writeLabels[key]||key);
  const git=Object.entries(effects.git||{}).filter(([,value])=>value).map(([key])=>`Git ${key}`);
  return `<p>${changed.length?`将写入：${changed.map(escapeHtml).join("、")}`:"不会写入本地 Skill 状态"}</p><p>${effects.network?"正式执行会访问网络":"正式执行不访问网络"}${git.length?`；${git.map(escapeHtml).join("、")}`:"；无 Git 写操作"}</p>`;
}

function recoveryHint(value,fallback){
  return ({
    "The preview creates nothing. A predicted push commits later; pull has no automatic local backup.":"当前只是预览，不会创建备份。推送时会提交变更；拉取前不会自动备份本地内容。",
    "Resolve repository or content conflicts before running sync.":"如有同步冲突，需要先处理冲突再继续。",
    "The action keeps a temporary rollback copy until link verification succeeds.":"链接验证成功前会保留临时回滚副本。",
    "A failed rollback reports the preserved backup path and stops further mutations.":"如果回滚失败，会保留备份位置并停止后续写入。",
    "Disabling rolls the config and managed links back if unlinking fails.":"停用过程中如果移除链接失败，会自动恢复配置和已管理链接。",
    "If rollback cannot restore links, the action reports recovery details.":"如果自动恢复失败，会显示需要手动处理的位置。",
    "The action records previous managed targets before swapping links.":"更新链接前会记录原来的受管位置，以便失败时恢复。",
    "Interrupted swaps stop with a receipt that identifies links requiring recovery.":"如果更新中断，会停止后续操作并列出需要恢复的链接。",
    "No persistent backup remains after success; create an explicit backup first if needed.":"成功后不会保留额外备份；如需长期备份，请先创建本地备份。",
    "The action restores canonical paths and managed links if registry/config updates fail.":"如果同步记录或配置更新失败，会恢复原来的 Skill 和客户端链接。",
    "No backup created.":"确认前不会创建备份。",
    "Replan before retry.":"重试前会重新检查影响范围。",
  })[value]||value||fallback;
}
function planRecoveryHtml(plan){
  const backup=plan?.backup||{},recovery=plan?.recovery||{},operations=recovery.operations||[];
  return `<p>${escapeHtml(recoveryHint(backup.hint,"确认前不会创建备份。"))}</p><p>${escapeHtml(recoveryHint(recovery.hint,"失败时停止并保留诊断信息。"))}</p>${operations.length?`<p>待恢复操作：${operations.map(item=>escapeHtml(item.path||item.status||"未知")).join("、")}</p>`:""}`;
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
  const partial=pendingMutation.phase==="confirm"&&!pendingMutation.plan?.can_execute&&repairablePlanClients(pendingMutation.plan).length;
  (phrase?$("#mutation-delete-input"):partial?$("#mutation-partial"):pendingMutation.phase==="confirm"&&pendingMutation.plan?.can_execute?$("#mutation-confirm"):$("#mutation-dialog"))?.focus();
}

function renderMutationDialog(){
  if(!$("#mutation-layer"))return;
  if(!pendingMutation){setMutationLayer(false);return;}
  setMutationLayer(true);
  const pending=pendingMutation,plan=pending.plan,phase=pending.phase;
  $("#mutation-title").textContent=mutationTitle(pending.operation,pending.request);
  const status=$("#mutation-status");status.className=`mutation-status ${phase==="result"&&!pending.result?.ok?"result-error":phase==="confirm"?(plan?.status||"ready"):phase}`;
  status.textContent=phase==="planning"?"正在生成只读计划…":phase==="revalidating"?"正在重新规划…":phase==="running"?"正在执行…":phase==="result"?(pending.result?.ok?"操作已完成":"操作未完成"):mutationStatusLabel(plan?.status);
  $("#mutation-summary").textContent=planSummaryText(pending);
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
  const repairable=repairablePlanClients(plan),showPartial=phase==="confirm"&&pending.operation==="link-repair"&&!plan?.can_execute&&repairable.length>0;
  $("#mutation-partial").classList.toggle("hidden",!showPartial);$("#mutation-partial").disabled=working;$("#mutation-partial").textContent=`先修复可安全处理的 ${repairable.length} 个客户端`;
  $("#mutation-confirm").classList.toggle("hidden",phase==="result"||(phase==="confirm"&&plan&&!plan.can_execute));$("#mutation-confirm").setAttribute("aria-busy",String(working));$("#mutation-confirm").textContent=working?(phase==="running"?"正在执行…":"正在重新规划…"):mutationConfirmLabel(pending.operation,pending.request);
  updateMutationConfirmState();focusMutationDialog();
}

async function planSafeRepairSubset(){
  const pending=pendingMutation,agents=repairablePlanClients(pending?.plan).map(item=>item.client);
  if(!pending||pending.operation!=="link-repair"||pending.phase!=="confirm"||pending.plan?.can_execute||!agents.length)return false;
  pending.body={...pending.body,agents};pending.request={...pending.request,agents};pending.plan=null;pending.result=null;
  return refreshPendingPlan();
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
  if(ok&&pending.oneClickContext?.skipped?.length&&pending.result?.ok){
    pending.result.message=`已修复 ${pending.oneClickContext.repairable.join("、")}；${pending.oneClickContext.skipped.join("、")} 因目标位置已有其他内容未改动。`;
    renderMutationDialog();toast(pending.result.message);
  }
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
  $("#initial-loading")?.classList.add("hidden");
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
  const linkBusy=isBusy("link"),repairNames=repairCandidateSkillNames();
  setButtonState($("#repair-all"), linkBusy, '<i class="ri-loader-4-line"></i><span>正在修复…</span>', '<i class="ri-tools-line"></i><span>一键修复</span>', (locked&&!linkBusy)||!repairNames.length);
  setButtonState($("#setup-submit"), isBusy("init"), "正在连接…", "连接并开始使用", locked && !isBusy("init"));
  const selectionBusy = selectionOperationBusy();
  ["#select-all-checkbox","#select-selected","#link-selected","#copy-selected","#copy-agent","#delete-selected","#clear-selection"].forEach(selector => {
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
  setButtonState($("#detail-repair"), linkBusy, '<i class="ri-loader-4-line"></i>正在修复…', '<i class="ri-tools-line"></i>一键修复部署', (locked&&!linkBusy)||!detailSkill);
  const editStart=$("#detail-edit-start");if(editStart)editStart.disabled=locked||!detailSkill||Boolean(activeSessionForSkill(detailSkill));
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
  const issueRow=issue=>`<div class="issue-row"><i class="ri-error-warning-line"></i><strong>${escapeHtml(issueTitle(issue))}</strong><span>${escapeHtml(issue.detail || issue.skill || "需要检查")}</span></div>`;
  const visible=issues.slice(0,4),remaining=issues.slice(4);
  box.innerHTML=visible.map(issueRow).join("")+(remaining.length?`<details class="issue-overflow"><summary>查看其余 ${remaining.length} 项问题</summary>${remaining.map(issueRow).join("")}</details>`:"");
}

function managedDataReady(){return loadedViews.has("managed")||Boolean(state?.managed&&!state?.loaded_views);}
function skillUnits(skill){return Array.isArray(skill.units)?skill.units:[];}
function skillHasLocalChange(skill){return Boolean(skill.changed_local)||skillUnits(skill).some(unit=>unit.changed_local);}
function skillHasConflict(skill){return skillUnits(skill).some(unit=>unit.state==="conflict");}
function skillHasRemoteChange(skill){return skillUnits(skill).some(unit=>unit.changed_remote&&!unit.changed_local);}
function skillSyncState(skill){return !skill.selected?"local":skillHasLocalChange(skill)||skillHasConflict(skill)||skillHasRemoteChange(skill)?"changed":"synced";}
function skillSource(skill){return String(skill.platform||"global");}
function sourceLabel(source){return ({global:"全局库",codex:"Codex 导入",claude:"Claude Code 导入","claude-code":"Claude Code 导入",workbuddy:"WorkBuddy 导入",kimi:"Kimi Code 导入","kimi-code":"Kimi Code 导入"})[source]||source;}
function inventoryAgents(){return (state?.doctor?.agents||[]).filter(agent=>agent.detected);}
function agentCoversSkill(skill,agent){return ["linked","copied"].includes(skill.agents?.[agent]);}
function variantsForSkill(name){return (state?.managed?.variants?.variants||[]).filter(item=>item.skill===name);}
function sessionsForSkill(name){return (state?.managed?.sessions?.sessions||[]).filter(item=>item.skill===name||item.logical_skill===name);}
function deploymentForSkill(name){return (state?.managed?.deployments?.skills||[]).find(item=>item.name===name)||null;}
function activeSessionsForSkill(name){return sessionsForSkill(name).filter(item=>!["applied","aborted"].includes(item.status));}
function clientHasConflict(skill,client){return skillUnits(skill).some(unit=>unit.state==="conflict"&&(unit.kind==="base"||unit.target===client.agent||unit.target===client.client));}
function deploymentNeedsAttention(skill){return (deploymentForSkill(skill.name)?.clients||[]).some(client=>clientHasConflict(skill,client)||client.deployment_state!=="valid"||client.link_state!=="linked-render");}
function repairCandidateSkillNames(){return (state?.status?.skills||[]).filter(skill=>skill.selected&&deploymentNeedsAttention(skill)).map(skill=>skill.name);}
function shortHash(value){if(!value)return "hash unavailable";const text=String(value);return text.startsWith("sha256:")?text.slice(7,19):text.slice(0,12);}
function skillStateLabel(skill){if(!skill.selected)return "没有加入同步";if(skillHasConflict(skill))return "同步冲突";if(skillHasLocalChange(skill))return "本地待推送";if(skillHasRemoteChange(skill))return "远端待拉取";return "已同步";}
function skillStateClass(skill){if(skillHasConflict(skill))return "danger";if(skillHasLocalChange(skill)||skillHasRemoteChange(skill))return "warn";return skill.selected?"ok":"neutral";}
function skillBadgesHtml(skill){
  const variants=variantsForSkill(skill.name),sessions=activeSessionsForSkill(skill.name),badDeployment=deploymentNeedsAttention(skill);
  const badges=[`<span class="meta-badge mobile-sync-state ${skillStateClass(skill)}">${escapeHtml(skillStateLabel(skill))}</span>`];
  if(variants.length)badges.push(`<span class="meta-badge variant"><i class="ri-git-branch-line" aria-hidden="true"></i>${variants.length} Variant</span>`);
  if(sessions.length)badges.push(`<span class="meta-badge session"><i class="ri-edit-line" aria-hidden="true"></i>${sessions.length} 会话</span>`);
  if(badDeployment)badges.push(`<button type="button" class="meta-badge warning repair-badge" title="一键修复部署" aria-label="一键修复 ${escapeHtml(skill.name)} 的部署" data-action="repair-skill" data-skill-name="${escapeHtml(skill.name)}"><i class="ri-tools-line" aria-hidden="true"></i>部署异常</button>`);
  return badges.join("");
}
function renderInventoryControls(skills){
  const statuses=new Set(["synced","changed","local"]);
  const sources=[...new Set(skills.map(skillSource))].sort((a,b)=>sourceLabel(a).localeCompare(sourceLabel(b)));
  const agents=inventoryAgents();
  const readyInventory=inventoryDataReady(),readyAgents=agentDataReady();
  const previous=JSON.stringify(inventoryFilters);
  if(!statuses.has(inventoryFilters.status))inventoryFilters.status="synced";
  if(readyInventory&&inventoryFilters.source!=="all"&&!sources.includes(inventoryFilters.source))inventoryFilters.source="all";
  if(readyAgents&&inventoryFilters.agent!=="all"&&!agents.some(agent=>agent.name===inventoryFilters.agent))inventoryFilters.agent="all";
  const agentOptions=[...agents];
  if(!readyAgents&&inventoryFilters.agent!=="all"&&!agentOptions.some(agent=>agent.name===inventoryFilters.agent))agentOptions.push({name:inventoryFilters.agent,display_name:`${inventoryFilters.agent}（加载中）`});
  const counts={synced:0,changed:0,local:0};skills.forEach(skill=>{counts[skillSyncState(skill)]+=1;});
  for(const status of statuses){
    const button=$(`#status-${status}`);button.classList.toggle("active",inventoryFilters.status===status);button.setAttribute("aria-pressed",String(inventoryFilters.status===status));
    $(`#status-${status}-count`).textContent=counts[status];
  }
  updateSelectOptions($("#source-filter"),[{value:"all",label:"全部来源"},...sources.map(source=>({value:source,label:sourceLabel(source)}))]);
  updateSelectOptions($("#agent-filter"),[{value:"all",label:"全部 Agent"},...agentOptions.map(agent=>({value:agent.name,label:agent.display_name}))]);
  $("#source-filter").value=inventoryFilters.source;$("#agent-filter").value=inventoryFilters.agent;
  $("#clear-filters").disabled=!$("#search").value&&inventoryFilters.source==="all"&&inventoryFilters.agent==="all";
  if(previous!==JSON.stringify(inventoryFilters))persistUiContext();
}
function updateSelectOptions(select,options){
  const html=options.map(option=>`<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`).join("");
  if(select.dataset.optionsHtml!==html){select.innerHTML=html;select.dataset.optionsHtml=html;}
}
function visibleSkills(skills) {
  const query=$("#search").value.trim().toLowerCase();
  return skills.filter(skill => {
    const matchesQuery=!query||[skill.name,skill.description].some(value=>String(value||"").toLowerCase().includes(query));
    const matchesStatus=skillSyncState(skill)===inventoryFilters.status;
    const matchesSource=inventoryFilters.source==="all"||skillSource(skill)===inventoryFilters.source;
    const matchesAgent=inventoryFilters.agent==="all"||!agentDataReady()||agentCoversSkill(skill,inventoryFilters.agent);
    return matchesQuery&&matchesStatus&&matchesSource&&matchesAgent;
  });
}

function renderSkills(skills) {
  const previousFocus=captureSkillListFocus();
  let selectionChanged=false;
  if(inventoryDataReady())for (const name of [...selected]) if (!skills.some(skill => skill.name === name)){selected.delete(name);selectionChanged=true;}
  if(selectionChanged)persistUiContext();
  renderInventoryControls(skills);
  const shown = visibleSkills(skills);
  const agents = inventoryAgents();
  $("#skill-list").innerHTML = shown.map(skill => {
    const coverage = agents.length?agents.map(agent=>{const agentState=skill.agents?.[agent.name];return `<span class="agent-coverage ${agentStateClass(agentState)}" aria-label="${escapeHtml(agent.display_name)}：${escapeHtml(agentStateLabel(agentState))}"><i aria-hidden="true"></i><span>${escapeHtml(agent.display_name)}</span><small>${escapeHtml(agentStateLabel(agentState))}</small></span>`;}).join(""):'<span class="coverage-empty">暂无已检测 Agent</span>';
    const stateLabel=skillStateLabel(skill),stateClass=skillHasConflict(skill)?"conflict":skillHasLocalChange(skill)||skillHasRemoteChange(skill)?"pending":"";
    return `<article class="skill-row ${detailSkill === skill.name ? "focused" : ""}" tabindex="-1" data-action="open-detail" data-skill-name="${escapeHtml(skill.name)}" data-detail-trigger="row"><input type="checkbox" data-action="toggle-skill" data-skill-name="${escapeHtml(skill.name)}" aria-label="选择 ${escapeHtml(skill.name)}" ${selected.has(skill.name)?"checked":""} ${hasBusyOperation()?"disabled":""}><span class="file-icon"><i class="ri-file-text-line" aria-hidden="true"></i></span><span class="skill-identity"><strong>${escapeHtml(skill.name)}</strong><span class="skill-metadata"><code title="${escapeHtml(skill.local_hash||"")}">${escapeHtml(shortHash(skill.local_hash))}</code>${skillBadgesHtml(skill)}</span></span><span class="coverage">${coverage}</span><span class="row-status ${stateClass}">${escapeHtml(stateLabel)}</span><button type="button" class="icon-button" data-action="open-detail" data-skill-name="${escapeHtml(skill.name)}" data-detail-trigger="button" aria-label="查看 ${escapeHtml(skill.name)} 详情"><i class="ri-more-2-fill" aria-hidden="true"></i></button></article>`;
  }).join("") || `<p class="empty">这个分类中没有符合条件的 Skill</p>`;
  updateSkillSelectionUi(skills,shown);
  const categoryTotal=skills.filter(skill=>skillSyncState(skill)===inventoryFilters.status).length;
  $("#visible-count").textContent=`显示 ${shown.length} / ${categoryTotal}`;
  restoreSkillListFocus(previousFocus);
}

function captureSkillListFocus(){
  const active=document.activeElement,owner=active?.closest?.("[data-skill-name]");
  if(!owner||!$("#skill-list")?.contains?.(owner))return null;
  return {name:owner.dataset.skillName,action:active.dataset?.action||owner.dataset.action,trigger:active.dataset?.detailTrigger||owner.dataset.detailTrigger};
}
function restoreSkillListFocus(previous){
  if(!previous)return;
  const target=[...($("#skill-list")?.querySelectorAll?.("[data-skill-name]")||[])].find(item=>item.dataset.skillName===previous.name&&item.dataset.action===previous.action&&(!previous.trigger||item.dataset.detailTrigger===previous.trigger));
  target?.focus();
}
function updateSkillSelectionUi(skills,shown=visibleSkills(skills)){
  const allSelected=shown.length>0&&shown.every(skill=>selected.has(skill.name));
  const someSelected=shown.some(skill=>selected.has(skill.name));
  $("#select-all-checkbox").checked=allSelected;$("#select-all-checkbox").indeterminate=someSelected&&!allSelected;
  for(const checkbox of $("#skill-list")?.querySelectorAll?.('[data-action="toggle-skill"]')||[])checkbox.checked=selected.has(checkbox.dataset.skillName);
  const picked=skills.filter(skill=>selected.has(skill.name)),pickedAllSynced=picked.length>0&&picked.every(skill=>skill.selected);
  $("#select-selected").innerHTML=pickedAllSynced?'<i class="ri-subtract-line" aria-hidden="true"></i><span>取消同步</span>':'<i class="ri-add-circle-line" aria-hidden="true"></i><span>加入同步</span>';
  $("#select-selected").dataset.mode=pickedAllSynced?"deselect":"select";updateSelectionBar();
}

function renderAgents(agents) {
  $("#agent-list").innerHTML = agents.map(agent => {
    const busy = isBusy(`agent:${agent.name}`);
    const unavailable=(!agent.detected&&agent.enabled)||hasBusyOperation();
    return `<article class="agent-row"><span class="agent-icon"><i class="${agentIcon(agent.name)}" aria-hidden="true"></i></span><div><strong>${escapeHtml(agent.display_name)}</strong><p>${agent.detected ? escapeHtml(agent.skills_dir) : "未检测到此 Agent"}</p></div><span class="connection-state ${agent.detected && agent.enabled ? "online" : ""}"><i aria-hidden="true"></i>${agent.enabled ? (agent.detected ? "已连接" : "等待检测") : "已停用"}</span><button type="button" ${unavailable ? "disabled" : ""} aria-busy="${busy}" data-action="toggle-agent" data-agent-action="${escapeHtml(agent.name)}" data-enabled="${String(agent.enabled)}">${busy ? (agent.enabled ? "正在停用…" : "正在启用…") : (agent.enabled ? "停用" : "启用")}</button></article>`;
  }).join("");
}

function renderImports(items) {
  const sources=["codex","claude","workbuddy"], titles={codex:"Codex",claude:"Claude Code",workbuddy:"WorkBuddy"};
  for (const key of [...selectedImports]) if (!items.some(item => importKey(item) === key)) selectedImports.delete(key);
  $("#import-tabs").innerHTML = sources.map(agent => `<button type="button" class="source-tab ${activeImportAgent===agent?"active":""}" data-action="set-import-agent" data-agent="${agent}" ${hasBusyOperation()?"disabled":""}><i class="${agentIcon(agent)}" aria-hidden="true"></i><span>${titles[agent]}</span><b>${items.filter(item=>item.agent===agent).length}</b></button>`).join("");
  const group=items.filter(item=>item.agent===activeImportAgent);
  $("#imports").innerHTML = group.map(item => `<article class="import-row"><input type="checkbox" data-action="toggle-import" data-agent="${escapeHtml(item.agent)}" data-skill-name="${escapeHtml(item.name)}" aria-label="选择 ${escapeHtml(item.name)}" ${selectedImports.has(importKey(item))?"checked":""} ${item.state==="conflict" || hasBusyOperation()?"disabled":""}><span class="file-icon"><i class="ri-file-text-line" aria-hidden="true"></i></span><strong>${escapeHtml(item.name)}</strong><code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code>${item.state==="conflict"?'<span class="conflict-label"><i class="ri-error-warning-line" aria-hidden="true"></i>名称冲突</span>':""}</article>`).join("") || `<p class="empty">这个 Agent 中没有可导入的 Skill</p>`;
  const selectable=group.filter(item=>item.state!=="conflict");
  $("#select-all-imports").checked=selectable.length>0 && selectable.every(item=>selectedImports.has(importKey(item)));
  updateImportBar();
}

function unitForVariant(skill,target){return skillUnits(skill).find(unit=>unit.kind==="variant"&&unit.target===target);}
function variantStateLabel(skill,variant){const unit=unitForVariant(skill,variant.target);if(!variant.valid)return "无效";if(unit?.state==="conflict")return "冲突";if(unit?.changed_local)return "本地修改";if(unit?.changed_remote)return "远端修改";return "正常";}
function variantStateClass(skill,variant){const unit=unitForVariant(skill,variant.target);if(!variant.valid||unit?.state==="conflict")return "danger";if(unit?.changed_local||unit?.changed_remote)return "warn";return "ok";}
function sessionStatusLabel(value){return ({active:"编辑中",applying:"应用中",applied:"已应用",aborted:"已中止","needs-recovery":"需要恢复"})[value]||value||"未知";}
function sessionStatusClass(value){if(value==="needs-recovery")return "danger";if(["active","applying"].includes(value))return "warn";return "neutral";}
function clientDisplayName(value){return ({codex:"Codex",workbuddy:"WorkBuddy","kimi-code":"Kimi Code","claude-code":"Claude Code"})[value]||value;}
function familyDisplayName(value){return ({codex:"Codex",workbuddy:"WorkBuddy",kimi:"Kimi Code",claude:"Claude Code"})[value]||value;}
function deploymentLabel(skill,client){if(clientHasConflict(skill,client))return "内容同步冲突";if(client.deployment_state==="tampered")return "内容被修改";if(client.deployment_state==="stale")return "需要更新";if(client.deployment_state!=="valid")return "需要构建";if(client.link_state!=="linked-render")return "链接需要修复";return "正常";}
function deploymentDetail(skill,client){if(clientHasConflict(skill,client))return "Skill 内容存在同步冲突";if(client.link_state==="stale-render")return "链接仍指向旧版本";if(client.link_state==="conflict")return "目标位置已有其他内容";if(client.link_state==="wrong-link")return "链接指向其他位置";if(client.link_state==="broken-link")return "链接已失效";if(client.link_state==="tampered-render"||client.deployment_state==="tampered")return "部署内容被修改";if(client.link_state==="missing-render")return "部署文件已丢失";if(client.link_state==="missing")return "尚未创建链接";if(client.deployment_state!=="valid")return "需要重新构建";return "已连接当前版本";}
function deploymentClass(skill,client){if(clientHasConflict(skill,client))return "danger";return deploymentLabel(skill,client)==="正常"?"ok":"warn";}
function editScopeLabel(scope,target){if(scope==="base")return "全部客户端（Base）";if(scope==="family")return `${familyDisplayName(target)} 系列`;return clientDisplayName(target);}
function activeSessionForSkill(name){return sessionsForSkill(name).find(session=>session.status==="active")||null;}
function editChangeLabel(value){return ({added:"新增",modified:"修改",deleted:"删除"})[value]||value||"变更";}
function editClientActionLabel(value){return ({rebuild:"重新构建并更新",relink:"更新链接",noop:"无需部署变更",disabled:"客户端已停用",undetected:"本机未检测到",blocked:"暂时无法应用"})[value]||value||"检查";}
function editBlockerLabel(item){return ({invalid:"工作区校验未通过",unchanged:"工作区还没有改动","stale-baseline":"源内容在会话开始后发生了变化","canonical-changed-since-begin":"Base 内容在会话开始后发生了变化","canonical-layer-changed-since-begin":"差异层在会话开始后发生了变化",edit_inspection_scope_mismatch:"检查结果的修改范围不一致"})[item?.code]||item?.message||item?.code||"当前不能安全应用";}
function editInspectionHtml(inspection){
  const diff=inspection?.diff,validation=inspection?.validation,impact=inspection?.impact;
  const files=diff?.files||[],resolved=diff?.resolved_diffs||[],issues=validation?.issues||[],clients=(impact?.clients||[]).filter(client=>client.scope_affected??client.affected);
  const summary=diff?.summary||{};
  const changeSummary=diff?`${summary.added||0} 新增 · ${summary.modified||0} 修改 · ${summary.deleted||0} 删除`:"未能读取文件差异";
  const filesHtml=files.length?`<ul class="edit-file-list">${files.map(file=>`<li>${escapeHtml(editChangeLabel(file.change))} · ${escapeHtml(file.path)}</li>`).join("")}</ul>`:'<p class="edit-check-meta">工作区还没有改动</p>';
  const resolvedHtml=resolved.length?`<ul class="edit-client-list">${resolved.map(item=>`<li>${escapeHtml(clientDisplayName(item.client))}：${item.summary?.total||0} 个解析后文件变化</li>`).join("")}</ul>`:"";
  const issueHtml=issues.length?`<ul class="edit-issue-list">${issues.map(issue=>`<li>${escapeHtml(issue.path||"工作区")}：${escapeHtml(issue.message||issue.code)}</li>`).join("")}</ul>`:'<p class="edit-check-meta">文件结构和内容校验通过</p>';
  const clientHtml=clients.length?`<ul class="edit-client-list">${clients.map(client=>`<li>${escapeHtml(clientDisplayName(client.client))}：${escapeHtml(editClientActionLabel(client.action))}</li>`).join("")}</ul>`:'<p class="edit-check-meta">没有检测到需要更新的客户端</p>';
  const blockers=(inspection?.blockers||[]).map(editBlockerLabel);
  return `<div class="edit-inspection"><div class="edit-check"><header><strong>工作区改动</strong><em class="${diff?.changed?"warn":"neutral"}">${escapeHtml(changeSummary)}</em></header>${filesHtml}${resolvedHtml}</div><div class="edit-check"><header><strong>内容校验</strong><em class="${validation?.valid?"ok":"danger"}">${validation?.valid?"通过":"未通过"}</em></header>${issueHtml}</div><div class="edit-check"><header><strong>客户端影响</strong><em class="${impact?.blocked?"danger":"ok"}">${impact?.blocked?"已阻止":`${impact?.summary?.affected||0} 个受影响`}</em></header>${clientHtml}</div>${blockers.length?`<div class="mutation-finding"><strong>应用前需要处理</strong>：${escapeHtml([...new Set(blockers)].join("；"))}</div>`:""}</div>`;
}
async function inspectEditSession(sessionId,announce=true){
  const key=`edit-inspect:${sessionId}`;
  return runOperation(key,async()=>{
    if(announce)toast("正在检查编辑工作区…");
    try{
      const response=await postWithToken("/api/edit/inspect",{session_id:sessionId});
      const data=await readJsonResponse(response,"编辑会话检查结果无法解析");
      if(!response.ok)throw new Error(data.error||"无法检查编辑会话");
      editInspections.set(sessionId,data.inspection);renderDetail();
      if(announce)toast(data.inspection.can_apply?"检查完成，可以应用":"检查完成，请查看需要处理的项目");
      return true;
    }catch(error){if(announce)toast(`检查编辑会话失败：${error.message}`,true);return false;}
  });
}
function renderDetailVariants(skill){
  const box=$("#detail-variants");if(!box)return;
  if(!managedDataReady()){box.innerHTML='<p class="detail-empty">正在读取 Variant…</p>';return;}
  const variants=variantsForSkill(skill.name);
  box.innerHTML=variants.length?variants.map(variant=>`<div class="detail-item"><span><i class="ri-git-branch-line"></i><b>${escapeHtml(variant.target)}</b><small>${escapeHtml((variant.target_kinds||[]).join(" / ")||"客户端差异")} · ${variant.overlay_file_count??"?"} 个覆盖文件</small></span><em class="${variantStateClass(skill,variant)}">${escapeHtml(variantStateLabel(skill,variant))}</em></div>`).join(""):'<p class="detail-empty">所有客户端使用同一份内容</p>';
}
function renderDetailSessions(skill){
  const box=$("#detail-sessions");if(!box)return;
  if(!managedDataReady()){box.innerHTML='<p class="detail-empty">正在读取会话…</p>';return;}
  const sessions=sessionsForSkill(skill.name);
  box.innerHTML=sessions.length?sessions.map(session=>{
    const scope=session.target_scope?.kind||session.scope||"base",target=session.target_scope?.target||session.target||null,active=session.status==="active",inspection=editInspections.get(session.session_id),inspectedSession=inspection?.session||{};
    if(!active)return `<div class="detail-item"><span><i class="ri-edit-line"></i><b>${escapeHtml(editScopeLabel(scope,target))}</b><small>${escapeHtml(session.session_id||"")}</small></span><em class="${sessionStatusClass(session.status)}">${escapeHtml(sessionStatusLabel(session.status))}</em></div>`;
    const workspace=inspectedSession.workspace_path;
    return `<article class="edit-session-card"><div class="edit-session-head"><span><strong>${escapeHtml(editScopeLabel(scope,target))}</strong><small>${escapeHtml(session.session_id)}</small></span><em class="${sessionStatusClass(session.status)}">${escapeHtml(sessionStatusLabel(session.status))}</em></div>${workspace?`<div class="edit-session-path"><span>只在这个工作区修改文件</span><code>${escapeHtml(workspace)}</code></div>`:'<p class="detail-empty">检查工作区后显示编辑路径</p>'}${inspection?editInspectionHtml(inspection):'<p class="detail-empty">修改工作区文件后，点击“检查更改”查看差异和影响。</p>'}<div class="edit-session-actions"><button type="button" data-action="inspect-edit" data-session-id="${escapeHtml(session.session_id)}">${inspection?"重新检查":"检查更改"}</button><button type="button" class="primary" data-action="apply-edit" data-session-id="${escapeHtml(session.session_id)}" ${inspection?.can_apply?"":"disabled"}>应用更改</button><button type="button" class="danger" data-action="abort-edit" data-session-id="${escapeHtml(session.session_id)}">中止会话</button></div></article>`;
  }).join(""):'<p class="detail-empty">没有编辑会话</p>';
}
function renderDetailDeployments(skill){
  const box=$("#detail-deployments");if(!box)return;
  if(!managedDataReady()){box.innerHTML='<p class="detail-empty">正在读取 deployment…</p>';return;}
  const clients=deploymentForSkill(skill.name)?.clients||[];
  if(!clients.length){box.innerHTML='<p class="detail-empty">没有检测到可部署的 client</p>';return;}
  const groups=new Map();clients.forEach(client=>{const family=client.agent||"other";if(!groups.has(family))groups.set(family,[]);groups.get(family).push(client);});
  const order=["codex","workbuddy","kimi","claude"];
  box.innerHTML=[...groups].sort((a,b)=>(order.indexOf(a[0])<0?99:order.indexOf(a[0]))-(order.indexOf(b[0])<0?99:order.indexOf(b[0]))).map(([family,rows])=>`<section class="deployment-family"><header><strong>${escapeHtml(familyDisplayName(family))}</strong><span>${rows.length} 个客户端</span></header>${rows.sort((a,b)=>a.client.localeCompare(b.client)).map(client=>`<div class="deployment-client"><span><b>${escapeHtml(clientDisplayName(client.client))}</b><small>${escapeHtml(deploymentDetail(skill,client))}</small></span><em class="${deploymentClass(skill,client)}"><i></i>${escapeHtml(deploymentLabel(skill,client))}</em></div>`).join("")}</section>`).join("");
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
  $("#detail-status").className=`detail-status ${skillStateClass(skill)}`;
  $("#detail-status").innerHTML=`<i></i>${escapeHtml(skillStateLabel(skill))}`;
  $("#detail-description").textContent=skill.description||"暂无 description";
  $("#detail-sync").textContent=skill.selected?"已加入同步":"仅保存在本地";
  $("#detail-hash").textContent=skill.local_hash||"未计算";
  $("#detail-path").textContent=skill.local_path;
  renderDetailVariants(skill);
  renderDetailSessions(skill);
  renderDetailDeployments(skill);
  const editStart=$("#detail-edit-start");if(editStart)editStart.classList.toggle("hidden",Boolean(activeSessionForSkill(skill.name)));
  $("#detail-repair").classList.toggle("hidden",!deploymentNeedsAttention(skill));
  $("#detail-agents").innerHTML=(state.doctor?.agents||[]).map(agent=>`<div><span><i class="agent-dot ${agentStateClass(skill.agents?.[agent.name])}"></i>${escapeHtml(agent.display_name)}</span><b>${agentStateLabel(skill.agents?.[agent.name])}</b></div>`).join("");
  if (detailNeedsFocus && activeView==="skills") {
    detailNeedsFocus=false;
    $("#close-detail").focus();
  }
}

function toggle(name,checked){checked?selected.add(name):selected.delete(name);persistUiContext();updateSkillSelectionUi(state.status.skills||[]);}
function toggleVisibleSkills(){const shown=visibleSkills(state.status.skills||[]);const allSelected=shown.length>0&&shown.every(skill=>selected.has(skill.name));shown.forEach(skill=>allSelected ? selected.delete(skill.name) : selected.add(skill.name));persistUiContext();updateSkillSelectionUi(state.status.skills||[],shown);}
function clearSelection(){selected.clear();persistUiContext();updateSkillSelectionUi(state.status.skills||[]);}
function updateSelectionBar(){$("#selection-count").textContent=selected.size;$("#selection-bar").classList.toggle("hidden",!selected.size);}
function selectedNames(){if(!selected.size){toast("请先选择至少一个 Skill",true);return null;}return [...selected];}
function bulk(path){const skills=selectedNames();return skills&&action(path,{skills});}
function toggleSelectedSync(){return bulk($("#select-selected").dataset.mode==="deselect"?"/api/deselect":"/api/select");}
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
  if(updateHistory)updateDetailLocation(null,"replace");
  $("#detail-drawer").classList.add("hidden");renderSkills(state.status.skills||[]);
  if(restoreFocus)focusDetailReturnTarget();
}
function focusDetailReturnTarget(){
  const target=detailReturnTarget&&[...document.querySelectorAll("[data-skill-name]")].find(item=>item.dataset.skillName===detailReturnTarget.skill&&item.dataset.detailTrigger===detailReturnTarget.trigger);
  focusVisibleControl(target,$("#search"),$("#refresh"));
}
function handleDetailKeydown(event){
  if(!detailSkill)return;
  if(event.key==="Escape"){
    event.preventDefault();event.stopPropagation();closeDetail();return;
  }
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
  const fallback={activeView:"skills",search:"",filters:{status:"synced",source:"all",agent:"all"},selected:[]};
  try{
    const parsed=JSON.parse(globalThis.sessionStorage?.getItem(WEB_CONTEXT_STORAGE_KEY)||"null");
    if(!parsed||typeof parsed!=="object")return fallback;
    const filters=parsed.filters&&typeof parsed.filters==="object"?parsed.filters:{};
    return {
      activeView:["skills","agents","imports"].includes(parsed.activeView)?parsed.activeView:"skills",
      search:typeof parsed.search==="string"?parsed.search:"",
      filters:{status:typeof filters.status==="string"?filters.status:"synced",source:typeof filters.source==="string"?filters.source:"all",agent:typeof filters.agent==="string"?filters.agent:"all"},
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
function clearInventoryFilters(){inventoryFilters.source="all";inventoryFilters.agent="all";$("#search").value="";persistUiContext();renderSkills(state.status.skills||[]);$("#search").focus();}
function importKey(item){return `${item.agent}\u0000${item.name}`;}
function toggleImport(agent,name,checked){const key=`${agent}\u0000${name}`;checked?selectedImports.add(key):selectedImports.delete(key);renderImports(state.import_candidates||[]);}
function toggleAllImports(){const group=(state.import_candidates||[]).filter(item=>item.agent===activeImportAgent&&item.state!=="conflict");const all=group.length>0&&group.every(item=>selectedImports.has(importKey(item)));group.forEach(item=>all?selectedImports.delete(importKey(item)):selectedImports.add(importKey(item)));renderImports(state.import_candidates||[]);}
function updateImportBar(){const count=[...selectedImports].filter(key=>key.startsWith(`${activeImportAgent}\u0000`)).length;$("#import-count").textContent=count;$("#import-bar").classList.toggle("hidden",!count);}
async function importSelected(){const skills=[...selectedImports].filter(key=>key.startsWith(`${activeImportAgent}\u0000`)).map(key=>key.split("\u0000")[1]);if(!skills.length)return false;return requestPlannedMutation("import","/api/import",{skills,agent:activeImportAgent},$("#import-selected"),()=>{selectedImports.clear();renderImports(state.import_candidates||[]);});}
async function backupSkill(skill){await action("/api/backup", {skill});}
async function oneClickRepair(skills,origin){
  if(!skills.length){toast("当前没有需要修复的部署");return false;}
  if(!await requestPlannedMutation("link-repair","/api/link",{skills},origin))return false;
  const repairable=[...new Set(repairablePlanClients(pendingMutation?.plan).map(planClientName))];
  const skipped=[...new Set(blockedPlanClients(pendingMutation?.plan).map(planClientName))];
  if(pendingMutation?.plan&&!pendingMutation.plan.can_execute&&repairablePlanClients(pendingMutation.plan).length){
    if(!await planSafeRepairSubset())return false;
  }
  if(pendingMutation?.plan?.can_execute){
    pendingMutation.oneClickContext={repairable,skipped};pendingMutation.focusRequested=true;renderMutationDialog();return true;
  }
  return false;
}
async function repairSkill(skill,origin=null){return oneClickRepair([skill],origin||$("#detail-repair"));}
async function repairAllSkills(){return oneClickRepair(repairCandidateSkillNames(),$("#repair-all"));}
async function deleteSelected(){const skills=selectedNames();if(!skills)return false;return requestPlannedMutation("delete","/api/delete",{skills},$("#delete-selected"),()=>{selected.clear();persistUiContext();renderSkills(state.status.skills||[]);});}
function editTargetOptions(scope){return scope==="family"?[["codex","Codex"],["workbuddy","WorkBuddy"],["kimi","Kimi Code"],["claude","Claude Code"]]:[["codex","Codex"],["workbuddy","WorkBuddy"],["kimi-code","Kimi Code"],["claude-code","Claude Code"]];}
function setEditLayer(open){const layer=$("#edit-layer");if(!layer)return;layer.classList.toggle("hidden",!open);layer.setAttribute("aria-hidden",String(!open));if($("#app"))$("#app").inert=open||Boolean(pendingMutation);if($("#setup"))$("#setup").inert=open||Boolean(pendingMutation);}
function editConfirmationHtml(pending){
  if(pending.kind==="begin")return `<p><strong>${escapeHtml(pending.skill)}</strong> 将创建 ${escapeHtml(editScopeLabel(pending.scope,pending.target))} 编辑工作区。</p><p>创建后只应修改返回的 workspace；确认不会直接改全局源或客户端部署。</p>`;
  if(pending.kind==="abort")return `<p>将丢弃 <strong>${escapeHtml(editScopeLabel(pending.scope,pending.target))}</strong> 工作区中的未应用改动。</p><p>全局源和客户端部署不会被修改。</p>`;
  const inspection=pending.inspection,diff=inspection?.diff,impact=inspection?.impact,summary=diff?.summary||{},clients=(impact?.clients||[]).filter(client=>client.scope_affected??client.affected);
  return `<p>将应用 <strong>${summary.added||0} 个新增、${summary.modified||0} 个修改、${summary.deleted||0} 个删除</strong>。</p><p>受影响客户端：${clients.length?clients.map(client=>escapeHtml(clientDisplayName(client.client))).join("、"):"没有客户端部署变化"}</p><p>执行前服务端会再次检查差异、校验、影响范围和源内容是否变化；检查结果不同会停止。</p>`;
}
function renderEditDialog(){
  if(!$("#edit-layer"))return;if(!pendingEdit){setEditLayer(false);return;}setEditLayer(true);
  const pending=pendingEdit,working=pending.phase==="running",resultPhase=pending.phase==="result";
  $("#edit-title").textContent=pending.kind==="begin"?"开始托管编辑":pending.kind==="apply"?"应用编辑更改":"中止编辑会话";
  $("#edit-status").className=`mutation-status ${resultPhase&&!pending.result?.ok?"result-error":working?"running":""}`;
  $("#edit-status").textContent=pending.phase==="scope"?"选择修改范围":pending.phase==="confirm"?"等待确认":working?(pending.kind==="begin"?"正在创建…":pending.kind==="apply"?"正在应用…":"正在中止…"):(pending.result?.ok?"操作已完成":"操作未完成");
  $("#edit-summary").textContent=pending.kind==="begin"?(pending.phase==="scope"?"选择最小且准确的修改范围。":"确认后会创建独立工作区，不会立即修改已发布内容。"):(pending.kind==="apply"?"确认应用当前检查过的工作区更改。":"确认丢弃这个会话的工作区更改。");
  const scopeBox=$("#edit-scope");scopeBox.classList.toggle("hidden",pending.kind!=="begin"||pending.phase!=="scope");
  for(const input of document.querySelectorAll('input[name="edit-scope"]'))input.checked=input.value===pending.scope;
  const targetWrap=$("#edit-target-wrap"),showTarget=pending.kind==="begin"&&pending.phase==="scope"&&pending.scope!=="base";targetWrap.classList.toggle("hidden",!showTarget);
  if(showTarget){const options=editTargetOptions(pending.scope);updateSelectOptions($("#edit-target"),options.map(([value,label])=>({value,label})));if(!options.some(([value])=>value===pending.target))pending.target=options[0][0];$("#edit-target").value=pending.target;$("#edit-target-label").textContent=pending.scope==="family"?"客户端系列":"具体客户端";}
  const details=$("#edit-confirm-details");details.classList.toggle("hidden",pending.phase==="scope"||resultPhase);details.innerHTML=pending.phase==="scope"||resultPhase?"":editConfirmationHtml(pending);
  const result=$("#edit-result");result.className=`mutation-result ${resultPhase?(pending.result?.ok?"success":"error"):"hidden"}`;result.textContent=resultPhase?(pending.result?.message||"操作结果未知"):"";
  $("#edit-close").disabled=working;$("#edit-cancel").disabled=working;$("#edit-cancel").textContent=resultPhase?"关闭":"取消";
  const confirm=$("#edit-confirm");confirm.classList.toggle("hidden",resultPhase);confirm.disabled=working||(pending.kind==="apply"&&!pending.inspection?.can_apply);confirm.textContent=working?"正在处理…":pending.phase==="scope"?"下一步":pending.kind==="begin"?"确认开始":pending.kind==="apply"?"确认应用":"确认中止";
  if(pending.focusRequested){pending.focusRequested=false;(pending.phase==="scope"?document.querySelector('input[name="edit-scope"]:checked'):resultPhase?$("#edit-cancel"):confirm)?.focus();}
}
function openEditBegin(){if(!detailSkill||pendingEdit||pendingMutation||hasBusyOperation())return false;pendingEdit={kind:"begin",skill:detailSkill,scope:"base",target:null,phase:"scope",result:null,focusRequested:true};renderEditDialog();return true;}
function sessionById(sessionId){return (state?.managed?.sessions?.sessions||[]).find(session=>session.session_id===sessionId)||null;}
function openEditApply(sessionId){const session=sessionById(sessionId),inspection=editInspections.get(sessionId);if(!session||!inspection?.can_apply){toast("请先检查并处理工作区问题",true);return false;}pendingEdit={kind:"apply",skill:session.logical_skill||session.skill,sessionId,scope:session.target_scope?.kind||session.scope||"base",target:session.target_scope?.target||session.target||null,inspection,phase:"confirm",result:null,focusRequested:true};renderEditDialog();return true;}
function openEditAbort(sessionId){const session=sessionById(sessionId);if(!session||session.status!=="active")return false;pendingEdit={kind:"abort",skill:session.logical_skill||session.skill,sessionId,scope:session.target_scope?.kind||session.scope||"base",target:session.target_scope?.target||session.target||null,phase:"confirm",result:null,focusRequested:true};renderEditDialog();return true;}
async function confirmEditAction(){
  const pending=pendingEdit;if(!pending||pending.phase==="running"||pending.phase==="result")return false;
  if(pending.phase==="scope"){pending.phase="confirm";pending.focusRequested=true;renderEditDialog();return true;}
  pending.phase="running";renderEditDialog();let outcome=null,body,path;
  if(pending.kind==="begin"){body={skill:pending.skill,scope:pending.scope,...(pending.scope==="base"?{}:{target:pending.target})};path="/api/edit/begin";}
  else if(pending.kind==="apply"){body={session_id:pending.sessionId,inspection_id:pending.inspection.inspection_id};path="/api/edit/apply";}
  else{body={session_id:pending.sessionId};path="/api/edit/abort";}
  const ok=await action(path,body,{captureResult:value=>{outcome=value;}});if(pendingEdit!==pending)return ok;
  if(!ok&&outcome?.data?.details?.inspection){pending.inspection=outcome.data.details.inspection;editInspections.set(pending.sessionId,pending.inspection);}
  if(ok&&pending.kind==="begin"){pending.sessionId=outcome?.result?.session_id;if(pending.sessionId)await inspectEditSession(pending.sessionId,false);}
  if(ok&&pending.kind!=="begin")editInspections.delete(pending.sessionId);
  pending.phase="result";pending.result=outcome||{ok,message:ok?"操作已完成":"操作未完成"};pending.focusRequested=true;renderDetail();renderEditDialog();return ok;
}
function cancelEditAction(){if(!pendingEdit||pendingEdit.phase==="running")return false;pendingEdit=null;setEditLayer(false);focusVisibleControl($("#detail-edit-start"),$("#close-detail"),$("#search"));return true;}
function editFocusableElements(){return $("#edit-dialog")?[...$("#edit-dialog").querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(item=>!item.closest?.(".hidden")):[];}
function handleEditKeydown(event){
  if(!pendingEdit)return false;
  if(event.key==="Escape"){event.preventDefault();event.stopPropagation();cancelEditAction();return true;}
  if(event.key!=="Tab")return false;
  const focusable=editFocusableElements();if(!focusable.length){event.preventDefault();$("#edit-dialog").focus();return true;}
  const first=focusable[0],last=focusable[focusable.length-1],active=document.activeElement;
  if(event.shiftKey&&(active===first||!$("#edit-dialog").contains(active))){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&(active===last||!$("#edit-dialog").contains(active))){event.preventDefault();first.focus();}
  return true;
}
function handleDetailClick(event){const target=delegatedActionTarget(event),sessionId=target?.dataset?.sessionId;if(!target||!sessionId)return;if(target.dataset.action==="inspect-edit")inspectEditSession(sessionId);if(target.dataset.action==="apply-edit")openEditApply(sessionId);if(target.dataset.action==="abort-edit")openEditAbort(sessionId);}
function label(value){return({pull:"需要拉取",push:"需要推送","repair-links":"需要修复链接",conflict:"需要手动合并",blocked:"需要先处理",noop:"所有 Agent 已同步"})[value]||value;}
function previewSummary(value){return({pull:"远端有新的 Skill 变更可拉取。",push:"本地变更等待推送到远端。","repair-links":"Skill 内容一致，部分 Agent 链接需要修复。",conflict:"本地和远端均有修改，需要手动合并。",blocked:"同步仓库需要先处理。",noop:"所有受管 Skill 状态一致。"})[value]||"当前状态已更新。";}
function viewName(view){return({skills:"技能库",agents:"连接",imports:"导入"})[view]||"当前页面";}
function issueTitle(issue){return({"content-conflict":"本地与远端同时有修改","dirty-repository":"同步仓库存在额外改动",missing:"尚未创建链接",conflict:"目标位置已有其他内容",partial:"部分客户端尚未连接","stale-render":"链接仍指向旧版本","missing-render":"部署文件已丢失","tampered-render":"部署内容被修改","wrong-link":"链接指向其他位置","broken-link":"链接已失效"})[issue.type]||"需要检查部署";}
function agentStateClass(value){return["linked","copied"].includes(value)?"ok":value==="disabled"?"disabled":value==="missing"?"missing":"warn";}
function agentStateLabel(value){return value==="linked"?"已同步":value==="copied"?"本地副本":value==="disabled"?"已停用":value==="missing"?"未连接":"需检查";}
function agentIcon(name){return({codex:"ri-code-box-line",claude:"ri-command-line",workbuddy:"ri-robot-2-line",kimi:"ri-sparkling-line"})[name]||"ri-apps-line";}
function toast(message,error=false){
  if(activeToast?.error&&!error)return;
  const el=$("#toast");if(toastTimer!==null){clearTimeout(toastTimer);toastTimer=null;}
  activeToast={message,error};el.textContent=message;el.className=`show ${error?"error":""}`;el.setAttribute("role",error?"alert":"status");el.setAttribute("aria-live",error?"assertive":"polite");
  $("#toast-close")?.classList.toggle("hidden",!error);
  if(!error)toastTimer=setTimeout(dismissToast,3500);
}
function dismissToast(){
  if(toastTimer!==null){clearTimeout(toastTimer);toastTimer=null;}
  activeToast=null;const el=$("#toast");el.className="";$("#toast-close")?.classList.add("hidden");
}
function escapeHtml(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}

function delegatedActionTarget(event){return event.target?.closest?.("[data-action]")||null;}
function handleSkillListClick(event){
  const target=delegatedActionTarget(event);if(!target)return;
  if(target.dataset.action==="toggle-skill")return;
  const name=target.dataset.skillName;if(!name)return;
  if(target.dataset.action==="repair-skill"){event.stopPropagation();repairSkill(name,target);return;}
  if(target.dataset.action==="open-detail"){event.stopPropagation();openDetail(name,target);}
}
function handleSkillListChange(event){const target=delegatedActionTarget(event);if(target?.dataset.action==="toggle-skill")toggle(target.dataset.skillName,target.checked);}
function handleAgentListClick(event){
  const target=delegatedActionTarget(event);if(target?.dataset.action!=="toggle-agent")return;
  requestPlannedMutation("agent","/api/agent",{agent:target.dataset.agentAction,enabled:target.dataset.enabled!=="true"},target);
}
function handleImportTabsClick(event){const target=delegatedActionTarget(event);if(target?.dataset.action==="set-import-agent")setImportAgent(target.dataset.agent);}
function handleImportsChange(event){const target=delegatedActionTarget(event);if(target?.dataset.action==="toggle-import")toggleImport(target.dataset.agent,target.dataset.skillName,target.checked);}
function scheduleSearchRender(){
  persistUiContext();if(searchRenderTimer!==null)clearTimeout(searchRenderTimer);
  searchRenderTimer=setTimeout(()=>{searchRenderTimer=null;renderSkills(state.status.skills||[]);},200);
}
function isEditableTarget(target){return ["INPUT","TEXTAREA","SELECT"].includes(target?.tagName)||target?.isContentEditable;}

document.querySelectorAll(".nav-item[data-view]").forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$("#search").value=restoredUiContext.search;
showView(activeView);
$("#setup-form").onsubmit=async event=>{event.preventDefault();await action("/api/init",Object.fromEntries(new FormData(event.currentTarget)));};
$("#sync").onclick=()=>requestPlannedMutation("sync","/api/sync",{},$("#sync"));$("#repair-all").onclick=repairAllSkills;$("#refresh").onclick=()=>getState(true);
$("#retry-load").onclick=()=>getState(true);
$("#search").oninput=scheduleSearchRender;
$("#status-synced").onclick=()=>setInventoryFilter("status","synced");
$("#status-changed").onclick=()=>setInventoryFilter("status","changed");
$("#status-local").onclick=()=>setInventoryFilter("status","local");
$("#source-filter").onchange=event=>setInventoryFilter("source",event.currentTarget.value);
$("#agent-filter").onchange=event=>setInventoryFilter("agent",event.currentTarget.value);
$("#clear-filters").onclick=clearInventoryFilters;
$("#select-all-checkbox").onchange=toggleVisibleSkills;$("#clear-selection").onclick=clearSelection;
$("#select-selected").onclick=toggleSelectedSync;$("#link-selected").onclick=()=>{const skills=selectedNames();return skills&&requestPlannedMutation("link-repair","/api/link",{skills},$("#link-selected"));};
$("#copy-selected").onclick=()=>{const skills=selectedNames();return skills&&action("/api/copy",{skills,agents:[$("#copy-agent").value]});};$("#delete-selected").onclick=deleteSelected;
$("#close-detail").onclick=()=>closeDetail();$("#detail-repair").onclick=()=>detailSkill&&repairSkill(detailSkill,$("#detail-repair"));$("#detail-backup").onclick=()=>detailSkill&&backupSkill(detailSkill);
if($("#detail-edit-start"))$("#detail-edit-start").onclick=openEditBegin;
$("#detail-drawer").onkeydown=handleDetailKeydown;$("#detail-drawer").onclick=handleDetailClick;
$("#skill-list").onclick=handleSkillListClick;$("#skill-list").onchange=handleSkillListChange;
$("#agent-list").onclick=handleAgentListClick;$("#import-tabs").onclick=handleImportTabsClick;$("#imports").onchange=handleImportsChange;
if(globalThis.addEventListener){
  globalThis.addEventListener("popstate",restoreDetailFromLocation);
  globalThis.addEventListener("keydown",event=>{if(pendingMutation){handleMutationKeydown(event);return;}if(pendingEdit){handleEditKeydown(event);return;}if(event.key==="Escape"&&detailSkill&&!isEditableTarget(event.target))handleDetailKeydown(event);});
}
$("#select-all-imports").onchange=toggleAllImports;$("#clear-imports").onclick=()=>{selectedImports.clear();renderImports(state.import_candidates||[]);};$("#import-selected").onclick=importSelected;
if($("#mutation-confirm"))$("#mutation-confirm").onclick=confirmPendingMutation;
if($("#mutation-cancel"))$("#mutation-cancel").onclick=cancelPendingMutation;
if($("#mutation-close"))$("#mutation-close").onclick=cancelPendingMutation;
if($("#mutation-retry"))$("#mutation-retry").onclick=retryPendingMutation;
if($("#mutation-partial"))$("#mutation-partial").onclick=planSafeRepairSubset;
if($("#mutation-delete-input"))$("#mutation-delete-input").oninput=updateMutationConfirmState;
if($("#mutation-delete-input"))$("#mutation-delete-input").onkeydown=event=>{if(event.key==="Enter"&&!$("#mutation-confirm").disabled){event.preventDefault();confirmPendingMutation();}};
if($("#mutation-dialog"))$("#mutation-dialog").onkeydown=handleMutationKeydown;
if($("#edit-confirm"))$("#edit-confirm").onclick=confirmEditAction;
if($("#edit-cancel"))$("#edit-cancel").onclick=cancelEditAction;
if($("#edit-close"))$("#edit-close").onclick=cancelEditAction;
if($("#edit-scope"))$("#edit-scope").onchange=event=>{if(event.target?.name!=="edit-scope"||!pendingEdit)return;pendingEdit.scope=event.target.value;pendingEdit.target=null;renderEditDialog();};
if($("#edit-target"))$("#edit-target").onchange=event=>{if(pendingEdit)pendingEdit.target=event.currentTarget.value;};
if($("#edit-dialog"))$("#edit-dialog").onkeydown=handleEditKeydown;
if($("#toast-close"))$("#toast-close").onclick=dismissToast;
if (!globalThis.SKILL_SYNC_TEST) getState(false).catch(error=>toast(error.message,true));
