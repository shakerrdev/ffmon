/* Memory snapshots: Firefox's own memory report (what about:memory shows) for a content
 * process, boiled down to what explains a runaway one: explicit tree by area, heap-unclassified,
 * the biggest images with their decoded surfaces, the biggest windows and leaf reports.
 *
 * Taken automatically when a content process grows fast, or on request: drop a file named
 * memreport-request.json into the data folder ({} for the 3 biggest content processes, or
 * {"pids": [1234]}) and the next sample takes one.
 */

import { setTimeout, clearTimeout } from "resource://gre/modules/Timer.sys.mjs";

const GiB = 1024 ** 3;
const AUTO_MIN_MEM = 3 * GiB;
const AUTO_MIN_GROWTH = GiB;
const AUTO_WINDOW_MS = 3 * 60e3;
const AUTO_COOLDOWN_MS = 10 * 60e3;
const REPORT_TIMEOUT_MS = 120e3;
const REQUEST_FILE = "memreport-request.json";
const SKIP_TYPES = new Set(["browser", "gpu", "socket", "rdd", "utility", "gmpPlugin", "forkServer", "unknown"]);

export class MemReporter {
  constructor({ push, reportError, dataDir }) {
    this.push = push;
    this.reportError = reportError;
    this.requestPath = PathUtils.join(dataDir, REQUEST_FILE);
    this.hist = new Map();
    this.last = new Map();
    this.busy = false;
  }

  async tick(procs) {
    const now = Date.now();
    const content = procs.filter(p => !SKIP_TYPES.has(p.type));
    const live = new Set();
    for (const p of content) {
      live.add(p.pid);
      const h = this.hist.get(p.pid) || [];
      h.push([now, p.mem]);
      while (h.length && now - h[0][0] > AUTO_WINDOW_MS * 2) h.shift();
      this.hist.set(p.pid, h);
    }
    for (const pid of [...this.hist.keys()]) if (!live.has(pid)) this.hist.delete(pid);
    if (this.busy) return;
    this.busy = true;

    let targets = [];
    let reason = "";
    const req = await this.readRequest();
    if (req) {
      reason = "requested";
      const want = Array.isArray(req.pids) ? req.pids.map(Number) : [];
      targets = want.length ? content.filter(p => want.includes(p.pid)) : content.slice().sort((a, b) => b.mem - a.mem).slice(0, 3);
    } else {
      reason = "runaway";
      targets = content.filter(p => p.mem >= AUTO_MIN_MEM && this.growth(p.pid, now) >= AUTO_MIN_GROWTH && now - (this.last.get(p.pid) || 0) >= AUTO_COOLDOWN_MS);
    }
    if (!targets.length) {
      this.busy = false;
      return;
    }

    try {
      for (const p of targets) this.last.set(p.pid, now);
      const t0 = Date.now();
      const reps = await collect(targets.map(p => p.pid));
      const ms = Date.now() - t0;
      for (const p of targets) {
        this.push({ t: "ev", ts: Date.now(), kind: "mem_report", reason, pid: p.pid, site: p.site, ptype: p.type, mem: p.mem, growth: this.growth(p.pid, now), ms, rep: reps.get(p.pid) || null });
      }
    } catch (e) {
      this.reportError(e);
    } finally {
      this.busy = false;
    }
  }

  growth(pid, now) {
    const h = this.hist.get(pid) || [];
    const recent = h.filter(([ts]) => now - ts <= AUTO_WINDOW_MS);
    return recent.length > 1 ? recent[recent.length - 1][1] - recent[0][1] : 0;
  }

  async readRequest() {
    try {
      if (!(await IOUtils.exists(this.requestPath))) return null;
      const text = await IOUtils.readUTF8(this.requestPath);
      await IOUtils.remove(this.requestPath, { ignoreAbsent: true });
      try {
        return JSON.parse(text || "{}") || {};
      } catch (e) {
        return {};
      }
    } catch (e) {
      this.reportError(e);
      return null;
    }
  }
}

