const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];
const htmlPath = process.argv[3];
const appSource = fs.readFileSync(appPath, "utf8");
const htmlSource = fs.readFileSync(htmlPath, "utf8");

class ClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force) {
    if (force === undefined) force = !this.values.has(name);
    force ? this.values.add(name) : this.values.delete(name);
    return force;
  }
  contains(name) { return this.values.has(name); }
}

class Element {
  constructor(id = "") {
    this.id = id;
    this.classList = new ClassList();
    this.dataset = {};
    this.attributes = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.innerHTML = "";
    this.textContent = "";
    this.className = "";
    this.inert = false;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { document.activeElement = this; }
  contains(item) { return Object.values(elements).includes(item); }
  querySelectorAll() {
    if (this.id !== "mutation-dialog") return [];
    return [elements["mutation-close"], elements["mutation-delete-input"], elements["mutation-cancel"], elements["mutation-retry"], elements["mutation-partial"], elements["mutation-confirm"]]
      .filter(item => !item.disabled && !item.classList.contains("hidden"));
  }
}

const ids = [
  "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary",
  "issue-list","skill-list","search","search-wrap","repair-all","status-tabs","status-synced","status-synced-count","status-changed","status-changed-count","status-local","status-local-count","source-filter","agent-filter",
  "clear-filters","visible-count","select-all-checkbox","select-all","select-selected",
  "deselect-selected","link-selected","copy-selected","copy-agent","delete-selected",
  "clear-selection","selection-count","selection-bar","agent-list","import-tabs","imports",
  "select-all-imports","clear-imports","import-selected","import-count","import-bar","detail-drawer",
  "detail-name","detail-status","detail-description","detail-sync","detail-hash","detail-path","detail-agents","detail-variants","detail-sessions","detail-deployments",
  "detail-repair","detail-backup","close-detail","load-failure","retry-load","toast","mutation-layer",
  "mutation-dialog","mutation-close","mutation-title","mutation-status","mutation-summary",
  "mutation-findings","mutation-targets","mutation-steps","mutation-effects","mutation-recovery",
  "mutation-delete-confirmation","mutation-delete-phrase","mutation-delete-input","mutation-result",
  "mutation-cancel","mutation-retry","mutation-partial","mutation-confirm",
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
const views = ["skills","agents","imports"].map(name => new Element(`view-${name}`));
const nav = ["skills","agents","imports"].map(name => {
  const item = new Element(); item.dataset.view = name; return item;
});
const listeners = {};
const document = {
  activeElement: null,
  querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] : null; },
  querySelectorAll(selector) {
    if (selector === ".view") return views;
    if (selector === ".nav-item[data-view]") return nav;
    if (selector === "[data-agent-action]") return [];
    return [];
  },
};

let fetchImpl = async () => { throw new Error("unexpected fetch"); };
const location = {href:"http://skill-sync.test/"};
const history = {
  state:null,
  pushState(value,_title,url){this.state=value;location.href=new URL(url,location.href).href;},
  replaceState(value,_title,url){this.state=value;location.href=new URL(url,location.href).href;},
};
const storage = new Map();
const context = {
  console, document, location, history, URL, URLSearchParams, FormData: class {},
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,String(value))},
  fetch:(...args)=>fetchImpl(...args), setTimeout:()=>1, clearTimeout:()=>{},
  addEventListener:(name,handler)=>{listeners[name]=handler;}, SKILL_SYNC_TEST:true,
};
context.globalThis = context;
vm.createContext(context);
const source = appSource + `
globalThis.testApi = {
  requestPlannedMutation, confirmPendingMutation, cancelPendingMutation, retryPendingMutation, repairSkill, planSafeRepairSubset,
  setState(value){state=value;render();}, setToken(value){token=value;},
  setDeletePhrase(value){document.querySelector("#mutation-delete-input").value=value;updateMutationConfirmState();},
  snapshot(){return {pending:pendingMutation&&{phase:pendingMutation.phase,operation:pendingMutation.operation,plan:pendingMutation.plan,result:pendingMutation.result},busy:[...inFlightOperations.keys()]};}
};`;
vm.runInContext(source, context, {filename:appPath});
const api = context.testApi;

