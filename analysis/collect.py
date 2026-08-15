"""Durable recorder: everything Windows and the NVIDIA driver will say about
RAM, the pagefile and VRAM, straight into sqlite.

Runs OUTSIDE ComfyUI on purpose. ComfyUI dying is the most interesting moment
in a trace, so the recorder must outlive it: the system, pagefile and GPU
numbers all come from this process, and only the model ledger, the torch
allocator figures and the verdict are fetched over HTTP. Every sample is
committed, so a hard reset loses at most one row.

    python analysis/collect.py --label h3-run --interval 1
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memxray import nvml, winmem  # noqa: E402

try:
    import psutil
except Exception:
    psutil = None

SCHEMA_VERSION = 1
GB = 1024 ** 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS rec_sessions (
    id             INTEGER PRIMARY KEY,
    started_at     REAL,
    ended_at       REAL,
    label          TEXT,
    host           TEXT,
    interval_s     REAL,
    comfy_pid      INTEGER,
    comfy_url      TEXT,
    schema_version INTEGER,
    note           TEXT
);
CREATE TABLE IF NOT EXISTS rec_samples (
    id                  INTEGER PRIMARY KEY,
    session_id          INTEGER REFERENCES rec_sessions(id),
    ts                  REAL,
    seq                 INTEGER,
    comfy_ok            INTEGER,
    phys_total          INTEGER,
    phys_avail          INTEGER,
    in_use              REAL,
    standby             REAL,
    modified            REAL,
    free_bytes          REAL,
    cache_bytes         REAL,
    system_cache_res    REAL,
    pool_paged          REAL,
    pool_nonpaged       REAL,
    pool_paged_res      REAL,
    commit_total        INTEGER,
    commit_limit        INTEGER,
    commit_peak         INTEGER,
    commit_pct          REAL,
    pagefile_used       INTEGER,
    pagefile_size       INTEGER,
    pagefile_delta      REAL,
    page_reads_sec      REAL,
    pages_input_sec     REAL,
    page_writes_sec     REAL,
    pages_output_sec    REAL,
    page_faults_sec     REAL,
    transition_faults   REAL,
    demand_zero_faults  REAL,
    cache_faults        REAL,
    transition_repurp   REAL,
    proc_working_set    REAL,
    proc_ws_peak        REAL,
    proc_private_ws     REAL,
    proc_private_bytes  REAL,
    proc_virtual_bytes  REAL,
    proc_pagefile_bytes REAL,
    proc_pf_bytes_peak  REAL,
    proc_faults_sec     REAL,
    proc_io_read_sec    REAL,
    proc_io_write_sec   REAL,
    proc_pool_paged     REAL,
    proc_pool_nonpaged  REAL,
    proc_handles        REAL,
    proc_threads        REAL,
    paged_out           REAL,
    untouched_commit    REAL,
    evicted_max         REAL,
    mapped_ws           REAL,
    verdict_level       TEXT,
    verdict_text        TEXT,
    gpu_dedicated_tot   REAL,
    gpu_shared_tot      REAL,
    comfy_gpu_dedicated REAL,
    comfy_gpu_shared    REAL,
    models_count        INTEGER,
    models_total        INTEGER,
    models_on_device    INTEGER,
    models_offloaded    INTEGER,
    disk_read_bytes_sec  REAL,
    disk_write_bytes_sec REAL
);
CREATE INDEX IF NOT EXISTS idx_rec_samples_sess ON rec_samples(session_id, ts);
CREATE TABLE IF NOT EXISTS rec_gpus (
    sample_id        INTEGER REFERENCES rec_samples(id),
    gpu_index        INTEGER,
    name             TEXT,
    uuid             TEXT,
    pci_bus_id       TEXT,
    mem_total        INTEGER,
    mem_used         INTEGER,
    mem_free         INTEGER,
    bar1_total       INTEGER,
    bar1_used        INTEGER,
    util_gpu         INTEGER,
    util_mem         INTEGER,
    temp_c           INTEGER,
    power_w          REAL,
    power_limit_w    REAL,
    fan_pct          INTEGER,
    clock_sm_mhz     INTEGER,
    clock_mem_mhz    INTEGER,
    pstate           INTEGER,
    throttle_reasons INTEGER,
    torch_allocated  INTEGER,
    torch_reserved   INTEGER,
    adapter_luid     TEXT,
    adapter_dedicated REAL,
    adapter_shared   REAL,
    adapter_committed REAL
);
CREATE INDEX IF NOT EXISTS idx_rec_gpus_sid ON rec_gpus(sample_id);
CREATE TABLE IF NOT EXISTS rec_gpu_procs (
    sample_id  INTEGER REFERENCES rec_samples(id),
    pid        INTEGER,
    name       TEXT,
    luid       TEXT,
    phys       INTEGER,
    gpu_index  INTEGER,
    dedicated  REAL,
    shared     REAL,
    committed  REAL,
    local      REAL,
    non_local  REAL
);
CREATE INDEX IF NOT EXISTS idx_rec_gpu_procs_sid ON rec_gpu_procs(sample_id);
CREATE TABLE IF NOT EXISTS rec_procs (
    sample_id      INTEGER REFERENCES rec_samples(id),
    pid            INTEGER,
    name           TEXT,
    rss            INTEGER,
    vms            INTEGER,
    private_commit INTEGER,
    peak_commit    INTEGER,
    ws_peak        INTEGER,
    page_faults    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rec_procs_sid ON rec_procs(sample_id);
CREATE TABLE IF NOT EXISTS rec_pagefiles (
    sample_id  INTEGER REFERENCES rec_samples(id),
    name       TEXT,
    size       INTEGER,
    used_bytes INTEGER,
    peak_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rec_pagefiles_sid ON rec_pagefiles(sample_id);
CREATE TABLE IF NOT EXISTS rec_models (
    sample_id      INTEGER REFERENCES rec_samples(id),
    name           TEXT,
    total          INTEGER,
    on_device      INTEGER,
    offloaded      INTEGER,
    device         TEXT,
    device_index   INTEGER,
    currently_used INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rec_models_sid ON rec_models(sample_id);
CREATE TABLE IF NOT EXISTS rec_disks (
    sample_id       INTEGER REFERENCES rec_samples(id),
    instance        TEXT,
    read_bytes_sec  REAL,
    write_bytes_sec REAL,
    reads_sec       REAL,
    writes_sec      REAL,
    queue           REAL,
    avg_read_s      REAL,
    avg_write_s     REAL,
    idle_pct        REAL
);
CREATE INDEX IF NOT EXISTS idx_rec_disks_sid ON rec_disks(sample_id);
CREATE TABLE IF NOT EXISTS rec_events (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES rec_sessions(id),
    ts         REAL,
    kind       TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_events_sess ON rec_events(session_id, ts);
"""


