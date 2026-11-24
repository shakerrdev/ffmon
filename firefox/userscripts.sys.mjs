/* Per-userscript CPU time.
 *
 * Userscript managers give each script its own source URL (Tampermonkey:
 * moz-extension://<uuid>/userscripts/<name>.user.js?id=<id>). Firefox's
 * profiler records that URL for every JS frame, so short profiler bursts tell
 * how much CPU each script used, in which process.
 */

import { setTimeout } from "resource://gre/modules/Timer.sys.mjs";

const SCRIPT_RE = /moz-extension:\/\/([0-9a-f-]+)\/[^\s()]*?([^/\s()?]+\.user\.js)(?:\?id=([0-9a-zA-Z-]+))?/;
const ENTRIES = 1 << 21;
const INTERVAL_MS = 4;

export function parseScriptUrl(location) {
  const m = SCRIPT_RE.exec(location || "");
  if (!m) {
    return null;
  }
  const file = safeDecode(m[2]);
  return { ext: m[1], file, id: m[3] || file, name: file.replace(/\.user\.js$/, "") };
}

export function attributeProfile(profile) {
  const out = new Map();
  for (const proc of walkProcesses(profile)) {
    const meta = proc.meta || {};
    const interval = Number(meta.interval) || INTERVAL_MS;
    const cpuUnit = meta.sampleUnits?.threadCPUDelta;
    const cpuScale = cpuUnit === "µs" ? 1 / 1000 : cpuUnit === "ns" ? 1 / 1e6 : null;
    for (const thread of proc.threads || []) {
      attributeThread(thread, proc, interval, cpuScale, out);
    }
  }
  return out;
}

function attributeThread(thread, proc, interval, cpuScale, out) {
  const strings = thread.stringTable || thread.stringArray || proc.shared?.stringTable || proc.shared?.stringArray;
  const samples = thread.samples;
  const stackTable = thread.stackTable;
  const frameTable = thread.frameTable;
  if (!strings || !samples?.data?.length || !stackTable?.data || !frameTable?.data) {
    return;
  }
  const sCol = samples.schema;
  const stackCol = sCol.stack ?? 0;
  const weightCol = sCol.weight;
  const cpuCol = cpuScale ? sCol.threadCPUDelta : undefined;
  const prefixCol = stackTable.schema.prefix ?? 0;
  const frameCol = stackTable.schema.frame ?? 1;
  const locCol = frameTable.schema.location ?? 0;

  const frameScript = new Map();
  const scriptOfFrame = f => {
    if (!frameScript.has(f)) {
      const row = frameTable.data[f];
      frameScript.set(f, row ? parseScriptUrl(strings[row[locCol]]) : null);
    }
    return frameScript.get(f);
  };
  const stackScript = new Map();
  const scriptOfStack = s => {
    const seen = [];
    let found = null;
    let cur = s;
    while (cur != null) {
      if (stackScript.has(cur)) {
        found = stackScript.get(cur);
        break;
      }
      seen.push(cur);
      const row = stackTable.data[cur];
      if (!row) {
        break;
      }
      const sc = scriptOfFrame(row[frameCol]);
      if (sc) {
        found = sc;
        break;
      }
      cur = row[prefixCol];
    }
    for (const x of seen) {
      stackScript.set(x, found);
    }
    return found;
  };

  const pid = Number(thread.pid ?? proc.meta?.pid ?? 0);
  for (const row of samples.data) {
    const stack = row[stackCol];
    if (stack == null) {
      continue;
    }
    const sc = scriptOfStack(stack);
    if (!sc) {
      continue;
    }
    let ms;
    if (cpuCol !== undefined && row[cpuCol] != null) {
      ms = row[cpuCol] * cpuScale;
    } else {
      ms = interval * (weightCol !== undefined && row[weightCol] != null ? row[weightCol] : 1);
    }
    const key = `${sc.id}|${pid}`;
    const cur = out.get(key) || { ...sc, pid, ms: 0, samples: 0 };
    cur.ms += ms;
    cur.samples++;
    out.set(key, cur);
  }
}

function* walkProcesses(profile) {
  if (!profile) {
    return;
  }
  const p = typeof profile === "string" ? JSON.parse(profile) : profile;
  yield p;
  for (const child of p.processes || []) {
    yield* walkProcesses(child);
  }
}

function safeDecode(s) {
  try {
    return decodeURIComponent(s);
  } catch (e) {
    return s;
  }
}

export class UserscriptSampler {
  constructor({ push, reportError, cores, siteOfPid, extName, setBusy }) {
    Object.assign(this, { push, reportError, cores, siteOfPid, extName, setBusy });
    this.everyMs = Math.max(30, Services.prefs.getIntPref("ffmon.userscripts.everySec", 120)) * 1000;
    this.burstMs = Math.min(60, Math.max(2, Services.prefs.getIntPref("ffmon.userscripts.burstSec", 15))) * 1000;
    this.stopped = false;
  }

  start() {
    if (!Services.prefs.getBoolPref("ffmon.userscripts.enabled", true) || !Services.profiler) {
      return;
    }
    setTimeout(() => this.loop(), Math.max(1, Services.prefs.getIntPref("ffmon.userscripts.delaySec", 20)) * 1000);
  }

  stop() {
    this.stopped = true;
  }

  loop() {
    if (this.stopped) {
      return;
    }
    this.burst()
      .catch(e => this.reportError(e))
      .finally(() => {
        if (!this.stopped) {
          setTimeout(() => this.loop(), Math.max(5000, this.everyMs - this.burstMs));
        }
      });
  }

  isOurs() {
    let cfg = null;
    try {
      cfg = Services.profiler.activeConfiguration;
    } catch (e) {
      return true;
    }
    if (!cfg) {
      return true;
    }
    const threads = (cfg.threads || []).join(",");
    return Math.abs(cfg.interval - INTERVAL_MS) < 0.01 && (!threads || threads === "GeckoMain");
  }

  async burst() {
    if (Services.profiler.IsActive()) {
      return;
    }
    const started = Date.now();
    await Services.profiler.StartProfiler(ENTRIES, INTERVAL_MS, ["js", "cpu"], ["GeckoMain"]);
    await new Promise(r => setTimeout(r, this.burstMs));
    if (this.stopped || !Services.profiler.IsActive() || !this.isOurs()) {
      return;
    }
    let profile;
    this.setBusy(true);
    try {
      profile = await Services.profiler.getProfileDataAsync();
    } finally {
      await Services.profiler.StopProfiler();
    }
    const dur = Date.now() - started;
    let scripts;
    try {
      scripts = attributeProfile(profile);
    } finally {
      profile = null;
      this.setBusy(false);
    }
    const list = [...scripts.values()].map(s => ({
      id: s.id,
      name: s.name,
      file: s.file,
      ext: this.extName(s.ext) || s.ext,
      pid: s.pid,
      site: this.siteOfPid(s.pid),
      ms: Math.round(s.ms * 10) / 10,
      n: s.samples,
      cpu: Math.round((s.ms / dur / this.cores) * 1e5) / 1e5,
    }));
    this.push({ t: "us", ts: started, dur, scripts: list });
  }
}