function baseState(names=["alpha","beta"]){
  return {initialized:true,preview:{action:"noop",issues:[]},status:{skills:names.map(name=>({name,selected:true,changed_local:false,local_path:`/skills/${name}`,agents:{}}))},doctor:{agents:[],matrix:[],issues:[]},import_candidates:[]};
}
function response(data,{ok=true,status=200}={}){return {ok,status,json:async()=>data};}
function plan(operation,{skills=["alpha"],clients=[],summary="Ready",canExecute=true,status="ready"}={}){
  return {schema_version:1,operation,request:operation==="agent"?{agent:"codex",enabled:false}:{skills},status,can_execute:canExecute,summary,
    targets:{skills:skills.map(name=>({name,effect:operation})),clients},steps:skills.map((name,index)=>({order:index+1,action:operation,skill:name})),
    conflicts:status==="conflict"?[{code:"conflict",detail:"changed"}]:[],blockers:status==="blocked"?[{code:"blocked",detail:"stop"}]:[],warnings:[],
    effects:{predicted:true,network:false,git:{fetch:false,commit:false,push:false},writes:{config:true,registry:false,canonical:false,rendered:false,agent_links:false},permanent:operation==="delete",skills:skills.length,clients:clients.length,conflicts:[]},
    backup:{created:false,strategy:"none",hint:"No backup created."},recovery:{strategy:"retry",hint:"Replan before retry.",operations:[]},freshness:{remote_checked:false,replan_required:true},details:{}};
}

