"""Fold quickrec JSONL into the same rec_* tables collect.py writes.

The stopgap recorder was running before collect.py existed, so its output has
to land in the same schema or the run it captured is stranded in a text file.
Fields quickrec never had (the per-process PDH counters) are left NULL rather
than back-filled with plausible numbers.

    python analysis/ingest_quickrec.py analysis/raw/live_20260814.jsonl --label h3-live
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.collect import (  # noqa: E402
    SCHEMA_VERSION, add_event, map_luid_to_gpu, open_db, write_sample,
)


def _f(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def convert(rec: dict, prev: dict | None, comfy_pid: int | None):
    """One quickrec line -> (sysrow, gpu_rows, gpu_proc_rows, pagefile_rows,
    model_rows). Returns None when the line is unusable."""
    perf = rec.get("perf") or {}
    if not perf:
        return None

    now = _f(rec, "memxray", "now", default={}) or {}
    sysd = now.get("sys") or {}
    proc = now.get("proc") or {}
    faults = now.get("faults") or {}
    models = now.get("models") or {}
    verdict = now.get("verdict") or {}

    pf_total = (rec.get("pagefiles") or {}).get("_Total", {})
    pagefile_used = pf_total.get("used_bytes", 0)

    delta = 0.0
    if prev is not None:
        dt = max(1e-3, rec["t"] - prev["t"])
        prev_used = ((prev.get("pagefiles") or {}).get("_Total", {})
                     .get("used_bytes", 0))
        delta = (pagefile_used - prev_used) / dt

    gpu_procs = rec.get("gpu_procs") or []
    comfy_ded = sum((g.get("dedicated") or 0) for g in gpu_procs
                    if comfy_pid and g.get("pid") == comfy_pid)
    comfy_shr = sum((g.get("shared") or 0) for g in gpu_procs
                    if comfy_pid and g.get("pid") == comfy_pid)

    adapters = rec.get("gpu_adapters") or {}
    nvml_devs = rec.get("nvml") or []
    luid_map = map_luid_to_gpu(adapters, nvml_devs)
    gpu_by_index = {}
    for g in (models.get("gpus") or []):
        gpu_by_index[g.get("index")] = g

    commit_total = perf.get("commit_total", 0)
    commit_limit = perf.get("commit_limit", 0)

    sysrow = {
        "comfy_ok": 1 if now else 0,
        "phys_total": perf.get("phys_total", 0),
        "phys_avail": perf.get("phys_avail", 0),
        # quickrec kept no standby/modified/free breakdown, so in_use cannot be
        # computed the way the sampler does it. NULL, not a lookalike number.
        "in_use": None,
        "standby": None,
        "modified": None,
        "free_bytes": None,
        "cache_bytes": sysd.get("cache_bytes"),
        "system_cache_res": None,
        "pool_paged": None,
        "pool_nonpaged": None,
        "pool_paged_res": None,
        "commit_total": commit_total,
        "commit_limit": commit_limit,
        "commit_peak": perf.get("commit_peak"),
        "commit_pct": (100.0 * commit_total / commit_limit) if commit_limit else None,
        "pagefile_used": pagefile_used,
        "pagefile_size": pf_total.get("size"),
        "pagefile_delta": delta,
        "page_reads_sec": faults.get("page_reads_sec"),
        "pages_input_sec": faults.get("pages_input_sec"),
        "page_writes_sec": faults.get("page_writes_sec"),
        "pages_output_sec": faults.get("pages_output_sec"),
        "page_faults_sec": None,
        "transition_faults": None,
        "demand_zero_faults": None,
        "cache_faults": None,
        "transition_repurp": None,
        "proc_working_set": proc.get("working_set"),
        "proc_ws_peak": None,
        "proc_private_ws": proc.get("private_ws"),
        "proc_private_bytes": proc.get("private_bytes"),
        "proc_virtual_bytes": None,
        "proc_pagefile_bytes": None,
        "proc_pf_bytes_peak": None,
        "proc_faults_sec": proc.get("page_faults_sec"),
        "proc_io_read_sec": proc.get("io_read_bytes_sec"),
        "proc_io_write_sec": None,
        "proc_pool_paged": None,
        "proc_pool_nonpaged": None,
        "proc_handles": None,
        "proc_threads": None,
        "paged_out": proc.get("paged_out"),
        "untouched_commit": proc.get("untouched_commit"),
        "evicted_max": proc.get("evicted_max"),
        "mapped_ws": proc.get("mapped_ws"),
        "verdict_level": verdict.get("level"),
        "verdict_text": verdict.get("text"),
        "gpu_dedicated_tot": sum((a.get("dedicated") or 0) for a in adapters.values()),
        "gpu_shared_tot": sum((a.get("shared") or 0) for a in adapters.values()),
        "comfy_gpu_dedicated": comfy_ded,
        "comfy_gpu_shared": comfy_shr,
        "models_count": models.get("count"),
        "models_total": models.get("total_bytes"),
        "models_on_device": models.get("on_device_bytes"),
        "models_offloaded": models.get("offloaded_bytes"),
    }

    gpu_rows = []
    for d in nvml_devs:
        idx = d.get("index")
        luid_inst = next((k for k, v in luid_map.items() if v == idx), None)
        ad = adapters.get(luid_inst, {}) if luid_inst else {}
        tg = gpu_by_index.get(idx, {})
        gpu_rows.append({
            "gpu_index": idx,
            "name": d.get("name"),
            "uuid": d.get("uuid"),
            "pci_bus_id": d.get("pci_bus_id"),
            "mem_total": d.get("mem_total"),
            "mem_used": d.get("mem_used"),
            "mem_free": d.get("mem_free"),
            "bar1_total": d.get("bar1_total"),
            "bar1_used": d.get("bar1_used"),
            "util_gpu": d.get("util_gpu"),
            "util_mem": d.get("util_mem"),
            "temp_c": d.get("temp_c"),
            "power_w": d.get("power_w"),
            "power_limit_w": d.get("power_limit_w"),
            "fan_pct": d.get("fan_pct"),
            "clock_sm_mhz": d.get("clock_sm_mhz"),
            "clock_mem_mhz": d.get("clock_mem_mhz"),
            "pstate": d.get("pstate"),
            "throttle_reasons": d.get("throttle_reasons"),
            "torch_allocated": tg.get("torch_allocated"),
            "torch_reserved": tg.get("torch_reserved"),
            "adapter_luid": luid_inst,
            "adapter_dedicated": ad.get("dedicated"),
            "adapter_shared": ad.get("shared"),
            "adapter_committed": ad.get("committed"),
        })

    gpu_proc_rows = []
    for g in gpu_procs:
        luid = g.get("luid")
        gidx = None
        if luid:
            gidx = next(
                (v for k, v in luid_map.items() if luid in k), None
            )
        gpu_proc_rows.append({
            "pid": g.get("pid"),
            "name": None,          # quickrec did not resolve names
            "luid": luid,
            "phys": g.get("phys"),
            "gpu_index": gidx,
            "dedicated": g.get("dedicated"),
            "shared": g.get("shared"),
            "committed": g.get("committed"),
            "local": g.get("local"),
            "non_local": g.get("non_local"),
        })

    pagefile_rows = [
        {"name": k, "size": v.get("size"), "used_bytes": v.get("used_bytes"),
         "peak_bytes": v.get("peak_bytes")}
        for k, v in (rec.get("pagefiles") or {}).items()
    ]

    model_rows = [
        {"name": m.get("name"), "total": m.get("total"),
         "on_device": m.get("on_device"), "offloaded": m.get("offloaded"),
         "device": m.get("device"), "device_index": m.get("device_index"),
         "currently_used": 1 if m.get("currently_used") else 0}
        for m in (models.get("models") or [])
    ]

    return sysrow, gpu_rows, gpu_proc_rows, pagefile_rows, model_rows


def guess_comfy_pid(records: list) -> int | None:
    """The pid holding the most GPU memory across the trace is ComfyUI.

    quickrec never recorded which pid it was watching, and the ComfyUI process
    may since have exited, so it is inferred from the data rather than probed.
    """
    totals: dict[int, float] = {}
    for r in records:
        for g in (r.get("gpu_procs") or []):
            pid = g.get("pid")
            if pid is None:
                continue
            totals[pid] = max(totals.get(pid, 0.0),
                              (g.get("dedicated") or 0) + (g.get("shared") or 0))
    if not totals:
        return None
    return max(totals.items(), key=lambda kv: kv[1])[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--db", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "memxray_runs.db"))
    ap.add_argument("--label", default="quickrec")
    a = ap.parse_args()

    records = []
    with open(a.jsonl, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A trace cut short by a crash ends mid-line; that is a partial
                # sample, not a corrupt file.
                print("skipping truncated final line")
    if not records:
        print("no records")
        return 1

    comfy_pid = guess_comfy_pid(records)
    conn = open_db(a.db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rec_sessions (started_at, ended_at, label, host, interval_s,"
        " comfy_pid, comfy_url, schema_version, note) VALUES (?,?,?,?,?,?,?,?,?)",
        (records[0]["t"], records[-1]["t"], a.label, socket.gethostname(),
         (records[-1]["t"] - records[0]["t"]) / max(1, len(records) - 1),
         comfy_pid, None, SCHEMA_VERSION,
         "ingested from %s by ingest_quickrec.py" % os.path.basename(a.jsonl)),
    )
    session_id = cur.lastrowid
    conn.commit()

    prev = None
    n = 0
    for i, rec in enumerate(records):
        conv = convert(rec, prev, comfy_pid)
        prev = rec
        if conv is None:
            continue
        sysrow, gpus, gprocs, pfs, models = conv
        write_sample(conn, session_id, i, rec["t"], sysrow, gpus, gprocs, [], pfs, models)
        n += 1

    add_event(conn, session_id, records[0]["t"], "ingest",
              "%d samples from %s (comfy pid %s)" % (n, a.jsonl, comfy_pid))
    print("session %d: %d samples, comfy pid %s, %.1f s span"
          % (session_id, n, comfy_pid, records[-1]["t"] - records[0]["t"]))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
