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
    this.indeterminate = false;
    this.value = "";
    this._innerHTML = "";
    this.innerHTMLWrites = 0;
    this.textContent = "";
    this.className = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  set innerHTML(value) { this._innerHTML = value; this.innerHTMLWrites += 1; }
  get innerHTML() { return this._innerHTML; }
  focus() { document.activeElement = this; }
  querySelectorAll() { return []; }
  contains() { return false; }
}

const ids = [
  "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary",
  "issue-list","skill-list","search","search-wrap","search-toggle","repair-all","status-tabs","status-synced","status-synced-count","status-changed","status-changed-count","status-local","status-local-count","source-filter","agent-filter",
  "clear-filters","visible-count","select-all-checkbox","select-all","select-selected",
  "deselect-selected","link-selected","copy-selected","copy-agent","delete-selected",
  "clear-selection","selection-count","selection-bar","agent-list","import-tabs","imports",
  "select-all-imports","clear-imports","import-selected","import-count","import-bar","detail-drawer",
  "detail-name","detail-status","detail-description","detail-sync","detail-hash","detail-path","detail-agents","detail-variants","detail-sessions","detail-deployments",
  "detail-repair","detail-backup","close-detail","load-failure","retry-load","toast",
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
const views = ["skills","agents","imports"].map(name => new Element(`view-${name}`));
const nav = ["skills","agents","imports"].map(name => {
  const item = new Element(); item.dataset.view = name; return item;
});
const document = {
  activeElement: null,
  querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] : null; },
  querySelectorAll(selector) {
    if (selector === ".view") return views;
    if (selector === ".nav-item[data-view]") return nav;
    return [];
  },
};

const location = {href: "http://skill-sync.test/?detail=alpha"};
const history = {
  state: null,
  pushState(state, _title, url) { this.state = state; location.href = new URL(url, location.href).href; },
  replaceState(state, _title, url) { this.state = state; location.href = new URL(url, location.href).href; },
};
const context = {
  console, document, location, history, URL, URLSearchParams,
  FormData: class {}, confirm: () => true, fetch: async () => { throw new Error("unexpected fetch"); },
  setTimeout: () => 1, clearTimeout: () => {}, SKILL_SYNC_TEST: true,
  addEventListener: () => {},
};
context.globalThis = context;
vm.createContext(context);
const source = appSource + `
globalThis.testApi = {
  setState(value) { state = value; render(); },
  setContext({search, filters, names, view = "skills"}) {
    document.querySelector("#search").value = search;
    Object.assign(inventoryFilters, filters);
    selected.clear(); names.forEach(name => selected.add(name));
    activeView = view; render();
  },
  setFilters(filters) { Object.assign(inventoryFilters, filters); renderSkills(state.status.skills || []); },
  toggleVisibleSkills, toggle,
  snapshot() {
    return {
      visible: visibleSkills(state.status.skills || []).map(skill => skill.name),
      selected: [...selected], filters: {...inventoryFilters}, activeView, detailSkill,
    };
  }
};`;
vm.runInContext(source, context, {filename: appPath});
const api = context.testApi;

function inventoryState() {
  const agents = [
    {name: "codex", display_name: "Codex", detected: true, enabled: true},
    {name: "claude", display_name: "Claude Code", detected: true, enabled: true},
    {name: "workbuddy", display_name: "WorkBuddy", detected: false, enabled: true},
  ];
  const skill = (name, selected, changed_local, platform, codex, claude) => ({
    name, selected, changed_local, platform, local_path: `/skills/${name}`,
    local_hash: `sha256:${name.padEnd(64,"a")}`,
    description: `${name} description`, agents: {codex, claude}, units: [],
  });
  return {
    initialized: true, preview: {action: "noop", issues: []},
    status: {skills: [
      skill("alpha", true, false, "global", "linked", "missing"),
      skill("beta", true, true, "codex", "linked", "linked"),
      skill("gamma", false, false, undefined, "missing", "copied"),
      skill("delta", true, false, "claude", "linked", "missing"),
    ]},
    doctor: {agents, matrix: [], issues: []},
    managed: {
      variants: {variants: [
        {skill:"alpha",target:"kimi",target_kinds:["family"],overlay_file_count:2,valid:true},
        {skill:"alpha",target:"kimi-code",target_kinds:["client"],overlay_file_count:1,valid:true},
      ]},
      sessions: {sessions: [{session_id:"session-1",skill:"alpha",scope:"family",target:"kimi",status:"active"}]},
      deployments: {skills: [{name:"alpha",clients:[
        {client:"codex",agent:"codex",deployment_state:"tampered",link_state:"linked-render"},
        {client:"kimi-code",agent:"kimi",deployment_state:"tampered",link_state:"linked-render"},
      ]}]},
    },
    import_candidates: [],
  };
}