function assertHtmlContract(){
  for(const id of ["mutation-layer","mutation-dialog","mutation-confirm","mutation-cancel","mutation-retry","mutation-partial","mutation-delete-input"]){
    assert.match(htmlSource,new RegExp(`id="${id}"`));
  }
  assert.doesNotMatch(appSource,/\bconfirm\s*\(/,"native confirm must be removed");
}

async function testPlanBeforeMutationAndFreshReplan(){
  api.setState(baseState());api.setToken("token");elements.sync.focus();
  const ready=plan("sync");const calls=[];
  fetchImpl=async(url,options)=>{calls.push([url,JSON.parse(options.body)]);if(url==="/api/plan")return response(ready);return response({result:{},state:baseState()});};
  await api.requestPlannedMutation("sync","/api/sync",{},elements.sync);
  assert.deepEqual(calls.map(item=>item[0]),["/api/plan"]);
  assert.equal(api.snapshot().pending.phase,"confirm");
  assert.equal(elements["mutation-layer"].classList.contains("hidden"),false);
  await api.confirmPendingMutation();
  assert.deepEqual(calls.map(item=>item[0]),["/api/plan","/api/plan","/api/sync"]);
  assert.equal(api.snapshot().pending.phase,"result");
  assert.equal(api.snapshot().pending.result.ok,true);
}

async function testChangedPlanRequiresAnotherConfirmation(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");
  const first=plan("sync",{summary:"first"}),changed=plan("sync",{summary:"changed"});let planCalls=0,mutations=0;
  fetchImpl=async(url)=>{if(url==="/api/plan")return response(planCalls++===0?first:changed);mutations+=1;return response({result:{},state:baseState()});};
  await api.requestPlannedMutation("sync","/api/sync",{},elements.sync);
  await api.confirmPendingMutation();
  assert.equal(mutations,0,"changed replan must not mutate");
  assert.equal(api.snapshot().pending.phase,"confirm");
  assert.equal(api.snapshot().pending.plan.summary,"changed");
  await api.confirmPendingMutation();
  assert.equal(mutations,1,"second confirmation may execute an unchanged fresh plan");
}

async function testBlockedPlanAndDeletePhraseGate(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");let mutations=0;
  fetchImpl=async(url)=>url==="/api/plan"?response(plan("delete",{skills:["alpha"],canExecute:false,status:"blocked"})):(mutations+=1,response({}));
  await api.requestPlannedMutation("delete","/api/delete",{skills:["alpha"]},elements["delete-selected"]);
  assert.equal(elements["mutation-confirm"].disabled,true);
  assert.equal(elements["mutation-confirm"].classList.contains("hidden"),true);
  assert.equal(await api.confirmPendingMutation(),false);assert.equal(mutations,0);

  api.cancelPendingMutation();const multi=plan("delete",{skills:["alpha","beta"]});
  fetchImpl=async(url)=>url==="/api/plan"?response(multi):(mutations+=1,response({result:{},state:baseState([])}));
  await api.requestPlannedMutation("delete","/api/delete",{skills:["alpha","beta"]},elements["delete-selected"]);
  assert.equal(elements["mutation-delete-confirmation"].classList.contains("hidden"),false);
  assert.equal(elements["mutation-confirm"].disabled,true);
  api.setDeletePhrase("DELETE 2");assert.equal(elements["mutation-confirm"].disabled,false);
  await api.confirmPendingMutation();assert.equal(mutations,1);
}

async function testFailureResultRemainsActionable(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");const ready=plan("link-repair");let calls=0;
  fetchImpl=async(url)=>{if(url==="/api/plan")return response(ready);calls+=1;return response({error:"path changed"},{ok:false,status:400});};
  await api.requestPlannedMutation("link-repair","/api/link",{skills:["alpha"]},elements["link-selected"]);
  await api.confirmPendingMutation();
  const snapshot=api.snapshot();assert.equal(snapshot.pending.phase,"result");assert.equal(snapshot.pending.result.ok,false);
  assert.match(elements["mutation-result"].textContent,/path changed/);
  assert.equal(elements["mutation-retry"].classList.contains("hidden"),false);
  assert.equal(elements["mutation-layer"].classList.contains("hidden"),false);
  assert.equal(calls,1);
}

async function testEveryMutationUsesItsPlanContract(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");
  const cases=[
    ["import","/api/import",{skills:["alpha"],agent:"codex"},{skills:["alpha"],agent:"codex"}],
    ["agent","/api/agent",{agent:"codex",enabled:false},{agent:"codex",enabled:false}],
    ["link-repair","/api/link",{skills:["alpha"],agents:["codex"]},{skills:["alpha"],agents:["codex"]}],
    ["delete","/api/delete",{skills:["alpha"]},{skills:["alpha"]}],
  ];
  for(const [operation,path,body,expected] of cases){
    let posted;
    fetchImpl=async(url,options)=>{assert.equal(url,"/api/plan");posted=JSON.parse(options.body);return response(plan(operation,{skills:body.skills||["alpha"]}));};
    await api.requestPlannedMutation(operation,path,body,elements.sync);
    assert.deepEqual(posted,{operation,request:expected});
    assert.equal(api.snapshot().pending.operation,operation);
    api.cancelPendingMutation();
  }
}

async function testSkillRepairEntryUsesPlanThenLink(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");
  const ready=plan("link-repair",{skills:["alpha"]});const calls=[];
  fetchImpl=async(url,options)=>{calls.push([url,JSON.parse(options.body)]);if(url==="/api/plan")return response(ready);return response({result:{},state:baseState()});};
  await api.repairSkill("alpha",elements["detail-repair"]);
  assert.deepEqual(calls.map(item=>item[0]),["/api/plan"]);
  assert.deepEqual(calls[0][1],{operation:"link-repair",request:{skills:["alpha"]}});
  assert.equal(api.snapshot().pending.phase,"confirm");
  assert.equal(elements["mutation-confirm"].disabled,false);
  await api.confirmPendingMutation();
  assert.deepEqual(calls.map(item=>item[0]),["/api/plan","/api/plan","/api/link"]);
  assert.deepEqual(calls[2][1],{skills:["alpha"],views:["summary","inventory","agents","managed"]});
  assert.equal(api.snapshot().pending.phase,"result");
  assert.equal(api.snapshot().pending.result.ok,true);
  api.cancelPendingMutation();
}

async function testBlockedRepairExplainsConflictAndCanRetargetSafeClients(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");
  const clients=[
    {skill:"dws",client:"codex",effect:"blocked",current_state:"conflict"},
    {skill:"dws",client:"workbuddy",effect:"build-and-swap",current_state:"stale-render"},
    {skill:"dws",client:"kimi-code",effect:"build-and-swap",current_state:"stale-render"},
    {skill:"dws",client:"claude-code",effect:"blocked",current_state:"conflict"},
  ];
  const blocked=plan("link-repair",{skills:["dws"],clients,canExecute:false,status:"conflict"});
  blocked.steps=clients.map((item,index)=>({order:index+1,action:item.effect,skill:"dws",client:item.client,state:item.current_state}));
  blocked.conflicts=[{code:"conflict",client:"codex",destination:"/agents/codex/dws",detail:"Agent destination contains unmanaged content."},{code:"conflict",client:"claude-code",destination:"/agents/claude/dws",detail:"Agent destination contains unmanaged content."}];
  blocked.blockers=[{code:"unsafe-agent-path",client:"codex",destination:"/agents/codex/dws",detail:"Agent destination contains unmanaged content."},{code:"unsafe-agent-path",client:"claude-code",destination:"/agents/claude/dws",detail:"Agent destination contains unmanaged content."}];
  const safe=plan("link-repair",{skills:["dws"],clients:clients.slice(1,3)});const posted=[];
  fetchImpl=async(_url,options)=>{const body=JSON.parse(options.body);posted.push(body);return response(body.request.agents?safe:blocked);};
  await api.requestPlannedMutation("link-repair","/api/link",{skills:["dws"]},elements["detail-repair"]);
  assert.match(elements["mutation-summary"].textContent,/WorkBuddy、Kimi Code 可以自动修复/);
  assert.match(elements["mutation-summary"].textContent,/Codex、Claude Code.*不会覆盖|Codex、Claude Code.*避免覆盖/);
  assert.doesNotMatch(elements["mutation-findings"].innerHTML,/unsafe-agent-path|deployment is missing|stale-render/);
  assert.match(elements["mutation-findings"].innerHTML,/目标位置存在冲突/);
  assert.match(elements["mutation-findings"].innerHTML,/先备份或移走这个目录/);
  assert.match(elements["mutation-findings"].innerHTML,/\/agents\/codex\/dws/);
  assert.equal(elements["mutation-confirm"].classList.contains("hidden"),true);
  assert.equal(elements["mutation-partial"].classList.contains("hidden"),false);
  assert.equal(elements["mutation-partial"].textContent,"先修复可安全处理的 2 个客户端");
  await api.planSafeRepairSubset();
  assert.deepEqual(posted[1],{operation:"link-repair",request:{skills:["dws"],agents:["workbuddy","kimi-code"]}});
  assert.equal(elements["mutation-partial"].classList.contains("hidden"),true);
  assert.equal(elements["mutation-confirm"].classList.contains("hidden"),false);
  assert.equal(elements["mutation-confirm"].disabled,false);
  assert.doesNotMatch(elements["mutation-effects"].innerHTML,/Rendered deployment|Agent 链接/);
  assert.doesNotMatch(elements["mutation-recovery"].innerHTML,/The action records|Interrupted swaps/);
  api.cancelPendingMutation();
}

async function testOneClickRepairTargetsSafeClientsAndWaitsForConfirmation(){
  api.cancelPendingMutation();api.setState(baseState());api.setToken("token");
  const clients=[
    {skill:"dws",client:"codex",effect:"blocked",current_state:"conflict"},
    {skill:"dws",client:"workbuddy",effect:"build-and-swap",current_state:"stale-render"},
    {skill:"dws",client:"kimi-code",effect:"build-and-swap",current_state:"stale-render"},
  ];
  const blocked=plan("link-repair",{skills:["dws"],clients,canExecute:false,status:"conflict"});
  const safe=plan("link-repair",{skills:["dws"],clients:clients.slice(1)});const calls=[];
  fetchImpl=async(url,options)=>{
    const body=JSON.parse(options.body);calls.push([url,body]);
    if(url==="/api/link")return response({result:{},state:baseState()});
    return response(body.request.agents?safe:blocked);
  };
  await api.repairSkill("dws",elements["detail-repair"]);
  assert.deepEqual(calls.map(item=>item[0]),["/api/plan","/api/plan"]);
  assert.deepEqual(calls[1][1],{operation:"link-repair",request:{skills:["dws"],agents:["workbuddy","kimi-code"]}});
  assert.equal(api.snapshot().pending.phase,"confirm");
  assert.equal(elements["mutation-confirm"].disabled,false);
  assert.match(elements["mutation-summary"].textContent,/WorkBuddy、Kimi Code/);
  assert.match(elements["mutation-summary"].textContent,/Codex.*不会改动/);
  await api.confirmPendingMutation();
  assert.deepEqual(calls.map(item=>item[0]),["/api/plan","/api/plan","/api/plan","/api/link"]);
  assert.deepEqual(calls[3][1],{skills:["dws"],agents:["workbuddy","kimi-code"],views:["summary","inventory","agents","managed"]});
  assert.equal(api.snapshot().pending.phase,"result");
  assert.equal(api.snapshot().pending.result.ok,true);
  assert.equal(elements["mutation-result"].textContent,"已修复 WorkBuddy、Kimi Code；Codex 因目标位置已有其他内容未改动。");
  api.cancelPendingMutation();
}

async function testEscapeCancelsAndRestoresFocus(){
  api.setState(baseState());api.setToken("token");elements.sync.focus();
  fetchImpl=async()=>response(plan("sync"));
  await api.requestPlannedMutation("sync","/api/sync",{},elements.sync);
  let prevented=false;
  listeners.keydown({key:"Escape",preventDefault(){prevented=true;},stopPropagation(){}});
  assert.equal(prevented,true);assert.equal(api.snapshot().pending,null);assert.equal(document.activeElement,elements.sync);
}

async function testTabAndShiftTabStayInsideDialog(){
  api.setState(baseState());api.setToken("token");fetchImpl=async()=>response(plan("sync"));
  await api.requestPlannedMutation("sync","/api/sync",{},elements.sync);
  elements["mutation-confirm"].focus();let prevented=false;
  listeners.keydown({key:"Tab",shiftKey:false,preventDefault(){prevented=true;},stopPropagation(){}});
  assert.equal(prevented,true);assert.equal(document.activeElement,elements["mutation-close"]);
  prevented=false;elements["mutation-close"].focus();
  listeners.keydown({key:"Tab",shiftKey:true,preventDefault(){prevented=true;},stopPropagation(){}});
  assert.equal(prevented,true);assert.equal(document.activeElement,elements["mutation-confirm"]);
  api.cancelPendingMutation();
}

(async()=>{
  assertHtmlContract();
  await testPlanBeforeMutationAndFreshReplan();
  await testChangedPlanRequiresAnotherConfirmation();
  await testBlockedPlanAndDeletePhraseGate();
  await testFailureResultRemainsActionable();
  await testEveryMutationUsesItsPlanContract();
  await testSkillRepairEntryUsesPlanThenLink();
  await testBlockedRepairExplainsConflictAndCanRetargetSafeClients();
  await testOneClickRepairTargetsSafeClientsAndWaitsForConfirmation();
  await testEscapeCancelsAndRestoresFocus();
  await testTabAndShiftTabStayInsideDialog();
  process.stdout.write("mutation confirmation tests passed\n");
})().catch(error=>{console.error(error);process.exitCode=1;});
