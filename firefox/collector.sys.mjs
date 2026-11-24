/* Firefox Monitor collector.
 *
 * Runs inside Firefox with chrome privileges (loaded by the autoconfig stub
 * installed next to firefox.exe) and appends one JSON record per line to
 * <dataDir>/ff-YYYYMMDD-<pid>.jsonl. The Python watchdog ingests those files.
 */

import { setTimeout } from "resource://gre/modules/Timer.sys.mjs";
import { UserscriptSampler, parseScriptUrl } from "./userscripts.sys.mjs";

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  AddonManager: "resource://gre/modules/AddonManager.sys.mjs",
  PrivateBrowsingUtils: "resource://gre/modules/PrivateBrowsingUtils.sys.mjs",
});

export const COLLECTOR_VERSION = 4;

const LAG_TICK_MS = 250;
const SLOW_TICK_MS = 100;
const MAX_URL = 400;
const MAX_TITLE = 200;
const META_KEEP_LIMIT = 4000;

const state = {
  started: false,
  dataDir: null,
  intervalMs: 10000,
  session: Date.now().toString(36),
  cores: 1,
  mainPid: 0,
  queue: [],
  writing: null,
  tabIds: new WeakMap(),
  tabSeq: 0,
  tabMeta: new Map(),
  tabWasDiscarded: new Map(),
  extMeta: new Map(),
  extNameById: new Map(),
  prevCpu: new Map(),
  procByChildId: new Map(),
  pidsByTab: new Map(),
  hangThrottle: new Map(),
  crashIdsSent: new Set(),
  lag: { last: 0, max: 0, sum: 0, n: 0, slow: 0, blocked: 0 },
  lagPaused: false,
  siteByPid: new Map(),
  userscripts: null,
  sampleNo: 0,
  lastError: 0,
  quitting: false,
};

export function start(opts = {}) {
  if (state.started) {
    return;
  }
  state.started = true;
  state.dataDir = opts.dataDir;
  state.mainPid = Services.appinfo.processID;
  const sec = Services.prefs.getIntPref("ffmon.intervalSec", opts.intervalSec || 10);
  state.intervalMs = Math.max(2, sec) * 1000;

  registerObservers();
  lagTick();
  setTimeout(() => boot().catch(reportError), 3000);
}

async function boot() {
  await IOUtils.makeDirectory(state.dataDir, { createAncestors: true, ignoreExisting: true });
  try {
    state.cores = Math.max(1, Number((await Services.sysinfo.processInfo)?.count) || 1);
  } catch (e) {
    state.cores = Math.max(1, Services.sysinfo.getProperty("cpucount", 1));
  }
  push({
    t: "start",
    ts: Date.now(),
    v: COLLECTOR_VERSION,
    session: state.session,
    pid: state.mainPid,
    ff: Services.appinfo.version,
    build: Services.appinfo.appBuildID,
    profile: PathUtils.profileDir,
    cores: state.cores,
    memsize: safe(() => Services.sysinfo.getProperty("memsize"), 0),
    interval: state.intervalMs / 1000,
  });
  await refreshExtensions();
  sampleLoop();
  state.userscripts = new UserscriptSampler({
    push,
    reportError,
    cores: state.cores,
    siteOfPid: pid => state.siteByPid.get(pid) || "",
    extName: uuid => safe(() => WebExtensionPolicy.getByHostname(uuid)?.name, ""),
    setBusy: busy => {
      state.lagPaused = busy;
      state.lag.last = 0;
    },
  });
  state.userscripts.start();
  setTimeout(() => sendCrashes().catch(reportError), 60000);
}

function sampleLoop() {
  if (state.quitting) {
    return;
  }
  const begin = Date.now();
  sample()
    .catch(reportError)
    .finally(() => {
      flush();
      const spent = Date.now() - begin;
      setTimeout(sampleLoop, Math.max(1000, state.intervalMs - spent));
    });
}