function testPermanentSearchAndNativeControls() {
  assert.match(htmlSource,/id="initial-loading"[^>]*role="status"/);
  assert.doesNotMatch(htmlSource,/id="detail-drawer"[^>]*aria-modal/);
  assert.doesNotMatch(htmlSource,/id="deselect-selected"|id="select-all"/);
  assert.match(htmlSource, /id="search-wrap" class="search-wrap"/);
  assert.doesNotMatch(htmlSource, /id="search-wrap"[^>]*collapsed/);
  assert.match(htmlSource, /id="status-tabs"[^>]*role="group"/);
  for (const id of ["status-synced", "status-changed", "status-local"]) {
    assert.match(htmlSource, new RegExp(`id="${id}"[^>]*aria-pressed=`), `${id} must be a keyboard-operable segmented button`);
  }
  assert.doesNotMatch(htmlSource, /id="sync-filter"/);
  for (const id of ["source-filter", "agent-filter"]) {
    assert.match(htmlSource, new RegExp(`<select id="${id}"`), `${id} must be a native keyboard-operable select`);
  }
}

function testCombinedFiltersAndVisibleLabels() {
  api.setState(inventoryState());
  assert.equal(elements["status-synced-count"].textContent,2);
  assert.equal(elements["status-changed-count"].textContent,1);
  assert.equal(elements["status-local-count"].textContent,1);
  api.setFilters({status: "changed", source: "codex", agent: "claude"});
  assert.deepEqual(api.snapshot().visible, ["beta"]);
  assert.match(elements["skill-list"].innerHTML, /Claude Code/);
  assert.match(elements["skill-list"].innerHTML, /已同步/);
  assert.match(elements["skill-list"].innerHTML, /agent-coverage/);
  assert.match(elements["agent-filter"].innerHTML, /Codex/);
  assert.match(elements["agent-filter"].innerHTML, /Claude Code/);
  assert.doesNotMatch(elements["agent-filter"].innerHTML, /WorkBuddy/);

  api.setFilters({status: "synced", source: "claude", agent: "codex"});
  assert.deepEqual(api.snapshot().visible, ["delta"]);
  api.setFilters({status: "local", source: "global", agent: "claude"});
  assert.deepEqual(api.snapshot().visible, ["gamma"]);
}

function testVisibleSelectionOnly() {
  api.setState(inventoryState());
  api.setContext({search: "", filters: {status: "changed", source: "codex", agent: "all"}, names: ["alpha", "beta"]});
  assert.deepEqual(api.snapshot().visible, ["beta"]);
  api.toggleVisibleSkills();
  assert.deepEqual(api.snapshot().selected, ["alpha"], "only the visible selected row is deselected");
  api.toggleVisibleSkills();
  assert.deepEqual(api.snapshot().selected.sort(), ["alpha", "beta"], "only the visible row is selected again");
}

function testSelectionIsLocalAndStableOptionsAreNotRebuilt() {
  api.setState(inventoryState());
  const listMarkup=elements["skill-list"].innerHTML;
  const sourceWrites=elements["source-filter"].innerHTMLWrites,agentWrites=elements["agent-filter"].innerHTMLWrites;
  api.toggle("alpha",true);
  assert.equal(elements["skill-list"].innerHTML,listMarkup,"checkbox selection must not rebuild the list");
  api.setFilters({status:"synced",source:"all",agent:"all"});
  assert.equal(elements["source-filter"].innerHTMLWrites,sourceWrites,"unchanged source options must not be rebuilt");
  assert.equal(elements["agent-filter"].innerHTMLWrites,agentWrites,"unchanged Agent options must not be rebuilt");
  assert.match(appSource,/setTimeout\(\(\)=>\{searchRenderTimer=null;renderSkills[\s\S]*?\},200\)/,
    "search rendering must be debounced");
}

