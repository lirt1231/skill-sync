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

let document;
class Element {
  constructor(id = "") {
    this.id = id; this.classList = new ClassList(); this.dataset = {}; this.attributes = {};
    this.disabled = false; this.checked = false; this.value = ""; this.innerHTML = "";
    this.textContent = ""; this.className = ""; this.inert = false; this.name = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  focus() { document.activeElement = this; }
  closest(selector) { return selector === ".hidden" && this.classList.contains("hidden") ? this : null; }
  contains(item) { return Object.values(elements).includes(item) || radios.includes(item); }
  querySelectorAll() {
    if (this.id === "edit-dialog") return [elements["edit-close"], ...radios, elements["edit-target"], elements["edit-cancel"], elements["edit-confirm"]].filter(item => !item.disabled && !item.classList.contains("hidden"));
    return [];
  }
}

const ids = [
  "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary","issue-list",
  "skill-list","search","search-wrap","repair-all","status-synced","status-synced-count","status-changed","status-changed-count","status-local","status-local-count",
  "source-filter","agent-filter","clear-filters","visible-count","select-all-checkbox","select-selected","link-selected","copy-selected","copy-agent","delete-selected","clear-selection","selection-count","selection-bar",
  "agent-list","import-tabs","imports","select-all-imports","clear-imports","import-selected","import-count","import-bar",
  "detail-drawer","detail-name","detail-status","detail-description","detail-sync","detail-hash","detail-path","detail-agents","detail-variants","detail-sessions","detail-deployments","detail-repair","detail-backup","detail-edit-start","close-detail",
  "load-failure","retry-load","toast","toast-close","mutation-layer","mutation-dialog","mutation-close","mutation-title","mutation-status","mutation-summary","mutation-findings","mutation-targets","mutation-steps","mutation-effects","mutation-recovery","mutation-delete-confirmation","mutation-delete-phrase","mutation-delete-input","mutation-result","mutation-cancel","mutation-retry","mutation-partial","mutation-confirm",
  "edit-layer","edit-dialog","edit-close","edit-status","edit-title","edit-summary","edit-scope","edit-target-wrap","edit-target-label","edit-target","edit-confirm-details","edit-result","edit-cancel","edit-confirm",
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
const radios = ["base", "family", "client"].map(value => { const item = new Element(); item.name = "edit-scope"; item.value = value; item.checked = value === "base"; return item; });
const views = ["skills", "agents", "imports"].map(name => { const item = new Element(`view-${name}`); item.dataset.view = name; return item; });
const nav = ["skills", "agents", "imports"].map(name => { const item = new Element(); item.dataset.view = name; return item; });
document = {
  activeElement: null,
  querySelector(selector) {
    if (selector.startsWith("#")) return elements[selector.slice(1)];
    if (selector === 'input[name="edit-scope"]:checked') return radios.find(item => item.checked) || null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === ".view") return views;
    if (selector === ".nav-item[data-view]") return nav;
    if (selector === 'input[name="edit-scope"]') return radios;
    if (selector === "[data-agent-action]" || selector === "[data-skill-name]") return [];
    return [];
  },
};

let fetchImpl = async () => { throw new Error("unexpected fetch"); };
const listeners = {};
const context = {
  console, document, URL, URLSearchParams, FormData: class {},
  location: {href: "http://skill-sync.test/"}, history: {state: null, pushState() {}, replaceState() {}},
  sessionStorage: {getItem: () => null, setItem() {}},
  fetch: (...args) => fetchImpl(...args), setTimeout: () => 1, clearTimeout: () => {},
  addEventListener: (name, handler) => { listeners[name] = handler; }, SKILL_SYNC_TEST: true,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(appSource + `
globalThis.testApi={
  setState(value){state=value;detailSkill="alpha";render();},setToken(value){token=value;},
  setEditScope(value){pendingEdit.scope=value;pendingEdit.target=null;renderEditDialog();},
  setEditTarget(value){pendingEdit.target=value;document.querySelector("#edit-target").value=value;},
  openEditBegin,openEditApply,openEditAbort,confirmEditAction,cancelEditAction,inspectEditSession,handleEditKeydown,
  snapshot(){return {pending:pendingEdit&&{kind:pendingEdit.kind,phase:pendingEdit.phase,scope:pendingEdit.scope,target:pendingEdit.target,result:pendingEdit.result},inspection:editInspections.get("session-1")||null};}
};`, context, {filename: appPath});
const api = context.testApi;

function session() {
  return {session_id: "session-1", logical_skill: "alpha", status: "active", target_scope: {kind: "client", target: "kimi-code"}};
}
function baseState(active = null) {
  return {initialized: true, preview: {action: "noop", issues: []}, status: {skills: [{name: "alpha", selected: true, changed_local: false, local_path: "/skills/alpha", agents: {}}]}, doctor: {agents: [], matrix: [], issues: []}, managed: {variants: {variants: []}, deployments: {skills: []}, sessions: {sessions: active ? [active] : []}}, import_candidates: []};
}
function inspection({canApply = true, changed = true} = {}) {
  return {
    schema_version: 1, inspection_id: "sha256:" + "a".repeat(64), can_apply: canApply,
    session: {...session(), workspace_path: "/data/edit-sessions/session-1/workspace"},
    diff: {session_id: "session-1", skill: "alpha", scope: "client", target: "kimi-code", changed, summary: {added: 1, modified: 1, deleted: 0, total: 2}, files: [{change: "modified", path: "SKILL.md"}], resolved_diffs: [{client: "kimi-code", summary: {total: 2}}]},
    validation: {valid: canApply, changed, issues: canApply ? [] : [{path: "SKILL.md", message: "frontmatter 无效"}]},
    impact: {blocked: !canApply, has_workspace_changes: changed, summary: {affected: 1, requires_rebuild: 1}, clients: [{client: "kimi-code", scope_affected: true, affected: true, action: canApply ? "rebuild" : "blocked"}]},
    blockers: canApply ? [] : [{code: "invalid", message: "workspace validation failed"}], errors: [],
  };
}
function response(data, {ok = true, status = 200} = {}) { return {ok, status, json: async () => data}; }

function assertContract() {
  for (const id of ["detail-edit-start", "edit-layer", "edit-dialog", "edit-scope", "edit-target", "edit-confirm", "edit-cancel"]) assert.match(htmlSource, new RegExp(`id="${id}"`));
  assert.doesNotMatch(appSource, /on(?:click|change)="/, "dynamic markup must not contain inline handlers");
}

async function testBeginHasScopeStepAndExplicitConfirmation() {
  api.setState(baseState()); api.setToken("token"); const calls = [];
  fetchImpl = async (url, options) => {
    const body = JSON.parse(options.body); calls.push([url, body]);
    if (url === "/api/edit/begin") return response({result: {...session(), skill: "alpha", scope: "client", target: "kimi-code"}, state: baseState(session())});
    if (url === "/api/edit/inspect") return response({inspection: inspection({canApply: false, changed: false})});
    throw new Error("unexpected URL");
  };
  assert.equal(api.openEditBegin(), true); assert.equal(api.snapshot().pending.phase, "scope"); assert.equal(calls.length, 0);
  api.setEditScope("client"); api.setEditTarget("kimi-code");
  await api.confirmEditAction(); assert.equal(api.snapshot().pending.phase, "confirm"); assert.equal(calls.length, 0);
  assert.match(elements["edit-confirm-details"].innerHTML, /Kimi Code/);
  await api.confirmEditAction();
  assert.deepEqual(calls.map(item => item[0]), ["/api/edit/begin", "/api/edit/inspect"]);
  assert.deepEqual(calls[0][1], {skill: "alpha", scope: "client", target: "kimi-code", views: ["summary", "inventory", "agents", "managed"]});
  assert.equal(api.snapshot().pending.phase, "result");
  assert.match(elements["detail-sessions"].innerHTML, /\/data\/edit-sessions\/session-1\/workspace/);
}

async function testInspectionControlsApplyAndShowsConcreteImpact() {
  api.cancelEditAction(); api.setState(baseState(session())); api.setToken("token");
  fetchImpl = async () => response({inspection: inspection({canApply: false})});
  await api.inspectEditSession("session-1", false);
  assert.match(elements["detail-sessions"].innerHTML, /SKILL\.md/);
  assert.match(elements["detail-sessions"].innerHTML, /Kimi Code：2 个解析后文件变化/);
  assert.match(elements["detail-sessions"].innerHTML, /Kimi Code：暂时无法应用/);
  assert.match(elements["detail-sessions"].innerHTML, /data-action="apply-edit"[^>]*disabled/);
  assert.equal(api.openEditApply("session-1"), false);
}

async function testApplyAndAbortWaitForConfirmation() {
  api.setState(baseState(session())); api.setToken("token"); const calls = [];
  fetchImpl = async (url, options) => {
    const body = JSON.parse(options.body); calls.push([url, body]);
    if (url === "/api/edit/inspect") return response({inspection: inspection()});
    if (url === "/api/edit/apply") return response({result: {status: "applied"}, state: baseState()});
    if (url === "/api/edit/abort") return response({result: {status: "aborted"}, state: baseState()});
    throw new Error("unexpected URL");
  };
  await api.inspectEditSession("session-1", false); calls.length = 0;
  assert.equal(api.openEditApply("session-1"), true); assert.equal(calls.length, 0);
  assert.match(elements["edit-confirm-details"].innerHTML, /服务端会再次检查/);
  await api.confirmEditAction(); assert.deepEqual(calls.map(item => item[0]), ["/api/edit/apply"]);
  assert.equal(api.snapshot().pending.phase, "result");

  api.cancelEditAction(); api.setState(baseState(session())); calls.length = 0;
  assert.equal(api.openEditAbort("session-1"), true); assert.equal(calls.length, 0);
  assert.match(elements["edit-confirm-details"].innerHTML, /丢弃/);
  await api.confirmEditAction(); assert.deepEqual(calls.map(item => item[0]), ["/api/edit/abort"]);
}

async function testFreshnessFailureRemainsActionable() {
  api.cancelEditAction(); api.setState(baseState(session())); api.setToken("token"); const first = inspection(), changed = inspection({canApply: false});
  fetchImpl = async (url) => url === "/api/edit/inspect" ? response({inspection: first}) : response({error: "检查结果已变化", code: "edit_inspection_changed", details: {inspection: changed}}, {ok: false, status: 400});
  await api.inspectEditSession("session-1", false); api.openEditApply("session-1"); await api.confirmEditAction();
  assert.equal(api.snapshot().pending.phase, "result"); assert.equal(api.snapshot().pending.result.ok, false);
  assert.equal(api.snapshot().inspection.can_apply, false);
  assert.match(elements["edit-result"].textContent, /检查结果已变化/);
  assert.equal(elements["edit-layer"].classList.contains("hidden"), false);
}

(async () => {
  assertContract();
  await testBeginHasScopeStepAndExplicitConfirmation();
  await testInspectionControlsApplyAndShowsConcreteImpact();
  await testApplyAndAbortWaitForConfirmation();
  await testFreshnessFailureRemainsActionable();
  process.stdout.write("web ui edit session tests passed\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
