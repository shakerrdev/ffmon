"""Generate a realistic fake recording to develop and test the dashboard.

Writes collector-format JSONL into <data>/inbox and runs it through the real
Ingestor; OS-level samples and watchdog events are inserted directly.
"""
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffmon.crashes import record_crash  # noqa: E402
from ffmon.db import Store  # noqa: E402
from ffmon.ingest import Ingestor  # noqa: E402

MB = 1024 * 1024
GB = 1024 * MB


def main(data_dir, hours=8, end_ms=None):
    import time

    random.seed(7)
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    (data / "ffmon.db").unlink(missing_ok=True)
    inbox = data / "inbox"
    inbox.mkdir(exist_ok=True)
    for f in inbox.glob("*.jsonl"):
        f.unlink()
    store = Store(data / "ffmon.db")

    end = end_ms or int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    crash_at = start + int(hours * 0.62 * 3600 * 1000)
    restart_at = crash_at + 4 * 60 * 1000
    close_at = end - 50 * 60 * 1000
    linger_until = close_at + 9 * 60 * 1000
    reopen_at = linger_until + 60 * 1000

    sites = [
        ("youtube.com", "YouTube - Lofi beats to code to", "https://www.youtube.com/watch?v=x", 420),
        ("reddit.com", "r/GlobalOffensive", "https://www.reddit.com/r/GlobalOffensive/", 260),
        ("github.com", "shakerrdev/comic-viewer", "https://github.com/shakerrdev/comic-viewer", 140),
        ("leetify.com", "Leetify - Match details", "https://leetify.com/app/match-details/1", 300),
        ("twitch.tv", "Twitch", "https://www.twitch.tv/", 520),
        ("google.com", "Google Search", "https://www.google.com/search?q=cs2", 90),
        ("mozilla.org", "Firefox Support", "https://support.mozilla.org/", 70),
        ("wikipedia.org", "Memory leak - Wikipedia", "https://en.wikipedia.org/wiki/Memory_leak", 60),
        ("discord.com", "Discord", "https://discord.com/channels/@me", 380),
        ("x.com", "Home / X", "https://x.com/home", 330),
    ]
    exts = [("uBlock0@raymondhill.net", "uBlock Origin", 95), ("fox-utils@shakerrdev", "Firefox Utils", 60),
            ("{dark-reader}", "Dark Reader", 130), ("{tampermonkey}", "Tampermonkey", 85)]

    def session(s0, s1, pid, sid):
        lines = []
        def push(r):
            lines.append(json.dumps(r))
        push({"t": "start", "ts": s0, "v": 3, "session": sid, "pid": pid, "ff": "146.0", "build": "20261117", "profile": "C:\\Users\\you\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles\\abcd.default-release", "cores": 12, "memsize": 16 * GB, "interval": 10})
        for eid, name, _ in exts:
            push({"t": "ext", "ts": s0, "id": eid, "name": name, "version": "1.2.3", "enabled": 1})
        tabs = []
        for i, (site, title, url, base) in enumerate(sites):
            key = f"{sid}.{i + 1}"
            tabs.append({"key": key, "site": site, "base": base * MB, "pid": pid + 10 + i, "discard_at": None})
            push({"t": "tab", "ts": s0, "k": key, "title": title, "url": url, "site": site, "priv": 0, "pin": 1 if i == 2 else 0, "win": 1})
        next_tab = len(tabs)
        t = s0
        while t < s1:
            age_h = (t - s0) / 3600000
            if random.random() < 0.004 and next_tab < 40:
                next_tab += 1
                site, title, url, base = random.choice(sites)
                key = f"{sid}.{next_tab}"
                tabs.append({"key": key, "site": site, "base": base * MB * 0.6, "pid": pid + 10 + next_tab, "discard_at": None})
                push({"t": "tab", "ts": t, "k": key, "title": title + " (2)", "url": url + "?2", "site": site, "priv": 0, "pin": 0, "win": 1})
            procs = []
            tab_recs = []
            web_total = 0
            for i, tb in enumerate(tabs):
                discarded = tb["discard_at"] is not None and t >= tb["discard_at"]
                if not discarded and age_h > 2.5 and tb["site"] in ("wikipedia.org", "mozilla.org") and random.random() < 0.0008:
                    tb["discard_at"] = t
                    push({"t": "ev", "ts": t, "kind": "tab_unloaded", "tab": tb["key"], "site": tb["site"]})
                    discarded = True
                if discarded:
                    tab_recs.append({"k": tb["key"], "m": 0, "c": None, "f": 1, "p": []})
                    continue
                mem = tb["base"] * (1 + 0.08 * math.sin(t / 600000 + i)) + random.uniform(-8, 8) * MB
                if tb["site"] == "youtube.com" and tb["key"].endswith(".1"):
                    mem += age_h * 380 * MB
                if tb["site"] == "twitch.tv":
                    mem += age_h * 60 * MB
                cpu = max(0, (0.02 if tb["site"] in ("youtube.com", "twitch.tv") else 0.002) + random.gauss(0, 0.003))
                if tb["site"] == "discord.com" and random.random() < 0.03:
                    cpu += 0.08
                flags = (2 if i == 0 else 0) | (4 if tb["site"] == "youtube.com" else 0) | (8 if i == 2 else 0)
                tab_recs.append({"k": tb["key"], "m": int(mem), "c": round(cpu, 4), "f": flags, "p": [tb["pid"]]})
                procs.append({"pid": tb["pid"], "cid": i + 2, "type": "webIsolated", "site": "https://" + tb["site"], "mem": int(mem * 1.05), "cpu": round(cpu, 4), "thr": 30, "win": 1})
                web_total += mem
            ext_mem = sum(e[2] for e in exts) * MB + age_h * 25 * MB
            ext_cpu = 0.004 + random.random() * 0.003 + (0.03 if random.random() < 0.02 else 0)
            procs.append({"pid": pid + 1, "cid": 1, "type": "extension", "site": "", "mem": int(ext_mem), "cpu": round(ext_cpu, 4), "thr": 40, "win": 6})
            procs.append({"pid": pid, "cid": 0, "type": "browser", "site": "", "mem": int(650 * MB + age_h * 40 * MB + random.uniform(0, 30) * MB), "cpu": round(0.01 + random.random() * 0.01, 4), "thr": 110, "win": 0})
            procs.append({"pid": pid + 2, "cid": 50, "type": "gpu", "site": "", "mem": int(420 * MB + random.uniform(0, 60) * MB), "cpu": round(0.012 + random.random() * 0.01, 4), "thr": 25, "win": 0})
            procs.append({"pid": pid + 3, "cid": 51, "type": "rdd", "site": "", "mem": int(90 * MB), "cpu": 0.004, "thr": 12, "win": 0})
            procs.append({"pid": pid + 4, "cid": 52, "type": "socket", "site": "", "mem": int(40 * MB), "cpu": 0.001, "thr": 10, "win": 0})
            ext_recs = []
            for eid, name, base in exts:
                share = base / sum(e[2] for e in exts)
                ext_recs.append({"id": eid, "m": int(ext_mem * share), "c": round(ext_cpu * share * (3 if eid == "{dark-reader}" else 1), 4)})
            lagmax = int(abs(random.gauss(30, 40)))
            if web_total > 6 * GB and random.random() < 0.15:
                lagmax = random.randint(500, 3000)
            push({"t": "s", "ts": t, "pid": pid, "win": 1, "procs": procs, "tabs": tab_recs, "exts": ext_recs,
                  "lag": {"max": lagmax, "avg": round(lagmax / 8, 1), "slow": lagmax // 100, "blocked": lagmax if lagmax > 50 else 0},
                  "ghost": 0 if age_h < 3 else int(age_h - 2), "sd": random.randint(4, 15)})
            if random.random() < 0.0015:
                push({"t": "ev", "ts": t, "kind": "script_hang", "addon": "{dark-reader}", "addonName": "Dark Reader", "script": "moz-extension://abc/inject/index.js", "dur": 11000, "tab": tabs[1]["key"]})
            if random.random() < 0.0008:
                push({"t": "ev", "ts": t, "kind": "script_hang", "addon": "", "script": "https://www.reddit.com/app.js", "dur": 8000, "tab": tabs[1]["key"]})
            t += 10000
        return lines

    pid1, pid2, pid3 = 4120, 9004, 15320
    lines = session(start, crash_at, pid1, "s1")
    lines.append(json.dumps({"t": "ev", "ts": crash_at - 60000, "kind": "memory_pressure", "data": "low-memory"}))
    lines.append(json.dumps({"t": "ev", "ts": crash_at - 200000, "kind": "content_crash", "cid": 8, "pid": pid1 + 14, "ptype": "webIsolated", "site": "https://twitch.tv", "mem": int(1.9 * GB), "dump": "aaaa-content", "tabs": ["s1.5"]}))
    lines += session(restart_at, close_at, pid2, "s2")
    lines.append(json.dumps({"t": "ev", "ts": close_at, "kind": "quit_requested", "pid": pid2}))
    lines.append(json.dumps({"t": "crash", "ts": restart_at + 60000, "id": "bbbb-main-oom", "date": crash_at, "type": "main-crash", "ptype": "main", "remote": "bp-1234", "meta": {"OOMAllocationSize": "67108864", "AvailablePageFile": "104857600", "TotalPageFile": "21474836480", "AvailablePhysicalMemory": "314572800", "TotalPhysicalMemory": "17179869184", "MozCrashReason": ""}}))
    lines += session(reopen_at, end, pid3, "s3")
    (inbox / "ff-sim-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ing = Ingestor(store, inbox)
    ing.run_once()

    def os_rows(c):
        ffm = {r["ts"]: r for r in c.execute("SELECT ts, mem_total, cpu_total FROM ff_sample")}
        keys = sorted(ffm)
        import bisect

        t = start
        while t <= end:
            i = bisect.bisect_right(keys, t) - 1
            ff = ffm[keys[i]] if i >= 0 and t - keys[i] < 15000 else None
            running = ff is not None or (close_at <= t < linger_until)
            if close_at <= t < linger_until:
                ffmem = int(3.8 * GB - (t - close_at) / (linger_until - close_at) * 1.2 * GB)
                ffcpu = 0.08
            elif ff:
                ffmem = int(ff["mem_total"] * 1.08)
                ffcpu = ff["cpu_total"] + 0.01
            else:
                ffmem, ffcpu = 0, 0
            other = 5.2 * GB + 0.6 * GB * math.sin(t / 3000000)
            total = 16 * GB
            avail = max(150 * MB, total - other - ffmem)
            commit_limit = 24 * GB
            commit = min(commit_limit * 0.995, other * 1.3 + ffmem * 1.15)
            wins = 0 if (close_at <= t < linger_until) else (1 if running else 0)
            hung = 1 if (crash_at - 9 * 60000 <= t < crash_at - 7 * 60000) else 0
            c.execute("INSERT OR REPLACE INTO os_sample VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (t, total, int(avail), int(commit), commit_limit, int(ffmem * 0.9), ffmem, round(ffcpu, 4), 18 if running else 0, wins, hung, 1 if running else 0, round(0.15 + ffcpu, 4)))
            t += 5000

        def ev(ts, end_ts, kind, sev, title, detail, dedupe):
            c.execute("INSERT OR IGNORE INTO event(ts, end_ts, kind, severity, title, detail, dedupe) VALUES (?,?,?,?,?,?,?)", (ts, end_ts, kind, sev, title, json.dumps(detail), dedupe))

        ev(start, None, "ff_started", "info", "Firefox started", {"pid": pid1}, "s1")
        ev(crash_at, None, "ff_exited", "info", "Firefox exited after 4h 57m", {"pid": pid1}, "e1")
        ev(restart_at, None, "ff_started", "info", "Firefox started", {"pid": pid2}, "s2")
        ev(close_at, linger_until, "linger", "bad", "Firefox kept running after its windows closed (9m, 3.8 GB peak)",
           {"pid": pid2, "reason": "windows_closed", "duration_ms": linger_until - close_at, "ongoing": False, "peak_mem": int(3.8 * GB), "mem_at_start": int(3.8 * GB), "avg_cpu": 0.08, "procs": 9}, "l1")
        ev(crash_at - 9 * 60000, crash_at - 7 * 60000, "ui_hang", "bad", "Firefox window was not responding (2m 5s)", {"pid": pid1, "duration_ms": 125000, "peak_mem": int(7.5 * GB)}, "h1")
        ev(crash_at - 12 * 60000, crash_at, "system_pressure", "warn", "System low on memory (12m, Firefox held 7.9 GB)", {"duration_ms": 720000, "firefox_peak": int(7.9 * GB), "firefox_share": 0.49, "min_avail": 150 * MB, "max_commit_ratio": 0.99}, "p1")
        ev(reopen_at, None, "ff_started", "info", "Firefox started", {"pid": pid3}, "s3")
        record_crash(store, c, "aaaa-content", crash_at - 200000, {"ProcessType": "content", "RemoteType": "webIsolated=https://twitch.tv", "MozCrashReason": "MOZ_CRASH(IPC message too large)"}, source="minidumps")

    store.write(os_rows)
    print("simulated", hours, "h into", data)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ffmon-sim", float(sys.argv[2]) if len(sys.argv) > 2 else 8)