async function sample() {
  const ts = Date.now();
  const t0 = ChromeUtils.now();
  state.sampleNo++;

  const main = await ChromeUtils.requestProcInfo();
  const now = ChromeUtils.now();
  const all = [main, ...main.children];
  const procs = [];
  const procByPid = new Map();
  const nextCpu = new Map();

  for (const p of all) {
    const prev = state.prevCpu.get(p.pid);
    let cpu = null;
    if (prev && now > prev.at && p.cpuTime >= prev.ns) {
      cpu = (p.cpuTime - prev.ns) / ((now - prev.at) * 1e6) / state.cores;
    }
    nextCpu.set(p.pid, { ns: p.cpuTime, at: now });
    const origin = p.origin || "";
    const priv = origin.includes("privateBrowsingId=1");
    const rec = {
      pid: p.pid,
      cid: p.childID || 0,
      type: p === main ? "browser" : p.type || "unknown",
      site: priv ? "(private)" : cleanOrigin(origin),
      mem: p.memory || 0,
      cpu: round4(cpu),
      thr: p.threads ? p.threads.length : 0,
      win: p.windows ? p.windows.length : 0,
    };
    procs.push(rec);
    procByPid.set(p.pid, { rec, raw: p });
    if (rec.cid) {
      state.procByChildId.set(rec.cid, rec);
    }
  }
  state.prevCpu = nextCpu;
  state.siteByPid = new Map(procs.map(p => [p.pid, p.site]));

  const { tabs, windows } = collectTabs(procByPid);
  const exts = attributeExtensions(procByPid);

  let ghost = null;
  if (state.sampleNo % 6 === 1) {
    ghost = safe(() => {
      const mgr = Cc["@mozilla.org/memory-reporter-manager;1"].getService(Ci.nsIMemoryReporterManager);
      return mgr.ghostWindows;
    }, null);
  }

  const lag = state.lag;
  const lagRec = {
    max: Math.round(lag.max),
    avg: lag.n ? Math.round((lag.sum / lag.n) * 10) / 10 : 0,
    slow: lag.slow,
    blocked: Math.round(lag.blocked),
  };
  state.lag = { last: lag.last, max: 0, sum: 0, n: 0, slow: 0, blocked: 0 };

  push({
    t: "s",
    ts,
    pid: state.mainPid,
    win: windows,
    procs,
    tabs,
    exts,
    lag: lagRec,
    ghost,
    sd: Math.round(ChromeUtils.now() - t0),
  });

  if (state.sampleNo % 30 === 0) {
    refreshExtensions().catch(reportError);
  }
  if (state.sampleNo % 360 === 0) {
    sendCrashes().catch(reportError);
  }
}

