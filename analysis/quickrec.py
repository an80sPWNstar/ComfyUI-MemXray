"""Stopgap recorder: raw JSONL now, ingest into sqlite later.

Exists so a run happening *right now* is not lost while collect.py is still
being built. Same sources as collect.py - NVML, the Windows GPU/pagefile
counter sets, and the MemXray HTTP endpoint - but no schema and no derived
fields: one JSON object per line, whatever each source said.

    python analysis/quickrec.py --out analysis/raw/live.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memxray import nvml, winmem  # noqa: E402


def fetch(url, timeout=3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="analysis/raw/live.jsonl")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--url", default="http://127.0.0.1:8188/memxray/stats")
    ap.add_argument("--rebuild", type=float, default=20.0)
    a = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    gproc = winmem.WildcardQuery(winmem.GPU_PROCESS_COUNTERS)
    gadapt = winmem.WildcardQuery(winmem.GPU_ADAPTER_COUNTERS)
    built = time.time()
    n = 0
    print("quickrec -> %s (nvml=%s)" % (a.out, nvml.available()), flush=True)

    with open(a.out, "a", encoding="utf-8") as fh:
        while True:
            t0 = time.time()
            try:
                # Instances are per-process and go stale as processes exit; a
                # stale handle keeps reporting its last value forever.
                if t0 - built > a.rebuild:
                    gproc.rebuild()
                    gadapt.rebuild()
                    built = t0
                rec = {
                    "t": t0,
                    "perf": winmem.performance_info(),
                    "gms": winmem.global_memory_status(),
                    "pagefiles": winmem.pagefile_instances(),
                    "nvml": nvml.devices(),
                    "gpu_procs": [
                        dict(winmem.parse_gpu_process_instance(k), **v)
                        for k, v in gproc.read().items()
                        if (v.get("dedicated") or v.get("shared") or 0) > 0
                    ],
                    "gpu_adapters": gadapt.read(),
                    "memxray": fetch(a.url),
                }
                fh.write(json.dumps(rec) + "\n")
                fh.flush()  # a crash mid-run is the sample worth keeping
                os.fsync(fh.fileno())
                n += 1
                if n % 60 == 0:
                    p = rec["perf"] or {}
                    print("%s  n=%d commit %.1f/%.1f GB  pagefile %.2f GB"
                          % (time.strftime("%H:%M:%S"), n,
                             p.get("commit_total", 0) / 1024 ** 3,
                             p.get("commit_limit", 0) / 1024 ** 3,
                             (rec["pagefiles"].get("_Total", {}).get("used_bytes", 0))
                             / 1024 ** 3), flush=True)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print("sample failed: %s" % exc, flush=True)
            time.sleep(max(0.0, a.interval - (time.time() - t0)))
    print("quickrec stopped after %d samples" % n, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
