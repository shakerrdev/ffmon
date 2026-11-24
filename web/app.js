"use strict";

const PRESETS = { "15m": 15 * 60e3, "1h": 3600e3, "6h": 6 * 3600e3, "24h": 24 * 3600e3, "3d": 3 * 86400e3, "7d": 7 * 86400e3, all: null };
const VIEWS = ["overview", "tabs", "sites", "extensions", "userscripts", "events", "setup"];
const KIND_LABEL = {
  crash: "Crash", content_crash: "Tab crash", linger: "Didn't exit", ui_hang: "Freeze", script_hang: "Slow script",
  system_pressure: "Low memory", memory_pressure: "Low-memory signal", tab_unloaded: "Tab unloaded", quit_requested: "Quit",
  ff_started: "Started", ff_exited: "Exited", collector_start: "Tracking", collector_error: "Collector error",
};

const state = {
  view: "overview",
  range: "6h",
  status: null,
  charts: [],
  timer: null,
  token: 0,
  tabsFilter: { q: "", openOnly: false },
  eventKinds: new Set(),
};

const root = document.documentElement;
const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const css = name => getComputedStyle($(".viz-root")).getPropertyValue(name).trim();
const seriesColor = i => css(`--s${(i % 8) + 1}`);
const sevColor = s => css(s === "bad" ? "--critical" : s === "warn" ? "--warning" : "--accent");
function alpha(hex, a) {
  const m = hex.replace("#", "");
  const n = parseInt(m.length === 3 ? m.split("").map(c => c + c).join("") : m, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function fmtBytes(n, digits) {
  if (n == null || isNaN(n)) return "–";
  const a = Math.abs(n);
  if (a >= 1024 ** 3) return (n / 1024 ** 3).toFixed(digits ?? (a >= 100 * 1024 ** 3 ? 0 : 1)) + " GB";
  if (a >= 1024 ** 2) return (n / 1024 ** 2).toFixed(0) + " MB";
  if (a >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n.toFixed(0) + " B";
}
function fmtPct(f, digits) {
  if (f == null || isNaN(f)) return "–";
  const p = f * 100;
  return p.toFixed(digits ?? (p < 10 ? 1 : 0)) + "%";
}
function fmtDur(ms) {
  if (ms == null) return "–";
  let s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  let m = Math.floor(s / 60);
  s %= 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const hh = Math.floor(m / 60);
  m %= 60;
  if (hh < 48) return `${hh}h ${m}m`;
  return `${Math.floor(hh / 24)}d ${hh % 24}h`;
}
function fmtTime(ms, withDate) {
  if (!ms) return "–";
  const d = new Date(ms);
  const today = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (!withDate && d.toDateString() === today.toDateString()) return t;
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + " " + t;
}
function fmtAgo(ms) {
  if (!ms) return "never";
  return fmtDur(Date.now() - ms) + " ago";
}
const stripScheme = s => (s || "").replace(/^https?:\/\//, "");

function niceSplits(step) {
  return (u, axisIdx, min, max) => {
    const span = max - min;
    if (!(span > 0)) return [min];
    let inc = step(span);
    const out = [];
    for (let v = Math.ceil(min / inc) * inc; v <= max + 1e-9; v += inc) out.push(v);
    return out;
  };
}
const BYTE_STEPS = [1, 2, 5, 10, 20, 50, 128, 256, 512].map(m => m * 2 ** 20).concat([1, 2, 4, 8, 16, 32, 64, 128].map(g => g * 2 ** 30));
const bytesStep = span => BYTE_STEPS.find(s => span / s <= 5) || BYTE_STEPS[BYTE_STEPS.length - 1];
const decStep = span => {
  const raw = span / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map(m => m * p).find(s => span / s <= 5);
};

/* ---------- time range ---------- */

function currentRange() {
  if (typeof state.range === "object") return { ...state.range };
  const to = Date.now();
  const ms = PRESETS[state.range];
  const first = state.status?.first_ts || to - 7 * 86400e3;
  return { from: ms ? to - ms : Math.min(first, to - 3600e3), to };
}

function setRange(r) {
  state.range = r;
  writeHash();
  render();
}

function updateRangeUi() {
  for (const b of document.querySelectorAll("#range button")) b.classList.toggle("on", b.dataset.r === state.range);
  const lab = $("#rangeLabel");
  if (typeof state.range === "object") {
    lab.textContent = `${fmtTime(state.range.from, true)} → ${fmtTime(state.range.to)}`;
  } else {
    lab.textContent = state.range === "all" ? "everything recorded" : `last ${state.range}, live`;
  }
}

function writeHash() {
  const r = typeof state.range === "object" ? `${Math.round(state.range.from)}-${Math.round(state.range.to)}` : state.range;
  history.replaceState(null, "", `#${state.view}/${r}`);
}

function readHash() {
  const [v, r] = location.hash.slice(1).split("/");
  if (VIEWS.includes(v)) state.view = v;
  if (r in PRESETS) state.range = r;
  else if (r && /^\d+-\d+$/.test(r)) {
    const [from, to] = r.split("-").map(Number);
    state.range = { from, to };
  }
}

async function api(path, params = {}) {
  const qs = new URLSearchParams(params).toString();
  const res = await fetch(`/api/${path}${qs ? "?" + qs : ""}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

/* ---------- charts ---------- */

function legend(series, { line = false, toggle = null } = {}) {
  const el = h("div", { class: "legend" + (toggle ? " toggle" : "") });
  series.forEach((s, i) => {
    const item = h("span", { title: s.full || s.label }, h("i", { class: line ? "line" : "", style: `background:${s.color}` }), s.label);
    if (toggle) item.addEventListener("click", () => {
      item.classList.toggle("off");
      toggle(i, !item.classList.contains("off"));
    });
    el.append(item);
  });
  return el;
}

function timeChart(host, opt) {
  const { ts, series, stacked = false, fmt = v => v, kind = "bytes", height = 220, events = [], total = stacked, yMax = null } = opt;
  const xs = ts.map(t => t / 1000);
  const n = xs.length;
  const hasData = series.some(s => s.values.some(v => v != null));
  if (!hasData) {
    host.append(h("div", { class: "empty" }, opt.emptyText || "No data in this time range."));
    return null;
  }
  let plotted = series.map(s => s.values);
  if (stacked) {
    const acc = new Array(n).fill(null);
    plotted = series.map(s => {
      return s.values.map((v, i) => {
        if (v == null && acc[i] == null) return null;
        acc[i] = (acc[i] || 0) + (v || 0);
        return acc[i];
      });
    });
  }
  const order = stacked ? series.map((_, i) => series.length - 1 - i) : series.map((_, i) => i);
  const surface = css("--surface-1");
  const muted = css("--text-muted");
  const grid = css("--grid");
  const axis = css("--axis");
  const chartEl = h("div", { class: "chart" });
  host.append(chartEl);

  const uSeries = [{}].concat(order.map(i => {
    const s = series[i];
    return stacked
      ? { label: s.label, stroke: surface, width: 1, fill: s.color, points: { show: false }, spanGaps: false }
      : { label: s.label, stroke: s.color, width: s.width || 2, dash: s.dash, fill: s.fill ? alpha(s.color, 0.1) : undefined, points: { show: false }, spanGaps: false };
  }));
  const data = [xs].concat(order.map(i => plotted[i]));

  const tip = $("#tip");
  const step = kind === "bytes" ? bytesStep : decStep;
  const opts = {
    width: Math.max(300, host.clientWidth),
    height,
    legend: { show: false },
    cursor: { sync: { key: "ffmon" }, drag: { x: true, y: false, setScale: false }, points: { show: !stacked, size: 8 } },
    select: { show: true },
    scales: { x: { time: true }, y: { range: (u, min, max) => [0, yMax ?? (max > 0 ? max * 1.08 : 1)] } },
    axes: [
      { stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: axis, width: 1, size: 4 }, font: "11px system-ui", space: 70 },
      {
        stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: "11px system-ui", size: 64,
        splits: niceSplits(step), values: (u, vals) => vals.map(v => fmt(v, true)),
      },
    ],
    series: uSeries,
    hooks: {
      setSelect: [u => {
        if (u.select.width < 8) return;
        const from = u.posToVal(u.select.left, "x") * 1000;
        const to = u.posToVal(u.select.left + u.select.width, "x") * 1000;
        u.setSelect({ left: 0, width: 0 }, false);
        setRange({ from: Math.round(from), to: Math.round(to) });
      }],
      draw: [u => drawEvents(u, events)],
      setCursor: [u => showTip(u)],
    },
  };

  function showTip(u) {
    const idx = u.cursor.idx;
    if (idx == null || u.cursor.left < 0) {
      tip.hidden = true;
      return;
    }
    const rect = u.over.getBoundingClientRect();
    tip.replaceChildren();
    tip.append(h("div", { class: "t" }, fmtTime(ts[idx], true)));
    const rows = stacked ? [...series].reverse() : [...series].sort((a, b) => (b.values[idx] ?? -1) - (a.values[idx] ?? -1));
    let sum = 0;
    let any = false;
    for (const s of rows) {
      const v = s.values[idx];
      if (v == null) continue;
      any = true;
      sum += v;
      tip.append(h("div", { class: "row" }, h("i", { style: `background:${s.color}` }), h("span", { class: "v" }, fmt(v)), h("span", { class: "n" }, s.label)));
    }
    if (!any) tip.append(h("div", { class: "muted" }, "Firefox wasn't running or not recorded"));
    else if (total && rows.length > 1) tip.append(h("div", { class: "row tot" }, h("i"), h("span", { class: "v" }, fmt(sum)), h("span", { class: "n" }, "total")));
    const x = u.cursor.left;
    const near = events.filter(e => {
      const x0 = u.valToPos(e.ts / 1000, "x");
      const x1 = e.end ? u.valToPos(e.end / 1000, "x") : x0;
      return x >= x0 - 5 && x <= x1 + 5;
    }).slice(0, 4);
    if (near.length) {
      const box = h("div", { class: "ev" });
      for (const e of near) box.append(h("div", {}, h("span", { class: `sev ${e.severity}` }), e.title));
      tip.append(box);
    }
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const left = rect.left + x + 14 + tw > window.innerWidth ? rect.left + x - tw - 14 : rect.left + x + 14;
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.min(window.innerHeight - tip.offsetHeight - 8, rect.top + 8)}px`;
  }

  const u = new uPlot(opts, data, chartEl);
  u.over.addEventListener("mouseleave", () => (tip.hidden = true));
  const ro = new ResizeObserver(() => {
    const w = Math.max(300, host.clientWidth);
    if (Math.abs(w - u.width) > 2) u.setSize({ width: w, height });
  });
  ro.observe(host);
  const entry = { u, ro, toggle: (i, show) => u.setSeries(order.indexOf(i) + 1, { show }) };
  state.charts.push(entry);
  return entry;
}

function drawEvents(u, events) {
  if (!events.length) return;
  const ctx = u.ctx;
  const { left, top, width, height } = u.bbox;
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, top, width, height);
  ctx.clip();
  for (const e of events) {
    const x0 = u.valToPos(e.ts / 1000, "x", true);
    const color = sevColor(e.severity);
    const strong = e.severity === "bad";
    if (e.end && e.end > e.ts) {
      const x1 = u.valToPos(e.end / 1000, "x", true);
      ctx.fillStyle = alpha(color, 0.12);
      ctx.fillRect(x0, top, Math.max(2, x1 - x0), height);
    }
    ctx.strokeStyle = strong ? color : alpha(color, 0.35);
    ctx.lineWidth = (strong ? 1.5 : 1) * devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(x0, top);
    ctx.lineTo(x0, top + height);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    const s = (strong ? 5 : 3.5) * devicePixelRatio;
    ctx.moveTo(x0 - s, top);
    ctx.lineTo(x0 + s, top);
    ctx.lineTo(x0, top + s * 1.4);
    ctx.fill();
  }
  ctx.restore();
}

function chartCard(title, sub, build, extra) {
  const body = h("div");
  const card = h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, title), extra ? h("div", { class: "right" }, extra) : null), sub ? h("div", { class: "sub" }, sub) : null, body);
  requestAnimationFrame(() => build(body));
  return card;
}

