# Session store — 2026-08-14, MiniMax-H3 on a 5070 Ti

Everything captured while running MiniMax-H3 generations under MemXray, in one
queryable SQLite file. Built for analysing what the pack should measure next, or
for feeding a separate tool.

```
analysis/
  memxray_runs.db      <- the store (rebuildable, disposable)
  ingest.py            <- rebuilds the DB from raw/
  watch_memxray.py     <- the live state watcher used during the session
  raw/
    *.jsonl.gz         <- every /memxray/stats sample, 2s cadence
    comfyui_8188*.log  <- ComfyUI's own log, all three rotations
```

Rebuild at any time — `raw/` is the source of truth, the `.db` is derived:

```
python analysis/ingest.py
```

## What's in it

| table | rows | what |
|---|---|---|
| `samples` | 8,645 | one per `/memxray/stats` poll |
| `sample_models` | 16,170 | per-model rows inside each sample |
| `sample_gpus` | 8,645 | per-GPU rows inside each sample |
| `log_events` | 134 | job start/end, model loads, crashes, monitor trips |
| `runs` | 27 | one per completed prompt, with duration |
| `traces` | 7 | one per capture file |
| `findings` | 9 | what the session established |
| `notes` | 5 | machine, model, baselines, counter caveats |

**Byte columns are raw bytes**, not GB — divide by `1073741824.0`. Timestamps are
unix seconds (`ts`), so `datetime(ts,'unixepoch','localtime')` reads them.

Coverage is 13:35–19:20 local with two gaps where a sampler window expired
between restarts; `traces.started_at/ended_at` show exactly what's continuous.

## The columns that matter

Most analysis hinges on separating three things that look alike:

- `paged_out` — committed but not resident. **Mostly not eviction.**
- `untouched_commit` — of that gap, address space never written. Subtract it:
  `paged_out - untouched_commit` is real eviction.
- `evicted_max` — `min(paged_out, system pagefile_used)`. An upper bound, and
  the source of the `alarm-latch` bug.
- `page_reads_sec` — hard faults against **any** backing store, including
  mapped model files. Not a pagefile signal.
- `pagefile_delta` — bytes/sec the pagefile grew. The only pagefile-specific one.
- `commit_total` / `commit_limit` — **the real ceiling on this box.** See below.

## Headline results

Eleven H3 generations. Standard resolution settles at ~465 s
(547.75 cold, then 418.85 / 476.86 / 463.98 / 465.92). Higher resolution: 800 s.
A later 30-step run: 786 s.

Three things worth knowing before analysing anything else:

1. **Commit, not RAM, is the binding constraint.** Run 5 — reported as
   unremarkable at the time — drove the pagefile from 32 GB to **67 GB** and hit
   **100.0% of the commit limit in 8 samples** across a 50-second window. Windows
   extended the pagefile each time, which raised the limit and averted failure.
   On a fixed-size pagefile those are hard allocation failures.

2. **Removing `--disable-dynamic-vram` fixed the crash.** Before: estimate-based
   loading, GPU capped at 9.84 GB, RAM pinned high for minutes after a job, one
   dead `prompt_worker`. After: full 15.92 GB card use and memory released
   *before* the job ends, so `comfyui-multigpu`'s idle-only 85% check never fires.
   Eight runs, zero trips, zero exceptions.

3. **Duration alone can't tell thrash from work.** The 800 s and 786 s runs are
   nearly identical in wall-clock and opposite in cause — 19.50 GB evicted at
   98.9% commit versus 3.36 GB evicted at 90.0%.

## Useful queries

Verdict distribution — note 883 alarms, most of them the latch bug:

```sql
SELECT verdict_level, COUNT(*) FROM samples GROUP BY 1 ORDER BY 2 DESC;
```

Real eviction, with untouched commit removed:

```sql
SELECT datetime(ts,'unixepoch','localtime') AS clock,
       (paged_out - untouched_commit)/1073741824.0 AS evicted_gb,
       pagefile_used/1073741824.0 AS pagefile_gb,
       phys_avail/1073741824.0   AS free_gb,
       page_reads_sec, verdict_level
FROM samples
ORDER BY (paged_out - untouched_commit) DESC
LIMIT 20;
```

Commit pressure — the number nothing in the UI shows:

```sql
SELECT datetime(ts,'unixepoch','localtime') AS clock,
       commit_total/1073741824.0  AS commit_gb,
       commit_limit/1073741824.0  AS limit_gb,
       100.0*commit_total/commit_limit AS pct,
       pagefile_size/1073741824.0 AS pagefile_size_gb
FROM samples
WHERE commit_limit > 0
ORDER BY (1.0*commit_total/commit_limit) DESC
LIMIT 20;
```

False alarms — alarm raised while the pagefile was static and RAM healthy:

```sql
SELECT COUNT(*) FROM samples
WHERE verdict_level='alarm'
  AND pagefile_delta <= 1048576
  AND phys_avail > 8*1073741824.0;
```

Join samples to a run:

```sql
SELECT s.* FROM samples s
JOIN runs r ON s.ts BETWEEN r.started_at AND r.ended_at
WHERE r.id = 6;            -- the high-resolution run
```

Per-model offload behaviour over time:

```sql
SELECT m.name,
       COUNT(*) AS samples,
       MAX(m.total)/1073741824.0     AS max_total_gb,
       AVG(m.offloaded)/1073741824.0 AS avg_offloaded_gb
FROM sample_models m GROUP BY 1 ORDER BY 3 DESC;
```

## Findings

`SELECT slug, severity, status, title FROM findings;` — three open bugs in
`memxray/sampler.py` (`alarm-latch`, `verdict-strobe`, `alarm-text-mismatch`),
two risks (`multigpu-crash`, `commit-ceiling`), and four observations including
`live-monitoring-blind-spot`, which is the methodological one: the live alerting
watched `phys_avail` and the verdict, and both looked fine while commit quietly
reached its ceiling.

Full text with evidence:

```sql
SELECT title, summary, evidence FROM findings WHERE slug='commit-ceiling';
```
