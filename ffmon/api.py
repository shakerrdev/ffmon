import json
import math
import time

from .osmon import fmt_bytes, fmt_dur

MAX_POINTS = 480

CATEGORIES = ["Browser UI", "Web pages", "Extensions", "GPU", "Media", "Network", "Other"]
TYPE_CATEGORY = {
    "browser": "Browser UI", "main": "Browser UI",
    "web": "Web pages", "webIsolated": "Web pages", "webServiceWorker": "Web pages", "withCoopCoep": "Web pages",
    "file": "Web pages", "privilegedabout": "Web pages", "privilegedmozilla": "Web pages", "tab": "Web pages",
    "extension": "Extensions",
    "gpu": "GPU",
    "rdd": "Media", "gmpPlugin": "Media", "gmplugin": "Media", "utility": "Media",
    "socket": "Network",
}
WEB_TYPES = ("web", "webIsolated", "webServiceWorker", "withCoopCoep", "file", "privilegedabout", "privilegedmozilla")


def _cat_sql(col):
    whens = " ".join(f"WHEN '{t}' THEN '{c}'" for t, c in TYPE_CATEGORY.items())
    return f"CASE {col} {whens} ELSE 'Other' END"


class Api:
    def __init__(self, store, interval_os=5):
        self.store = store
        self.interval_os = interval_os

    def _conn(self):
        return self.store.reader()

    def _range(self, q):
        now = int(time.time() * 1000)
        to = int(q.get("to") or now)
        frm = int(q.get("from") or (to - 6 * 3600 * 1000))
        span = max(1, to - frm)
        bucket = max(self.interval_os * 1000, int(math.ceil(span / MAX_POINTS / 1000.0)) * 1000)
        return frm, to, bucket

    @staticmethod
    def _grid(frm, to, bucket):
        start = (frm // bucket) * bucket
        return list(range(start, to + 1, bucket))

    def _stack(self, c, frm, to, bucket, table, value_col, cat_expr, sample_table, extra_where=""):
        counts = {}
        for r in c.execute(
            f"SELECT (ts/?)*? b, main_pid m, COUNT(DISTINCT ts) n FROM {sample_table} WHERE ts BETWEEN ? AND ? GROUP BY b, m",
            (bucket, bucket, frm, to),
        ):
            counts[(r["b"], r["m"])] = r["n"]
        acc = {}
        for r in c.execute(
            f"SELECT (ts/?)*? b, main_pid m, {cat_expr} cat, SUM({value_col}) v FROM {table} "
            f"WHERE ts BETWEEN ? AND ? {extra_where} GROUP BY b, m, cat",
            (bucket, bucket, frm, to),
        ):
            n = counts.get((r["b"], r["m"])) or 1
            key = (r["b"], r["cat"])
            acc[key] = acc.get(key, 0) + (r["v"] or 0) / n
        buckets = {b for b, _ in counts}
        return acc, buckets

    def _series_from(self, grid, acc, buckets, names):
        series = {}
        for name in names:
            series[name] = [round(acc.get((b, name), 0), 5) if b in buckets else None for b in grid]
        return series

    def overview(self, q):
        frm, to, bucket = self._range(q)
        grid = self._grid(frm, to, bucket)
        c = self._conn()
        try:
            has_ff = c.execute("SELECT 1 FROM ff_sample WHERE ts BETWEEN ? AND ? LIMIT 1", (frm, to)).fetchone() is not None
            if has_ff:
                cat = _cat_sql("type")
                mem_acc, mem_b = self._stack(c, frm, to, bucket, "ff_proc", "mem", cat, "ff_sample")
                cpu_acc, cpu_b = self._stack(c, frm, to, bucket, "ff_proc", "COALESCE(cpu,0)", cat, "ff_sample")
            else:
                cat = _cat_sql("kind")
                mem_acc, mem_b = self._stack(c, frm, to, bucket, "os_proc", "COALESCE(private, rss)", cat, "os_proc")
                cpu_acc, cpu_b = self._stack(c, frm, to, bucket, "os_proc", "cpu", cat, "os_proc")
            used_cats = [k for k in CATEGORIES if any(v for (b, cc), v in mem_acc.items() if cc == k)]

            sysrows = {
                r["b"]: r
                for r in c.execute(
                    "SELECT (ts/?)*? b, AVG(mem_total) total, AVG(mem_total-mem_avail) used, MIN(mem_avail) avail_min, "
                    "AVG(ff_private) ff, MAX(ff_private) ff_max, AVG(commit_total) ct, AVG(commit_limit) cl, AVG(ff_cpu) ffcpu, AVG(sys_cpu) syscpu, "
                    "MAX(ff_hung) hung, AVG(ff_windows) win "
                    "FROM os_sample WHERE ts BETWEEN ? AND ? GROUP BY b",
                    (bucket, bucket, frm, to),
                )
            }

            def col(key, rows):
                return [(rows[b][key] if b in rows else None) for b in grid]

            ffrows = {
                r["b"]: r
                for r in c.execute(
                    "SELECT (ts/?)*? b, MAX(lag_max) lag_max, AVG(lag_avg) lag_avg, SUM(lag_blocked) blocked, AVG(tabs_loaded) loaded, "
                    "AVG(tabs_discarded) discarded, MAX(ghost) ghost FROM ff_sample WHERE ts BETWEEN ? AND ? GROUP BY b",
                    (bucket, bucket, frm, to),
                )
            }
            other_used = []
            for b in grid:
                r = sysrows.get(b)
                other_used.append(None if r is None else max(0, (r["used"] or 0) - (r["ff"] or 0)))

            out = {
                "range": {"from": frm, "to": to, "bucket": bucket},
                "source": "firefox" if has_ff else "os",
                "ts": grid,
                "categories": used_cats,
                "mem": self._series_from(grid, mem_acc, mem_b, used_cats),
                "cpu": self._series_from(grid, cpu_acc, cpu_b, used_cats),
                "system": {
                    "firefox": col("ff", sysrows),
                    "other": other_used,
                    "free": [(sysrows[b]["total"] - sysrows[b]["used"]) if b in sysrows else None for b in grid],
                    "commit": col("ct", sysrows),
                    "commit_limit": col("cl", sysrows),
                    "ff_cpu": col("ffcpu", sysrows),
                    "sys_cpu": col("syscpu", sysrows),
                    "hung": col("hung", sysrows),
                },
                "lag": {"max": col("lag_max", ffrows), "avg": col("lag_avg", ffrows), "blocked": col("blocked", ffrows)},
                "tabs": {"loaded": col("loaded", ffrows), "discarded": col("discarded", ffrows)},
                "kpi": self._kpis(c, frm, to),
                "events": self._events(c, frm, to, limit=400, min_severity="warn"),
                "insights": self._insights(c, frm, to),
            }
            return out
        finally:
            c.close()

    def _kpis(self, c, frm, to):
        latest_os = c.execute("SELECT * FROM os_sample ORDER BY ts DESC LIMIT 1").fetchone()
        latest_ff = c.execute("SELECT * FROM ff_sample ORDER BY ts DESC LIMIT 1").fetchone()
        peak = c.execute("SELECT MAX(ff_private) p, ts FROM os_sample WHERE ts BETWEEN ? AND ?", (frm, to)).fetchone()
        avg = c.execute("SELECT AVG(ff_private) a, AVG(ff_cpu) cpu FROM os_sample WHERE ts BETWEEN ? AND ? AND ff_procs > 0", (frm, to)).fetchone()
        counts = {}
        for e in self._events(c, frm, to, limit=100000):
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        now = int(time.time() * 1000)
        k = {
            "now_ms": now,
            "live": bool(latest_os and now - latest_os["ts"] < 30000),
            "ff_mem": latest_os["ff_private"] if latest_os else None,
            "ff_rss": latest_os["ff_rss"] if latest_os else None,
            "ff_procs": latest_os["ff_procs"] if latest_os else None,
            "ff_running": bool(latest_os and latest_os["ff_mains"]),
            "mem_total": latest_os["mem_total"] if latest_os else None,
            "mem_avail": latest_os["mem_avail"] if latest_os else None,
            "commit_total": latest_os["commit_total"] if latest_os else None,
            "commit_limit": latest_os["commit_limit"] if latest_os else None,
            "ff_cpu": latest_os["ff_cpu"] if latest_os else None,
            "peak_mem": peak["p"] if peak else None,
            "avg_mem": avg["a"] if avg else None,
            "avg_cpu": avg["cpu"] if avg else None,
            "tabs": latest_ff["tabs"] if latest_ff and now - latest_ff["ts"] < 60000 else None,
            "tabs_loaded": latest_ff["tabs_loaded"] if latest_ff and now - latest_ff["ts"] < 60000 else None,
            "crashes": counts.get("crash", 0) + counts.get("content_crash", 0),
            "hangs": counts.get("ui_hang", 0) + counts.get("script_hang", 0),
            "lingers": counts.get("linger", 0),
            "pressure": counts.get("system_pressure", 0),
        }
        return k

    def _events(self, c, frm, to, limit=1000, min_severity=None, kind=None):
        sev = {"info": 0, "warn": 1, "bad": 2}
        rows = []
        sql = "SELECT * FROM event WHERE ((ts BETWEEN ? AND ?) OR (end_ts IS NOT NULL AND end_ts >= ? AND ts <= ?))"
        args = [frm, to, frm, to]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        for r in c.execute(sql, args):
            if min_severity and sev.get(r["severity"], 0) < sev[min_severity]:
                continue
            rows.append({"id": r["id"], "ts": r["ts"], "end": r["end_ts"], "kind": r["kind"], "severity": r["severity"],
                         "title": r["title"], "detail": json.loads(r["detail"] or "{}")})
        return _merge_crashes(rows)

    def events(self, q):
        frm, to, _ = self._range(q)
        c = self._conn()
        try:
            return {"events": self._events(c, frm, to, limit=int(q.get("limit") or 2000), kind=q.get("kind") or None)}
        finally:
            c.close()

    def tabs(self, q):
        frm, to, bucket = self._range(q)
        c = self._conn()
        try:
            rows = []
            for r in c.execute(
                """
                SELECT t.id, t.title, t.url, t.site, t.private, t.pinned, t.window, t.first_seen, t.last_seen,
                  COUNT(*) n, AVG(CASE WHEN f.flags & 1 = 0 THEN f.mem END) avg_mem, MAX(f.mem) max_mem,
                  AVG(f.cpu) avg_cpu, MAX(f.cpu) max_cpu, SUM(f.flags & 1) disc_n, SUM((f.flags & 4) > 0) audible_n,
                  MIN(f.ts) t0, MAX(f.ts) t1,
                  SUM(CASE WHEN f.flags & 1 = 0 THEN 1 END) ln,
                  SUM(CASE WHEN f.flags & 1 = 0 THEN (f.ts - ?) / 3600000.0 END) sx,
                  SUM(CASE WHEN f.flags & 1 = 0 THEN f.mem / 1048576.0 END) sy,
                  SUM(CASE WHEN f.flags & 1 = 0 THEN ((f.ts - ?) / 3600000.0) * ((f.ts - ?) / 3600000.0) END) sxx,
                  SUM(CASE WHEN f.flags & 1 = 0 THEN ((f.ts - ?) / 3600000.0) * (f.mem / 1048576.0) END) sxy,
                  SUM(CASE WHEN f.flags & 1 = 0 THEN (f.mem / 1048576.0) * (f.mem / 1048576.0) END) syy
                FROM ff_tab f JOIN tab t ON t.id = f.tab_id
                WHERE f.ts BETWEEN ? AND ?
                GROUP BY t.id
                """,
                (frm, frm, frm, frm, frm, to),
            ):
                d = dict(r)
                slope, r2 = _regress(d)
                last = c.execute("SELECT mem, cpu, flags FROM ff_tab WHERE tab_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (d["id"], to)).fetchone()
                rows.append({
                    "id": d["id"], "title": d["title"], "url": d["url"], "site": d["site"], "private": d["private"],
                    "pinned": d["pinned"], "window": d["window"], "first_seen": d["first_seen"], "last_seen": d["last_seen"],
                    "avg_mem": d["avg_mem"], "max_mem": d["max_mem"], "avg_cpu": d["avg_cpu"], "max_cpu": d["max_cpu"],
                    "discarded_share": (d["disc_n"] or 0) / d["n"], "audible_share": (d["audible_n"] or 0) / d["n"],
                    "last_mem": last["mem"] if last else None, "last_cpu": last["cpu"] if last else None,
                    "open_now": bool(d["last_seen"] and time.time() * 1000 - d["last_seen"] < 60000),
                    "discarded_now": bool(last and last["flags"] & 1),
                    "growth_mb_h": slope, "growth_r2": r2, "loaded_span_h": (d["t1"] - d["t0"]) / 3600000.0,
                })
            rows.sort(key=lambda x: (x["max_mem"] or 0), reverse=True)
            top_ids = [r["id"] for r in rows[:8]]
            grid = self._grid(frm, to, bucket)
            series = {}
            if top_ids:
                q_ids = ",".join("?" * len(top_ids))
                acc = {}
                for r in c.execute(
                    f"SELECT tab_id, (ts/?)*? b, AVG(mem) m, AVG(cpu) cpu FROM ff_tab WHERE ts BETWEEN ? AND ? AND tab_id IN ({q_ids}) GROUP BY tab_id, b",
                    (bucket, bucket, frm, to, *top_ids),
                ):
                    acc[(r["tab_id"], r["b"])] = (r["m"], r["cpu"])
                for tid in top_ids:
                    series[tid] = {
                        "mem": [acc.get((tid, b), (None, None))[0] for b in grid],
                        "cpu": [acc.get((tid, b), (None, None))[1] for b in grid],
                    }
            return {"range": {"from": frm, "to": to, "bucket": bucket}, "ts": grid, "tabs": rows, "top": top_ids, "series": series}
        finally:
            c.close()

    def tab(self, q):
        frm, to, bucket = self._range(q)
        tid = int(q["id"])
        c = self._conn()
        try:
            meta = c.execute("SELECT * FROM tab WHERE id=?", (tid,)).fetchone()
            grid = self._grid(frm, to, bucket)
            acc = {r["b"]: r for r in c.execute(
                "SELECT (ts/?)*? b, AVG(mem) m, MAX(mem) mx, AVG(cpu) cpu, MAX(flags & 1) disc FROM ff_tab WHERE tab_id=? AND ts BETWEEN ? AND ? GROUP BY b",
                (bucket, bucket, tid, frm, to))}
            pids = [r["pids"] for r in c.execute("SELECT DISTINCT pids FROM ff_tab WHERE tab_id=? AND ts BETWEEN ? AND ? LIMIT 50", (tid, frm, to))]
            return {
                "meta": dict(meta) if meta else None, "ts": grid,
                "mem": [acc[b]["m"] if b in acc else None for b in grid],
                "mem_max": [acc[b]["mx"] if b in acc else None for b in grid],
                "cpu": [acc[b]["cpu"] if b in acc else None for b in grid],
                "discarded": [acc[b]["disc"] if b in acc else None for b in grid],
                "pids": pids,
            }
        finally:
            c.close()

    def processes(self, q):
        frm, to, bucket = self._range(q)
        c = self._conn()
        try:
            has_ff = c.execute("SELECT 1 FROM ff_proc WHERE ts BETWEEN ? AND ? LIMIT 1", (frm, to)).fetchone() is not None
            procs = []
            if has_ff:
                for r in c.execute(
                    "SELECT pid, type, site, MIN(ts) t0, MAX(ts) t1, AVG(mem) avg_mem, MAX(mem) max_mem, AVG(cpu) avg_cpu, MAX(cpu) max_cpu, MAX(threads) threads "
                    "FROM ff_proc WHERE ts BETWEEN ? AND ? GROUP BY pid, type, site ORDER BY max_mem DESC LIMIT 400",
                    (frm, to),
                ):
                    procs.append(dict(r))
            os_procs = []
            for r in c.execute(
                "SELECT pid, kind, MIN(ts) t0, MAX(ts) t1, AVG(rss) avg_rss, MAX(rss) max_rss, AVG(private) avg_private, MAX(private) max_private, "
                "AVG(cpu) avg_cpu, MAX(cpu) max_cpu, MAX(threads) threads, MAX(handles) handles "
                "FROM os_proc WHERE ts BETWEEN ? AND ? GROUP BY pid, kind ORDER BY max_private DESC, max_rss DESC LIMIT 400",
                (frm, to),
            ):
                os_procs.append(dict(r))
            grid = self._grid(frm, to, bucket)
            sites = []
            series = {}
            if has_ff:
                web = ",".join(f"'{t}'" for t in WEB_TYPES)
                ranked = [r["site"] for r in c.execute(
                    f"SELECT site, AVG(mem) a FROM ff_proc WHERE ts BETWEEN ? AND ? AND type IN ({web}) GROUP BY site ORDER BY a DESC",
                    (frm, to))]
                top = ranked[:7]
                expr = "CASE " + " ".join(f"WHEN site = ? THEN ?" for _ in top) + " ELSE 'Other sites' END" if top else "'Other sites'"
                params = []
                for s in top:
                    params += [s, s or "(no site)"]
                counts = {}
                for r in c.execute("SELECT (ts/?)*? b, main_pid m, COUNT(DISTINCT ts) n FROM ff_sample WHERE ts BETWEEN ? AND ? GROUP BY b, m", (bucket, bucket, frm, to)):
                    counts[(r["b"], r["m"])] = r["n"]
                acc = {}
                for r in c.execute(
                    f"SELECT (ts/?)*? b, main_pid m, {expr} s, SUM(mem) v, SUM(COALESCE(cpu,0)) cv FROM ff_proc WHERE ts BETWEEN ? AND ? AND type IN ({web}) GROUP BY b, m, s",
                    (bucket, bucket, *params, frm, to),
                ):
                    n = counts.get((r["b"], r["m"])) or 1
                    a = acc.setdefault((r["b"], r["s"]), [0, 0])
                    a[0] += r["v"] / n
                    a[1] += r["cv"] / n
                names = [s or "(no site)" for s in top] + (["Other sites"] if len(ranked) > len(top) else [])
                buckets = {b for b, _ in counts}
                for name in names:
                    series[name] = {
                        "mem": [acc.get((b, name), [0, 0])[0] if b in buckets else None for b in grid],
                        "cpu": [acc.get((b, name), [0, 0])[1] if b in buckets else None for b in grid],
                    }
                sites = names
            return {"range": {"from": frm, "to": to, "bucket": bucket}, "ts": grid, "procs": procs, "os_procs": os_procs, "sites": sites, "series": series, "source": "firefox" if has_ff else "os"}
        finally:
            c.close()

    def extensions(self, q):
        frm, to, bucket = self._range(q)
        c = self._conn()
        try:
            hangs = {}
            for r in c.execute("SELECT json_extract(detail,'$.addon') a, COUNT(*) n FROM event WHERE kind='script_hang' AND ts BETWEEN ? AND ? GROUP BY a", (frm, to)):
                if r["a"]:
                    hangs[r["a"]] = r["n"]
            rows = []
            for r in c.execute(
                "SELECT e.id, e.key, e.name, e.version, e.enabled, e.first_seen, e.last_seen, "
                "AVG(f.mem) avg_mem, MAX(f.mem) max_mem, AVG(f.cpu) avg_cpu, MAX(f.cpu) max_cpu, COUNT(f.ts) n "
                "FROM ext e LEFT JOIN ff_ext f ON f.ext_id = e.id AND f.ts BETWEEN ? AND ? GROUP BY e.id",
                (frm, to),
            ):
                d = dict(r)
                d["hangs"] = hangs.get(d["key"], 0)
                rows.append(d)
            rows.sort(key=lambda x: ((x["avg_cpu"] or 0), (x["max_mem"] or 0)), reverse=True)
            grid = self._grid(frm, to, bucket)
            top = [r["id"] for r in rows if r["n"]][:8]
            series = {}
            if top:
                acc = {}
                ids = ",".join("?" * len(top))
                for r in c.execute(
                    f"SELECT ext_id, (ts/?)*? b, AVG(mem) m, AVG(cpu) cpu FROM ff_ext WHERE ts BETWEEN ? AND ? AND ext_id IN ({ids}) GROUP BY ext_id, b",
                    (bucket, bucket, frm, to, *top),
                ):
                    acc[(r["ext_id"], r["b"])] = (r["m"], r["cpu"])
                for eid in top:
                    series[eid] = {"mem": [acc.get((eid, b), (None, None))[0] for b in grid],
                                   "cpu": [acc.get((eid, b), (None, None))[1] for b in grid]}
            return {"range": {"from": frm, "to": to, "bucket": bucket}, "ts": grid, "extensions": rows, "top": top, "series": series}
        finally:
            c.close()

    def userscripts(self, q):
        frm, to, bucket = self._range(q)
        c = self._conn()
        try:
            bursts = {(r["ts"], r["main_pid"]): r["dur"] for r in c.execute(
                "SELECT ts, main_pid, dur FROM us_burst WHERE ts BETWEEN ? AND ?", (frm, to))}
            n_bursts = len(bursts)
            hangs = {}
            for r in c.execute("SELECT json_extract(detail,'$.usId') u, COUNT(*) n FROM event WHERE kind='script_hang' AND ts BETWEEN ? AND ? GROUP BY u", (frm, to)):
                if r["u"]:
                    hangs[r["u"]] = r["n"]
            per_burst = {}
            sites = {}
            totals = {}
            for r in c.execute("SELECT ts, us_id, site, ms, cpu FROM ff_us WHERE ts BETWEEN ? AND ?", (frm, to)):
                k = (r["us_id"], r["ts"])
                per_burst[k] = per_burst.get(k, 0) + (r["cpu"] or 0)
                totals[r["us_id"]] = totals.get(r["us_id"], 0) + (r["ms"] or 0)
                if r["site"]:
                    s = sites.setdefault(r["us_id"], {})
                    s[r["site"]] = s.get(r["site"], 0) + (r["ms"] or 0)
            rows = []
            for r in c.execute("SELECT * FROM userscript WHERE id IN (SELECT DISTINCT us_id FROM ff_us WHERE ts BETWEEN ? AND ?)", (frm, to)):
                uid = r["id"]
                cpus = [v for (u, _), v in per_burst.items() if u == uid]
                site_ms = sorted(sites.get(uid, {}).items(), key=lambda x: -x[1])
                rows.append({
                    "id": uid, "key": r["key"], "name": r["name"], "manager": r["manager"],
                    "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                    "avg_cpu": sum(cpus) / n_bursts if n_bursts else None,
                    "max_cpu": max(cpus) if cpus else None,
                    "active_share": len(cpus) / n_bursts if n_bursts else None,
                    "cpu_ms": totals.get(uid, 0),
                    "sites": [s.split("://", 1)[-1] for s, _ in site_ms[:4]],
                    "hangs": hangs.get(r["key"], 0),
                })
            rows.sort(key=lambda x: x["avg_cpu"] or 0, reverse=True)
            times = sorted({ts for ts, _ in bursts})
            if len(times) > 2:
                gaps = sorted(b - a for a, b in zip(times, times[1:]))
                median = gaps[len(gaps) // 2]
                bucket = max(bucket, int(math.ceil(median * 1.5 / 60000.0)) * 60000)
            grid = self._grid(frm, to, bucket)
            burst_buckets = {}
            for (ts, _m) in bursts:
                b = (ts // bucket) * bucket
                burst_buckets[b] = burst_buckets.get(b, 0) + 1
            top = [r["id"] for r in rows[:8]]
            series = {}
            for uid in top:
                acc = {}
                for (u, ts), v in per_burst.items():
                    if u == uid:
                        b = (ts // bucket) * bucket
                        acc[b] = acc.get(b, 0) + v
                series[uid] = [(acc.get(b, 0) / burst_buckets[b]) if b in burst_buckets else None for b in grid]
            measured_ms = sum(bursts.values())
            return {"range": {"from": frm, "to": to, "bucket": bucket}, "ts": grid, "userscripts": rows, "top": top,
                    "series": series, "bursts": n_bursts, "measured_ms": measured_ms}
        finally:
            c.close()

    def status(self, q, extra=None):
        c = self._conn()
        try:
            last_os = c.execute("SELECT MAX(ts) t FROM os_sample").fetchone()["t"]
            last_ff = c.execute("SELECT MAX(ts) t FROM ff_sample").fetchone()["t"]
            first = c.execute("SELECT MIN(ts) t FROM os_sample").fetchone()["t"]
            first_ff = c.execute("SELECT MIN(ts) t FROM ff_sample").fetchone()["t"]
            meta = {r["k"]: json.loads(r["v"]) for r in c.execute("SELECT k, v FROM meta")}
            out = {"last_os": last_os, "last_ff": last_ff, "first_ts": first, "first_ff": first_ff, "meta": meta,
                   "now": int(time.time() * 1000), "db_bytes": self.store.path.stat().st_size if self.store.path.exists() else 0}
            if extra:
                out.update(extra)
            return out
        finally:
            c.close()

    def _insights(self, c, frm, to):
        out = []

        def add(sev, title, body, kind=None):
            out.append({"severity": sev, "title": title, "body": body, "kind": kind})

        lingers = [json.loads(r["detail"]) | {"ts": r["ts"]} for r in c.execute(
            "SELECT ts, detail FROM event WHERE kind='linger' AND ts BETWEEN ? AND ?", (frm, to))]
        if lingers:
            longest = max(lingers, key=lambda d: d.get("duration_ms", 0))
            biggest = max(d.get("peak_mem", 0) for d in lingers)
            after_quit = sum(1 for d in lingers if d.get("reason") in ("windows_closed", "quit_requested"))
            add("bad", f"Firefox didn't exit properly ({len(lingers)}×)",
                f"After closing, Firefox stayed running for up to {fmt_dur(longest.get('duration_ms'))} and held up to {fmt_bytes(biggest)}. "
                + ("This happened after you closed or quit it, which points to a shutdown hang. " if after_quit else "")
                + "Common causes are an extension blocking shutdown, a huge session file being written, or a stuck content process. "
                "To narrow it down: restart in Troubleshoot Mode (Help → Troubleshoot Mode) for a day; if lingering stops, it is an extension.",
                "linger")

        merged = self._events(c, frm, to, limit=100000)
        crashes = [e["detail"] for e in merged if e["kind"] == "crash"]
        crashes += [e["detail"]["report"] for e in merged if e["kind"] == "content_crash" and e["detail"].get("report")]
        content_crashes = sum(1 for e in merged if e["kind"] == "content_crash" and not e["detail"].get("report"))
        if crashes or content_crashes:
            oom = sum(1 for d in crashes if d.get("oom"))
            main = sum(1 for d in crashes if (d.get("process") or "main").lower() in ("main", "default", "browser"))
            body = f"{len(crashes)} crash report{'s' if len(crashes) != 1 else ''} ({main} of the whole browser)"
            if content_crashes:
                body += f", plus {content_crashes} tab process crash{'es' if content_crashes != 1 else ''} seen live"
            body += ". "
            if oom:
                body += (f"{oom} of them ran out of memory. On Windows that usually means the commit limit (RAM + page file) was used up: "
                         "let Windows manage the page file size or raise it, and cut down on loaded tabs. ")
            body += "Open about:crashes in Firefox to see the full reports."
            add("bad", "Crashes", body, "crash")

        hang = c.execute("SELECT COUNT(*) n, SUM(COALESCE(end_ts, ts) - ts) d FROM event WHERE kind='ui_hang' AND ts BETWEEN ? AND ?", (frm, to)).fetchone()
        if hang["n"]:
            add("bad", "Firefox froze", f"The window stopped responding {hang['n']}× for {fmt_dur(hang['d'])} in total. "
                "Check the memory chart around those markers: freezes during high memory or system pressure mean swapping; "
                "freezes with high Browser UI CPU mean the main process itself was busy.", "ui_hang")

        sh = list(c.execute(
            "SELECT COALESCE(NULLIF(json_extract(detail,'$.addonName'),''), json_extract(detail,'$.addon')) who, COUNT(*) n FROM event "
            "WHERE kind='script_hang' AND ts BETWEEN ? AND ? AND COALESCE(json_extract(detail,'$.usName'),'') = '' GROUP BY who ORDER BY n DESC", (frm, to)))
        us_hangs = list(c.execute(
            "SELECT json_extract(detail,'$.usName') who, COUNT(*) n FROM event WHERE kind='script_hang' AND ts BETWEEN ? AND ? "
            "AND COALESCE(json_extract(detail,'$.usName'),'') <> '' GROUP BY who ORDER BY n DESC", (frm, to)))
        n_bursts = c.execute("SELECT COUNT(*) n FROM us_burst WHERE ts BETWEEN ? AND ?", (frm, to)).fetchone()["n"]
        heavy = []
        if n_bursts:
            for r in c.execute(
                "SELECT u.name, SUM(f.cpu) s, MAX(f.cpu) mx FROM ff_us f JOIN userscript u ON u.id = f.us_id WHERE f.ts BETWEEN ? AND ? "
                "GROUP BY u.id ORDER BY s DESC LIMIT 4", (frm, to)):
                avg = r["s"] / n_bursts
                if avg >= 0.005 or (r["mx"] or 0) >= 0.05:
                    heavy.append((r["name"], avg, r["mx"]))
        if heavy or us_hangs:
            parts = [f"{n} averages {a * 100:.1f}% of your CPU (peaks {m * 100:.0f}%)" for n, a, m in heavy]
            parts += [f"{r['who']} froze a page {r['n']}×" for r in us_hangs if r["who"] not in {h[0] for h in heavy}]
            add("warn", "Userscripts using CPU", "; ".join(parts) + ". Their time is included in the tabs they run in.", "userscripts")
        ext_hangs = [r for r in sh if r["who"]]
        if ext_hangs:
            add("warn", "Extensions slowing pages down",
                ", ".join(f"{r['who']} ({r['n']}×)" for r in ext_hangs[:4]) + " ran scripts long enough for Firefox to flag them. "
                "Try disabling them one at a time.", "script_hang")
        page_hangs = sum(r["n"] for r in sh if not r["who"])
        if page_hangs:
            add("warn", "Slow page scripts", f"Pages ran scripts slow enough to trigger a warning {page_hangs}×. The Events tab lists which tabs.", "script_hang")

        pressure = c.execute("SELECT COUNT(*) n, SUM(COALESCE(end_ts, ts) - ts) d, MAX(json_extract(detail,'$.firefox_share')) share FROM event "
                             "WHERE kind='system_pressure' AND ts BETWEEN ? AND ?", (frm, to)).fetchone()
        if pressure["n"]:
            share = pressure["share"]
            add("warn", "Windows ran low on memory",
                f"The system was short on RAM or commit space {pressure['n']}× ({fmt_dur(pressure['d'])} total)"
                + (f"; Firefox held up to {share * 100:.0f}% of RAM then." if share else ".")
                + " Slowdowns and crashes are most likely in these windows.", "system_pressure")

        tabs = list(c.execute(
            """
            SELECT t.id, t.title, t.site, COUNT(*) ln, MIN(f.ts) t0, MAX(f.ts) t1, MAX(f.mem) mx,
              SUM((f.ts - ?) / 3600000.0) sx, SUM(f.mem / 1048576.0) sy,
              SUM(((f.ts - ?) / 3600000.0) * ((f.ts - ?) / 3600000.0)) sxx,
              SUM(((f.ts - ?) / 3600000.0) * (f.mem / 1048576.0)) sxy,
              SUM((f.mem / 1048576.0) * (f.mem / 1048576.0)) syy
            FROM ff_tab f JOIN tab t ON t.id = f.tab_id
            WHERE f.ts BETWEEN ? AND ? AND f.flags & 1 = 0
            GROUP BY t.id HAVING ln >= 20 AND (t1 - t0) >= 1800000
            """, (frm, frm, frm, frm, frm, to)))
        leaks = []
        for r in tabs:
            slope, r2 = _regress(dict(r))
            if slope and slope > 150 and r2 and r2 > 0.6:
                leaks.append((slope * (r["t1"] - r["t0"]) / 3600000.0, r, slope))
        leaks.sort(key=lambda x: x[0], reverse=True)
        if leaks:
            parts = [f"“{(r['title'] or r['site'] or 'tab')[:60]}” +{fmt_bytes(grow * 1048576)} at ~{slope:.0f} MB/h" for grow, r, slope in leaks[:3]]
            add("warn", "Tabs that keep growing", "; ".join(parts) + ". Memory that climbs steadily without dropping is a leak in the page "
                "(or an extension injecting into it). Reloading the tab frees it.", "leak")

        top_sites = list(c.execute(
            "SELECT site, AVG(s) a, MAX(s) mx FROM (SELECT ts, site, SUM(mem) s FROM ff_proc WHERE ts BETWEEN ? AND ? "
            "AND type IN ('web','webIsolated','withCoopCoep','webServiceWorker') GROUP BY ts, site) GROUP BY site ORDER BY a DESC LIMIT 3", (frm, to)))
        total = c.execute("SELECT AVG(mem_total) a FROM ff_sample WHERE ts BETWEEN ? AND ?", (frm, to)).fetchone()["a"]
        if top_sites and total:
            add("info", "Biggest sites", ", ".join(f"{_bare(r['site']) or '(unknown)'} avg {fmt_bytes(r['a'])} ({r['a'] / total * 100:.0f}%), peak {fmt_bytes(r['mx'])}" for r in top_sites) + ".", "sites")

        ext = c.execute("SELECT AVG(s) a, MAX(s) mx FROM (SELECT ts, SUM(mem) s FROM ff_proc WHERE ts BETWEEN ? AND ? AND type='extension' GROUP BY ts)", (frm, to)).fetchone()
        if ext["mx"] and ext["mx"] > 800 * 1048576:
            add("warn", "Extension process is large", f"The shared extension process averaged {fmt_bytes(ext['a'])} and peaked at {fmt_bytes(ext['mx'])}. "
                "See the Extensions tab for which add-on owns it.", "extensions")

        lag = c.execute("SELECT COUNT(*) n, SUM(lag_max >= 500) bad, MAX(lag_max) mx FROM ff_sample WHERE ts BETWEEN ? AND ?", (frm, to)).fetchone()
        if lag["n"] and lag["bad"] and lag["bad"] / lag["n"] > 0.02:
            add("warn", "Browser UI stutters", f"The main browser thread was blocked for ≥0.5 s in {lag['bad'] / lag['n'] * 100:.0f}% of samples (worst {lag['mx'] / 1000:.1f} s). "
                "That's the jank you feel when scrolling, typing or switching tabs.", "lag")

        ghost = c.execute("SELECT MAX(ghost) g FROM ff_sample WHERE ts BETWEEN ? AND ?", (frm, to)).fetchone()["g"]
        if ghost and ghost >= 3:
            add("warn", "Leaked windows", f"Firefox's main process reported up to {ghost} ghost windows (closed pages still held in memory), usually an extension or browser bug keeping them alive.", "ghost")

        tabs_now = c.execute("SELECT MAX(tabs_loaded) l, MAX(tabs) t FROM ff_sample WHERE ts BETWEEN ? AND ?", (frm, to)).fetchone()
        if tabs_now["l"] and tabs_now["l"] >= 80:
            add("info", "Lots of loaded tabs", f"Up to {tabs_now['l']} tabs were loaded at once (of {tabs_now['t']} open). Unloading idle tabs (right-click a tab → Unload Tab) frees their memory without closing them.", "tabs")

        growth = c.execute("SELECT MIN(ts) t0, MAX(ts) t1 FROM os_sample WHERE ts BETWEEN ? AND ? AND ff_procs > 0", (frm, to)).fetchone()
        if growth["t0"] and growth["t1"] - growth["t0"] > 2 * 3600000:
            r = c.execute(
                "SELECT COUNT(*) ln, SUM((ts - ?) / 3600000.0) sx, SUM(ff_private / 1048576.0) sy, SUM(((ts - ?) / 3600000.0) * ((ts - ?) / 3600000.0)) sxx, "
                "SUM(((ts - ?) / 3600000.0) * (ff_private / 1048576.0)) sxy, SUM((ff_private / 1048576.0) * (ff_private / 1048576.0)) syy "
                "FROM os_sample WHERE ts BETWEEN ? AND ? AND ff_procs > 0", (frm, frm, frm, frm, frm, to)).fetchone()
            slope, r2 = _regress(dict(r))
            if slope and slope > 100 and r2 and r2 > 0.5:
                add("warn", "Firefox grows over time", f"Total Firefox memory climbed ~{slope:.0f} MB per hour across this range. "
                    "Compare the Tabs list sorted by growth to find the source.", "growth")

        has_ff = c.execute("SELECT 1 FROM ff_sample LIMIT 1").fetchone()
        if not has_ff:
            add("info", "Only system-level data so far", "The in-Firefox collector hasn't reported yet, so tabs, sites and extensions are empty. "
                "Run `python ffmon.py install` (as administrator) and restart Firefox.", "setup")
        sev = {"bad": 0, "warn": 1, "info": 2}
        out.sort(key=lambda x: sev[x["severity"]])
        return out


def _bare(site):
    return (site or "").split("://", 1)[-1]


def _merge_crashes(rows):
    live = {}
    for e in rows:
        if e["kind"] == "content_crash" and e["detail"].get("dump"):
            live[e["detail"]["dump"]] = e
    if not live:
        return rows
    out = []
    for e in rows:
        if e["kind"] == "crash" and e["detail"].get("id") in live:
            cc = live[e["detail"]["id"]]
            cc["detail"]["report"] = e["detail"]
            reason = e["detail"].get("meta", {}).get("MozCrashReason")
            if reason and reason not in cc["title"]:
                cc["title"] += f": {reason[:100]}"
            continue
        out.append(e)
    return out


def _regress(d):
    n = d.get("ln") or 0
    if n < 5:
        return None, None
    sx, sy, sxx, sxy, syy = (d.get(k) or 0 for k in ("sx", "sy", "sxx", "sxy", "syy"))
    den = n * sxx - sx * sx
    if den <= 1e-9:
        return None, None
    slope = (n * sxy - sx * sy) / den
    vy = n * syy - sy * sy
    r2 = ((n * sxy - sx * sy) ** 2) / (den * vy) if vy > 1e-9 else 0
    return slope, r2
