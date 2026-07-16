const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

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
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  focus() {}
}

const ids = [
  "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary",
  "issue-list","skill-list","search","search-wrap","search-toggle","sync-filter","source-filter","agent-filter","clear-filters","visible-count","select-all-checkbox",
  "select-all","select-selected","deselect-selected","link-selected","copy-selected","copy-agent",
  "delete-selected","clear-selection","selection-count","selection-bar","agent-list","import-tabs",
  "imports","select-all-imports","clear-imports","import-selected","import-count","import-bar",
  "detail-drawer","detail-name","detail-status","detail-description","detail-sync","detail-path",
  "detail-agents","detail-backup","close-detail","load-failure","retry-load","toast",
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
const views = ["skills","agents","imports"].map(name => new Element(`view-${name}`));
const nav = ["skills","agents","imports"].map(name => {
  const item = new Element(); item.dataset.view = name; return item;
});
const document = {
  querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] : null; },
  querySelectorAll(selector) {
    if (selector === ".view") return views;
    if (selector === ".nav-item[data-view]") return nav;
    return [];
  },
};

let fetchImpl = async () => { throw new Error("unexpected fetch"); };
let nextTimer = 1;
let clearedTimers = 0;
const timers = new Map();
const context = {
  console,
  document,
  URLSearchParams,
  FormData: class {},
  confirm: () => true,
  setTimeout: callback => { const id = nextTimer++; timers.set(id, callback); return id; },
  clearTimeout: id => { if (timers.delete(id)) clearedTimers += 1; },
  fetch: (...args) => fetchImpl(...args),
  SKILL_SYNC_TEST: true,
};
context.globalThis = context;
vm.createContext(context);
const appPath = process.argv[2];
const source = fs.readFileSync(appPath, "utf8") + `
globalThis.testApi = {
  action, getState, loadViews,
  setState(value) { state = value; render(); },
  clearState() { state = null; token = ""; },
  setToken(value) { token = value; },
  setActiveView(value) { activeView = value; showView(value); },
  setContext(search, names, detail) {
    document.querySelector("#search").value = search;
    selected.clear(); names.forEach(name => selected.add(name));
    detailSkill = detail; render();
  },
  snapshot() { return {state, activeView, selected:[...selected], detailSkill, busy:[...inFlightOperations.keys()]}; }
};`;
vm.runInContext(source, context, {filename: appPath});
const api = context.testApi;

function baseState(names = ["alpha","beta"]) {
  return {
    initialized: true,
    preview: {action:"noop", issues:[]},
    status: {skills:names.map(name => ({name, selected:true, changed_local:false, local_path:`/skills/${name}`}))},
    doctor: {agents:[], matrix:[], issues:[]},
    import_candidates: [],
  };
}