function testManagedBadgesAndConcreteClientDetail() {
  const fixture=inventoryState();
  fixture.status.skills[0].units=[
    {kind:"variant",target:"kimi-code",state:"conflict",changed_local:true,changed_remote:true},
  ];
  api.setState(fixture);
  api.setContext({search:"",filters:{status:"changed",source:"all",agent:"all"},names:[]});
  assert.match(elements["skill-list"].innerHTML, /alpha[a]*[^<]*<\/code>/);
  assert.match(elements["skill-list"].innerHTML, /2 Variant/);
  assert.match(elements["skill-list"].innerHTML, /1 会话/);
  assert.match(elements["skill-list"].innerHTML, /部署异常/);
  assert.match(elements["skill-list"].innerHTML, /data-action="repair-skill"/);
  assert.equal(elements["detail-hash"].textContent.startsWith("sha256:"), true);
  assert.match(elements["detail-variants"].innerHTML, /kimi-code/);
  assert.match(elements["detail-variants"].innerHTML, /class="danger">冲突/);
  assert.match(elements["detail-sessions"].innerHTML, /session-1/);
  assert.match(elements["detail-deployments"].innerHTML, /Kimi Code/);
  assert.match(elements["detail-deployments"].innerHTML, /deployment-family"><header><strong>Kimi Code<\/strong>/);
  assert.doesNotMatch(elements["detail-deployments"].innerHTML, /<strong>Kimi<\/strong>/);
  assert.match(elements["detail-deployments"].innerHTML, /内容被修改/);
  assert.doesNotMatch(elements["detail-deployments"].innerHTML, /Kimi Desktop/);
  assert.match(elements["detail-deployments"].innerHTML, /Kimi Code[\s\S]*内容同步冲突/);
  assert.equal(elements["detail-repair"].classList.contains("hidden"), false);
  assert.equal(elements["detail-status"].className, "detail-status danger");
}

function testDynamicNamesNeverCreateInlineHandlers() {
  const fixture=inventoryState(),name='unsafe" onclick="globalThis.pwned=true';
  elements.search.value="";
  fixture.status.skills=[{...fixture.status.skills[0],name,description:name,local_path:`/skills/${name}`}];
  fixture.managed={variants:{variants:[]},sessions:{sessions:[]},deployments:{skills:[{name,clients:[{client:"codex",agent:"codex",deployment_state:"tampered",link_state:"linked-render"}]}]}};
  api.setState(fixture);
  api.setFilters({status:"synced",source:"all",agent:"all"});
  const markup=elements["skill-list"].innerHTML;
  assert.doesNotMatch(markup,/\sonclick="|\sonchange="|\sonkeydown="/i);
  assert.match(markup,/unsafe&quot;/);
  assert.doesNotMatch(appSource,/onclick="|onchange="|onkeydown="/i);
}

function testIssueSummaryKeepsInventoryAboveTheFold() {
  const fixture=inventoryState();
  fixture.preview.issues=Array.from({length:7},(_,index)=>({type:"conflict",skill:`issue-${index}`}));
  api.setState(fixture);
  assert.match(elements["issue-list"].innerHTML, /查看其余 3 项问题/);
  assert.equal((elements["issue-list"].innerHTML.match(/class="issue-row"/g)||[]).length,7,
    "collapsed issues remain available in the disclosure");
}

function testRefreshPreservesContextAndCleansInvalidFilters() {
  api.setState(inventoryState());
  api.setContext({
    search: "alp", filters: {status: "synced", source: "codex", agent: "claude"},
    names: ["alpha"], view: "skills",
  });
  const refreshed = inventoryState();
  refreshed.status.skills = [
    {...refreshed.status.skills[0], platform: "global", agents: {codex: "linked"}},
  ];
  refreshed.doctor.agents = [refreshed.doctor.agents[0]];
  api.setState(refreshed);
  const snapshot = api.snapshot();
  assert.equal(elements.search.value, "alp");
  assert.equal(snapshot.filters.status, "synced");
  assert.equal(snapshot.filters.source, "all", "a removed source resets safely");
  assert.equal(snapshot.filters.agent, "all", "an undetected Agent resets safely");
  assert.deepEqual(snapshot.selected, ["alpha"]);
  assert.equal(snapshot.activeView, "skills");
  assert.equal(snapshot.detailSkill, "alpha");
  assert.equal(new URL(location.href).searchParams.get("detail"), "alpha");
}

testPermanentSearchAndNativeControls();
testCombinedFiltersAndVisibleLabels();
testVisibleSelectionOnly();
testSelectionIsLocalAndStableOptionsAreNotRebuilt();
testManagedBadgesAndConcreteClientDetail();
testIssueSummaryKeepsInventoryAboveTheFold();
testRefreshPreservesContextAndCleansInvalidFilters();
testDynamicNamesNeverCreateInlineHandlers();
process.stdout.write("web ui inventory filter tests passed\n");