# Columns added after the first traces were recorded. CREATE TABLE IF NOT
# EXISTS will not add a column to a table that already exists, so a DB written
# by an older build needs them bolted on - otherwise every insert fails with
# "no such column" against a database that looks perfectly healthy.
LATER_COLUMNS = {
    "rec_samples": {
        "disk_read_bytes_sec": "REAL",
        "disk_write_bytes_sec": "REAL",
    },
}


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    for table, cols in LATER_COLUMNS.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for col, decl in cols.items():
            if col not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
                log("migrated: added %s.%s" % (table, col))
    conn.commit()
    return conn


def log(msg: str) -> None:
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def find_comfy_pid(port: int):
    """PID listening on `port`, or None."""
    if psutil is None:
        return None
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port:
                return conn.pid
    except Exception:
        pass
    return None


def map_luid_to_gpu(adapters: dict, gpus: list) -> dict:
    """Join Windows adapter LUIDs to NVML indices.

    Windows never states which LUID is which NVML index, and the orderings are
    not the same. Both report dedicated bytes for the same cards at the same
    instant though, so the join is made on that: greedily, largest adapter
    first, each NVML index claimed at most once, and only when the two agree to
    within 256 MB. A card that does not match simply gets no LUID rather than a
    guessed one - mislabelling the 3090 as the 5060 Ti would poison the trace.
    """
    out: dict = {}
    claimed: set = set()
    tolerance = 256 * 1024 * 1024

    ordered = sorted(
        adapters.items(), key=lambda kv: kv[1].get("dedicated", 0) or 0, reverse=True
    )
    for inst, data in ordered:
        dedicated = data.get("dedicated", 0) or 0
        best, best_diff = None, None
        for gpu in gpus:
            idx = gpu.get("index")
            if idx is None or idx in claimed:
                continue
            used = gpu.get("mem_used") or 0
            diff = abs(dedicated - used)
            if best_diff is None or diff < best_diff:
                best, best_diff = idx, diff
        if best is not None and best_diff is not None and best_diff <= tolerance:
            out[inst] = best
            claimed.add(best)
    return out


