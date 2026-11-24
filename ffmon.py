#!/usr/bin/env python3
import argparse
import json
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ffmon import install as inst  # noqa: E402
from ffmon.db import Store  # noqa: E402

log = logging.getLogger("ffmon")


def loop(name, interval, fn, stop):
    while not stop.is_set():
        t0 = time.time()
        try:
            fn()
        except Exception:
            log.exception("%s failed", name)
        stop.wait(max(0.5, interval - (time.time() - t0)))


def cmd_run(args):
    from ffmon.api import Api
    from ffmon.crashes import CrashScanner
    from ffmon.ingest import Ingestor
    from ffmon.osmon import OsMonitor
    from ffmon.server import make_server

    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(data / "ffmon.log", encoding="utf-8")] + ([logging.StreamHandler()] if sys.stdout else []),
    )
    store = Store(data / "ffmon.db")
    api = Api(store, interval_os=args.interval)
    try:
        server = make_server(api, ROOT / "web", "127.0.0.1", args.port, lambda: {"data_dir": str(data), "install": _read_install(data)})
    except OSError:
        msg = f"Port {args.port} is busy - the monitor is probably already running at http://127.0.0.1:{args.port}"
        log.error(msg)
        print(msg)
        if args.open:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return 1

    osmon = OsMonitor(store, interval=args.interval)
    ingest = Ingestor(store, data / "inbox")
    crashes = CrashScanner(store)
    stop = threading.Event()
    jobs = [
        ("os sampler", args.interval, osmon.sample),
        ("ingest", 5, ingest.run_once),
        ("crash scan", 60, crashes.scan),
        ("prune", 3600, lambda: store.prune(args.keep_days)),
    ]
    for name, interval, fn in jobs:
        threading.Thread(target=loop, args=(name, interval, fn, stop), daemon=True, name=name).start()

    url = f"http://127.0.0.1:{args.port}"
    log.info("Firefox Monitor running - dashboard at %s, data in %s", url, data)
    print(f"Firefox Monitor running. Dashboard: {url}  (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def _read_install(data):
    p = Path(data) / "install.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pick_firefox_dir(args):
    if args.firefox_dir:
        return Path(args.firefox_dir)
    dirs = inst.find_firefox_dirs()
    if not dirs:
        raise inst.InstallError("Couldn't find Firefox. Pass its folder with --firefox-dir \"C:\\Program Files\\Mozilla Firefox\"")
    if len(dirs) > 1:
        print("Found several Firefox installs, using the first (pass --firefox-dir to choose):")
        for d in dirs:
            print("  ", d)
    return dirs[0]


def cmd_install(args):
    data = Path(args.data)
    try:
        ff_dir = _pick_firefox_dir(args)
        target, notes = inst.install(ff_dir, data)
    except inst.InstallError as e:
        print(f"Install failed: {e}")
        return 1
    info = {"firefox_dir": str(ff_dir), "installed_at": int(time.time() * 1000), **target}
    (data / "install.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"Collector hooked into Firefox at {ff_dir}")
    for n in notes:
        print("  " + n)
    if not args.no_autostart:
        try:
            print(inst.set_autostart(True, args.port, data))
        except OSError as e:
            print(f"Couldn't set autostart ({e}); run `python ffmon.py autostart on` from a normal terminal.")
    print("Restart Firefox completely (close all windows) so the collector loads, then run `python ffmon.py run --open`.")
    return 0


def cmd_uninstall(args):
    try:
        ff_dir = _pick_firefox_dir(args)
        removed = inst.uninstall(ff_dir)
    except inst.InstallError as e:
        print(f"Uninstall failed: {e}")
        return 1
    for r in removed:
        print("Removed", r)
    if not removed:
        print("Nothing to remove in", ff_dir)
    try:
        print(inst.set_autostart(False))
    except OSError:
        pass
    (Path(args.data) / "install.json").unlink(missing_ok=True)
    print("Restart Firefox to unload the collector. Your recorded data stays in", args.data)
    return 0


def cmd_autostart(args):
    print(inst.set_autostart(args.state == "on", args.port, Path(args.data)))
    return 0


def cmd_status(args):
    from ffmon.api import Api
    from ffmon.osmon import fmt_bytes

    store = Store(Path(args.data) / "ffmon.db")
    s = Api(store).status({})
    now = s["now"]

    def ago(ts):
        return "never" if not ts else f"{(now - ts) / 1000:.0f}s ago"

    print(f"Database:          {store.path} ({fmt_bytes(s['db_bytes'])})")
    print(f"Last OS sample:    {ago(s['last_os'])}")
    print(f"Last Firefox data: {ago(s['last_ff'])}")
    col = s["meta"].get("collector")
    if col:
        print(f"Collector:         v{col.get('v')} in Firefox {col.get('ff')} ({col.get('profile')})")
    install = _read_install(args.data)
    print(f"Installed into:    {install['firefox_dir'] if install else 'not installed'}")
    return 0


def main():
    p = argparse.ArgumentParser(description="Track Firefox memory, CPU, tabs, extensions, crashes and hangs over time.")
    p.add_argument("--data", default=str(ROOT / "data"), help="where recordings are stored (default: ./data)")
    p.add_argument("--port", type=int, default=8765)
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="run the watchdog and dashboard (default)")
    r.add_argument("--interval", type=int, default=5, help="OS sampling interval in seconds")
    r.add_argument("--keep-days", type=int, default=14, help="days of detailed samples to keep")
    r.add_argument("--open", action="store_true", help="open the dashboard in your browser")

    i = sub.add_parser("install", help="hook the collector into Firefox (needs an administrator terminal)")
    i.add_argument("--firefox-dir")
    i.add_argument("--no-autostart", action="store_true")
    u = sub.add_parser("uninstall", help="remove the collector from Firefox and disable autostart")
    u.add_argument("--firefox-dir")
    a = sub.add_parser("autostart", help="start the watchdog when you log in (Windows)")
    a.add_argument("state", choices=["on", "off"])
    sub.add_parser("status", help="show whether data is coming in")

    for sp in sub.choices.values():
        sp.add_argument("--data", default=argparse.SUPPRESS)
        sp.add_argument("--port", type=int, default=argparse.SUPPRESS)

    args = p.parse_args()
    if args.cmd in (None, "run"):
        for k, v in (("interval", 5), ("keep_days", 14), ("open", False)):
            if not hasattr(args, k):
                setattr(args, k, v)
        return cmd_run(args)
    return {"install": cmd_install, "uninstall": cmd_uninstall, "autostart": cmd_autostart, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
