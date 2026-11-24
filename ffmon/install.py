import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_DIR = ROOT / "firefox"
PREF_FILE = "ffmon-autoconfig.js"
CFG_FILE = "ffmon.cfg"
BEGIN, END = "// ffmon-begin", "// ffmon-end"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "FirefoxMonitor"


class InstallError(Exception):
    pass


def find_firefox_dirs():
    found = []
    if sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe") as k:
                    exe = winreg.QueryValueEx(k, None)[0]
                    found.append(Path(exe.strip('"')).parent)
            except OSError:
                pass
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                found.append(Path(base) / "Mozilla Firefox")
    elif sys.platform == "darwin":
        found.append(Path("/Applications/Firefox.app/Contents/Resources"))
    else:
        found += [Path("/usr/lib/firefox"), Path("/usr/lib64/firefox"), Path("/opt/firefox")]
    out = []
    for d in found:
        exe = d / ("firefox.exe" if sys.platform == "win32" else "firefox")
        if (exe.exists() or (d / "omni.ja").exists()) and d.resolve() not in [o.resolve() for o in out]:
            out.append(d)
    return out


def _cfg_block(data_dir: Path):
    template = (COLLECTOR_DIR / "ffmon.cfg.template").read_text(encoding="utf-8")
    block = template[template.index(BEGIN): template.index(END) + len(END)]
    js = lambda p: json.dumps(str(p))[1:-1]
    return block.replace("@@COLLECTOR_DIR@@", js(COLLECTOR_DIR)).replace("@@DATA_DIR@@", js(data_dir))


def _existing_autoconfig(ff_dir: Path):
    pref_dir = ff_dir / "defaults" / "pref"
    if not pref_dir.exists():
        return None
    for f in pref_dir.glob("*.js"):
        if f.name == PREF_FILE:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'pref\(\s*["\']general\.config\.filename["\']\s*,\s*["\']([^"\']+)["\']', text)
        if m:
            sandbox_off = re.search(r'general\.config\.sandbox_enabled["\']\s*,\s*false', text) is not None
            obscure_zero = re.search(r'general\.config\.obscure_value["\']\s*,\s*0\b', text) is not None
            return f, m.group(1), sandbox_off, obscure_zero
    return None


def _write(path: Path, text: str):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    except PermissionError as e:
        raise InstallError(
            f"No permission to write {path}.\n"
            "Run this command from a terminal opened with 'Run as administrator'."
            + ("\nIf Firefox came from the Microsoft Store, its folder can't be changed; use the regular installer from mozilla.org." if "WindowsApps" in str(path) else "")
        ) from e


def install(ff_dir: Path, data_dir: Path):
    ff_dir = Path(ff_dir)
    inbox = Path(data_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    block = _cfg_block(inbox)
    notes = []
    existing = _existing_autoconfig(ff_dir)
    if existing:
        pref, cfg_name, sandbox_off, obscure_zero = existing
        cfg = ff_dir / cfg_name
        text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.exists() else "// autoconfig\n"
        text = _strip_block(text).rstrip("\n") + "\n" + block + "\n"
        _write(cfg, text)
        notes.append(f"Firefox already had an autoconfig ({pref.name} → {cfg_name}); added the monitor block to it.")
        if not sandbox_off or not obscure_zero:
            notes.append(f"Warning: {pref.name} needs general.config.sandbox_enabled=false and general.config.obscure_value=0 for the collector to run.")
        target = {"mode": "merged", "cfg": str(cfg)}
    else:
        _write(ff_dir / "defaults" / "pref" / PREF_FILE,
               '// Firefox Monitor\npref("general.config.filename", "ffmon.cfg");\n'
               'pref("general.config.obscure_value", 0);\npref("general.config.sandbox_enabled", false);\n')
        template = (COLLECTOR_DIR / "ffmon.cfg.template").read_text(encoding="utf-8")
        header = template[: template.index(BEGIN)]
        _write(ff_dir / CFG_FILE, header + block + "\n")
        target = {"mode": "own", "cfg": str(ff_dir / CFG_FILE), "pref": str(ff_dir / "defaults" / "pref" / PREF_FILE)}
    return target, notes


def _strip_block(text):
    if BEGIN not in text:
        return text
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END) if END in text[start:] else len(text)
    return text[:start] + text[end:].lstrip("\n")


def uninstall(ff_dir: Path):
    ff_dir = Path(ff_dir)
    removed = []
    pref = ff_dir / "defaults" / "pref" / PREF_FILE
    own_cfg = ff_dir / CFG_FILE
    try:
        if pref.exists():
            pref.unlink()
            removed.append(str(pref))
        if own_cfg.exists():
            own_cfg.unlink()
            removed.append(str(own_cfg))
        existing = _existing_autoconfig(ff_dir)
        if existing:
            cfg = ff_dir / existing[1]
            if cfg.exists():
                text = cfg.read_text(encoding="utf-8", errors="replace")
                if BEGIN in text:
                    _write(cfg, _strip_block(text))
                    removed.append(f"monitor block in {cfg}")
    except PermissionError as e:
        raise InstallError("No permission to change the Firefox folder. Run this from an administrator terminal.") from e
    return removed


def autostart_command(port, data_dir):
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    return f'"{exe}" "{ROOT / "ffmon.py"}" run --port {port} --data "{data_dir}"'


def set_autostart(enabled, port=8765, data_dir=None):
    if sys.platform != "win32":
        return "Autostart is only automated on Windows; add `python ffmon.py run` to your session startup."
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command(port, data_dir))
            return "Watchdog will start automatically when you log in."
        try:
            winreg.DeleteValue(k, RUN_NAME)
        except FileNotFoundError:
            pass
        return "Autostart removed."