function newAgg() {
  return { explicit: 0, heapExplicit: 0, other: {}, groups: new Map(), windows: new Map(), images: new Map(), leaves: [], n: 0 };
}

function collect(pids) {
  const mgr = Cc["@mozilla.org/memory-reporter-manager;1"].getService(Ci.nsIMemoryReporterManager);
  const aggs = new Map(pids.map(pid => [pid, newAgg()]));
  const mainPid = Services.appinfo.processID;
  return new Promise(resolve => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve(new Map([...aggs].map(([pid, a]) => [pid, summarize(a)])));
    };
    const timer = setTimeout(finish, REPORT_TIMEOUT_MS);
    mgr.getReports(
      (process, path, kind, units, amount) => {
        const m = /\(pid (\d+)\)/.exec(process);
        const agg = aggs.get(m ? Number(m[1]) : mainPid);
        if (agg) add(agg, path, kind, units, amount);
      },
      null,
      () => {
        clearTimeout(timer);
        finish();
      },
      null,
      false
    );
  });
}

function add(a, path, kind, units, amount) {
  a.n++;
  if (units !== Ci.nsIMemoryReporter.UNITS_BYTES) {
    if (!path.includes("/") && a.n < 100000) a.other[path] = amount;
    return;
  }
  if (!path.startsWith("explicit/")) {
    if (!path.includes("/") || /^(imagelib|gfx|js-main-runtime|heap-|resident|private|vsize)/.test(path)) {
      if (path.split("/").length <= 2) a.other[path] = (a.other[path] || 0) + amount;
    }
    return;
  }
  a.explicit += amount;
  if (kind === Ci.nsIMemoryReporter.KIND_HEAP) a.heapExplicit += amount;
  const seg = path.split("/");
  for (const depth of [2, 3]) {
    if (seg.length > depth) bump(a.groups, seg.slice(0, depth).join("/"), amount);
  }
  if (seg[1] === "window-objects" && seg[2]) bump(a.windows, seg[2], amount);
  if (seg[1] === "images") {
    const i = seg.findIndex(s => s.startsWith("image("));
    if (i >= 0) {
      let img = a.images.get(seg[i]);
      if (!img) a.images.set(seg[i], (img = { b: 0, surfaces: new Map(), where: new Set() }));
      img.b += amount;
      img.where.add(seg.slice(2, i).join("/"));
      const s = seg.slice(i + 1).find(x => x.startsWith("surface("));
      if (s) bump(img.surfaces, s, amount);
    }
  }
  a.leaves.push([path, amount]);
  if (a.leaves.length > 4000) a.leaves = topLeaves(a.leaves, 200);
}

function bump(map, key, amount) {
  map.set(key, (map.get(key) || 0) + amount);
}

function topLeaves(leaves, n) {
  return leaves.sort((x, y) => y[1] - x[1]).slice(0, n);
}

const top = (map, n) => [...map].sort((x, y) => y[1] - x[1]).slice(0, n);
const clip = (s, n = 300) => (s.length > n ? s.slice(0, n) + "…" : s);

function summarize(a) {
  const heapAllocated = a.other["heap-allocated"];
  return {
    explicit: a.explicit,
    heapUnclassified: heapAllocated != null ? Math.max(0, heapAllocated - a.heapExplicit) : null,
    other: a.other,
    groups: top(a.groups, 30).map(([p, b]) => ({ p: clip(p), b })),
    windows: top(a.windows, 10).map(([p, b]) => ({ p: clip(p), b })),
    images: [...a.images]
      .sort((x, y) => y[1].b - x[1].b)
      .slice(0, 25)
      .map(([key, img]) => ({
        key: clip(key),
        b: img.b,
        where: [...img.where].slice(0, 4),
        surfaceCount: img.surfaces.size,
        surfaces: top(img.surfaces, 12).map(([s, b]) => ({ s: clip(s, 120), b })),
      })),
    imageCount: a.images.size,
    leaves: topLeaves(a.leaves, 25).map(([p, b]) => ({ p: clip(p), b })),
    reports: a.n,
  };
}
