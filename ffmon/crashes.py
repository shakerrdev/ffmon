import configparser
import json
import os
import sys
from pathlib import Path

from .osmon import fmt_bytes

INTERESTING = [
    "ProcessType", "RemoteType", "MozCrashReason", "OOMAllocationSize", "TotalPhysicalMemory",
    "AvailablePhysicalMemory", "TotalPageFile", "AvailablePageFile", "TotalVirtualMemory",
    "AvailableVirtualMemory", "SystemMemoryUsePercentage", "LowCommitSpaceEvents",
    "LowPhysicalMemoryEvents", "Version", "BuildID", "CrashTime", "UptimeTS", "ipc_channel_error",
    "GraphicsCriticalError", "Hang", "ShutdownProgress", "AsyncShutdownTimeout", "ExperimentalHeapState",
]


def firefox_root():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


def profile_dirs():
    root = firefox_root()
    ini = root / "profiles.ini"
    out = []
    if not ini.exists():
        return out
    cp = configparser.ConfigParser()
    try:
        cp.read(ini, encoding="utf-8")
    except configparser.Error:
        return out
    for section in cp.sections():
        if not section.startswith("Profile"):
            continue
        path = cp.get(section, "Path", fallback=None)
        if not path:
            continue
        rel = cp.get(section, "IsRelative", fallback="1") == "1"
        p = (root / path) if rel else Path(path)
        if p.exists():
            out.append(p)
    return out


def crash_title(meta, ptype=""):
    ptype = (meta.get("ProcessType") or ptype or "main").lower()
    who = {
        "main": "Firefox crashed", "default": "Firefox crashed", "browser": "Firefox crashed",
        "content": "A tab process crashed", "tab": "A tab process crashed", "gpu": "The GPU process crashed",
        "rdd": "The media decoder crashed", "utility": "A utility process crashed", "socket": "The network process crashed",
        "gmplugin": "A media plugin crashed",
    }.get(ptype, f"A {ptype} process crashed")
    oom = meta.get("OOMAllocationSize")
    reason = meta.get("MozCrashReason") or ""
    bits = []
    if oom:
        bits.append(f"out of memory while allocating {fmt_bytes(_num(oom))}")
    elif reason:
        bits.append(reason[:120])
    if meta.get("Hang") or meta.get("AsyncShutdownTimeout"):
        bits.append("shutdown hang" if meta.get("AsyncShutdownTimeout") else "hang")
    avail_pf = _num(meta.get("AvailablePageFile"))
    total_pf = _num(meta.get("TotalPageFile"))
    if total_pf and avail_pf is not None and avail_pf / total_pf < 0.05:
        bits.append(f"only {fmt_bytes(avail_pf)} commit space left")
    avail_phys = _num(meta.get("AvailablePhysicalMemory"))
    total_phys = _num(meta.get("TotalPhysicalMemory"))
    if total_phys and avail_phys is not None and avail_phys / total_phys < 0.05:
        bits.append(f"only {fmt_bytes(avail_phys)} RAM free")
    return who + (": " + ", ".join(bits) if bits else "")


def is_oom(meta):
    if meta.get("OOMAllocationSize"):
        return True
    avail_pf = _num(meta.get("AvailablePageFile"))
    total_pf = _num(meta.get("TotalPageFile"))
    return bool(total_pf and avail_pf is not None and avail_pf / total_pf < 0.05)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def record_crash(store, c, crash_id, ts_ms, meta, ptype="", remote="", source=""):
    meta = {k: meta[k] for k in INTERESTING if k in meta}
    detail = {"id": crash_id, "process": (meta.get("ProcessType") or ptype or "main"), "remote_id": remote,
              "oom": is_oom(meta), "source": source, "meta": meta}
    title = crash_title(meta, ptype)
    exists = c.execute("SELECT id, detail FROM event WHERE dedupe=?", (f"crash:{crash_id}",)).fetchone()
    if exists:
        old = json.loads(exists["detail"] or "{}")
        if not old.get("remote_id") and remote:
            old["remote_id"] = remote
            c.execute("UPDATE event SET detail=? WHERE id=?", (json.dumps(old), exists["id"]))
        if len(old.get("meta", {})) < len(meta):
            c.execute("UPDATE event SET detail=?, title=? WHERE id=?", (json.dumps(detail), title, exists["id"]))
        return
    store.add_event(c, ts_ms, "crash", "bad", title, detail, dedupe=f"crash:{crash_id}")


class CrashScanner:
    def __init__(self, store):
        self.store = store
        self.seen = set()

    def scan(self):
        root = firefox_root() / "Crash Reports"
        candidates = list((root / "pending").glob("*.extra")) if (root / "pending").exists() else []
        for prof in profile_dirs():
            md = prof / "minidumps"
            if md.exists():
                candidates.extend(md.glob("*.extra"))
        found = []
        for path in candidates:
            key = (str(path), path.stat().st_mtime if path.exists() else 0)
            if key in self.seen:
                continue
            self.seen.add(key)
            try:
                meta = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            ts = _num(meta.get("CrashTime"))
            ts_ms = int(ts * 1000) if ts else int(path.stat().st_mtime * 1000)
            found.append((path.stem, ts_ms, {k: str(v) for k, v in meta.items()}, str(path.parent.name)))
        events_dir = root / "events"
        if events_dir.exists():
            for path in events_dir.iterdir():
                key = (str(path), path.stat().st_mtime)
                if key in self.seen or not path.is_file():
                    continue
                self.seen.add(key)
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
                except OSError:
                    continue
                if len(lines) >= 4 and lines[0] == "crash.main.3":
                    try:
                        meta = json.loads(lines[3])
                    except ValueError:
                        meta = {}
                    found.append((lines[2].strip(), int(_num(lines[1]) * 1000), {k: str(v) for k, v in meta.items()}, "events"))
        if found:
            def write(c):
                for crash_id, ts_ms, meta, src in found:
                    record_crash(self.store, c, crash_id, ts_ms, meta, source=src)
            self.store.write(write)
