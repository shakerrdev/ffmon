// Runs firefox/collector.sys.mjs under Node with mocked Firefox APIs.
// Usage: node tests/collector_harness.mjs <outDir>
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = process.argv[2] || "/tmp/ffmon-harness";
fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

const timerImport = 'import { setTimeout } from "resource://gre/modules/Timer.sys.mjs";';
for (const f of ["collector.sys.mjs", "userscripts.sys.mjs"]) {
  const code = fs.readFileSync(path.join(here, "..", "firefox", f), "utf8").replace(timerImport, "const { setTimeout } = globalThis;");
  fs.writeFileSync(path.join(outDir, f), code);
}
const modPath = path.join(outDir, "collector.sys.mjs");

const TM = "4e5b3a85-af36-46a9-a93a-a0310ab92dd0";
const usUrl = (name, id) => `moz-extension://${TM}/userscripts/${name}.user.js?id=${id}`;
const profileChildA = {
  meta: { interval: 4, sampleUnits: { threadCPUDelta: "µs" } },
  threads: [{
    name: "GeckoMain", pid: 201,
    stringTable: [
      "(root)", "Pt (https://example.com/:10:91)",
      `window["__f__x"]/< (${usUrl("Comic%20Reader", "aaa")}:6:28)`,
      `loop (${usUrl("pixiv-turbo", "bbb")}:40:2)`,
      "js::RunScript",
    ],
    frameTable: { schema: { location: 0, relevantForJS: 1 }, data: [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]] },
    stackTable: { schema: { prefix: 0, frame: 1 }, data: [[null, 0], [0, 2], [1, 1], [2, 4], [0, 3], [0, 1]] },
    samples: { schema: { stack: 0, time: 1, threadCPUDelta: 2 }, data: [
      [3, 1, 4000], [3, 2, 4000], [2, 3, 2000], [4, 4, 4000], [5, 5, 4000], [null, 6, 0], [0, 7, 100],
    ] },
  }],
};
const profileChildB = JSON.stringify({
  meta: { interval: 4 },
  threads: [{
    name: "GeckoMain", pid: 202,
    stringArray: ["(root)", `x (${usUrl("pixiv-turbo", "bbb")}:1:1)`],
    frameTable: { schema: { location: 0 }, data: [[0], [1]] },
    stackTable: { schema: { prefix: 0, frame: 1 }, data: [[null, 0], [0, 1]] },
    samples: { schema: { stack: 0, time: 1, weight: 2 }, data: [[1, 1, 3], [1, 2, null]] },
  }],
});
let profilerActive = false;
const profilerMock = {
  IsActive: () => profilerActive,
  StartProfiler: async () => { profilerActive = true; },
  StopProfiler: async () => { profilerActive = false; },
  getProfileDataAsync: async () => ({ meta: { interval: 4 }, threads: [], processes: [profileChildA, profileChildB] }),
  get activeConfiguration() { return { interval: 4, threads: ["GeckoMain"] }; },
};

const uri = spec => {
  const u = new URL(spec);
  return {
    spec, host: u.host, prePath: `${u.protocol}//${u.host}`, filePath: u.pathname, scheme: u.protocol.slice(0, -1),
    schemeIs: s => u.protocol === s + ":",
  };
};
const bc = (pid, children = []) => ({ currentWindowGlobal: { osPid: pid }, children });

const tabs = [];
const gBrowser = { tabs, selectedTab: null, getTabForBrowser: b => tabs.find(t => t.linkedBrowser === b) };
const win = { gBrowser };
function addTab(title, url, pid, sub = [], opts = {}) {
  const browser = { isConnected: !opts.discarded, currentURI: uri(url), contentTitle: title, browsingContext: bc(pid, sub), ownerGlobal: win };
  const tab = { label: title, pinned: !!opts.pinned, soundPlaying: !!opts.audio, closing: false, linkedBrowser: browser };
  tabs.push(tab);
  return tab;
}
const yt = addTab("YouTube", "https://www.youtube.com/watch?v=1", 201, [bc(205)], { audio: true });
addTab("YouTube 2", "https://www.youtube.com/watch?v=2", 201);
addTab("GitHub", "https://github.com/shakerrdev", 202, [], { pinned: true });
const lazyTab = addTab("Old tab", "https://example.org/", 203, [], { discarded: true });
gBrowser.selectedTab = tabs[0];