function response(data, {ok = true, status = 200, jsonError = null} = {}) {
  return {ok, status, json: async () => { if (jsonError) throw jsonError; return data; }};
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

async function testDuplicateAndGlobalMutationLock() {
  api.setState(baseState()); api.setToken("token");
  const pending = deferred(); let calls = 0;
  fetchImpl = async () => { calls += 1; return pending.promise; };
  const first = api.action("/api/sync");
  const duplicate = api.action("/api/sync");
  assert.equal(calls, 1, "double click must issue one request");
  assert.equal(elements.sync.disabled, true);
  assert.equal(elements.sync.getAttribute("aria-busy"), "true");
  assert.match(elements.sync.innerHTML, /正在同步/);
  const blocked = await api.action("/api/delete", {skills:["alpha"]});
  assert.equal(blocked, false, "a different mutation must not inherit the first result");
  assert.equal(calls, 1, "a different mutation must be globally blocked");
  const refresh = await api.getState();
  assert.equal(refresh, false, "refresh must not run during mutation");
  assert.equal(calls, 1);
  pending.resolve(response({result:{}, state:baseState()}));
  assert.equal(await first, true); assert.equal(await duplicate, true);
  assert.equal(elements.sync.disabled, false);
  assert.equal(elements.sync.getAttribute("aria-busy"), "false");
  assert.equal(elements.toast.textContent, "同步已完成");
  assert.equal(timers.size, 1, "only the latest toast timer may remain active");
  assert.ok(clearedTimers >= 2, "running toasts must not clear later results");
}

async function testStaleGetCannotOverwriteMutation() {
  api.setState(baseState(["old"])); api.setToken("token");
  const stale = deferred();
  fetchImpl = async (url, options) => {
    if (!options) return stale.promise;
    return response({result:{}, state:baseState(["fresh"])});
  };
  const read = api.loadViews(["inventory"], true);
  assert.equal(await api.action("/api/sync"), true);
  stale.resolve(response({initialized:true, loaded_views:["inventory"], status:baseState(["stale"]).status}));
  assert.equal(await read, false);
  assert.deepEqual(api.snapshot().state.status.skills.map(item => item.name), ["fresh"]);
}

async function testRefreshPreservesValidContext() {
  api.setState(baseState()); api.setToken("token");
  api.setActiveView("skills"); api.setContext("alp", ["alpha","beta"], "alpha");
  const calls = [];
  fetchImpl = async url => {
    calls.push(url);
    if (url.includes("inventory")) return response({initialized:true,loaded_views:["inventory"],status:baseState(["alpha"]).status});
    return response({initialized:true,loaded_views:["summary","agents"],preview:{action:"noop",issues:[]},doctor:{agents:[],matrix:[],issues:[]}});
  };
  assert.equal(await api.getState(), true);
  const snapshot = api.snapshot();
  assert.equal(snapshot.activeView, "skills");
  assert.equal(elements.search.value, "alp");
  assert.deepEqual(snapshot.selected, ["alpha"]);
  assert.equal(snapshot.detailSkill, "alpha", "a valid detail target must survive refresh");
  assert.deepEqual(calls, ["/api/state?view=inventory", "/api/state?view=summary&view=agents"]);
}

async function testFailureMessagesAndUnlock() {
  const cases = [
    [async () => response({error:"磁盘只读"}, {ok:false,status:400}), "同步失败：磁盘只读"],
    [async () => response({}, {jsonError:new Error("bad json")}), "操作结果未知，请刷新核验"],
    [async () => { throw new Error("offline"); }, "操作结果未知，请刷新核验"],
    [async () => response({error:"read failed",mutation_applied:true}, {ok:false,status:500}), "同步已完成，但状态刷新失败，请刷新核验"],
    [async () => response({error:"server failed"}, {ok:false,status:500}), "操作结果未知，请刷新核验"],
    [async () => response({result:{},state:null}), "操作已执行，但界面更新失败，请刷新核验"],
  ];
  for (const [implementation, message] of cases) {
    api.setState(baseState()); api.setToken("token"); fetchImpl = implementation;
    assert.equal(await api.action("/api/sync"), false);
    assert.equal(elements.toast.textContent, message);
    assert.deepEqual(api.snapshot().busy, []);
    assert.equal(elements.sync.disabled, false, `busy leaked after ${message}`);
  }
}

async function testPostKeepsCapturedView() {
  api.setState(baseState()); api.setToken("token"); api.setActiveView("agents");
  let posted;
  fetchImpl = async (_url, options) => { posted = JSON.parse(options.body); return response({result:{},state:{initialized:true,loaded_views:["agents"],doctor:{agents:[],matrix:[],issues:[]}}}); };
  assert.equal(await api.action("/api/agent", {agent:"codex",enabled:false}), true);
  assert.deepEqual(posted.views, ["agents"]);
}

async function testDynamicAgentAndSetupStates() {
  const agentState = enabled => ({
    ...baseState(),
    doctor: {agents:[
      {name:"codex",display_name:"Codex",detected:true,enabled,skills_dir:"/codex"},
      {name:"claude",display_name:"Claude Code",detected:false,enabled:true,skills_dir:"/claude"},
    ],matrix:[],issues:[]},
  });
  api.setState(agentState(true)); api.setToken("token");
  const pending = deferred(); fetchImpl = async () => pending.promise;
  const operation = api.action("/api/agent", {agent:"codex",enabled:false});
  assert.match(elements["agent-list"].innerHTML, /disabled aria-busy="true"[^>]*>正在停用…<\/button>/);
  pending.resolve(response({result:{},state:agentState(false)}));
  assert.equal(await operation, true);
  assert.match(elements["agent-list"].innerHTML, /aria-busy="false"[^>]*>启用<\/button>/);
  assert.match(elements["agent-list"].innerHTML, /Claude Code[\s\S]*?<button disabled aria-busy="false"/);

  api.setState({initialized:false,status:{skills:[]},doctor:{agents:[],matrix:[],issues:[]},import_candidates:[]});
  const setupPending = deferred(); fetchImpl = async () => setupPending.promise;
  const setup = api.action("/api/init", {repo:"git@example.test:skills.git"});
  assert.equal(elements["setup-submit"].disabled, true);
  assert.equal(elements["setup-submit"].getAttribute("aria-busy"), "true");
  assert.equal(elements["setup-submit"].innerHTML, "正在连接…");
  setupPending.resolve(response({result:{},state:baseState()}));
  assert.equal(await setup, true);
  assert.equal(elements["setup-submit"].disabled, false);
  assert.equal(elements["setup-submit"].getAttribute("aria-busy"), "false");
}

async function testInitialFailureHasVisibleRetry() {
  api.setActiveView("skills"); api.clearState(); fetchImpl = async () => { throw new Error("offline"); };
  assert.equal(await api.getState(false), false);
  assert.equal(elements["load-failure"].classList.contains("hidden"), false);
  assert.equal(elements["retry-load"].disabled, false);
  assert.equal(typeof elements["retry-load"].onclick, "function");
  assert.match(elements.toast.textContent, /刷新技能库失败/);
}

(async () => {
  await testDuplicateAndGlobalMutationLock();
  await testStaleGetCannotOverwriteMutation();
  await testRefreshPreservesValidContext();
  await testFailureMessagesAndUnlock();
  await testPostKeepsCapturedView();
  await testDynamicAgentAndSetupStates();
  await testInitialFailureHasVisibleRetry();
  process.stdout.write("web ui operation progress tests passed\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