function collectTabs(procByPid) {
  const entries = [];
  const weightsByPid = new Map();
  let windows = 0;

  for (const win of Services.wm.getEnumerator("navigator:browser")) {
    const gBrowser = win.gBrowser;
    if (!gBrowser) {
      continue;
    }
    windows++;
    const privWin = safe(() => lazy.PrivateBrowsingUtils.isWindowPrivate(win), false);
    for (const tab of gBrowser.tabs) {
      if (tab.closing) {
        continue;
      }
      const browser = tab.linkedBrowser;
      if (!browser) {
        continue;
      }
      const key = tabKey(tab);
      const weights = new Map();
      const root = browser.browsingContext;
      if (root && browser.isConnected) {
        for (const bc of walk(root)) {
          const pid = bc.currentWindowGlobal?.osPid;
          if (pid == null || pid < 0) {
            continue;
          }
          const w = bc === root ? 2 : 1;
          weights.set(pid, (weights.get(pid) || 0) + w);
          weightsByPid.set(pid, (weightsByPid.get(pid) || 0) + w);
        }
      }
      entries.push({ tab, browser, key, weights, privWin, gBrowser, winIndex: windows });
    }
  }

  const tabs = [];
  const seen = new Set();
  for (const e of entries) {
    let mem = 0;
    let cpu = 0;
    let cpuKnown = true;
    for (const [pid, w] of e.weights) {
      const p = procByPid.get(pid);
      const total = weightsByPid.get(pid);
      if (!p || !total) {
        continue;
      }
      const share = w / total;
      mem += p.rec.mem * share;
      if (p.rec.cpu == null) {
        cpuKnown = false;
      } else {
        cpu += p.rec.cpu * share;
      }
    }
    const discarded = !e.browser.isConnected;
    const flags =
      (discarded ? 1 : 0) |
      (e.gBrowser.selectedTab === e.tab ? 2 : 0) |
      (e.tab.soundPlaying ? 4 : 0) |
      (e.tab.pinned ? 8 : 0);
    const pids = [...e.weights.keys()];
    tabs.push({
      k: e.key,
      m: Math.round(mem),
      c: cpuKnown && e.weights.size ? round4(cpu) : null,
      f: flags,
      p: pids,
    });
    state.pidsByTab.set(e.key, pids);
    seen.add(e.key);
    emitTabMeta(e);

    const was = state.tabWasDiscarded.get(e.key);
    if (was === false && discarded) {
      const meta = state.tabMeta.get(e.key);
      push({ t: "ev", ts: Date.now(), kind: "tab_unloaded", tab: e.key, site: meta?.site || "" });
    }
    state.tabWasDiscarded.set(e.key, discarded);
  }

  for (const key of state.tabWasDiscarded.keys()) {
    if (!seen.has(key)) {
      state.tabWasDiscarded.delete(key);
      state.pidsByTab.delete(key);
      if (state.tabMeta.size > META_KEEP_LIMIT) {
        state.tabMeta.delete(key);
      }
    }
  }
  return { tabs, windows };
}

function emitTabMeta(e) {
  const priv = e.privWin || safe(() => lazy.PrivateBrowsingUtils.isBrowserPrivate(e.browser), false);
  let title = "Private tab";
  let url = "";
  let site = "(private)";
  if (!priv) {
    const uri = e.browser.currentURI;
    title = (e.tab.label || e.browser.contentTitle || "").slice(0, MAX_TITLE);
    url = uri ? uri.spec.slice(0, MAX_URL) : "";
    site = uri ? siteKey(uri) : "";
  }
  const sig = `${title}\u0001${url}\u0001${e.tab.pinned ? 1 : 0}\u0001${e.winIndex}`;
  const prev = state.tabMeta.get(e.key);
  if (prev && prev.sig === sig) {
    return;
  }
  state.tabMeta.set(e.key, { sig, site });
  push({
    t: "tab",
    ts: Date.now(),
    k: e.key,
    title,
    url,
    site,
    priv: priv ? 1 : 0,
    pin: e.tab.pinned ? 1 : 0,
    win: e.winIndex,
  });
}

function attributeExtensions(procByPid) {
  const out = new Map();
  for (const { rec, raw } of procByPid.values()) {
    if (rec.type !== "extension") {
      continue;
    }
    const weights = new Map();
    for (const w of raw.windows || []) {
      const policy = w.documentURI ? safe(() => WebExtensionPolicy.getByURI(w.documentURI), null) : null;
      const id = policy ? policy.id : "(extension process)";
      if (policy && !state.extNameById.has(id)) {
        state.extNameById.set(id, policy.name);
      }
      weights.set(id, (weights.get(id) || 0) + 1);
    }
    if (!weights.size) {
      weights.set("(extension process)", 1);
    }
    let total = 0;
    for (const v of weights.values()) {
      total += v;
    }
    for (const [id, w] of weights) {
      const share = w / total;
      const cur = out.get(id) || { id, m: 0, c: 0, cpuKnown: true };
      cur.m += rec.mem * share;
      if (rec.cpu == null) {
        cur.cpuKnown = false;
      } else {
        cur.c += rec.cpu * share;
      }
      out.set(id, cur);
    }
  }
  return [...out.values()].map(x => ({ id: x.id, m: Math.round(x.m), c: x.cpuKnown ? round4(x.c) : null }));
}

