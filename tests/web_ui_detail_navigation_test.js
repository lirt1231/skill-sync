const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];
const stylePath = process.argv[3];
const appSource = fs.readFileSync(appPath, "utf8");
const styleSource = fs.readFileSync(stylePath, "utf8");

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

function boot(initialHref = "http://skill-sync.test/") {
  let document;
  let skillTriggers = [];

  class Element {
    constructor(id = "") {
      this.id = id;
      this.classList = new ClassList();
      this.dataset = {};
      this.attributes = {};
      this.disabled = false;
      this.checked = false;
      this.value = "";
      this.textContent = "";
      this.className = "";
      this._innerHTML = "";
    }
    set innerHTML(value) {
      this._innerHTML = value;
      if (this.id !== "skill-list") return;
      skillTriggers = [];
      const pattern = /<(article|button)[^>]*data-skill-name="([^"]+)"[^>]*data-detail-trigger="([^"]+)"/g;
      let match;
      while ((match = pattern.exec(value))) {
        const trigger = new Element();
        trigger.tagName = match[1].toUpperCase();
        trigger.dataset.skillName = match[2];
        trigger.dataset.detailTrigger = match[3];
        skillTriggers.push(trigger);
      }
    }
    get innerHTML() { return this._innerHTML; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    focus() { document.activeElement = this; }
    closest(selector) { return selector === ".hidden" && this.classList.contains("hidden") ? this : null; }
    querySelectorAll() {
      if (this.id !== "detail-drawer") return [];
      return [elements["close-detail"], elements["detail-repair"], elements["detail-backup"]].filter(item => !item.disabled);
    }
    contains(item) {
      return this.id === "detail-drawer" && [this, elements["close-detail"], elements["detail-repair"], elements["detail-backup"]].includes(item);
    }
  }

  const ids = [
    "setup","app","setup-form","setup-submit","refresh","sync","sync-label","sync-summary",
    "issue-list","skill-list","search","search-wrap","search-toggle","repair-all","status-tabs","status-synced","status-synced-count","status-changed","status-changed-count","status-local","status-local-count","source-filter","agent-filter","clear-filters","visible-count","select-all-checkbox",
    "select-all","select-selected","deselect-selected","link-selected","copy-selected","copy-agent",
    "delete-selected","clear-selection","selection-count","selection-bar","agent-list","import-tabs",
    "imports","select-all-imports","clear-imports","import-selected","import-count","import-bar",
    "detail-drawer","detail-name","detail-status","detail-description","detail-sync","detail-hash","detail-path","detail-variants","detail-sessions","detail-deployments",
    "detail-agents","detail-repair","detail-backup","close-detail","load-failure","retry-load","toast",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  const views = ["skills","agents","imports"].map(name => new Element(`view-${name}`));
  const nav = ["skills","agents","imports"].map(name => {
    const item = new Element(); item.dataset.view = name; return item;
  });
  document = {
    activeElement: null,
    documentElement: {scrollTop: 0},
    querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] : null; },
    querySelectorAll(selector) {
      if (selector === ".view") return views;
      if (selector === ".nav-item[data-view]") return nav;
      if (selector === "[data-skill-name]") return skillTriggers;
      return [];
    },
  };

  const listeners = new Map();
  const location = {href: initialHref};
  const entries = [{href: initialHref, state: null}];
  let historyIndex = 0;
  const dispatch = type => (listeners.get(type) || []).forEach(listener => listener({type}));
  const history = {
    state: null,
    pushState(state, _title, url) {
      const href = new URL(url, location.href).href;
      entries.splice(historyIndex + 1, entries.length, {href, state});
      historyIndex += 1; location.href = href; this.state = state;
    },
    replaceState(state, _title, url) {
      const href = new URL(url, location.href).href;
      entries[historyIndex] = {href, state}; location.href = href; this.state = state;
    },
    back() {
      if (!historyIndex) return;
      historyIndex -= 1; location.href = entries[historyIndex].href; this.state = entries[historyIndex].state; dispatch("popstate");
    },
    forward() {
      if (historyIndex >= entries.length - 1) return;
      historyIndex += 1; location.href = entries[historyIndex].href; this.state = entries[historyIndex].state; dispatch("popstate");
    },
  };
  const context = {
    console, document, location, history, URL, URLSearchParams,
    FormData: class {}, confirm: () => true, fetch: async () => { throw new Error("unexpected fetch"); },
    setTimeout: () => 1, clearTimeout: () => {}, SKILL_SYNC_TEST: true,
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  const source = appSource + `
globalThis.testApi = {
  setState(value) { state = value; render(); },
  setContext(search, names, view = "skills") {
    document.querySelector("#search").value = search;
    selected.clear(); names.forEach(name => selected.add(name));
    activeView = view; render();
  },
  openDetail, closeDetail, handleDetailKeydown,
  restoreDetailFromLocation,
  snapshot() { return {detailSkill, activeView, selected:[...selected]}; }
};`;
  vm.runInContext(source, context, {filename: appPath});
  return {
    api: context.testApi, document, elements, history, location,
    getTrigger(name, kind) { return skillTriggers.find(item => item.dataset.skillName === name && item.dataset.detailTrigger === kind); },
  };
}

function baseState(names = ["alpha", "beta"]) {
  return {
    initialized: true,
    preview: {action: "noop", issues: []},
    status: {skills: names.map(name => ({name, selected: true, changed_local: false, local_path: `/skills/${name}`}))},
    doctor: {agents: [], matrix: [], issues: []},
    import_candidates: [],
  };
}

function keyEvent(key, currentTarget, {shiftKey = false, target = currentTarget} = {}) {
  return {
    key, currentTarget, target, shiftKey, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
}

function testFixedDrawerAndLongListOpen() {
  assert.match(styleSource, /\.detail-drawer\{[^}]*position:fixed[^}]*z-index:30[^}]*height:100dvh[^}]*overflow-y:auto/);
  assert.match(styleSource, /\.detail-drawer\{[^}]*z-index:30[^}]*height:100dvh/,
    "the mobile drawer must cover bottom navigation and follow the dynamic viewport");
  assert.match(styleSource, /\.skill-metadata \.mobile-sync-state\{display:inline-flex\}/,
    "mobile inventory must retain its sync state");
  assert.match(styleSource, /\.sync-strip\{display:grid;grid-template-columns:31px minmax\(0,1fr\)\}/,
    "mobile sync summary must not force horizontal overflow");
  assert.match(styleSource, /\.issue-list span\{min-width:0;overflow-wrap:anywhere\}/,
    "long issue details must wrap inside the mobile viewport");
  assert.match(styleSource, /#toast\{[^}]*z-index:60/,
    "toasts must remain visible above the drawer and mutation dialog");
  assert.match(styleSource, /\.connection-state\.online i,\.detail-status\.ok i\{background:var\(--green\)\}/);
  const app = boot(); app.api.setState(baseState(Array.from({length: 120}, (_, index) => `skill-${index}`)));
  app.document.documentElement.scrollTop = 6400;
  const row = app.getTrigger("skill-119", "row");
  app.api.openDetail("skill-119", row);
  assert.equal(app.document.documentElement.scrollTop, 6400, "opening detail must not move document scroll");
  assert.equal(app.elements["detail-drawer"].classList.contains("hidden"), false);
  assert.equal(app.document.activeElement, app.elements["close-detail"], "focus must enter the visible drawer");
}

