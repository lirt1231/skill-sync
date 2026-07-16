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
    this.innerHTML = "";
    this.textContent = "";
    this.className = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  focus() { document.activeElement = this; }
  querySelectorAll() { return []; }
  contains() { return false; }
}

const ids = [
  "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary",
  "issue-list","skill-list","search","search-wrap","search-toggle","sync-filter","source-filter","agent-filter",
  "clear-filters","visible-count","select-all-checkbox","select-all","select-selected",
  "deselect-selected","link-selected","copy-selected","copy-agent","delete-selected",
  "clear-selection","selection-count","selection-bar","agent-list","import-tabs","imports",
  "select-all-imports","clear-imports","import-selected","import-count","import-bar","detail-drawer",
  "detail-name","detail-status","detail-description","detail-sync","detail-path","detail-agents",
  "detail-backup","close-detail","load-failure","retry-load","toast",
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
  toggleVisibleSkills,
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
    description: `${name} description`, agents: {codex, claude},
  });
  return {
    initialized: true, preview: {action: "noop", issues: []},
    status: {skills: [
      skill("alpha", true, false, "global", "linked", "missing"),
      skill("beta", true, true, "codex", "linked", "linked"),
      skill("gamma", false, false, undefined, "missing", "copied"),
      skill("delta", true, false, "claude", "linked", "missing"),
    ]},
    doctor: {agents, matrix: [], issues: []}, import_candidates: [],
  };
}

function testPermanentSearchAndNativeControls() {
  assert.match(htmlSource, /id="search-wrap" class="search-wrap"/);
  assert.doesNotMatch(htmlSource, /id="search-wrap"[^>]*collapsed/);
  for (const id of ["sync-filter", "source-filter", "agent-filter"]) {
    assert.match(htmlSource, new RegExp(`<select id="${id}"`), `${id} must be a native keyboard-operable select`);
  }
}

function testCombinedFiltersAndVisibleLabels() {
  api.setState(inventoryState());
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
  api.setContext({search: "", filters: {status: "all", source: "codex", agent: "all"}, names: ["alpha", "beta"]});
  assert.deepEqual(api.snapshot().visible, ["beta"]);
  api.toggleVisibleSkills();
  assert.deepEqual(api.snapshot().selected, ["alpha"], "only the visible selected row is deselected");
  api.toggleVisibleSkills();
  assert.deepEqual(api.snapshot().selected.sort(), ["alpha", "beta"], "only the visible row is selected again");
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
testRefreshPreservesContextAndCleansInvalidFilters();
process.stdout.write("web ui inventory filter tests passed\n");