async function refreshExtensions() {
  let addons = [];
  try {
    addons = await lazy.AddonManager.getAddonsByTypes(["extension"]);
  } catch (e) {
    reportError(e);
  }
  for (const a of addons) {
    if (a.isBuiltin || a.isSystem) {
      continue;
    }
    const rec = {
      t: "ext",
      ts: Date.now(),
      id: a.id,
      name: a.name || a.id,
      version: a.version || "",
      enabled: a.isActive ? 1 : 0,
    };
    const sig = `${rec.name}|${rec.version}|${rec.enabled}`;
    if (state.extMeta.get(a.id) !== sig) {
      state.extMeta.set(a.id, sig);
      state.extNameById.set(a.id, rec.name);
      push(rec);
    }
  }
}

async function sendCrashes() {
  let crashes = [];
  try {
    crashes = (await Services.crashmanager.getCrashes()) || [];
  } catch (e) {
    return;
  }
  for (const c of crashes) {
    if (!c?.id || state.crashIdsSent.has(c.id)) {
      continue;
    }
    state.crashIdsSent.add(c.id);
    const meta = {};
    for (const [k, v] of Object.entries(c.metadata || {})) {
      const s = typeof v === "string" ? v : JSON.stringify(v);
      if (s && s.length <= 2000) {
        meta[k] = s;
      }
    }
    push({
      t: "crash",
      ts: Date.now(),
      id: c.id,
      date: c.crashDate ? c.crashDate.getTime() : null,
      type: c.type || "",
      ptype: c.processType || "",
      remote: c.remoteID || "",
      meta,
    });
  }
}

function registerObservers() {
  const obs = {
    observe(subject, topic, data) {
      try {
        handleTopic(subject, topic, data);
      } catch (e) {
        reportError(e);
      }
    },
    QueryInterface: ChromeUtils.generateQI(["nsIObserver", "nsISupportsWeakReference"]),
  };
  state.observer = obs;
  for (const topic of [
    "ipc:content-shutdown",
    "process-hang-report",
    "memory-pressure",
    "quit-application-granted",
    "quit-application",
  ]) {
    Services.obs.addObserver(obs, topic);
  }
}

function handleTopic(subject, topic, data) {
  const ts = Date.now();
  switch (topic) {
    case "ipc:content-shutdown": {
      const bag = subject.QueryInterface(Ci.nsIPropertyBag2);
      const abnormal = safe(() => bag.get("abnormal"), false);
      const childID = Number(safe(() => bag.get("childID"), 0));
      if (!abnormal) {
        state.procByChildId.delete(childID);
        return;
      }
      const p = state.procByChildId.get(childID);
      const tabs = [];
      if (p) {
        for (const [key, pids] of state.pidsByTab) {
          if (pids.includes(p.pid)) {
            tabs.push(key);
          }
        }
      }
      push({
        t: "ev",
        ts,
        kind: "content_crash",
        cid: childID,
        pid: p?.pid || 0,
        ptype: p?.type || "",
        site: p?.site || "",
        mem: p?.mem || 0,
        dump: safe(() => bag.get("dumpID"), "") || "",
        tabs,
      });
      state.procByChildId.delete(childID);
      flush();
      break;
    }
    case "process-hang-report": {
      const r = subject.QueryInterface(Ci.nsIHangReport);
      const addonId = safe(() => r.addonId, "") || "";
      const script = safe(() => r.scriptFileName, "") || "";
      const throttleKey = `${safe(() => r.childID, 0)}|${addonId}|${script}`;
      const last = state.hangThrottle.get(throttleKey) || 0;
      if (ts - last < 30000) {
        return;
      }
      state.hangThrottle.set(throttleKey, ts);
      let tab = "";
      const br = safe(() => r.scriptBrowser, null);
      if (br) {
        const gb = br.ownerGlobal?.gBrowser;
        const t = gb?.getTabForBrowser(br);
        if (t) {
          tab = tabKey(t);
        }
      }
      const us = parseScriptUrl(script);
      push({
        t: "ev",
        ts,
        kind: "script_hang",
        usName: us?.name || "",
        usId: us?.id || "",
        addon: addonId,
        addonName: state.extNameById.get(addonId) || "",
        script: script.slice(0, MAX_URL),
        dur: Math.round(safe(() => r.hangDuration, 0)),
        tab,
      });
      break;
    }
    case "memory-pressure":
      push({ t: "ev", ts, kind: "memory_pressure", data: String(data || "") });
      break;
    case "quit-application-granted":
      push({ t: "ev", ts, kind: "quit_requested", pid: state.mainPid });
      flush();
      break;
    case "quit-application":
      state.quitting = true;
      state.userscripts?.stop();
      push({ t: "ev", ts, kind: "quit", pid: state.mainPid });
      flush();
      break;
  }
}

