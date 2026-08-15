"""Build a SQLite store from the 2026-08-14 MiniMax-H3 monitoring session.

Everything the session produced lands in one queryable file:

  - every /memxray/stats sample taken at 2s intervals (8k+ of them)
  - the per-model and per-GPU rows inside each sample
  - ComfyUI log events (job start/end, model loads, crashes, monitor trips)
  - one row per generation run, with wall-clock duration
  - the findings and measured facts the session established

Re-runnable: drops and rebuilds from analysis/raw/, so the raw JSONL stays the
source of truth and the database is disposable.

    python analysis/ingest.py
"""
import gzip
import io
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
DB = os.path.join(HERE, "memxray_runs.db")

SCHEMA = """
DROP TABLE IF EXISTS samples;
DROP TABLE IF EXISTS sample_models;
DROP TABLE IF EXISTS sample_gpus;
DROP TABLE IF EXISTS traces;
DROP TABLE IF EXISTS log_events;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS findings;
DROP TABLE IF EXISTS notes;

-- One row per JSONL capture file.
CREATE TABLE traces (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE,
    started_at  REAL,
    ended_at    REAL,
    n_samples   INTEGER,
    note        TEXT
);

-- One row per /memxray/stats poll. Bytes are stored raw (not GB) so queries
-- stay exact; divide by 1073741824.0 for GB.
CREATE TABLE samples (
    id                INTEGER PRIMARY KEY,
    trace_id          INTEGER REFERENCES traces(id),
    ts                REAL,       -- unix seconds
    verdict_level     TEXT,       -- ok | mapped | warn | alarm
    verdict_text      TEXT,
    -- system
    phys_total        INTEGER,
    phys_avail        INTEGER,
    in_use            INTEGER,
    standby           INTEGER,
    modified          INTEGER,
    free_bytes        INTEGER,
    commit_total      INTEGER,
    commit_limit      INTEGER,
    pagefile_used     INTEGER,
    pagefile_size     INTEGER,
    pagefile_delta    REAL,       -- bytes/sec, +grow -shrink
    -- process
    working_set       INTEGER,
    private_ws        INTEGER,
    private_bytes     INTEGER,
    paged_out         INTEGER,    -- committed but not resident
    untouched_commit  INTEGER,    -- of that gap, never written
    evicted_max       INTEGER,    -- min(paged_out, system pagefile)
    mapped_ws         INTEGER,
    proc_faults_sec   REAL,
    io_read_bytes_sec REAL,
    -- faults (system-wide)
    page_reads_sec    REAL,
    pages_input_sec   REAL,
    page_writes_sec   REAL,
    pages_output_sec  REAL,
    -- model rollup
    models_count      INTEGER,
    models_total      INTEGER,
    models_on_device  INTEGER,
    models_offloaded  INTEGER
);
CREATE INDEX idx_samples_ts ON samples(ts);
CREATE INDEX idx_samples_level ON samples(verdict_level);
CREATE INDEX idx_samples_trace ON samples(trace_id);

CREATE TABLE sample_models (
    sample_id      INTEGER REFERENCES samples(id),
    name           TEXT,
    total          INTEGER,
    on_device      INTEGER,
    offloaded      INTEGER,
    device         TEXT,
    device_index   INTEGER,
    currently_used INTEGER
);
CREATE INDEX idx_sample_models_sid ON sample_models(sample_id);
CREATE INDEX idx_sample_models_name ON sample_models(name);

CREATE TABLE sample_gpus (
    sample_id       INTEGER REFERENCES samples(id),
    gpu_index       INTEGER,
    name            TEXT,
    total           INTEGER,
    used            INTEGER,
    free            INTEGER,
    torch_allocated INTEGER,
    torch_reserved  INTEGER
);
CREATE INDEX idx_sample_gpus_sid ON sample_gpus(sample_id);

-- Parsed from ComfyUI's own log, so job boundaries can be joined to samples.
CREATE TABLE log_events (
    id     INTEGER PRIMARY KEY,
    ts     REAL,
    clock  TEXT,
    kind   TEXT,   -- prompt_queued | model_load | prompt_done | monitor_trip
                   -- | executor_reset | exception | traceback | cancelled | other
    detail TEXT,
    raw    TEXT
);
CREATE INDEX idx_log_ts ON log_events(ts);
CREATE INDEX idx_log_kind ON log_events(kind);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    started_at  REAL,
    ended_at    REAL,
    duration_s  REAL,
    label       TEXT,
    note        TEXT
);

-- What the session established. Kept in the DB so analysis and conclusions
-- travel together rather than living only in chat.
CREATE TABLE findings (
    id       INTEGER PRIMARY KEY,
    slug     TEXT UNIQUE,
    title    TEXT,
    severity TEXT,      -- bug | risk | observation
    location TEXT,
    summary  TEXT,
    evidence TEXT,
    status   TEXT        -- open | fixed | wontfix
);

CREATE TABLE notes (
    id    INTEGER PRIMARY KEY,
    topic TEXT,
    body  TEXT
);
"""


