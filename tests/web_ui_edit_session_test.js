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
  contains(item) { return Object.values(elements).includes(item) || radios.includes(item) || agentRadios.includes(item); }
  querySelectorAll() {
    if (this.id === "edit-dialog") return [elements["edit-close"], ...radios, ...agentRadios, elements["edit-target"], elements["edit-cancel"], elements["edit-confirm"]].filter(item => !item.disabled && !item.classList.contains("hidden"));
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
  "edit-layer","edit-dialog","edit-close","edit-status","edit-title","edit-summary","edit-scope","edit-agent","edit-agent-codex-option","edit-agent-codex-hint","edit-agent-kimi-code-option","edit-agent-kimi-code-hint","edit-target-wrap","edit-target-label","edit-target","edit-confirm-details","edit-result","edit-cancel","edit-confirm",
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
const radios = ["base", "family", "client"].map(value => { const item = new Element(); item.name = "edit-scope"; item.value = value; item.checked = value === "base"; return item; });
const agentRadios = ["codex", "kimi-code"].map(value => { const item = new Element(); item.name = "edit-agent"; item.value = value; item.checked = value === "codex"; return item; });
const views = ["skills", "agents", "imports"].map(name => { const item = new Element(`view-${name}`); item.dataset.view = name; return item; });
const nav = ["skills", "agents", "imports"].map(name => { const item = new Element(); item.dataset.view = name; return item; });
document = {
  activeElement: null,
  querySelector(selector) {
    if (selector.startsWith("#")) return elements[selector.slice(1)];
    if (selector === 'input[name="edit-scope"]:checked') return radios.find(item => item.checked) || null;
    if (selector === 'input[name="edit-agent"]:checked') return agentRadios.find(item => item.checked) || null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === ".view") return views;
    if (selector === ".nav-item[data-view]") return nav;
    if (selector === 'input[name="edit-scope"]') return radios;
    if (selector === 'input[name="edit-agent"]') return agentRadios;
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
  setEditAgent(value){pendingEdit.agent=value;renderEditDialog();},
  setEditTarget(value){pendingEdit.target=value;document.querySelector("#edit-target").value=value;},
  openEditBegin,openEditApply,openEditAbort,openEditDelete,confirmEditAction,cancelEditAction,inspectEditSession,launchEditAgent,handleEditKeydown,
  snapshot(){return {pending:pendingEdit&&{kind:pendingEdit.kind,phase:pendingEdit.phase,scope:pendingEdit.scope,target:pendingEdit.target,agent:pendingEdit.agent,result:pendingEdit.result},inspection:editInspections.get("session-1")||null};}
};`, context, {filename: appPath});
const api = context.testApi;

function session() {
  return {session_id: "session-1", logical_skill: "alpha", status: "active", target_scope: {kind: "client", target: "kimi-code"}};
}
function baseState(active = null) {
  return {initialized: true, preview: {action: "noop", issues: []}, status: {skills: [{name: "alpha", selected: true, changed_local: false, local_path: "/skills/alpha", agents: {}}]}, doctor: {agents: [], matrix: [], issues: []}, managed: {variants: {variants: []}, deployments: {skills: []}, sessions: {sessions: active ? [active] : []}, edit_agents: {agents: [
    {agent: "codex", display_name: "Codex", executable_name: "codex", executable_path: "/opt/homebrew/bin/codex", installed: true, available: true, reason: null},
    {agent: "kimi-code", display_name: "Kimi Code", executable_name: "kimi", executable_path: "/Users/test/.kimi-code/bin/kimi", installed: true, available: true, reason: null},
  ]}}, import_candidates: []};
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
  for (const id of ["detail-edit-start", "edit-layer", "edit-dialog", "edit-scope", "edit-agent", "edit-target", "edit-confirm", "edit-cancel"]) assert.match(htmlSource, new RegExp(`id="${id}"`));
  assert.doesNotMatch(appSource, /on(?:click|change)="/, "dynamic markup must not contain inline handlers");
}

async function testBeginHasScopeStepAndExplicitConfirmation() {
  api.setState(baseState()); api.setToken("token"); const calls = [];
  fetchImpl = async (url, options) => {
    const body = JSON.parse(options.body); calls.push([url, body]);
    if (url === "/api/edit/begin") return response({result: {...session(), skill: "alpha", scope: "client", target: "kimi-code"}, state: baseState(session())});
    if (url === "/api/edit/launch") return response({result: {session_id: "session-1", agent: "kimi-code", launched: true, instruction: "Kimi Code 已在受管工作区中打开。请在会话中说明要修改的内容。"}});
    if (url === "/api/edit/inspect") return response({inspection: inspection({canApply: false, changed: false})});
    throw new Error("unexpected URL");
  };
  assert.equal(api.openEditBegin(), true); assert.equal(api.snapshot().pending.phase, "scope"); assert.equal(calls.length, 0);
  assert.match(elements["edit-agent-codex-hint"].textContent, /已安装.*codex/);
  api.setEditScope("client"); api.setEditTarget("kimi-code"); api.setEditAgent("kimi-code");
  await api.confirmEditAction(); assert.equal(api.snapshot().pending.phase, "confirm"); assert.equal(calls.length, 0);
  assert.match(elements["edit-confirm-details"].innerHTML, /新终端中打开 <strong>Kimi Code/);
  await api.confirmEditAction();
  assert.deepEqual(calls.map(item => item[0]), ["/api/edit/begin", "/api/edit/launch", "/api/edit/inspect"]);
  assert.deepEqual(calls[0][1], {skill: "alpha", scope: "client", actor: "kimi-code", target: "kimi-code", views: ["summary", "inventory", "agents", "managed"]});
  assert.deepEqual(calls[1][1], {session_id: "session-1", agent: "kimi-code"});
  assert.equal(api.snapshot().pending.phase, "result");
  assert.match(elements["edit-result"].textContent, /在会话中说明要修改的内容/);
  assert.match(elements["detail-sessions"].innerHTML, /\/data\/edit-sessions\/session-1\/workspace/);
  assert.match(elements["detail-sessions"].innerHTML, /data-action="launch-edit-agent" data-agent="codex"/);
  assert.match(elements["detail-sessions"].innerHTML, /data-agent="kimi-code"/);
}

async function testUnavailableAgentsAreDisabledBeforeConfirmation() {
  api.cancelEditAction(); const onlyKimi = baseState();
  onlyKimi.managed.edit_agents.agents[0] = {...onlyKimi.managed.edit_agents.agents[0], executable_path: null, installed: false, available: false, reason: "not-installed"};
  api.setState(onlyKimi); api.setToken("token");
  assert.equal(api.openEditBegin(), true);
  assert.equal(api.snapshot().pending.agent, "kimi-code");
  assert.equal(agentRadios[0].disabled, true);
  assert.match(elements["edit-agent-codex-hint"].textContent, /未安装/);
  assert.equal(agentRadios[1].disabled, false);
  assert.match(elements["detail-sessions"].innerHTML, /^$/);

  api.cancelEditAction(); const neither = baseState();
  neither.managed.edit_agents.agents = neither.managed.edit_agents.agents.map(item=>({...item, executable_path: null, installed: false, available: false, reason: "not-installed"}));
  api.setState(neither); api.openEditBegin();
  assert.equal(api.snapshot().pending.agent, null);
  assert.equal(elements["edit-confirm"].disabled, true);
  assert.equal(await api.confirmEditAction(), false);
  assert.equal(api.snapshot().pending.phase, "scope");
}

async function testLaunchFailureKeepsSessionAndRetryWorks() {
  api.cancelEditAction(); api.setState(baseState()); api.setToken("token"); const calls = [];
  fetchImpl = async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    if (url === "/api/edit/begin") return response({result: {...session(), actor: "codex"}, state: baseState(session())});
    if (url === "/api/edit/launch") return response({error: "Terminal denied"}, {ok: false, status: 400});
    if (url === "/api/edit/inspect") return response({inspection: inspection({canApply: false, changed: false})});
    throw new Error("unexpected URL");
  };
  api.openEditBegin(); await api.confirmEditAction(); await api.confirmEditAction();
  assert.equal(api.snapshot().pending.phase, "result");
  assert.equal(api.snapshot().pending.result.ok, false);
  assert.match(elements["edit-result"].textContent, /工作区已创建，但未能打开 Codex/);
  assert.match(elements["detail-sessions"].innerHTML, /在 Codex 中打开/);
  const retry = await api.launchEditAgent("session-1", "codex", false);
  assert.equal(retry.ok, false);
  assert.equal(calls.filter(item => item[0] === "/api/edit/launch").length, 2);
}

async function testOldBackendHasActionableRestartMessage() {
  api.cancelEditAction(); api.setState(baseState(session())); api.setToken("token");
  fetchImpl = async url => url === "/api/edit/launch"
    ? response({error: "unknown action"}, {ok: false, status: 404})
    : response({inspection: inspection({canApply: false, changed: false})});
  const result = await api.launchEditAgent("session-1", "codex", false);
  assert.equal(result.ok, false);
  assert.match(result.message, /服务版本过旧，请重启 skill-sync web/);
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

async function testDeleteWaitsForConfirmationAndBlocksRecoverySession() {
  api.cancelEditAction(); api.setState(baseState(session())); api.setToken("token"); const calls = [];
  fetchImpl = async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    if (url === "/api/edit/delete") return response({result: {task_id: "task-1", session_id: "session-1", status: "queued"}}, {status: 202});
    throw new Error("unexpected URL");
  };
  assert.equal(api.openEditDelete("session-1"), true);
  assert.equal(api.snapshot().pending.kind, "delete");
  assert.equal(calls.length, 0);
  assert.match(elements["edit-confirm-details"].innerHTML, /永久丢弃工作区中的全部未应用改动/);
  assert.equal(elements["edit-confirm"].textContent, "确认删除");
  await api.confirmEditAction();
  assert.deepEqual(calls.map(item => item[0]), ["/api/edit/delete"]);
  assert.equal(api.snapshot().pending, null);
  assert.doesNotMatch(elements["detail-sessions"].innerHTML, /session-1/);

  api.cancelEditAction(); const recovery = {...session(), status: "needs-recovery"};
  api.setState(baseState(recovery));
  assert.equal(api.openEditDelete("session-1"), false);
  assert.match(elements["detail-sessions"].innerHTML, /data-action="delete-edit"[^>]*disabled/);
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
  await testUnavailableAgentsAreDisabledBeforeConfirmation();
  await testLaunchFailureKeepsSessionAndRetryWorks();
  await testOldBackendHasActionableRestartMessage();
  await testInspectionControlsApplyAndShowsConcreteImpact();
  await testApplyAndAbortWaitForConfirmation();
  await testDeleteWaitsForConfirmationAndBlocksRecoverySession();
  await testFreshnessFailureRemainsActionable();
  process.stdout.write("web ui edit session tests passed\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