function lagTick() {
  const now = ChromeUtils.now();
  const lag = state.lag;
  if (lag.last && !state.lagPaused) {
    const late = Math.max(0, now - lag.last - LAG_TICK_MS);
    lag.n++;
    lag.sum += late;
    if (late > lag.max) {
      lag.max = late;
    }
    if (late >= SLOW_TICK_MS) {
      lag.slow++;
    }
    if (late >= 50) {
      lag.blocked += late;
    }
  }
  lag.last = now;
  if (!state.quitting) {
    setTimeout(lagTick, LAG_TICK_MS);
  }
}

function push(rec) {
  state.queue.push(JSON.stringify(rec) + "\n");
}

function flush() {
  if (!state.queue.length || !state.dataDir) {
    return;
  }
  const text = state.queue.join("");
  state.queue = [];
  const d = new Date();
  const day = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  const path = PathUtils.join(state.dataDir, `ff-${day}-${state.mainPid}.jsonl`);
  const prev = state.writing || Promise.resolve();
  state.writing = prev
    .then(() => IOUtils.writeUTF8(path, text, { mode: "appendOrCreate" }))
    .catch(reportError);
}

function tabKey(tab) {
  let k = state.tabIds.get(tab);
  if (!k) {
    k = `${state.session}.${++state.tabSeq}`;
    state.tabIds.set(tab, k);
  }
  return k;
}

function* walk(bc) {
  yield bc;
  for (const child of bc.children) {
    yield* walk(child);
  }
}

function siteKey(uri) {
  try {
    if (uri.schemeIs("moz-extension")) {
      return `ext:${safe(() => WebExtensionPolicy.getByHostname(uri.host)?.name, "") || uri.host}`;
    }
    return Services.eTLD.getSchemelessSite(uri) || uri.scheme;
  } catch (e) {
    return uri.schemeIs("about") ? uri.prePath + uri.filePath : uri.scheme;
  }
}

function cleanOrigin(origin) {
  if (!origin) {
    return "";
  }
  const caret = origin.indexOf("^");
  return caret >= 0 ? origin.slice(0, caret) : origin;
}

function round4(x) {
  return x == null ? null : Math.round(x * 10000) / 10000;
}

function safe(fn, fallback) {
  try {
    const v = fn();
    return v === undefined ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

function reportError(e) {
  const now = Date.now();
  if (now - state.lastError > 600000) {
    state.lastError = now;
    push({ t: "ev", ts: now, kind: "collector_error", msg: String(e?.message || e).slice(0, 500), stack: String(e?.stack || "").slice(0, 1500) });
  }
  console.error("[ffmon]", e);
}
