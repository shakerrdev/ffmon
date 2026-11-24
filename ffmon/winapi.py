import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    _user32.IsHungAppWindow.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int

    class _PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", wintypes.DWORD),
            ("ProcessCount", wintypes.DWORD),
            ("ThreadCount", wintypes.DWORD),
        ]

    _psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_PERFORMANCE_INFORMATION), wintypes.DWORD]
    _psapi.GetPerformanceInfo.restype = wintypes.BOOL


def commit_charge():
    """Return (commit_total, commit_limit) in bytes, or None when unavailable."""
    if not IS_WINDOWS:
        return None
    info = _PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(info)
    if not _psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return None
    return info.CommitTotal * info.PageSize, info.CommitLimit * info.PageSize


def firefox_windows(pids):
    """Count visible Firefox top-level windows per pid and how many are hung.

    Returns {pid: {"windows": n, "hung": n}} or None when not supported.
    """
    if not IS_WINDOWS:
        return None
    pids = set(pids)
    out = {}
    buf = ctypes.create_unicode_buffer(64)

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        _user32.GetClassNameW(hwnd, buf, 64)
        if buf.value != "MozillaWindowClass" or _user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        entry = out.setdefault(pid.value, {"windows": 0, "hung": 0})
        entry["windows"] += 1
        if _user32.IsHungAppWindow(hwnd):
            entry["hung"] += 1
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    for pid in pids:
        out.setdefault(pid, {"windows": 0, "hung": 0})
    return out
