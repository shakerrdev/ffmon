import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS os_sample (
  ts INTEGER PRIMARY KEY,
  mem_total INTEGER, mem_avail INTEGER,
  commit_total INTEGER, commit_limit INTEGER,
  ff_rss INTEGER, ff_private INTEGER, ff_cpu REAL,
  ff_procs INTEGER, ff_windows INTEGER, ff_hung INTEGER, ff_mains INTEGER,
  sys_cpu REAL
);

CREATE TABLE IF NOT EXISTS os_proc (
  ts INTEGER, pid INTEGER, main_pid INTEGER, kind TEXT,
  rss INTEGER, private INTEGER, cpu REAL, threads INTEGER, handles INTEGER
);
CREATE INDEX IF NOT EXISTS os_proc_ts ON os_proc(ts);

CREATE TABLE IF NOT EXISTS ff_sample (
  ts INTEGER, main_pid INTEGER, windows INTEGER,
  tabs INTEGER, tabs_loaded INTEGER, tabs_discarded INTEGER,
  mem_total INTEGER, cpu_total REAL,
  lag_max INTEGER, lag_avg REAL, lag_slow INTEGER, lag_blocked INTEGER,
  ghost INTEGER, sample_ms INTEGER,
  PRIMARY KEY (ts, main_pid)
);

CREATE TABLE IF NOT EXISTS ff_proc (
  ts INTEGER, main_pid INTEGER, pid INTEGER, type TEXT, site TEXT,
  mem INTEGER, cpu REAL, threads INTEGER
);
CREATE INDEX IF NOT EXISTS ff_proc_ts ON ff_proc(ts);

CREATE TABLE IF NOT EXISTS tab (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, title TEXT, url TEXT, site TEXT,
  private INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, window INTEGER,
  first_seen INTEGER, last_seen INTEGER, peak_mem INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tab_last ON tab(last_seen);

CREATE TABLE IF NOT EXISTS ff_tab (
  ts INTEGER, tab_id INTEGER, mem INTEGER, cpu REAL, flags INTEGER, pids TEXT
);
CREATE INDEX IF NOT EXISTS ff_tab_ts ON ff_tab(ts);
CREATE INDEX IF NOT EXISTS ff_tab_tab ON ff_tab(tab_id, ts);

CREATE TABLE IF NOT EXISTS ext (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, version TEXT,
  enabled INTEGER, first_seen INTEGER, last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS ff_ext (
  ts INTEGER, ext_id INTEGER, mem INTEGER, cpu REAL
);
CREATE INDEX IF NOT EXISTS ff_ext_ts ON ff_ext(ts);

CREATE TABLE IF NOT EXISTS userscript (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, manager TEXT,
  first_seen INTEGER, last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS us_burst (ts INTEGER, main_pid INTEGER, dur INTEGER, PRIMARY KEY (ts, main_pid));

CREATE TABLE IF NOT EXISTS ff_us (
  ts INTEGER, us_id INTEGER, pid INTEGER, site TEXT, ms REAL, cpu REAL
);
CREATE INDEX IF NOT EXISTS ff_us_ts ON ff_us(ts);

CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY, ts INTEGER, end_ts INTEGER, kind TEXT,
  severity TEXT, title TEXT, detail TEXT, dedupe TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS event_ts ON event(ts);

CREATE TABLE IF NOT EXISTS ingest_state (file TEXT PRIMARY KEY, offset INTEGER, mtime REAL);
"""

DETAIL_TABLES = ["os_sample", "os_proc", "ff_sample", "ff_proc", "ff_tab", "ff_ext", "us_burst", "ff_us"]


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = self._connect()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def reader(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def write(self, fn):
        with self.lock:
            try:
                result = fn(self.conn)
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise

    def get_meta(self, k, default=None):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, k, v):
        self.write(lambda c: c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, json.dumps(v))))

    def add_event(self, c, ts, kind, severity, title, detail=None, dedupe=None, end_ts=None):
        c.execute(
            "INSERT OR IGNORE INTO event(ts, end_ts, kind, severity, title, detail, dedupe) VALUES (?,?,?,?,?,?,?)",
            (int(ts), end_ts, kind, severity, title, json.dumps(detail or {}), dedupe),
        )

    def prune(self, keep_days: int):
        import time

        cutoff = int((time.time() - keep_days * 86400) * 1000)

        def run(c):
            for t in DETAIL_TABLES:
                c.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM tab WHERE last_seen < ?", (cutoff,))
            c.execute("DELETE FROM event WHERE ts < ?", (cutoff - 60 * 86400 * 1000,))

        self.write(run)
