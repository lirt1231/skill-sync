const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];
const appSource = fs.readFileSync(appPath, "utf8");
const storageValues = new Map();
const sessionStorage = {
  getItem(key) { return storageValues.has(key) ? storageValues.get(key) : null; },
  setItem(key, value) { storageValues.set(key, String(value)); },
};

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

function boot(href) {
  let document;
  class Element {
    constructor(id = "") {
      this.id = id; this.classList = new ClassList(); this.dataset = {}; this.attributes = {};
      this.disabled = false; this.checked = false; this.indeterminate = false; this.value = "";
      this.innerHTML = ""; this.textContent = ""; this.className = "";
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    focus() { document.activeElement = this; }
    querySelectorAll() { return []; }
    contains() { return false; }
  }
  const ids = [
    "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary",
    "issue-list","skill-list","search","search-wrap","sync-filter","source-filter","agent-filter",
    "clear-filters","visible-count","select-all-checkbox","select-all","select-selected",
    "deselect-selected","link-selected","copy-selected","copy-agent","delete-selected",
    "clear-selection","selection-count","selection-bar","agent-list","import-tabs","imports",
    "select-all-imports","clear-imports","import-selected","import-count","import-bar","detail-drawer",
    "detail-name","detail-status","detail-description","detail-sync","detail-path","detail-agents",
    "detail-backup","close-detail","load-failure","retry-load","toast",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  const views = ["skills","agents","imports"].map(name => new Element(`view-${name}`));
  const nav = ["skills","agents","imports"].map(name => { const item = new Element(); item.dataset.view = name; return item; });
  document = {
    activeElement: null,
    querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] : null; },
    querySelectorAll(selector) {
      if (selector === ".view") return views;
      if (selector === ".nav-item[data-view]") return nav;
      return [];
    },
  };
  const location = {href};
  const history = {
    state: null,
    pushState(value, _title, url) { this.state = value; location.href = new URL(url, location.href).href; },
    replaceState(value, _title, url) { this.state = value; location.href = new URL(url, location.href).href; },
  };
  const context = {
    console, document, location, history, sessionStorage, URL, URLSearchParams,
    FormData: class {}, confirm: () => true, fetch: async () => { throw new Error("unexpected fetch"); },
    setTimeout: () => 1, clearTimeout: () => {}, SKILL_SYNC_TEST: true, addEventListener: () => {},
  };
  context.globalThis = context;
  vm.createContext(context);
  const source = appSource + `
globalThis.testApi = {
  setState(value) { state = value; render(); },
  saveContext({search, filters, names, view}) {
    document.querySelector("#search").value = search;
    Object.assign(inventoryFilters, filters);
    selected.clear(); names.forEach(name => selected.add(name));
    activeView = view; persistUiContext(); render();
  },
  snapshot() { return {search:document.querySelector("#search").value,filters:{...inventoryFilters},selected:[...selected],activeView,detailSkill}; }
};`;
  vm.runInContext(source, context, {filename: appPath});
  return {api: context.testApi, elements, location};
}

function state({pruned = false} = {}) {
  const agents = pruned
    ? [{name: "codex", display_name: "Codex", detected: true, enabled: true}]
    : [
        {name: "codex", display_name: "Codex", detected: true, enabled: true},
        {name: "claude", display_name: "Claude Code", detected: true, enabled: true},
      ];
  const alpha = {name:"alpha",description:"alpha",selected:true,changed_local:false,platform:pruned?"global":"codex",local_path:"/skills/alpha",agents:{codex:"linked",claude:"linked"}};
  const skills = pruned ? [alpha] : [alpha,{name:"beta",description:"beta",selected:true,changed_local:true,platform:"global",local_path:"/skills/beta",agents:{codex:"missing",claude:"linked"}}];
  return {initialized:true,preview:{action:"noop",issues:[]},status:{skills},doctor:{agents,matrix:[],issues:[]},import_candidates:[]};
}

const href = "http://skill-sync.test/?detail=alpha";
const first = boot(href); first.api.setState(state());
first.api.saveContext({search:"alp",filters:{status:"synced",source:"codex",agent:"claude"},names:["alpha","beta"],view:"agents"});

const cold = boot(first.location.href);
cold.api.setState({
  initialized:true,preview:{action:"noop",issues:[]},status:{skills:[]},
  doctor:{agents:state().doctor.agents,matrix:[],issues:[]},import_candidates:[],
});
assert.deepEqual(cold.api.snapshot(), {
  search:"alp", filters:{status:"synced",source:"codex",agent:"claude"},
  selected:["alpha","beta"], activeView:"agents", detailSkill:"alpha",
}, "a partial cold-load response must not prematurely clear inventory context");
assert.equal(new URL(cold.location.href).searchParams.get("detail"), "alpha");

const reloaded = boot(first.location.href); reloaded.api.setState(state());
assert.deepEqual(reloaded.api.snapshot(), {
  search:"alp", filters:{status:"synced",source:"codex",agent:"claude"},
  selected:["alpha","beta"], activeView:"agents", detailSkill:"alpha",
}, "a new JS runtime must restore the complete inventory context");
assert.equal(new URL(reloaded.location.href).searchParams.get("detail"), "alpha", "detail remains URL-backed");

const pruned = boot(reloaded.location.href); pruned.api.setState(state({pruned:true}));
assert.deepEqual(pruned.api.snapshot(), {
  search:"alp", filters:{status:"synced",source:"all",agent:"all"},
  selected:["alpha"], activeView:"agents", detailSkill:"alpha",
}, "reload must safely clean removed sources, Agents, and selected Skills");

sessionStorage.setItem("skill-sync:web-context:v1", JSON.stringify({
  activeView:"unknown", search:42, filters:{status:"unknown",source:"missing",agent:"missing"}, selected:["missing",42],
}));
const invalid = boot(href); invalid.api.setState(state({pruned:true}));
assert.deepEqual(invalid.api.snapshot(), {
  search:"", filters:{status:"all",source:"all",agent:"all"},
  selected:[], activeView:"skills", detailSkill:"alpha",
}, "malformed and stale stored values must fail safely to valid defaults");

process.stdout.write("web ui inventory reload tests passed\n");
