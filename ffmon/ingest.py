import json
import time
from pathlib import Path

from .crashes import record_crash
from .osmon import fmt_bytes, fmt_dur


class Ingestor:
    def __init__(self, store, inbox: Path, keep_files_days=2):
        self.store = store
        self.inbox = Path(inbox)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.keep_files_days = keep_files_days
        self.tab_ids = {}
        self.ext_ids = {}
        self.ext_names = {}
        self.us_ids = {}
        self.main_pid = 0

    def run_once(self):
        files = sorted(self.inbox.glob("ff-*.jsonl"))
        for path in files:
            try:
                self._ingest_file(path)
            except OSError:
                continue
        self._cleanup(files)

    def _ingest_file(self, path: Path):
        st = path.stat()
        row = self.store.conn.execute("SELECT offset FROM ingest_state WHERE file=?", (path.name,)).fetchone()
        offset = row["offset"] if row else 0
        if st.st_size < offset:
            offset = 0
        if st.st_size == offset:
            return
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        lines = chunk[: end + 1].decode("utf-8", errors="replace").splitlines()
        new_offset = offset + end + 1

        def write(c):
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                self._handle(c, rec)
            c.execute("INSERT OR REPLACE INTO ingest_state(file, offset, mtime) VALUES (?,?,?)", (path.name, new_offset, st.st_mtime))

        self.store.write(write)

    def _cleanup(self, files):
        cutoff = time.time() - self.keep_files_days * 86400
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                continue
            row = self.store.conn.execute("SELECT offset FROM ingest_state WHERE file=?", (path.name,)).fetchone()
            if row and row["offset"] >= st.st_size:
                try:
                    path.unlink()
                    self.store.write(lambda c: c.execute("DELETE FROM ingest_state WHERE file=?", (path.name,)))
                except OSError:
                    pass

    def _handle(self, c, rec):
        t = rec.get("t")
        if t == "s":
            self._sample(c, rec)
        elif t == "tab":
            self._tab_meta(c, rec)
        elif t == "ext":
            self._ext_meta(c, rec)
        elif t == "us":
            self._userscripts(c, rec)
        elif t == "ev":
            self._event(c, rec)
        elif t == "crash":
            ts = rec.get("date") or rec.get("ts")
            record_crash(self.store, c, rec["id"], ts, rec.get("meta") or {}, ptype=rec.get("ptype", ""), remote=rec.get("remote", ""), source="firefox")
        elif t == "start":
            self.main_pid = rec.get("pid") or 0
            c.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES ('collector', ?)",
                (json.dumps({k: rec.get(k) for k in ("ts", "v", "ff", "build", "profile", "cores", "memsize", "interval", "pid")}),),
            )
            self.store.add_event(c, rec["ts"], "collector_start", "info", f"Tracking started in Firefox {rec.get('ff', '')}",
                                 {"pid": rec.get("pid"), "ff": rec.get("ff"), "profile": rec.get("profile")}, dedupe=f"cstart:{rec.get('session')}")

    def _tab_id(self, c, key, ts):
        tid = self.tab_ids.get(key)
        if tid is not None:
            return tid
        row = c.execute("SELECT id FROM tab WHERE key=?", (key,)).fetchone()
        if row:
            tid = row["id"]
        else:
            tid = c.execute("INSERT INTO tab(key, title, url, site, first_seen, last_seen) VALUES (?,?,?,?,?,?)", (key, "", "", "", ts, ts)).lastrowid
        self.tab_ids[key] = tid
        return tid

    def _tab_meta(self, c, rec):
        tid = self._tab_id(c, rec["k"], rec["ts"])
        c.execute(
            "UPDATE tab SET title=?, url=?, site=?, private=?, pinned=?, window=? WHERE id=?",
            (rec.get("title", ""), rec.get("url", ""), rec.get("site", ""), rec.get("priv", 0), rec.get("pin", 0), rec.get("win"), tid),
        )

    def _ext_id(self, c, key, ts):
        eid = self.ext_ids.get(key)
        if eid is not None:
            return eid
        row = c.execute("SELECT id FROM ext WHERE key=?", (key,)).fetchone()
        if row:
            eid = row["id"]
        else:
            name = "Extension process (shared)" if key == "(extension process)" else key
            eid = c.execute("INSERT INTO ext(key, name, version, enabled, first_seen, last_seen) VALUES (?,?,?,?,?,?)", (key, name, "", 1, ts, ts)).lastrowid
        self.ext_ids[key] = eid
        return eid

    def _ext_meta(self, c, rec):
        eid = self._ext_id(c, rec["id"], rec["ts"])
        self.ext_names[rec["id"]] = rec.get("name")
        c.execute("UPDATE ext SET name=?, version=?, enabled=? WHERE id=?", (rec.get("name"), rec.get("version"), rec.get("enabled", 1), eid))

    def _sample(self, c, rec):
        ts = rec["ts"]
        main = rec.get("pid", 0)
        procs = rec.get("procs") or []
        tabs = rec.get("tabs") or []
        exts = rec.get("exts") or []
        lag = rec.get("lag") or {}
        mem_total = sum(p.get("mem", 0) for p in procs)
        cpu_total = sum(p["cpu"] for p in procs if p.get("cpu") is not None)
        discarded = sum(1 for t in tabs if t.get("f", 0) & 1)
        c.execute(
            "INSERT OR REPLACE INTO ff_sample VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, main, rec.get("win", 0), len(tabs), len(tabs) - discarded, discarded, mem_total, round(cpu_total, 4),
             lag.get("max"), lag.get("avg"), lag.get("slow"), lag.get("blocked"), rec.get("ghost"), rec.get("sd")),
        )
        c.executemany(
            "INSERT INTO ff_proc VALUES (?,?,?,?,?,?,?,?)",
            [(ts, main, p["pid"], p.get("type", ""), p.get("site", ""), p.get("mem", 0), p.get("cpu"), p.get("thr")) for p in procs],
        )
        tab_rows = []
        tab_updates = []
        for t in tabs:
            tid = self._tab_id(c, t["k"], ts)
            tab_rows.append((ts, tid, t.get("m", 0), t.get("c"), t.get("f", 0), ",".join(str(p) for p in t.get("p") or [])))
            tab_updates.append((ts, t.get("m", 0), tid))
        c.executemany("INSERT INTO ff_tab VALUES (?,?,?,?,?,?)", tab_rows)
        c.executemany("UPDATE tab SET last_seen=?, peak_mem=MAX(peak_mem, ?) WHERE id=?", tab_updates)
        ext_rows = []
        for e in exts:
            eid = self._ext_id(c, e["id"], ts)
            ext_rows.append((ts, eid, e.get("m", 0), e.get("c")))
            c.execute("UPDATE ext SET last_seen=? WHERE id=?", (ts, eid))
        c.executemany("INSERT INTO ff_ext VALUES (?,?,?,?)", ext_rows)

    def _userscripts(self, c, rec):
        ts = rec["ts"]
        c.execute("INSERT OR REPLACE INTO us_burst VALUES (?,?,?)", (ts, self.main_pid, rec.get("dur", 0)))
        rows = []
        for s in rec.get("scripts") or []:
            key = s.get("id") or s.get("file") or s.get("name")
            uid = self.us_ids.get(key)
            if uid is None:
                row = c.execute("SELECT id FROM userscript WHERE key=?", (key,)).fetchone()
                uid = row["id"] if row else c.execute(
                    "INSERT INTO userscript(key, name, manager, first_seen, last_seen) VALUES (?,?,?,?,?)",
                    (key, s.get("name"), s.get("ext"), ts, ts)).lastrowid
                self.us_ids[key] = uid
            c.execute("UPDATE userscript SET name=?, manager=?, last_seen=? WHERE id=?", (s.get("name"), s.get("ext"), ts, uid))
            rows.append((ts, uid, s.get("pid"), s.get("site", ""), s.get("ms", 0), s.get("cpu", 0)))
        c.executemany("INSERT INTO ff_us VALUES (?,?,?,?,?,?)", rows)

    def _tab_label(self, c, key):
        row = c.execute("SELECT title, site FROM tab WHERE key=?", (key,)).fetchone()
        if not row:
            return ""
        return row["title"] or row["site"] or ""

    def _event(self, c, rec):
        kind = rec.get("kind")
        ts = rec["ts"]
        add = self.store.add_event
        if kind == "content_crash":
            site = (rec.get("site") or "").split("://", 1)[-1] or rec.get("ptype") or "unknown"
            tabs = [self._tab_label(c, k) for k in rec.get("tabs", [])]
            title = f"Tab process crashed ({site}, {fmt_bytes(rec.get('mem'))})"
            add(c, ts, "content_crash", "bad", title, {**rec, "tab_titles": tabs}, dedupe=f"cc:{rec.get('cid')}:{ts}")
            if rec.get("dump"):
                c.execute(
                    "UPDATE event SET detail=json_set(detail, '$.content_crash_site', ?) WHERE dedupe=?",
                    (site, f"crash:{rec['dump']}"),
                )
        elif kind == "script_hang":
            who = rec.get("addonName") or rec.get("addon")
            tab = self._tab_label(c, rec.get("tab", "")) if rec.get("tab") else ""
            if rec.get("usName"):
                title = f"Slow userscript: {rec['usName']}" + (f" (in {tab})" if tab else "")
            elif who:
                title = f"Slow extension script: {who}" + (f" (in {tab})" if tab else "")
            else:
                title = "Slow page script" + (f" in {tab}" if tab else "")
            if rec.get("dur"):
                title += f", {fmt_dur(rec['dur'])}"
            add(c, ts, "script_hang", "warn", title, rec, dedupe=f"sh:{ts}:{rec.get('addon')}:{rec.get('script')}")
        elif kind == "memory_pressure":
            add(c, ts, "memory_pressure", "warn", f"Firefox got a low-memory signal ({rec.get('data')})", rec, dedupe=f"mp:{ts // 60000}:{rec.get('data')}")
        elif kind == "tab_unloaded":
            label = self._tab_label(c, rec.get("tab", "")) or rec.get("site") or "a tab"
            add(c, ts, "tab_unloaded", "info", f"Tab unloaded: {label}", rec, dedupe=f"tu:{rec.get('tab')}:{ts}")
        elif kind == "quit_requested":
            add(c, ts, "quit_requested", "info", "You quit Firefox", rec, dedupe=f"q:{rec.get('pid')}:{ts}")
        elif kind == "mem_report":
            site = (rec.get("site") or "").split("://", 1)[-1] or rec.get("ptype") or "a"
            why = "growing fast" if rec.get("reason") == "runaway" else "on request"
            sev = "warn" if rec.get("reason") == "runaway" else "info"
            add(c, ts, "mem_report", sev, f"Memory snapshot of {site} process at {fmt_bytes(rec.get('mem'))} ({why})", rec, dedupe=f"mr:{rec.get('pid')}:{ts}")
        elif kind == "collector_error":
            add(c, ts, "collector_error", "warn", f"Collector error: {rec.get('msg', '')[:120]}", rec, dedupe=f"ce:{ts}")