def fetch_comfy(url: str, timeout: float = 3.0):
    """GET the MemXray stats endpoint. None on any failure - ComfyUI being
    restarted or dead is expected here, not exceptional."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def top_processes(limit: int, always: set) -> list[dict]:
    """Biggest memory consumers on the box, so a squeeze can be attributed to
    whoever caused it rather than to whoever noticed it first."""
    if psutil is None:
        return []

    rows: list[dict] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            mi = p.memory_info()
            rows.append(
                {
                    "pid": p.pid,
                    "name": p.info.get("name"),
                    "rss": getattr(mi, "rss", 0),
                    "vms": getattr(mi, "vms", 0),
                    "private_commit": getattr(mi, "private", 0),
                    "peak_commit": getattr(mi, "peak_pagefile", 0),
                    "ws_peak": getattr(mi, "peak_wset", 0),
                    "page_faults": getattr(mi, "num_page_faults", 0),
                }
            )
        except Exception:
            continue

    rows.sort(key=lambda r: r["private_commit"] or 0, reverse=True)
    kept = rows[:limit]
    seen = {r["pid"] for r in kept}
    # The process under investigation belongs in every sample even on the ticks
    # where a browser or a game outranks it.
    for r in rows:
        if r["pid"] in always and r["pid"] not in seen:
            kept.append(r)
            seen.add(r["pid"])
    return kept


def write_sample(conn, session_id, seq, ts, sysrow, gpu_rows, gpu_proc_rows,
                 proc_rows, pagefile_rows, model_rows, disk_rows=()) -> int:
    """One sample and its children, committed before returning."""
    cur = conn.cursor()
    cols = sorted(sysrow)
    sql = "INSERT INTO rec_samples (session_id, ts, seq, %s) VALUES (?, ?, ?, %s)" % (
        ", ".join(cols), ", ".join("?" * len(cols))
    )
    cur.execute(sql, [session_id, ts, seq] + [sysrow[c] for c in cols])
    sample_id = cur.lastrowid

    for table, rows in (
        ("rec_gpus", gpu_rows),
        ("rec_gpu_procs", gpu_proc_rows),
        ("rec_procs", proc_rows),
        ("rec_pagefiles", pagefile_rows),
        ("rec_models", model_rows),
        ("rec_disks", disk_rows),
    ):
        for row in rows or ():
            rcols = sorted(row)
            rsql = "INSERT INTO %s (sample_id, %s) VALUES (?, %s)" % (
                table, ", ".join(rcols), ", ".join("?" * len(rcols))
            )
            cur.execute(rsql, [sample_id] + [row[c] for c in rcols])

    conn.commit()  # an uncommitted sample does not survive a reset
    return sample_id


def add_event(conn, session_id: int, ts: float, kind: str, detail: str) -> None:
    try:
        conn.execute(
            "INSERT INTO rec_events (session_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (session_id, ts, kind, detail),
        )
        conn.commit()
    except Exception:
        pass


def open_process_counters(pid):
    """PDH session for one pid, or None. The instance name is
    `<procname>:<pid>`, so the name has to be resolved from the pid rather
    than assumed."""
    if pid is None:
        return None
    name = "python"
    if psutil is not None:
        try:
            name = os.path.splitext(psutil.Process(pid).name())[0]
        except Exception:
            pass
    try:
        sess = winmem.PdhSession(pid, name)
    except Exception as exc:
        log("PDH session for pid %s failed: %s" % (pid, exc))
        return None
    if not sess.ok:
        log("PDH unusable for pid %s (%s)" % (pid, sess.error))
    # Rate counters report nothing until they have two collections.
    sess.collect()
    return sess


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(here, "memxray_runs.db"))
    ap.add_argument("--label", default="live")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--url", default="http://127.0.0.1:8188/memxray/stats")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--gpu-rebuild", type=float, default=20.0)
    ap.add_argument("--proc-every", type=int, default=5,
                    help="scan all processes every Nth sample (0/1 = every sample)")
    ap.add_argument("--gpu-proc-floor", type=float, default=64 * 1024 * 1024,
                    help="skip GPU-process rows below this many bytes (ComfyUI is"
                         " always kept); 0 records every process")
    args = ap.parse_args()

    conn = open_db(args.db)
    started = time.time()
    comfy_pid = find_comfy_pid(args.port)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rec_sessions (started_at, label, host, interval_s, comfy_pid,"
        " comfy_url, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (started, args.label, socket.gethostname(), args.interval, comfy_pid,
         args.url, SCHEMA_VERSION),
    )
    session_id = cur.lastrowid
    conn.commit()

    # Built once and re-read every sample; rebuilt periodically because the
    # instance list is per-process and goes stale as processes come and go.
    gproc = winmem.WildcardQuery(winmem.GPU_PROCESS_COUNTERS)
    gadapt = winmem.WildcardQuery(winmem.GPU_ADAPTER_COUNTERS)
    gdisk = winmem.WildcardQuery(winmem.DISK_COUNTERS)
    last_rebuild = time.time()
    pdh = open_process_counters(comfy_pid)

    log("session %d -> %s (comfy pid %s, nvml %s, gpu counters %s)"
        % (session_id, args.db, comfy_pid, nvml.available(), gproc.ok))

    n = 0
    prev_t = None
    prev_pagefile = None
    comfy_ok_prev = None
    pagefile_mark = None
    commit_90_seen = False
    deadline = time.time()

    try:
        while True:
            now = time.time()
            if args.duration > 0 and now - started >= args.duration:
                break
            deadline = now + args.interval

            try:
                if now - last_rebuild >= args.gpu_rebuild:
                    gproc.rebuild()
                    gadapt.rebuild()
                    gdisk.rebuild()
                    last_rebuild = now

                # A restarted ComfyUI gets a new pid, and PDH counters are
                # bound to the old one; without this the trace would go quietly
                # blank for the whole rest of the run.
                pid_now = find_comfy_pid(args.port)
                if pid_now != comfy_pid:
                    if pdh is not None:
                        pdh.close()
                    comfy_pid = pid_now
                    pdh = open_process_counters(comfy_pid)
                    add_event(conn, session_id, now, "comfy_pid",
                              "pid is now %s" % comfy_pid)

                counters = pdh.collect() if pdh is not None else {}
                perf = winmem.performance_info() or {}
                pagefiles = winmem.pagefile_instances()
                gpu_proc_raw = gproc.read()
                adapters = gadapt.read()
                disks = gdisk.read()
                gpus = nvml.devices()
                stats = fetch_comfy(args.url)
                comfy_ok = 1 if stats else 0

                phys_total = perf.get("phys_total", 0)
                phys_avail = perf.get("phys_avail", 0)
                commit_total = perf.get("commit_total", 0)
                commit_limit = perf.get("commit_limit", 0)
                commit_pct = (100.0 * commit_total / commit_limit) if commit_limit else None

                standby = (counters.get("standby_normal", 0.0)
                           + counters.get("standby_reserve", 0.0)
                           + counters.get("standby_core", 0.0))
                modified = counters.get("modified_list", 0.0)
                free_zero = counters.get("free_zero_list", 0.0)
                # Same composition the sampler uses, so old and new traces are
                # comparable: phys_total minus the lists that are not "in use".
                in_use = max(0.0, phys_total - standby - free_zero - modified)

                pf_total = pagefiles.get("_Total", {})
                pagefile_used = pf_total.get("used_bytes", 0)
                pagefile_size = pf_total.get("size", 0)
                pagefile_delta = 0.0
                if prev_t is not None and prev_pagefile is not None:
                    dt = max(1e-3, now - prev_t)
                    pagefile_delta = (pagefile_used - prev_pagefile) / dt
                prev_t, prev_pagefile = now, pagefile_used

                priv_ws = counters.get("proc_private_working_set", 0.0)
                priv_bytes = counters.get("proc_private_bytes", 0.0)
                work_set = counters.get("proc_working_set", 0.0)
                paged_out = max(0.0, priv_bytes - priv_ws)
                # An upper bound, not an attribution: the pagefile is
                # system-wide and most of it may belong to someone else.
                evicted_max = min(paged_out, float(pagefile_used or 0))
                untouched_commit = max(0.0, paged_out - evicted_max)
                mapped_ws = max(0.0, work_set - priv_ws)

                # -- GPU ------------------------------------------------------
                luid_map = map_luid_to_gpu(adapters, gpus)
                torch_by_index = {}
                models = _f(stats, "now", "models") or {}
                for g in (models.get("gpus") or []):
                    torch_by_index[g.get("index")] = (
                        g.get("torch_allocated"), g.get("torch_reserved")
                    )

                gpu_rows = []
                for d in gpus:
                    idx = d.get("index")
                    luid_inst = next((k for k, v in luid_map.items() if v == idx), None)
                    ad = adapters.get(luid_inst, {}) if luid_inst else {}
                    t_alloc, t_res = torch_by_index.get(idx, (None, None))
                    row = {k: d.get(k) for k in (
                        "name", "uuid", "pci_bus_id", "mem_total", "mem_used",
                        "mem_free", "bar1_total", "bar1_used", "util_gpu",
                        "util_mem", "temp_c", "power_w", "power_limit_w",
                        "fan_pct", "clock_sm_mhz", "clock_mem_mhz", "pstate",
                        "throttle_reasons")}
                    row.update({
                        "gpu_index": idx,
                        "torch_allocated": t_alloc,
                        "torch_reserved": t_res,
                        "adapter_luid": luid_inst,
                        "adapter_dedicated": ad.get("dedicated"),
                        "adapter_shared": ad.get("shared"),
                        "adapter_committed": ad.get("committed"),
                    })
                    gpu_rows.append(row)

                gpu_proc_rows = []
                comfy_ded = 0.0
                comfy_shr = 0.0
                for inst, vals in gpu_proc_raw.items():
                    dedicated = vals.get("dedicated", 0.0)
                    shared = vals.get("shared", 0.0)
                    if dedicated <= 0 and shared <= 0:
                        continue
                    parsed = winmem.parse_gpu_process_instance(inst)
                    pid = parsed.get("pid")
                    is_comfy = pid is not None and pid == comfy_pid
                    if is_comfy:
                        comfy_ded += dedicated
                        comfy_shr += shared
                    # ~90 desktop apps hold a few MB of VRAM each and stay flat
                    # all night; storing every one of them was 90 rows a second
                    # of nothing. The totals above are summed before this
                    # filter, so the sample-level numbers stay exact.
                    if not is_comfy and max(dedicated, shared) < args.gpu_proc_floor:
                        continue
                    pname = None
                    if psutil is not None and pid is not None:
                        try:
                            pname = psutil.Process(pid).name()
                        except Exception:
                            pname = None
                    luid = parsed.get("luid")
                    gidx = next(
                        (v for k, v in luid_map.items() if luid and luid in k), None
                    )
                    gpu_proc_rows.append({
                        "pid": pid,
                        "name": pname,
                        "luid": luid,
                        "phys": parsed.get("phys"),
                        "gpu_index": gidx,
                        "dedicated": dedicated,
                        "shared": shared,
                        "committed": vals.get("committed"),
                        "local": vals.get("local"),
                        "non_local": vals.get("non_local"),
                    })

                # -- ComfyUI's own ledger ------------------------------------
                verdict = _f(stats, "now", "verdict") or {}
                model_rows = [{
                    "name": m.get("name"),
                    "total": m.get("total"),
                    "on_device": m.get("on_device"),
                    "offloaded": m.get("offloaded"),
                    "device": m.get("device"),
                    "device_index": m.get("device_index"),
                    "currently_used": 1 if m.get("currently_used") else 0,
                } for m in (models.get("models") or [])]

                disk_rows = [dict(vals, instance=inst)
                             for inst, vals in disks.items()]
                disk_total = disks.get("_Total", {})

                pagefile_rows = [{
                    "name": name,
                    "size": v.get("size"),
                    "used_bytes": v.get("used_bytes"),
                    "peak_bytes": v.get("peak_bytes"),
                } for name, v in pagefiles.items()]

                # Walking every process on the box costs ~0.5 s, which would
                # stretch a 1 s interval by half. It is a slow-moving list, so
                # it is sampled every --proc-every ticks instead of every tick.
                proc_rows = []
                if n % max(1, args.proc_every) == 0:
                    proc_rows = top_processes(
                        args.top, {comfy_pid} if comfy_pid else set()
                    )

                sysrow = {
                    "comfy_ok": comfy_ok,
                    "phys_total": phys_total,
                    "phys_avail": phys_avail,
                    "in_use": in_use,
                    "standby": standby,
                    "modified": modified,
                    "free_bytes": free_zero,
                    "cache_bytes": counters.get("cache_bytes"),
                    "system_cache_res": counters.get("system_cache_resident"),
                    "pool_paged": counters.get("pool_paged"),
                    "pool_nonpaged": counters.get("pool_nonpaged"),
                    "pool_paged_res": counters.get("pool_paged_resident"),
                    "commit_total": commit_total,
                    "commit_limit": commit_limit,
                    "commit_peak": perf.get("commit_peak"),
                    "commit_pct": commit_pct,
                    "pagefile_used": pagefile_used,
                    "pagefile_size": pagefile_size,
                    "pagefile_delta": pagefile_delta,
                    "page_reads_sec": counters.get("page_reads_sec"),
                    "pages_input_sec": counters.get("pages_input_sec"),
                    "page_writes_sec": counters.get("page_writes_sec"),
                    "pages_output_sec": counters.get("pages_output_sec"),
                    "page_faults_sec": counters.get("page_faults_sec"),
                    "transition_faults": counters.get("transition_faults_sec"),
                    "demand_zero_faults": counters.get("demand_zero_faults_sec"),
                    "cache_faults": counters.get("cache_faults_sec"),
                    "transition_repurp": counters.get("transition_repurposed_sec"),
                    "proc_working_set": work_set,
                    "proc_ws_peak": counters.get("proc_working_set_peak"),
                    "proc_private_ws": priv_ws,
                    "proc_private_bytes": priv_bytes,
                    "proc_virtual_bytes": counters.get("proc_virtual_bytes"),
                    "proc_pagefile_bytes": counters.get("proc_pagefile_bytes"),
                    "proc_pf_bytes_peak": counters.get("proc_pagefile_bytes_peak"),
                    "proc_faults_sec": counters.get("proc_page_faults_sec"),
                    "proc_io_read_sec": counters.get("proc_io_read_bytes_sec"),
                    "proc_io_write_sec": counters.get("proc_io_write_bytes_sec"),
                    "proc_pool_paged": counters.get("proc_pool_paged_bytes"),
                    "proc_pool_nonpaged": counters.get("proc_pool_nonpaged_bytes"),
                    "proc_handles": counters.get("proc_handle_count"),
                    "proc_threads": counters.get("proc_thread_count"),
                    "paged_out": paged_out,
                    "untouched_commit": untouched_commit,
                    "evicted_max": evicted_max,
                    "mapped_ws": mapped_ws,
                    "verdict_level": verdict.get("level"),
                    "verdict_text": verdict.get("text"),
                    "gpu_dedicated_tot": sum(
                        (a.get("dedicated") or 0) for a in adapters.values()),
                    "gpu_shared_tot": sum(
                        (a.get("shared") or 0) for a in adapters.values()),
                    "comfy_gpu_dedicated": comfy_ded,
                    "comfy_gpu_shared": comfy_shr,
                    "models_count": models.get("count"),
                    "models_total": models.get("total_bytes"),
                    "models_on_device": models.get("on_device_bytes"),
                    "models_offloaded": models.get("offloaded_bytes"),
                    "disk_read_bytes_sec": disk_total.get("read_bytes_sec"),
                    "disk_write_bytes_sec": disk_total.get("write_bytes_sec"),
                }

                write_sample(conn, session_id, n, now, sysrow, gpu_rows,
                             gpu_proc_rows, proc_rows, pagefile_rows, model_rows,
                             disk_rows)
                n += 1

                # -- events ---------------------------------------------------
                if comfy_ok_prev is not None and comfy_ok != comfy_ok_prev:
                    add_event(conn, session_id, now, "comfy_up" if comfy_ok else "comfy_down",
                              "endpoint %s" % ("answered" if comfy_ok else "unreachable"))
                comfy_ok_prev = comfy_ok

                if pagefile_mark is None:
                    pagefile_mark = pagefile_used
                elif abs(pagefile_used - pagefile_mark) > GB:
                    add_event(conn, session_id, now, "pagefile_move",
                              "%.2f -> %.2f GB" % (pagefile_mark / GB, pagefile_used / GB))
                    pagefile_mark = pagefile_used

                if not commit_90_seen and commit_pct is not None and commit_pct >= 90.0:
                    commit_90_seen = True
                    add_event(conn, session_id, now, "commit_90",
                              "commit %.1f%% (%.1f of %.1f GB)"
                              % (commit_pct, commit_total / GB, commit_limit / GB))

                if n % 30 == 0:
                    log("n=%d commit %.1f/%.1f GB (%.0f%%) avail %.1f GB pf %.2f GB "
                        "comfy vram %.2f ded %.2f shared"
                        % (n, commit_total / GB, commit_limit / GB, commit_pct or 0,
                           phys_avail / GB, pagefile_used / GB,
                           comfy_ded / GB, comfy_shr / GB))
            except Exception as exc:
                log("sample %d failed: %s" % (n, exc))

            # Paced off a deadline set before the work, so a slow sample steals
            # from the sleep instead of stretching the interval.
            time.sleep(max(0.0, deadline - time.time()))
    except KeyboardInterrupt:
        pass

    conn.execute("UPDATE rec_sessions SET ended_at = ? WHERE id = ?",
                 (time.time(), session_id))
    conn.commit()
    gproc.close()
    gadapt.close()
    gdisk.close()
    if pdh is not None:
        pdh.close()
    log("session %d ended after %d samples" % (session_id, n))
    conn.close()
    return 0


def _f(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


if __name__ == "__main__":
    raise SystemExit(main())