def opener(path):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def g(d, *keys, default=None):
    """Nested get that tolerates missing branches."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def ingest_traces(con):
    if not os.path.isdir(RAW):
        print("no raw/ directory - nothing to ingest", file=sys.stderr)
        return
    files = sorted(f for f in os.listdir(RAW) if f.endswith((".jsonl", ".jsonl.gz")))
    for fname in files:
        path = os.path.join(RAW, fname)
        cur = con.cursor()
        cur.execute("INSERT INTO traces (name, note) VALUES (?, ?)",
                    (fname, "2s poll of /memxray/stats"))
        trace_id = cur.lastrowid
        n = 0
        first_ts = last_ts = None
        with opener(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                now = d.get("now") or {}
                sys_ = now.get("sys") or {}
                proc = now.get("proc") or {}
                flt = now.get("faults") or {}
                mdl = now.get("models") or {}
                ts = now.get("t")
                if ts is None:
                    continue
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                cur.execute("""
                    INSERT INTO samples (
                        trace_id, ts, verdict_level, verdict_text,
                        phys_total, phys_avail, in_use, standby, modified, free_bytes,
                        commit_total, commit_limit, pagefile_used, pagefile_size, pagefile_delta,
                        working_set, private_ws, private_bytes, paged_out, untouched_commit,
                        evicted_max, mapped_ws, proc_faults_sec, io_read_bytes_sec,
                        page_reads_sec, pages_input_sec, page_writes_sec, pages_output_sec,
                        models_count, models_total, models_on_device, models_offloaded
                    ) VALUES (?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)
                """, (
                    trace_id, ts, g(now, "verdict", "level"), g(now, "verdict", "text"),
                    sys_.get("phys_total"), sys_.get("phys_avail"), sys_.get("in_use"),
                    sys_.get("standby"), sys_.get("modified"), sys_.get("free"),
                    sys_.get("commit_total"), sys_.get("commit_limit"),
                    sys_.get("pagefile_used"), sys_.get("pagefile_size"), sys_.get("pagefile_delta"),
                    proc.get("working_set"), proc.get("private_ws"), proc.get("private_bytes"),
                    proc.get("paged_out"), proc.get("untouched_commit"), proc.get("evicted_max"),
                    proc.get("mapped_ws"), proc.get("page_faults_sec"), proc.get("io_read_bytes_sec"),
                    flt.get("page_reads_sec"), flt.get("pages_input_sec"),
                    flt.get("page_writes_sec"), flt.get("pages_output_sec"),
                    mdl.get("count"), mdl.get("total_bytes"),
                    mdl.get("on_device_bytes"), mdl.get("offloaded_bytes"),
                ))
                sid = cur.lastrowid
                for m in (mdl.get("models") or []):
                    cur.execute("""INSERT INTO sample_models
                        (sample_id, name, total, on_device, offloaded, device, device_index, currently_used)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (sid, m.get("name"), m.get("total"), m.get("on_device"),
                         m.get("offloaded"), m.get("device"), m.get("device_index"),
                         1 if m.get("currently_used") else 0))
                for gpu in (mdl.get("gpus") or []):
                    cur.execute("""INSERT INTO sample_gpus
                        (sample_id, gpu_index, name, total, used, free, torch_allocated, torch_reserved)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (sid, gpu.get("index"), gpu.get("name"), gpu.get("total"),
                         gpu.get("used"), gpu.get("free"), gpu.get("torch_allocated"),
                         gpu.get("torch_reserved")))
                n += 1
        cur.execute("UPDATE traces SET started_at=?, ended_at=?, n_samples=? WHERE id=?",
                    (first_ts, last_ts, n, trace_id))
        con.commit()
        print("  %-20s %6d samples" % (fname, n))


LOG_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.(\d{3})\] (.*)$")
DURATION_HMS = re.compile(r"Prompt executed in (\d{2}):(\d{2}):(\d{2})")
DURATION_SEC = re.compile(r"Prompt executed in ([\d.]+) seconds")


def classify(msg):
    if msg.startswith("got prompt"):
        return "prompt_queued", None
    if msg.startswith("Requested to load "):
        return "model_load", msg[len("Requested to load "):].strip()
    if msg.startswith("Prompt executed in"):
        return "prompt_done", msg
    if "MultiGPU_Memory_Monitor" in msg:
        return "monitor_trip", msg
    if "Memory_Management" in msg:
        return "executor_reset", msg
    if msg.startswith("Traceback"):
        return "traceback", msg
    if "Exception" in msg or "Error" in msg:
        return "exception", msg
    if msg.startswith("Cancelling"):
        return "cancelled", msg
    return "other", msg


def ingest_log(con):
    """Read every rotation, oldest first - the MultiGPU crash that started this
    whole investigation is in comfyui_8188.prev.log, not the current one."""
    import datetime
    names = ["comfyui_8188.prev2.log", "comfyui_8188.prev.log", "comfyui_8188.log"]
    paths = [os.path.join(RAW, n) for n in names]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        print("  (no comfyui log copied into raw/, skipping events)")
        return
    cur = con.cursor()
    n = 0
    pending_start = None
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = LOG_LINE.match(line.rstrip("\n"))
                if not m:
                    continue
                date, clock, ms, msg = m.groups()
                try:
                    ts = datetime.datetime.strptime(
                        "%s %s.%s" % (date, clock, ms), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                except ValueError:
                    continue
                kind, detail = classify(msg)
                if kind == "other":
                    continue
                cur.execute("INSERT INTO log_events (ts, clock, kind, detail, raw) VALUES (?,?,?,?,?)",
                            (ts, clock, kind, detail, msg[:500]))
                n += 1

                if kind == "prompt_queued" and pending_start is None:
                    pending_start = ts
                elif kind == "prompt_done":
                    secs = None
                    hms = DURATION_HMS.search(msg)
                    sec = DURATION_SEC.search(msg)
                    if hms:
                        h, mi, s = (int(x) for x in hms.groups())
                        secs = h * 3600 + mi * 60 + s
                    elif sec:
                        secs = float(sec.group(1))
                    cur.execute("INSERT INTO runs (started_at, ended_at, duration_s, label, note) "
                                "VALUES (?,?,?,?,?)",
                                (ts - secs if secs is not None else pending_start, ts, secs,
                                 "MiniMax-H3", None))
                    pending_start = None
    con.commit()
    print("  log events: %d" % n)


FINDINGS = [
    ("alarm-latch", "Alarm latches on stale pagefile use", "bug",
     "memxray/sampler.py:305",
     "The alarm branch fires when reads >= HARD_FAULT_ALARM and "
     "(pagefile_growing OR evicted_max >= PAGED_OUT_ALARM). evicted_max is "
     "min(paged_out, system pagefile_used), and the pagefile does not shrink "
     "promptly, so once it has been used heavily every later read spike reports "
     "'with the pagefile in play' even when the pagefile is completely static. "
     "It self-clears only when Windows reclaims pagefile space below the floor.",
     "10 alarm samples during a fully idle hour (17:29-18:33) with 39 GB RAM free, "
     "ComfyUI working set pinned at 7.30 GB and pagefile flat at ~3.9 GB. Also "
     "observed at 22.56 GB free / ws 3.75 GB / pagefile delta 0.",
     "open"),
    ("verdict-strobe", "Verdict flaps between levels on instantaneous fault rate", "bug",
     "memxray/sampler.py:274-330",
     "The level is computed from a single sample's page_reads_sec with no "
     "hysteresis or confirmation, so during a load it flips alarm/warn/mapped "
     "every few seconds and the panel strobes.",
     "Observed alarm->warn->alarm within ~60s repeatedly across runs 3-7. Reads "
     "bounced 245 -> 369 -> 3938/s in one such window.",
     "open"),
    ("alarm-text-mismatch", "Alarm wording claims pagefile when trigger was RAM pressure", "bug",
     "memxray/sampler.py:305-318",
     "When the alarm is reached via the evicted_max floor rather than actual "
     "growth, the message still reads 'with the pagefile in play', so the text "
     "and the triggering condition disagree.",
     "availRAM 3.55 GB, ws 39.67 GB, pagefile_delta 0, pagefile static at 2.67 GB, "
     "message still claimed pagefile involvement.",
     "open"),
    ("multigpu-crash", "MultiGPU cache reset kills the prompt_worker thread", "risk",
     "custom_nodes/comfyui-multigpu/model_management_mgpu.py:142 -> "
     "ComfyUI comfy/model_management.py:812",
     "comfyui-multigpu monkey-patches mm.soft_empty_cache; when system RAM "
     "exceeds 85% while no job is running it sets the free_memory flag, and the "
     "resulting unload_all_models() dereferences a None model_finalizer, raising "
     "an uncaught AttributeError inside Thread-17 (prompt_worker). The thread "
     "dies, the HTTP API keeps accepting prompts, and nothing ever executes again.",
     "2026-08-14 13:43:06 monitor trip at 91.8% RAM, 13:43:07 AttributeError, then "
     "prompts queued at 13:43 and 14:46 never ran until a restart.",
     "open"),
    ("disable-dynamic-vram", "--disable-dynamic-vram drove the RAM pressure", "observation",
     "ComfyUI launch flags",
     "With the flag set, ComfyUI used estimate-based loading ('loaded partially; "
     "8484 MB loaded, 8832 MB offloaded') and capped GPU use at 9.84 GB. Removing "
     "it enabled comfy-aimdo dynamic VRAM, which uses the full 15.92 GB card and "
     "releases memory immediately at job end - which is what stops the MultiGPU "
     "threshold from firing during the idle gap.",
     "Before: 1 monitor trip, 1 dead worker. After: 8 runs, 0 trips, 0 exceptions. "
     "Post-job RAM went from staying pinned ~3 min to dropping to 28-45% used "
     "within 1 second.",
     "fixed"),
    ("true-positive-thrash", "First correctly-attributed pagefile thrash", "observation",
     "high-resolution run 16:52:53-17:06:13",
     "At higher resolution the machine genuinely ran out: physical RAM hit "
     "0.02 GB free (100% used), commit reached 101.8 of 102.9 GB (98.9%), and "
     "Windows auto-expanded the pagefile 32 -> 37 -> 39 GB mid-run. The gap "
     "decomposed cleanly - paged_out 42.21 GB, untouched 22.72 GB, leaving "
     "19.49 GB genuinely evicted, exactly matching the system pagefile. Note "
     "this run was NOT the session's worst by commit pressure - see "
     "commit-ceiling; its pagefile reached 39 GB against run 5's 67 GB.",
     "800 s versus a ~465 s baseline (+72%), sustained 1700+ faults/s, peak 7762.",
     "open"),
    ("commit-ceiling", "Commit hit 100% of limit; Windows doubled the pagefile to cope", "risk",
     "run 5, 16:37:07-16:44:53 (standard resolution)",
     "The binding constraint on this machine is the COMMIT limit, not physical "
     "RAM, and nothing in the panel surfaces it. Across run 5 the pagefile was "
     "grown by Windows from 32 GB to 67 GB - more than doubling - and "
     "commit_total reached 100.0% of commit_limit in 8 samples inside a 50 s "
     "window (16:37:42-16:38:32). Each time, Windows extended the pagefile, "
     "which raised the limit and averted an allocation failure. With a "
     "fixed-size pagefile those samples are hard failures.",
     "pagefile_size trace: 32 GB at 13:35, stepping to 46 GB during run 4, then "
     "45->67 GB across 16:37:42-16:44:02, falling back to 47 GB at 16:44:18 and "
     "32 GB by 16:51. Peak commit_total 129.9 GB against a 130.9 GB limit.",
     "open"),
    ("live-monitoring-blind-spot", "Live monitoring watched the wrong number", "observation",
     "session methodology",
     "Throughout the session the alerting watched available RAM and the verdict "
     "level. Both looked survivable (91-95% RAM) while commit was silently "
     "reaching 100% of its limit and the pagefile was doubling. Run 5 was "
     "reported at the time as unremarkable; it was the most extreme memory event "
     "captured. Any future alerting should key on commit_total/commit_limit and "
     "on pagefile_size changing, not on phys_avail alone.",
     "Run 5 reported live as '465.92 s, clean'. Post-hoc query found 8 samples at "
     ">=99.9% commit and 35 GB of pagefile growth inside it.",
     "open"),
    ("duration-vs-thrash", "Equal runtimes, opposite memory causes", "observation",
     "runs at 17:06 and 19:20",
     "The high-res run (800 s) and a later long run (786 s) took nearly the same "
     "wall-clock for entirely different reasons: the first was thrashing (19.50 GB "
     "evicted, 98.9% commit), the second was simply doing more work (3.36 GB "
     "evicted, 90.0% commit, 30 steps at 22.59 s/it). Duration alone cannot "
     "distinguish them; the memory decomposition can.",
     "See runs table and samples in the two windows.",
     "open"),
]

NOTES = [
    ("machine", "Windows 11, 63.9 GB usable RAM, RTX 5070 Ti 15.92 GB VRAM, "
                "pagefile initially 32 GB and auto-expanding to 39 GB under load. "
                "ComfyUI 0.33.1, torch 2.13.0+cu130, comfy-aimdo 0.4.13."),
    ("model", "MiniMax-H3 (joint video + synchronized audio) via "
              "ComfyUI-MiniMax-H3-Turbo. Four models register: MiniMaxH3 (main), "
              "MiniMaxH3TEModel_ (text encoder), MiniMaxH3VideoVAE, "
              "MiniMaxH3AudioVAE. Peak tracked footprint 39.55 GB."),
    ("baseline", "Standard-resolution runs settle at ~465 s: 547.75 (cold), "
                 "418.85 (warm), 476.86, 463.98, 465.92. Higher resolution: 800 s. "
                 "A later long run: 786 s at 30 steps / 22.59 s per step."),
    ("counter-caveats", "page_reads_sec counts hard faults against ANY backing "
                        "store including mapped model files, so it is not a "
                        "pagefile signal. pagefile_delta is the only "
                        "pagefile-specific one. A committed-vs-resident gap is "
                        "mostly untouched commit, not eviction - subtract "
                        "untouched_commit from paged_out to get real eviction."),
    ("release-behaviour", "Every healthy run released the big weights BEFORE the "
                          "job ended (models 4 -> 1 or 2, RAM to 28-46 GB free), "
                          "which is why MultiGPU's idle-only 85% check never fired "
                          "again. This, not lower peak usage, is the protective "
                          "mechanism - peak RAM still reached 91-95%."),
]


def seed(con):
    cur = con.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO findings (slug,title,severity,location,summary,evidence,status) "
        "VALUES (?,?,?,?,?,?,?)", FINDINGS)
    cur.executemany("INSERT INTO notes (topic, body) VALUES (?,?)", NOTES)
    con.commit()
    print("  findings: %d, notes: %d" % (len(FINDINGS), len(NOTES)))


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    print("ingesting traces...")
    ingest_traces(con)
    print("ingesting comfyui log...")
    ingest_log(con)
    print("seeding findings/notes...")
    seed(con)

    cur = con.cursor()
    for q, label in [
        ("SELECT COUNT(*) FROM samples", "samples"),
        ("SELECT COUNT(*) FROM sample_models", "model rows"),
        ("SELECT COUNT(*) FROM sample_gpus", "gpu rows"),
        ("SELECT COUNT(*) FROM log_events", "log events"),
        ("SELECT COUNT(*) FROM runs", "runs"),
    ]:
        print("  %-12s %d" % (label, cur.execute(q).fetchone()[0]))
    con.close()
    print("\nwrote %s" % DB)


if __name__ == "__main__":
    main()