let cpuBase = 0;
const procInfo = () => {
  cpuBase += 1e9;
  const child = (pid, childID, type, origin, memory, cpuF, windows = []) => ({ pid, childID, type, origin, memory, cpuTime: cpuBase * cpuF, threads: [{}, {}], windows });
  return {
    pid: 100, memory: 700e6, cpuTime: cpuBase * 0.2, threads: [{}, {}, {}],
    children: [
      child(201, 2, "webIsolated", "https://youtube.com", 900e6, 0.5),
      child(202, 3, "webIsolated", "https://github.com^userContextId=1", 300e6, 0.05),
      child(205, 5, "webIsolated", "https://doubleclick.net", 80e6, 0.02),
      child(209, 9, "webIsolated", "https://secret.example^privateBrowsingId=1", 120e6, 0.01),
      child(300, 10, "extension", "", 400e6, 0.1, [{ documentURI: uri("moz-extension://ublock/background.html") }, { documentURI: uri("moz-extension://dark/bg.html") }, { documentURI: uri("moz-extension://dark/popup.html") }]),
      child(400, 11, "gpu", "", 350e6, 0.1),
    ],
  };
};

const observers = new Map();
const policies = { ublock: { id: "uBlock0@raymondhill.net", name: "uBlock Origin" }, dark: { id: "addon@darkreader.org", name: "Dark Reader" } };
let t0 = performance.now();

Object.assign(globalThis, {
  setTimeout: globalThis.setTimeout,
  ChromeUtils: {
    defineESModuleGetters(obj) {
      obj.AddonManager = { getAddonsByTypes: async () => [
        { id: "uBlock0@raymondhill.net", name: "uBlock Origin", version: "1.60", isActive: true },
        { id: "addon@darkreader.org", name: "Dark Reader", version: "4.9", isActive: true },
        { id: "formautofill@mozilla.org", name: "Form Autofill", isBuiltin: true, isActive: true },
      ] };
      obj.PrivateBrowsingUtils = { isWindowPrivate: () => false, isBrowserPrivate: () => false };
    },
    requestProcInfo: async () => procInfo(),
    now: () => performance.now() - t0,
    generateQI: () => () => {},
  },
  Services: {
    appinfo: { processID: 100, version: "146.0", appBuildID: "20261117" },
    prefs: {
      getIntPref: (n, d) => ({ "ffmon.intervalSec": 2, "ffmon.userscripts.delaySec": 1, "ffmon.userscripts.burstSec": 2 })[n] ?? d,
      getBoolPref: (n, d) => d,
    },
    profiler: profilerMock,
    sysinfo: { processInfo: Promise.resolve({ count: 8 }), getProperty: k => (k === "memsize" ? 16e9 : 8) },
    wm: { getEnumerator: () => [win] },
    obs: { addObserver: (o, topic) => observers.set(topic, o) },
    eTLD: { getSchemelessSite: u => u.host.split(".").slice(-2).join(".") },
    crashmanager: { getCrashes: async () => [{ id: "c-1", crashDate: new Date(), type: "main-crash", processType: "main", remoteID: "bp-9", metadata: { OOMAllocationSize: "1048576" } }] },
  },
  IOUtils: {
    makeDirectory: async d => fs.mkdirSync(d, { recursive: true }),
    writeUTF8: async (p, text, opts) => {
      if (opts.mode !== "appendOrCreate") throw new Error("unexpected mode");
      fs.appendFileSync(p, text);
    },
  },
  PathUtils: { join: (...p) => path.join(...p), profileDir: "/profile" },
  WebExtensionPolicy: {
    getByURI: u => policies[u.host] || null,
    getByHostname: h => policies[h] || (h === TM ? { id: "firefox@tampermonkey.net", name: "Tampermonkey" } : null),
  },
  Cc: { "@mozilla.org/memory-reporter-manager;1": { getService: () => ({ ghostWindows: 2 }) } },
  Ci: { nsIMemoryReporterManager: {}, nsIPropertyBag2: {}, nsIHangReport: {} },
});

const mod = await import(pathToFileURL(modPath).href);
mod.start({ dataDir: outDir });
const sleep = ms => new Promise(r => setTimeout(r, ms));
await sleep(7500);
await sleep(1500);