function testNonModalDrawerAndStableFocusRestore() {
  const app = boot(); app.api.setState(baseState());
  const originalRow = app.getTrigger("alpha", "row");
  app.api.openDetail("alpha", originalRow);

  const close = app.elements["close-detail"], backup = app.elements["detail-backup"];
  close.focus();
  const reverse = keyEvent("Tab", close, {shiftKey: true});
  app.api.handleDetailKeydown(reverse);
  assert.equal(reverse.prevented, false); assert.equal(app.document.activeElement, close,
    "a non-modal detail panel must not trap keyboard focus");
  const forward = keyEvent("Tab", backup);
  app.api.handleDetailKeydown(forward);
  assert.equal(forward.prevented, false);

  const escape = keyEvent("Escape", close);
  app.api.handleDetailKeydown(escape);
  assert.equal(escape.prevented, true); assert.equal(escape.stopped, true);
  assert.equal(app.api.snapshot().detailSkill, null);
  const rebuiltRow = app.getTrigger("alpha", "row");
  assert.notEqual(rebuiltRow, originalRow, "render must replace the original trigger node");
  assert.equal(app.document.activeElement, rebuiltRow, "close must restore by stable skill identity after DOM rebuild");
  assert.match(app.elements["skill-list"].innerHTML, /<button[^>]*data-detail-trigger="button"[^>]*aria-label="查看 alpha 详情"/);
  assert.doesNotMatch(app.elements["skill-list"].innerHTML, /role="button"|onclick=|onkeydown=/);
}

function testRefreshHistoryAndContextPreservation() {
  const app = boot(); app.api.setState(baseState()); app.api.setContext("alp", ["alpha"], "skills");
  app.api.openDetail("alpha", app.getTrigger("alpha", "button"));
  const detailUrl = app.location.href;
  assert.equal(new URL(detailUrl).searchParams.get("detail"), "alpha");
  app.api.closeDetail();
  app.history.back();
  assert.equal(app.api.snapshot().detailSkill, null, "closing detail must not add another history step");
  assert.equal(app.elements.search.value, "alp");
  assert.deepEqual(app.api.snapshot().selected, ["alpha"]);
  assert.equal(app.api.snapshot().activeView, "skills");
  app.history.forward();
  assert.equal(app.api.snapshot().detailSkill, null, "forward must restore the closed state");

  const refreshed = boot(detailUrl); refreshed.api.setState(baseState());
  assert.equal(refreshed.api.snapshot().detailSkill, "alpha", "refresh must recover detail from the URL");
  assert.equal(refreshed.document.activeElement, refreshed.elements["close-detail"]);

  const invalid = boot("http://skill-sync.test/?detail=removed"); invalid.api.setState(baseState());
  assert.equal(invalid.api.snapshot().detailSkill, null);
  assert.equal(new URL(invalid.location.href).searchParams.has("detail"), false, "a missing target must be safely removed");
}

testFixedDrawerAndLongListOpen();
testNonModalDrawerAndStableFocusRestore();
testRefreshHistoryAndContextPreservation();
process.stdout.write("web ui detail navigation tests passed\n");