const fmtB = (v, axis) => (axis && v === 0 ? "0" : fmtBytes(v));
const fmtP = (v, axis) => (axis && v === 0 ? "0" : fmtPct(v));
const fmtMs = (v, axis) => (axis && v === 0 ? "0" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
const fmtN = v => (v == null ? "–" : Math.round(v).toString());

/* ---------- table ---------- */

function table({ columns, rows, sort, onRow, empty = "Nothing recorded in this time range.", max = 500 }) {
  const wrap = h("div", { class: "scroll" });
  let key = sort?.key ?? columns[0].key;
  let dir = sort?.dir ?? -1;
  function draw() {
    const col = columns.find(c => c.key === key);
    const val = col?.sortVal || (r => r[key]);
    const sorted = [...rows].sort((a, b) => {
      const va = val(a), vb = val(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return (va > vb ? 1 : va < vb ? -1 : 0) * dir;
    });
    const thead = h("thead", {}, h("tr", {}, columns.map(c => h("th", {
      class: [c.num ? "num" : "", c.key === key ? "sorted" : "", c.key === key && dir > 0 ? "asc" : ""].join(" "),
      title: c.hint || null,
      onclick: () => {
        if (key === c.key) dir = -dir;
        else { key = c.key; dir = c.num ? -1 : 1; }
        draw();
      },
    }, c.label))));
    const tbody = h("tbody");
    for (const r of sorted.slice(0, max)) {
      const tr = h("tr", { class: onRow ? "click" : "" }, columns.map(c => {
        const v = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
        return h("td", { class: (c.num ? "num " : "") + (c.cls || "") }, v ?? "–");
      }));
      if (onRow) tr.addEventListener("click", () => onRow(r));
      tbody.append(tr);
    }
    wrap.replaceChildren(rows.length ? h("table", { class: "t" }, thead, tbody) : h("div", { class: "empty" }, empty));
    if (rows.length > max) wrap.append(h("div", { class: "muted", style: "padding:8px" }, `Showing ${max} of ${rows.length}.`));
  }
  draw();
  return wrap;
}

/* ---------- views ---------- */

const CAT_ORDER = ["Browser UI", "Web pages", "Extensions", "GPU", "Media", "Network", "Other"];
const catColor = cat => seriesColor(Math.max(0, CAT_ORDER.indexOf(cat)));

async function viewOverview(main, r) {
  const d = await api("overview", r);
  const k = d.kpi;
  const ramPct = k.mem_total ? k.ff_mem / k.mem_total : null;
  const usedPct = k.mem_total ? 1 - k.mem_avail / k.mem_total : null;
  const commitPct = k.commit_limit ? k.commit_total / k.commit_limit : null;
  const problems = k.crashes + k.hangs + k.lingers;
  const meter = (f) => h("div", { class: "meter " + (f > 0.9 ? "bad" : f > 0.8 ? "warn" : "") }, h("div", { style: `width:${Math.min(100, (f || 0) * 100)}%` }));
  const kpi = (label, value, small, note, extra, cls) =>
    h("div", { class: "card kpi " + (cls || "") }, h("div", { class: "label" }, label), h("div", { class: "value" }, value, small ? h("small", {}, small) : null), note ? h("div", { class: "note" }, note) : null, extra);

  main.append(h("div", { class: "grid kpis" },
    kpi("Firefox memory now", k.ff_running ? fmtBytes(k.ff_mem) : "Not running", k.ff_running && ramPct != null ? `${fmtPct(ramPct, 0)} of RAM` : null,
      k.ff_running ? `${k.ff_procs} processes` : (k.live ? "watchdog is live" : "no live data")),
    kpi("Peak in range", fmtBytes(k.peak_mem), null, k.avg_mem ? `average ${fmtBytes(k.avg_mem)}` : null),
    kpi("Firefox CPU now", k.ff_running ? fmtPct(k.ff_cpu) : "–", null, k.avg_cpu != null ? `average ${fmtPct(k.avg_cpu)} of all cores` : "of all cores"),
    kpi("Tabs", k.tabs != null ? String(k.tabs_loaded) : "–", k.tabs != null ? `loaded of ${k.tabs}` : null, k.tabs == null ? "needs the Firefox collector" : `${k.tabs - k.tabs_loaded} unloaded`),
    kpi("System RAM used", fmtPct(usedPct, 0), null, k.mem_total ? `${fmtBytes(k.mem_avail)} free of ${fmtBytes(k.mem_total, 0)}` : null, meter(usedPct)),
    kpi("Commit charge", fmtPct(commitPct, 0), null, k.commit_limit ? `${fmtBytes(k.commit_total)} of ${fmtBytes(k.commit_limit, 0)} (RAM + page file)` : null, meter(commitPct)),
    kpi("Problems in range", String(problems), null, `${k.crashes} crashes · ${k.hangs} freezes/slow scripts · ${k.lingers} didn't exit`, null, problems ? "alert" : ""),
  ));

  if (d.insights.length) {
    const box = h("div", { class: "insights" });
    for (const i of d.insights) {
      const link = { crash: "events", content_crash: "events", linger: "events", ui_hang: "events", script_hang: "extensions", leak: "tabs", sites: "sites", extensions: "extensions", tabs: "tabs", setup: "setup", system_pressure: "events", userscripts: "userscripts" }[i.kind];
      box.append(h("div", { class: `insight ${i.severity}` },
        h("div", { class: "ico" }, i.severity === "info" ? "i" : "!"),
        h("div", {}, h("b", {}, i.title), h("p", {}, i.body, link ? [" ", h("a", { href: "#", onclick: ev => { ev.preventDefault(); go(link); } }, "Details →")] : null))));
    }
    main.append(h("section", { class: "card", style: "margin-bottom:16px" }, h("h2", {}, "What stands out"), h("div", { class: "sub" }, "Automatic diagnosis for the selected time range."), box));
  }

  const ev = d.events;
  const catSeries = d.categories.map(cat => ({ label: cat, values: d.mem[cat], color: catColor(cat) }));
  const cpuSeries = d.categories.map(cat => ({ label: cat, values: d.cpu[cat], color: catColor(cat) }));
  const src = d.source === "firefox" ? "From Firefox's own process list." : "From Windows process data (install the collector for tab and site detail).";

  main.append(h("div", { class: "grid" },
    chartCard("Firefox memory by part", `${src} Markers show crashes, freezes and other problems; drag across a chart to zoom.`, body => {
      body.append(legend(catSeries, { toggle: (i, s) => c?.toggle(i, s) }));
      const c = timeChart(body, { ts: d.ts, series: catSeries, stacked: true, fmt: fmtB, events: ev, height: 260 });
    }),
  ));
  main.append(h("div", { class: "grid two section" },
    chartCard("System memory", "Where your RAM goes: Firefox vs. every other app.", body => {
      const s = [
        { label: "Firefox", values: d.system.firefox, color: seriesColor(1) },
        { label: "Other apps", values: d.system.other, color: css("--other") },
        { label: "Free", values: d.system.free, color: alpha(seriesColor(0), 0.25) },
      ];
      body.append(legend(s));
      timeChart(body, { ts: d.ts, series: s, stacked: true, fmt: fmtB, events: ev });
    }),
    chartCard("Commit charge", "Memory promised to programs vs. the limit (RAM + page file). Hitting the limit crashes apps.", body => {
      const s = [
        { label: "Committed", values: d.system.commit, color: seriesColor(0), fill: true },
        { label: "Limit", values: d.system.commit_limit, color: css("--text-muted"), width: 1, dash: [4, 4] },
      ];
      body.append(legend(s, { line: true }));
      timeChart(body, { ts: d.ts, series: s, fmt: fmtB, events: ev, total: false });
    }),
    chartCard("Firefox CPU by part", "Share of your whole CPU (all cores).", body => {
      body.append(legend(cpuSeries));
      timeChart(body, { ts: d.ts, series: cpuSeries, stacked: true, fmt: fmtP, kind: "pct", events: ev });
    }),
    chartCard("Browser responsiveness", "Longest main-thread stall per interval. Above ~100 ms you feel it; seconds means freezing.", body => {
      const s = [{ label: "Longest stall", values: d.lag.max, color: seriesColor(1), fill: true }];
      timeChart(body, { ts: d.ts, series: s, fmt: fmtMs, kind: "num", events: ev, emptyText: "Needs the Firefox collector." });
    }),
    chartCard("Tabs", "Loaded tabs use memory; unloaded tabs are kept but asleep.", body => {
      const s = [
        { label: "Loaded", values: d.tabs.loaded, color: seriesColor(0) },
        { label: "Unloaded", values: d.tabs.discarded, color: css("--other") },
      ];
      body.append(legend(s));
      timeChart(body, { ts: d.ts, series: s, stacked: true, fmt: (v) => fmtN(v), kind: "num", events: ev, emptyText: "Needs the Firefox collector." });
    }),
    h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Recent problems"), h("div", { class: "right" }, h("a", { href: "#", onclick: e => { e.preventDefault(); go("events"); } }, "All events →"))),
      h("div", { class: "sub" }, "Newest first."), eventList(ev.slice(0, 12), true)),
  ));
}

function tabStatus(r) {
  const bits = [];
  if (r.open_now) bits.push(r.discarded_now ? h("span", { class: "pill" }, "unloaded") : h("span", { class: "pill" }, "open"));
  else bits.push(h("span", { class: "pill" }, "closed"));
  if (r.pinned) bits.push(h("span", { class: "pill" }, "pinned"));
  if (r.audible_share > 0.2) bits.push(h("span", { class: "pill" }, "audio"));
  if (isLeak(r)) bits.push(h("span", { class: "pill warn", title: "Memory climbs steadily while loaded" }, "growing"));
  return bits;
}
const isLeak = r => r.growth_mb_h > 150 && r.growth_r2 > 0.6 && r.loaded_span_h >= 0.5;
const tabName = r => r.private ? "Private tab" : r.title || stripScheme(r.url) || r.site || "(untitled)";

async function viewTabs(main, r) {
  const d = await api("tabs", r);
  const maxMem = Math.max(1, ...d.tabs.map(t => t.max_mem || 0));
  const top = d.top.map((id, i) => {
    const t = d.tabs.find(x => x.id === id);
    return { id, label: trunc(tabName(t), 38), full: tabName(t), values: d.series[id].mem, cpu: d.series[id].cpu, color: seriesColor(i) };
  });
  main.append(h("div", { class: "grid two" },
    chartCard("Biggest tabs: memory", "Top 8 tabs by peak memory. A tab's share of a process it shares with same-site tabs is estimated by its frames.", body => {
      body.append(legend(top, { line: true }));
      timeChart(body, { ts: d.ts, series: top, fmt: fmtB, total: false, emptyText: "No tab data yet. Install the Firefox collector (Setup)." });
    }),
    chartCard("Biggest tabs: CPU", "Same tabs, share of all cores.", body => {
      const s = top.map(t => ({ ...t, values: t.cpu }));
      body.append(legend(s, { line: true }));
      timeChart(body, { ts: d.ts, series: s, fmt: fmtP, kind: "pct", total: false, emptyText: "No tab data yet." });
    }),
  ));

  const tableHost = h("div");
  const search = h("input", { type: "search", placeholder: "Filter by title, site or URL", value: state.tabsFilter.q });
  const openOnly = h("input", { type: "checkbox" });
  openOnly.checked = state.tabsFilter.openOnly;
  const drawTable = () => {
    const q = state.tabsFilter.q.toLowerCase();
    const rows = d.tabs.filter(t => (!state.tabsFilter.openOnly || t.open_now) && (!q || `${t.title} ${t.site} ${t.url}`.toLowerCase().includes(q)));
    tableHost.replaceChildren(table({
      rows,
      sort: { key: "max_mem", dir: -1 },
      onRow: t => openTab(t.id),
      columns: [
        { key: "title", label: "Tab", sortVal: tabName, cls: "title", fmt: (_, t) => h("div", {}, h("div", { class: "title", title: t.url }, tabName(t)), h("div", { class: "muted" }, t.site || "")) },
        { key: "last_mem", label: "Now", num: true, fmt: (v, t) => t.open_now && !t.discarded_now ? fmtBytes(v) : h("span", { class: "muted" }, "–") },
        { key: "avg_mem", label: "Average", num: true, fmt: v => fmtBytes(v) },
        { key: "max_mem", label: "Peak", num: true, fmt: v => h("span", {}, fmtBytes(v), h("span", { class: "bar" }, h("i", { style: `width:${(v / maxMem) * 100}%` }))) },
        { key: "avg_cpu", label: "CPU avg", num: true, fmt: v => fmtPct(v) },
        { key: "growth_mb_h", label: "Growth", num: true, hint: "Memory trend while loaded, in MB per hour", fmt: (v, t) => v == null ? "–" : h("span", { class: isLeak(t) ? "" : "muted" }, `${v > 0 ? "+" : ""}${Math.round(v)} MB/h`) },
        { key: "first_seen", label: "Status", sortVal: t => (t.open_now ? 1 : 0), fmt: (_, t) => tabStatus(t) },
        { key: "last_seen", label: "Last seen", num: true, fmt: (v, t) => t.open_now ? "now" : fmtTime(v) },
      ],
    }));
  };
  search.addEventListener("input", () => { state.tabsFilter.q = search.value; drawTable(); });
  openOnly.addEventListener("change", () => { state.tabsFilter.openOnly = openOnly.checked; drawTable(); });
  drawTable();
  main.append(h("section", { class: "card section" },
    h("div", { class: "card-head" }, h("h2", {}, `Tabs (${d.tabs.length})`)),
    h("div", { class: "sub" }, "Every tab seen in this range. Click one for its own timeline."),
    h("div", { class: "toolbar" }, search, h("label", { class: "check" }, openOnly, "Only tabs open now")),
    tableHost));
}

async function openTab(id) {
  const r = currentRange();
  const d = await api("tab", { ...r, id });
  const drawer = $("#drawer");
  drawer.replaceChildren();
  const m = d.meta || {};
  drawer.append(
    h("button", { class: "ghost close", onclick: closeDrawer }, "✕"),
    h("h3", {}, m.private ? "Private tab" : m.title || "(untitled)"),
    h("div", { class: "url" }, m.url || ""),
    h("dl", { class: "kv" },
      h("dt", {}, "Site"), h("dd", {}, m.site || "–"),
      h("dt", {}, "First seen"), h("dd", {}, fmtTime(m.first_seen, true)),
      h("dt", {}, "Last seen"), h("dd", {}, fmtTime(m.last_seen, true)),
      h("dt", {}, "Peak memory"), h("dd", {}, fmtBytes(m.peak_mem)),
      h("dt", {}, "Processes"), h("dd", {}, [...new Set(d.pids.join(",").split(",").filter(Boolean))].join(", ") || "–"),
    ));
  drawer.hidden = false;
  const memHost = h("div", { style: "margin-top:16px" }, h("h2", { style: "font-size:14px;margin:0 0 6px" }, "Memory"));
  const cpuHost = h("div", { style: "margin-top:16px" }, h("h2", { style: "font-size:14px;margin:0 0 6px" }, "CPU"));
  drawer.append(memHost, cpuHost);
  requestAnimationFrame(() => {
    timeChart(memHost, { ts: d.ts, series: [{ label: "Memory", values: d.mem, color: seriesColor(0), fill: true }], fmt: fmtB, total: false, height: 200 });
    timeChart(cpuHost, { ts: d.ts, series: [{ label: "CPU", values: d.cpu, color: seriesColor(1), fill: true }], fmt: fmtP, kind: "pct", total: false, height: 160 });
  });
}

function closeDrawer() {
  const drawer = $("#drawer");
  drawer.hidden = true;
  state.charts = state.charts.filter(c => {
    if (drawer.contains(c.u.root)) {
      c.ro.disconnect();
      c.u.destroy();
      return false;
    }
    return true;
  });
}

async function viewSites(main, r) {
  const d = await api("processes", r);
  const siteSeries = d.sites.map((s, i) => ({
    label: s === "Other sites" ? s : stripScheme(s),
    values: d.series[s].mem, cpu: d.series[s].cpu,
    color: s === "Other sites" ? css("--other") : seriesColor(i),
  }));
  main.append(h("div", { class: "grid two" },
    chartCard("Memory by site", "Web content processes grouped by site (top 7).", body => {
      body.append(legend(siteSeries));
      timeChart(body, { ts: d.ts, series: siteSeries, stacked: true, fmt: fmtB, emptyText: "Needs the Firefox collector." });
    }),
    chartCard("CPU by site", "Share of all cores.", body => {
      const s = siteSeries.map(x => ({ ...x, values: x.cpu }));
      body.append(legend(s));
      timeChart(body, { ts: d.ts, series: s, stacked: true, fmt: fmtP, kind: "pct", emptyText: "Needs the Firefox collector." });
    }),
  ));
  if (d.procs.length) {
    main.append(h("section", { class: "card section" }, h("h2", {}, "Firefox processes"),
      h("div", { class: "sub" }, "As Firefox reports them (like about:processes), over the selected range."),
      table({
        rows: d.procs, sort: { key: "max_mem", dir: -1 },
        columns: [
          { key: "pid", label: "PID", num: true },
          { key: "type", label: "Type" },
          { key: "site", label: "Site", fmt: v => stripScheme(v) || h("span", { class: "muted" }, "–") },
          { key: "avg_mem", label: "Avg memory", num: true, fmt: v => fmtBytes(v) },
          { key: "max_mem", label: "Peak", num: true, fmt: v => fmtBytes(v) },
          { key: "avg_cpu", label: "CPU avg", num: true, fmt: v => fmtPct(v) },
          { key: "max_cpu", label: "CPU peak", num: true, fmt: v => fmtPct(v) },
          { key: "threads", label: "Threads", num: true },
          { key: "t0", label: "Lived", num: true, sortVal: p => p.t1 - p.t0, fmt: (_, p) => `${fmtTime(p.t0)} – ${fmtTime(p.t1)}` },
        ],
      })));
  }
  main.append(h("section", { class: "card section" }, h("h2", {}, "Windows view of firefox.exe"),
    h("div", { class: "sub" }, "What Task Manager sees. Private bytes is committed memory, which is what runs out when Firefox crashes from low memory."),
    table({
      rows: d.os_procs, sort: { key: "max_private", dir: -1 },
      columns: [
        { key: "pid", label: "PID", num: true },
        { key: "kind", label: "Kind" },
        { key: "avg_private", label: "Private avg", num: true, fmt: v => fmtBytes(v) },
        { key: "max_private", label: "Private peak", num: true, fmt: v => fmtBytes(v) },
        { key: "max_rss", label: "Working set peak", num: true, fmt: v => fmtBytes(v) },
        { key: "avg_cpu", label: "CPU avg", num: true, fmt: v => fmtPct(v) },
        { key: "threads", label: "Threads", num: true },
        { key: "handles", label: "Handles", num: true },
        { key: "t0", label: "Lived", num: true, sortVal: p => p.t1 - p.t0, fmt: (_, p) => `${fmtTime(p.t0)} – ${fmtTime(p.t1)}` },
      ],
    })));
}

async function viewExtensions(main, r) {
  const d = await api("extensions", r);
  const top = d.top.map((id, i) => {
    const e = d.extensions.find(x => x.id === id);
    return { label: trunc(e.name, 30), full: e.name, values: d.series[id].cpu, mem: d.series[id].mem, color: seriesColor(i) };
  });
  main.append(h("div", { class: "grid two" },
    chartCard("Extension CPU", "Background work in the shared extension process. Content scripts running inside pages count toward the page instead.", body => {
      body.append(legend(top, { line: true }));
      timeChart(body, { ts: d.ts, series: top, fmt: fmtP, kind: "pct", total: false, emptyText: "Needs the Firefox collector." });
    }),
    chartCard("Extension memory", "Each extension's share of the extension process.", body => {
      const s = top.map(x => ({ ...x, values: x.mem }));
      body.append(legend(s, { line: true }));
      timeChart(body, { ts: d.ts, series: s, fmt: fmtB, total: false, emptyText: "Needs the Firefox collector." });
    }),
  ));
  main.append(h("section", { class: "card section" }, h("h2", {}, "Extensions"),
    h("div", { class: "sub" }, "Slow-script warnings mean the extension froze a page long enough for Firefox to notice."),
    table({
      rows: d.extensions, sort: { key: "avg_cpu", dir: -1 },
      columns: [
        { key: "name", label: "Extension", fmt: (v, e) => h("div", {}, h("div", {}, v), h("div", { class: "muted" }, e.key)) },
        { key: "version", label: "Version" },
        { key: "enabled", label: "State", fmt: v => (v ? "enabled" : h("span", { class: "muted" }, "disabled")) },
        { key: "avg_mem", label: "Memory avg", num: true, fmt: v => fmtBytes(v) },
        { key: "max_mem", label: "Peak", num: true, fmt: v => fmtBytes(v) },
        { key: "avg_cpu", label: "CPU avg", num: true, fmt: v => fmtPct(v, 2) },
        { key: "max_cpu", label: "CPU peak", num: true, fmt: v => fmtPct(v) },
        { key: "hangs", label: "Slow scripts", num: true, fmt: v => (v ? h("span", { class: "pill warn" }, String(v)) : "0") },
      ],
    })));
}

async function viewUserscripts(main, r) {
  const d = await api("userscripts", r);
  const top = d.top.map((id, i) => {
    const u = d.userscripts.find(x => x.id === id);
    return { label: trunc(u.name, 34), full: u.name, values: d.series[id], color: seriesColor(i) };
  });
  const every = d.bursts ? `${d.bursts} samples of ${fmtDur(d.measured_ms / d.bursts)}` : "no samples yet";
  main.append(h("div", { class: "grid" },
    chartCard("Userscript CPU", `Share of all cores while measured. Firefox's profiler samples for a few seconds every couple of minutes (${every} in this range).`, body => {
      body.append(legend(top, { line: true }));
      timeChart(body, { ts: d.ts, series: top, fmt: fmtP, kind: "pct", total: false,
        emptyText: d.bursts ? "No userscript code ran during the samples in this range." : "No samples yet. Needs collector v4 (restart Firefox after updating)." });
    }),
  ));
  main.append(h("section", { class: "card section" }, h("h2", {}, "Userscripts"),
    h("div", { class: "sub" }, "Only CPU can be split per script; Firefox doesn't track memory per script. Their CPU also counts toward the tab they run in."),
    table({
      rows: d.userscripts, sort: { key: "avg_cpu", dir: -1 },
      empty: d.bursts ? "No userscript ran while measuring." : "No samples yet.",
      columns: [
        { key: "name", label: "Script", fmt: (v, u) => h("div", {}, h("div", {}, v), h("div", { class: "muted" }, u.manager || "")) },
        { key: "avg_cpu", label: "CPU avg", num: true, hint: "Average over all samples in this range", fmt: v => fmtPct(v, 2) },
        { key: "max_cpu", label: "CPU peak", num: true, hint: "Busiest single sample", fmt: v => fmtPct(v) },
        { key: "active_share", label: "Running in", num: true, hint: "Share of samples in which the script ran at all", fmt: v => fmtPct(v, 0) },
        { key: "cpu_ms", label: "CPU time measured", num: true, fmt: v => fmtDur(v) === "0s" ? `${Math.round(v)} ms` : fmtDur(v) },
        { key: "sites", label: "Sites", sortVal: u => u.sites.join(), fmt: v => v.length ? v.join(", ") : h("span", { class: "muted" }, "–") },
        { key: "hangs", label: "Slow scripts", num: true, fmt: v => (v ? h("span", { class: "pill warn" }, String(v)) : "0") },
        { key: "last_seen", label: "Last ran", num: true, fmt: v => fmtTime(v) },
      ],
    })));
}

function eventDetail(e) {
  const d = e.detail || {};
  const rows = [];
  const add = (k, v) => v != null && v !== "" && rows.push(h("dt", {}, k), h("dd", {}, v));
  if (e.end) add("Duration", fmtDur(e.end - e.ts) + (d.ongoing ? " (still going)" : ""));
  if (d.peak_mem) add("Firefox memory peak", fmtBytes(d.peak_mem));
  if (d.mem_at_start) add("Memory when it started", fmtBytes(d.mem_at_start));
  if (d.mem) add("Process memory", fmtBytes(d.mem));
  if (d.avg_cpu != null && e.kind !== "crash") add("CPU while it lasted", fmtPct(d.avg_cpu));
  if (d.procs) add("Processes still alive", d.procs);
  if (d.reason) add("Trigger", { windows_closed: "last window closed", no_windows: "no window ever appeared", quit_requested: "you quit Firefox" }[d.reason] || d.reason);
  if (d.firefox_share) add("Firefox share of RAM", fmtPct(d.firefox_share, 0));
  if (d.min_avail) add("Lowest free RAM", fmtBytes(d.min_avail));
  if (d.max_commit_ratio) add("Highest commit use", fmtPct(d.max_commit_ratio, 0));
  if (d.site) add("Site", stripScheme(d.site));
  if (d.tab_titles?.length) add("Tabs affected", d.tab_titles.filter(Boolean).join(", "));
  if (d.usName) add("Userscript", d.usName);
  else if (d.addonName || d.addon) add("Extension", d.addonName || d.addon);
  if (d.script) add("Script", d.script);
  if (d.pid) add("PID", d.pid);
  const rep = e.kind === "crash" ? d : d.report;
  if (rep) {
    add("Process", rep.process);
    add("Out of memory", rep.oom ? "yes" : "no");
    for (const [k, v] of Object.entries(rep.meta || {})) add(k, /Memory|PageFile|AllocationSize/.test(k) && !isNaN(v) ? `${fmtBytes(Number(v))} (${v})` : v);
    if (rep.remote_id) {
      const id = String(rep.remote_id).replace(/^bp-/, "");
      rows.push(h("dt", {}, "Report"), h("dd", {}, h("a", { href: `https://crash-stats.mozilla.org/report/index/${id}`, target: "_blank", rel: "noopener" }, rep.remote_id)));
    }
    add("Crash ID", rep.id);
  }
  return h("dl", { class: "kv" }, rows);
}

function eventList(events, compact) {
  if (!events.length) return h("div", { class: "empty" }, "Nothing happened in this range.");
  const list = h("div", { class: "evlist" });
  for (const e of events) {
    const row = h("div", { class: "ev" },
      h("div", { class: "when" }, fmtTime(e.ts, !compact)),
      h("div", {}, h("span", { class: `sev ${e.severity}` }, h("span", {}, e.title))),
      h("div", { class: "dur" }, e.end ? fmtDur(e.end - e.ts) : KIND_LABEL[e.kind] || e.kind),
      h("div", { class: "details" }, eventDetail(e)));
    row.addEventListener("click", () => row.classList.toggle("open"));
    list.append(row);
  }
  return list;
}

async function viewEvents(main, r) {
  const d = await api("events", r);
  const counts = {};
  for (const e of d.events) counts[e.kind] = (counts[e.kind] || 0) + 1;
  const listHost = h("div");
  const chips = h("div", { class: "chips" });
  const kinds = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  const draw = () => {
    const sel = state.eventKinds;
    const evs = d.events.filter(e => !sel.size || sel.has(e.kind));
    listHost.replaceChildren(eventList(evs));
    chips.replaceChildren(...kinds.map(k => h("button", {
      class: sel.has(k) ? "on" : "",
      onclick: () => { sel.has(k) ? sel.delete(k) : sel.add(k); draw(); },
    }, `${KIND_LABEL[k] || k} ${counts[k]}`)));
  };
  draw();
  main.append(h("section", { class: "card" }, h("h2", {}, "Events & crashes"),
    h("div", { class: "sub" }, "Crashes come from Firefox's crash reports; freezes and 'didn't exit' are detected from outside Firefox. Click an event for details."),
    h("div", { class: "toolbar" }, chips), listHost));
}

async function viewSetup(main) {
  const s = await api("status");
  const now = s.now;
  const fresh = ts => ts && now - ts < 30000;
  const col = s.meta?.collector;
  const line = (ok, text) => h("div", { class: "status-line" }, h("span", { class: `sev ${ok === true ? "info" : ok === false ? "bad" : "warn"}` }), text);
  main.append(h("section", { class: "card setup" }, h("h2", {}, "Status"),
    line(fresh(s.last_os), `Watchdog: last system sample ${fmtAgo(s.last_os)}`),
    line(s.last_ff ? (now - s.last_ff < 60000 ? true : null) : false,
      s.last_ff ? `Firefox collector: last report ${fmtAgo(s.last_ff)}${col ? ` (v${col.v}, Firefox ${col.ff})` : ""}` : "Firefox collector: no data yet"),
    line(s.install ? true : null, s.install ? `Hooked into ${s.install.firefox_dir}` : "Collector not installed by this copy (or installed elsewhere)"),
    h("div", { class: "muted", style: "margin-top:8px" }, `Data folder: ${s.data_dir} · database ${fmtBytes(s.db_bytes)} · recording since ${fmtTime(s.first_ts, true)}`),
  ));
  main.append(h("section", { class: "card setup section" }, h("h2", {}, "How it works"),
    h("ol", {},
      h("li", {}, "The ", h("b", {}, "watchdog"), " (this program) samples every firefox.exe process every few seconds, checks whether Firefox's windows respond, notices when Firefox keeps running after you close it, and reads new crash reports."),
      h("li", {}, "The ", h("b", {}, "collector"), " runs inside Firefox (loaded through Firefox's autoconfig feature). It records per-tab, per-site and per-extension memory and CPU, main-thread stalls, slow-script warnings and tab crashes."),
      h("li", {}, "This dashboard reads both from a local SQLite database. Nothing leaves your computer."),
    ),
    h("h2", { style: "margin-top:14px" }, "Commands"),
    h("pre", {}, [
      "python ffmon.py install        # admin terminal: hook the collector into Firefox + autostart",
      "python ffmon.py run --open     # watchdog + dashboard",
      "python ffmon.py status         # is data coming in?",
      "python ffmon.py uninstall      # admin terminal: remove the collector",
    ].join("\n")),
    h("p", { class: "muted" }, "If Firefox is running but the collector has no recent reports, a Firefox update may have removed the hook: run install again and restart Firefox."),
  ));
}

function trunc(s, n) {
  s = s || "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/* ---------- shell ---------- */

const RENDER = { overview: viewOverview, tabs: viewTabs, sites: viewSites, extensions: viewExtensions, userscripts: viewUserscripts, events: viewEvents, setup: viewSetup };

function go(view) {
  state.view = view;
  writeHash();
  render();
  window.scrollTo(0, 0);
}

async function render() {
  const token = ++state.token;
  updateRangeUi();
  for (const b of document.querySelectorAll("#views button")) b.classList.toggle("on", b.dataset.v === state.view);
  const main = $("#main");
  main.classList.add("loading");
  const next = h("div");
  const r = currentRange();
  try {
    await RENDER[state.view](next, { from: Math.round(r.from), to: Math.round(r.to) });
  } catch (e) {
    next.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Couldn't load data"), h("p", { class: "muted" }, String(e.message || e))));
  }
  if (token !== state.token) return;
  const scroll = window.scrollY;
  const drawer = $("#drawer");
  const keep = [];
  for (const c of state.charts) {
    if (!drawer.hidden && drawer.contains(c.u.root)) keep.push(c);
    else {
      c.ro?.disconnect();
      c.u.destroy();
    }
  }
  state.charts = keep;
  main.replaceChildren(...next.childNodes);
  main.classList.remove("loading");
  window.scrollTo(0, scroll);
  $("#tip").hidden = true;
}

async function pollStatus() {
  try {
    state.status = await api("status");
    const s = state.status;
    const live = $("#live");
    const osFresh = s.last_os && s.now - s.last_os < 30000;
    const ffFresh = s.last_ff && s.now - s.last_ff < 60000;
    live.className = "live " + (osFresh ? "on" : "stale");
    live.textContent = osFresh ? (ffFresh ? "Live · collector reporting" : "Live · system only") : `Not recording (last ${fmtAgo(s.last_os)})`;
  } catch (e) {
    $("#live").className = "live stale";
    $("#live").textContent = "Watchdog not reachable";
  }
}

function tick() {
  if (document.hidden || typeof state.range === "object" || !$("#drawer").hidden) return;
  if (document.activeElement && document.activeElement.matches("input")) return;
  render();
}

function initTheme() {
  const saved = localStorage.getItem("ffmon-theme");
  if (saved) root.dataset.theme = saved;
  $("#theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ffmon-theme", root.dataset.theme); } catch (e) {}
    render();
  });
}

function init() {
  readHash();
  initTheme();
  for (const b of document.querySelectorAll("#range button")) b.addEventListener("click", () => setRange(b.dataset.r));
  for (const b of document.querySelectorAll("#views button")) b.addEventListener("click", () => go(b.dataset.v));
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
  window.addEventListener("hashchange", () => { readHash(); render(); });
  pollStatus().then(render);
  setInterval(pollStatus, 10000);
  setInterval(tick, 15000);
}

init();
