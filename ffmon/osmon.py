import json
import time

import psutil

from . import winapi

FIREFOX_NAMES = {"firefox.exe", "firefox", "firefox-bin", "firefox-esr"}
CHILD_KINDS = {"tab", "gpu", "rdd", "socket", "utility", "gmplugin", "vr", "forkserver", "remotesandboxbroker", "ipdlunittest", "inference"}

LINGER_GRACE_S = 15
NO_WINDOW_STARTUP_S = 120
PRESSURE_AVAIL = 0.08
PRESSURE_COMMIT = 0.92


def classify(cmdline):
    args = [a.lower() for a in cmdline or []]
    if "-contentproc" in args or "--contentproc" in args:
        last = args[-1] if args else ""
        return last if last in CHILD_KINDS else "child"
    if any(a.lstrip("-") == "backgroundtask" for a in args):
        return "backgroundtask"
    return "main"


def now_ms():
    return int(time.time() * 1000)


class OsMonitor:
    def __init__(self, store, interval=5):
        self.store = store
        self.interval = interval
        self.cores = psutil.cpu_count() or 1
        self.procs = {}
        self.kinds = {}
        self.mains = {}
        self.pressure = None
        psutil.cpu_percent(None)

    def _scan(self):
        found = {}
        for p in psutil.process_iter(["name", "ppid", "create_time"]):
            name = (p.info.get("name") or "").lower()
            if name not in FIREFOX_NAMES:
                continue
            key = (p.pid, p.info.get("create_time"))
            if key not in self.kinds:
                try:
                    self.kinds[key] = classify(p.cmdline())
                except (psutil.Error, OSError):
                    self.kinds[key] = "child"
            cached = self.procs.get(p.pid)
            if cached is None or cached[0] != key:
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    continue
                self.procs[p.pid] = (key, p)
            found[p.pid] = (key, self.procs[p.pid][1], p.info.get("ppid"))
        for pid in list(self.procs):
            if pid not in found:
                del self.procs[pid]
        live = {v[0] for v in found.values()}
        for key in list(self.kinds):
            if key not in live:
                del self.kinds[key]
        return found

    def sample(self):
        ts = now_ms()
        found = self._scan()
        mains = {pid for pid, (key, _, _) in found.items() if self.kinds.get(key) == "main"}

        rows = []
        family = {m: {"rss": 0, "private": 0, "cpu": 0.0, "procs": 0} for m in mains}
        for pid, (key, proc, ppid) in found.items():
            kind = self.kinds.get(key, "child")
            try:
                with proc.oneshot():
                    mi = proc.memory_info()
                    cpu = proc.cpu_percent(None) / 100.0 / self.cores
                    threads = proc.num_threads()
                    handles = proc.num_handles() if winapi.IS_WINDOWS else None
            except (psutil.Error, OSError):
                continue
            private = getattr(mi, "private", None)
            main_pid = pid if kind == "main" else (ppid if ppid in mains else (next(iter(mains)) if len(mains) == 1 else None))
            rows.append((ts, pid, main_pid, kind, mi.rss, private, round(cpu, 4), threads, handles))
            if main_pid in family:
                f = family[main_pid]
                f["rss"] += mi.rss
                f["private"] += private or mi.rss
                f["cpu"] += cpu
                f["procs"] += 1

        wins = winapi.firefox_windows(mains) if mains else ({} if winapi.IS_WINDOWS else None)
        vm = psutil.virtual_memory()
        commit = winapi.commit_charge()
        if commit is None:
            sw = psutil.swap_memory()
            commit = (vm.total - vm.available + sw.used, vm.total + sw.total)
        ff_rss = sum(r[4] for r in rows)
        ff_private = sum((r[5] or r[4]) for r in rows)
        ff_cpu = sum(r[6] for r in rows)
        n_windows = sum(w["windows"] for w in wins.values()) if wins is not None else None
        n_hung = sum(w["hung"] for w in wins.values()) if wins is not None else None
        sys_cpu = psutil.cpu_percent(None) / 100.0

        def write(c):
            c.execute(
                "INSERT OR REPLACE INTO os_sample VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, vm.total, vm.available, commit[0], commit[1], ff_rss, ff_private, round(ff_cpu, 4),
                 len(rows), n_windows, n_hung, len(mains), round(sys_cpu, 4)),
            )
            c.executemany("INSERT INTO os_proc VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._track_mains(c, ts, found, mains, family, wins)
            self._track_pressure(c, ts, vm, commit, ff_private)

        self.store.write(write)

    def _track_mains(self, c, ts, found, mains, family, wins):
        quit_by_pid = {}
        if mains:
            q = ",".join("?" * len(mains))
            for row in c.execute(
                f"SELECT CAST(json_extract(detail,'$.pid') AS INTEGER) pid, MAX(ts) ts FROM event "
                f"WHERE kind='quit_requested' AND CAST(json_extract(detail,'$.pid') AS INTEGER) IN ({q}) GROUP BY pid",
                list(mains),
            ):
                quit_by_pid[row["pid"]] = row["ts"]

        for pid in mains:
            key, proc, _ = found[pid]
            st = self.mains.get(pid)
            if st is None or st["key"] != key:
                created = int((key[1] or time.time()) * 1000)
                st = self.mains[pid] = {
                    "key": key, "created": created, "had_windows": False, "last_windows": None,
                    "linger": None, "hang": None, "peak": 0, "peak_ts": ts,
                }
                already = ts - created > 60000
                self.store.add_event(
                    c, created, "ff_started", "info",
                    "Firefox started" + (" (was already running)" if already else ""),
                    {"pid": pid}, dedupe=f"start:{pid}:{created}",
                )
            fam = family[pid]
            if fam["private"] > st["peak"]:
                st["peak"], st["peak_ts"] = fam["private"], ts
            w = wins.get(pid) if wins is not None else None
            self._linger_step(c, ts, pid, st, fam, w, quit_by_pid.get(pid))
            self._hang_step(c, ts, pid, st, fam, w)

        for pid in list(self.mains):
            if pid in mains:
                continue
            st = self.mains.pop(pid)
            if st["linger"]:
                self._finish_linger(c, ts, pid, st)
            if st["hang"]:
                self._finish_hang(c, ts, pid, st)
            uptime = ts - st["created"]
            self.store.add_event(
                c, ts, "ff_exited", "info", f"Firefox exited after {fmt_dur(uptime)}",
                {"pid": pid, "uptime_ms": uptime, "peak_mem": st["peak"], "peak_ts": st["peak_ts"]},
                dedupe=f"exit:{pid}:{st['created']}",
            )

    def _linger_step(self, c, ts, pid, st, fam, w, quit_ts):
        windows = w["windows"] if w is not None else None
        if windows:
            st["had_windows"] = True
            st["last_windows"] = ts
            if st["linger"]:
                self._finish_linger(c, ts, pid, st, recovered=True)
            return

        start = reason = None
        if windows == 0:
            if st["had_windows"] and ts - st["last_windows"] >= LINGER_GRACE_S * 1000:
                start, reason = st["last_windows"], "windows_closed"
            elif not st["had_windows"] and ts - st["created"] >= NO_WINDOW_STARTUP_S * 1000:
                start, reason = max(st["created"], ts - NO_WINDOW_STARTUP_S * 1000), "no_windows"
        if quit_ts and quit_ts >= st["created"] and ts - quit_ts >= LINGER_GRACE_S * 1000:
            if start is None or quit_ts < start:
                start, reason = quit_ts, "quit_requested"

        if start is None:
            return
        lg = st["linger"]
        if lg is None:
            lg = st["linger"] = {
                "start": start, "reason": reason, "mem_start": fam["private"], "peak": fam["private"],
                "cpu_sum": 0.0, "n": 0, "procs": fam["procs"],
            }
        lg["peak"] = max(lg["peak"], fam["private"])
        lg["cpu_sum"] += fam["cpu"]
        lg["n"] += 1
        lg["procs"] = fam["procs"]
        self._write_linger(c, ts, pid, st, ongoing=True)

    def _write_linger(self, c, ts, pid, st, ongoing, recovered=False):
        lg = st["linger"]
        dur = ts - lg["start"]
        detail = {
            "pid": pid, "reason": lg["reason"], "duration_ms": dur, "ongoing": ongoing,
            "recovered": recovered, "mem_at_start": lg["mem_start"], "peak_mem": lg["peak"],
            "avg_cpu": round(lg["cpu_sum"] / lg["n"], 4) if lg["n"] else 0, "procs": lg["procs"],
        }
        why = {"windows_closed": "after its windows closed", "no_windows": "without any window",
               "quit_requested": "after you quit it"}[lg["reason"]]
        state = "still running" if ongoing else ("came back" if recovered else "kept running")
        title = f"Firefox {state} {why} ({fmt_dur(dur)}, {fmt_bytes(lg['peak'])} peak)"
        dedupe = f"linger:{pid}:{lg['start']}"
        c.execute(
            "INSERT INTO event(ts, end_ts, kind, severity, title, detail, dedupe) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(dedupe) DO UPDATE SET end_ts=excluded.end_ts, title=excluded.title, detail=excluded.detail",
            (lg["start"], ts, "linger", "bad", title, json.dumps(detail), dedupe),
        )

    def _finish_linger(self, c, ts, pid, st, recovered=False):
        self._write_linger(c, ts, pid, st, ongoing=False, recovered=recovered)
        st["linger"] = None

    def _hang_step(self, c, ts, pid, st, fam, w):
        hung = w["hung"] if w is not None else 0
        hg = st["hang"]
        if hung:
            if hg is None:
                hg = st["hang"] = {"start": ts - 5000, "peak": 0, "cpu_sum": 0.0, "n": 0}
            hg["peak"] = max(hg["peak"], fam["private"])
            hg["cpu_sum"] += fam["cpu"]
            hg["n"] += 1
            self._write_hang(c, ts, pid, st, ongoing=True)
        elif hg:
            self._finish_hang(c, ts, pid, st)

    def _write_hang(self, c, ts, pid, st, ongoing):
        hg = st["hang"]
        dur = ts - hg["start"]
        detail = {"pid": pid, "duration_ms": dur, "ongoing": ongoing, "peak_mem": hg["peak"],
                  "avg_cpu": round(hg["cpu_sum"] / max(1, hg["n"]), 4)}
        title = f"Firefox window {'is not responding' if ongoing else 'was not responding'} ({fmt_dur(dur)})"
        c.execute(
            "INSERT INTO event(ts, end_ts, kind, severity, title, detail, dedupe) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(dedupe) DO UPDATE SET end_ts=excluded.end_ts, title=excluded.title, detail=excluded.detail",
            (hg["start"], ts, "ui_hang", "bad", title, json.dumps(detail), f"hang:{pid}:{hg['start']}"),
        )

    def _finish_hang(self, c, ts, pid, st):
        self._write_hang(c, ts, pid, st, ongoing=False)
        st["hang"] = None

    def _track_pressure(self, c, ts, vm, commit, ff_private):
        avail = vm.available / vm.total if vm.total else 1
        commit_ratio = commit[0] / commit[1] if commit[1] else 0
        under = avail < PRESSURE_AVAIL or commit_ratio > PRESSURE_COMMIT
        pr = self.pressure
        if under:
            if pr is None:
                pr = self.pressure = {"start": ts, "n": 0, "min_avail": vm.available, "max_commit": commit_ratio, "ff_peak": 0}
            pr["n"] += 1
            pr["min_avail"] = min(pr["min_avail"], vm.available)
            pr["max_commit"] = max(pr["max_commit"], commit_ratio)
            pr["ff_peak"] = max(pr["ff_peak"], ff_private)
            if pr["n"] >= 2:
                self._write_pressure(c, ts, ongoing=True, total=vm.total)
        elif pr:
            if pr["n"] >= 2:
                self._write_pressure(c, ts, ongoing=False, total=vm.total)
            self.pressure = None

    def _write_pressure(self, c, ts, ongoing, total):
        pr = self.pressure
        dur = ts - pr["start"]
        detail = {"duration_ms": dur, "ongoing": ongoing, "min_avail": pr["min_avail"], "max_commit_ratio": round(pr["max_commit"], 3),
                  "firefox_peak": pr["ff_peak"], "firefox_share": round(pr["ff_peak"] / total, 3) if total else None}
        title = f"System low on memory ({fmt_dur(dur)}, Firefox held {fmt_bytes(pr['ff_peak'])})"
        c.execute(
            "INSERT INTO event(ts, end_ts, kind, severity, title, detail, dedupe) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(dedupe) DO UPDATE SET end_ts=excluded.end_ts, title=excluded.title, detail=excluded.detail",
            (pr["start"], ts, "system_pressure", "warn", title, json.dumps(detail), f"pressure:{pr['start']}"),
        )


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_dur(ms):
    s = int((ms or 0) / 1000)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"