lazyTab.linkedBrowser.isConnected = true;
tabs[1].linkedBrowser.isConnected = false;
const notify = (topic, subject, data) => observers.get(topic).observe(subject, topic, data);
notify("ipc:content-shutdown", { QueryInterface() { return this; }, get: k => ({ abnormal: true, childID: 2, dumpID: "dump-1" })[k] });
notify("process-hang-report", { QueryInterface() { return this; }, addonId: "addon@darkreader.org", scriptFileName: "moz-extension://dark/inject.js", hangDuration: 9000, childID: 3, scriptBrowser: yt.linkedBrowser });
notify("process-hang-report", { QueryInterface() { return this; }, addonId: "", scriptFileName: usUrl("Comic%20Reader", "aaa"), hangDuration: 12000, childID: 2, scriptBrowser: yt.linkedBrowser });
notify("memory-pressure", null, "low-memory");
await sleep(2500);
notify("quit-application-granted");
notify("quit-application");
await sleep(500);

const files = fs.readdirSync(outDir).filter(f => f.endsWith(".jsonl"));
const lines = files.flatMap(f => fs.readFileSync(path.join(outDir, f), "utf8").trim().split("\n")).map(l => JSON.parse(l));
const by = t => lines.filter(l => l.t === t);
const samples = by("s");
const check = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exitCode = 1; } else console.log("ok  ", msg); };
check(files.length === 1 && /^ff-\d{8}-100\.jsonl$/.test(files[0]), "writes one ff-YYYYMMDD-pid.jsonl file");
check(by("start").length === 1 && by("start")[0].cores === 8, "start record with core count");
check(samples.length >= 2, `samples written (${samples.length})`);
const last = samples[samples.length - 1];
check(last.procs.find(p => p.pid === 202).site === "https://github.com", "origin attributes stripped");
check(last.procs.find(p => p.pid === 209).site === "(private)", "private origin hidden");
check(last.procs.some(p => p.cpu != null && p.cpu > 0), "cpu computed from deltas");
const yt1 = samples[1].tabs.find(t => t.k.endsWith(".1"));
const yt2 = samples[1].tabs.find(t => t.k.endsWith(".2"));
check(yt1.m > yt2.m && Math.abs(yt1.m + yt2.m - (900e6 + 80e6)) < 2, "shared process split by frame weight incl. subframe process");
check(yt1.f === (2 | 4), "selected + audible flags");
check(samples[1].tabs.find(t => t.k.endsWith(".4")).f & 1, "discarded flag");
check(by("tab").length >= 4 && by("tab").every(t => t.site), "tab meta with sites");
check(by("ext").length === 2, "extension meta, builtins skipped");
const ex = last.exts;
const dark = ex.find(e => e.id === "addon@darkreader.org"), ub = ex.find(e => e.id === "uBlock0@raymondhill.net");
check(dark && ub && Math.abs(dark.m - 2 * ub.m) < 2, "extension process split by window count");
const ev = by("ev");
check(ev.some(e => e.kind === "content_crash" && e.site === "https://youtube.com" && e.tabs.length === 2 && e.dump === "dump-1"), "content crash mapped to tabs");
check(ev.some(e => e.kind === "script_hang" && e.addonName === "Dark Reader" && e.tab), "script hang attributed to extension and tab");
check(ev.some(e => e.kind === "memory_pressure"), "memory pressure event");
check(ev.some(e => e.kind === "tab_unloaded"), "tab unload detected");
check(ev.some(e => e.kind === "quit_requested") && ev.some(e => e.kind === "quit"), "quit events");
check(!ev.some(e => e.kind === "collector_error"), "no collector errors");
check(last.lag && typeof last.lag.max === "number", "lag stats present");
check(samples[0].ghost === 2, "ghost windows sampled");
const us = by("us");
check(us.length >= 1, "userscript burst recorded");
const u0 = us[0]?.scripts || [];
const comic = u0.find(x => x.id === "aaa");
const pixA = u0.find(x => x.id === "bbb" && x.pid === 201);
const pixB = u0.find(x => x.id === "bbb" && x.pid === 202);
check(comic && comic.name === "Comic Reader" && comic.ext === "Tampermonkey" && comic.ms === 10 && comic.n === 3, "innermost userscript frame wins, CPU deltas in µs used, name decoded");
check(pixA && pixA.ms === 4 && pixA.site === "https://youtube.com", "second script in same process with its site");
check(pixB && pixB.ms === 16 && pixB.n === 2, "string child profile, stringArray, sample weights");
check(!u0.some(x => x.pid === 201 && x.n > 4), "page-only and idle samples not attributed");
check(ev.some(e => e.kind === "script_hang" && e.usName === "Comic Reader"), "slow userscript named in hang event");
check(!profilerActive, "profiler stopped after burst");
console.log(`${lines.length} records`);
process.exit();
