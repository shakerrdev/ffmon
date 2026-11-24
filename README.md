# Firefox Monitor

Records what Firefox does with your memory and CPU over hours and days, and helps
you work out why it crashes, slows down, fills up your RAM or stays running after
you close it.

It has three parts:

| Part | Runs where | What it records |
|---|---|---|
| **Collector** (`firefox/collector.sys.mjs`) | inside Firefox, via Firefox's autoconfig feature | memory + CPU per process, tab, site and extension; main-thread stalls; slow-script warnings (incl. which extension); tab-process crashes; low-memory signals; tabs being unloaded; quit; Firefox's crash list |
| **Watchdog** (`ffmon.py run`) | outside Firefox, as a normal Python program | every `firefox.exe` (private bytes, working set, CPU, threads, handles); system RAM and commit charge; frozen windows ("Not Responding"); Firefox **still running after you closed it**; new crash reports |
| **Dashboard** | http://127.0.0.1:8765 | overview with automatic diagnosis, stacked memory/CPU charts with problem markers, tabs, sites, processes, extensions, events and crash details |

Nothing leaves your computer. Data lives in `data/ffmon.db` (SQLite); detailed
samples are kept 14 days, events longer.

## Setup (Windows)

Needs Python 3.9+ and Firefox from mozilla.org (the Microsoft Store version's
folder can't be modified).

**Easy way:** double-click `install.cmd`. It installs `psutil`, asks for admin
rights once to hook the collector into Firefox, turns on autostart and opens
the dashboard.

**By hand:**

```
pip install -r requirements.txt
python ffmon.py install          # in a terminal opened with "Run as administrator"
# close every Firefox window, start Firefox again
python ffmon.py run --open       # or double-click start.cmd
```

`install` finds Firefox automatically; pass `--firefox-dir "C:\Program Files\Mozilla Firefox"`
to choose. It writes two small files into the Firefox folder
(`defaults\pref\ffmon-autoconfig.js` and `ffmon.cfg`) that load
`firefox\collector.sys.mjs` from this folder at startup. If you already use an
autoconfig setup (e.g. userChrome.js loaders), it adds its block to your
existing `.cfg` instead. It also registers the watchdog to start at login
(`python ffmon.py autostart off` to stop that).

Check it's working: `python ffmon.py status`, or the Setup tab in the dashboard.

> Keep this folder where it is after installing: Firefox loads the collector
> from here. If you move it, run `install` again.

## Diagnosing your problems

Let it record through a few normal days, including at least one crash/slowdown,
then open the **Overview**. The "What stands out" box summarizes the range you
picked. Drag across any chart to zoom into an incident.

**Firefox stays open after closing / has to be killed.** Shows up as
*"Firefox kept running after its windows closed"* (red band on the charts).
The event records how long it lingered, how much memory it held and how many
processes were still alive. If it happens regularly, run Firefox in
Troubleshoot Mode (Help → Troubleshoot Mode) for a day: if it stops, an
extension is blocking shutdown; then disable extensions one at a time.

**Crashes.** Each crash report is listed with its reason. *Out of memory* with
little "commit space" left means Windows' commit limit (RAM + page file) ran
out. Letting Windows manage the page file (or making it bigger) and having fewer
tabs loaded fixes most of these. Crash reports link to crash-stats.mozilla.org
when they were submitted.

**Slow / freezing.** *Browser responsiveness* shows main-thread stalls;
*"Firefox window was not responding"* events come from Windows itself. Compare
with the memory and *System memory* charts: freezes while RAM is full mean
Windows is swapping. *Slow extension script* events name the extension.

**RAM filling up.** *Firefox memory by part* splits Firefox into the browser UI,
web pages, extensions, GPU, media and network processes. **Sites & processes**
breaks web pages down by site, **Tabs** shows each tab with a growth rate
(MB/hour); tabs marked *growing* are leaking. **Extensions** shows each
add-on's share of the extension process.

## How the numbers are measured

- Firefox runs one process per site, shared by all tabs of that site. A tab's
  memory/CPU is the process total split by how many frames of each tab live
  in it (the same method `about:processes` uses), so it's an estimate.
- Extension content scripts run inside the page's process and count toward
  the page; the Extensions tab covers their background pages.
- CPU is a share of the whole machine (all cores), like Task Manager.
- Memory per process is Firefox's own figure; the Windows view shows private
  bytes (commit) and working set as Task Manager reports them.
- Private windows are recorded as "Private tab" with no title, URL or site.

## Commands

```
python ffmon.py run [--open] [--interval 5] [--keep-days 14] [--port 8765] [--data DIR]
python ffmon.py install [--firefox-dir DIR] [--no-autostart]
python ffmon.py uninstall [--firefox-dir DIR]
python ffmon.py autostart on|off
python ffmon.py status
```

The collector's sampling interval defaults to 10 s; change it with the
`ffmon.intervalSec` pref in `about:config` (restart Firefox).

## Troubleshooting

- **Setup says the collector has no data**: restart Firefox completely (check
  Task Manager that no `firefox.exe` is left). A Firefox update can occasionally
  remove the hook files: run `install` again.
- **Changed `collector.sys.mjs` but nothing changed**: Firefox caches modules;
  restart it with `firefox.exe -purgecaches` once.
- **Browser Console** (Ctrl+Shift+J) shows collector errors prefixed `[ffmon]`;
  they also appear on the Events tab.

## Removing it

```
python ffmon.py uninstall     # admin terminal
```

Then restart Firefox and delete this folder.

## Development

- `python tests/simulate.py /tmp/ffmon-sim 8` builds an 8-hour fake recording;
  view it with `python ffmon.py --data /tmp/ffmon-sim run --open`.
- `node tests/collector_harness.mjs /tmp/h/inbox` runs the collector against
  mocked Firefox APIs and checks its output.
- Charts use [uPlot](https://github.com/leeoniya/uPlot) (MIT), vendored in `web/vendor`.
