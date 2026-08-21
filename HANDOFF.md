# Handoff

Append-only. Newest entry wins; earlier entries are history, not instructions.
Verify any claim here against the code before acting on it.

## 2026-08-13 22:01 -- initial build + three-view redesign

Built the pack from scratch and shipped it to
`github.com/an80sPWNstar/ComfyUI-MemXray` (private, MIT). Repo at `b4cfe43`,
working tree clean, in sync with origin.

Live install is a directory junction, not a copy:
`D:\ComfyUI_Installs\New Main\ComfyUI\custom_nodes\ComfyUI-MemXray` ->
`E:\vs_code_projects\ComfyUI-MemXray`. Edit the project dir; ComfyUI sees it
immediately. JS changes need only a browser refresh, Python changes need a
ComfyUI restart.

State: backend, all four HTTP endpoints, all four nodes, and the panel's four
views (Tiers / Models / Headroom / Details) are verified against the live
server on port 8188.

**Not yet verified, and the reason to be careful tomorrow:**
- The Models ledger has only ever been seen in its empty state. No models were
  loaded during the build.
- The pagefile alarm path (red fault bars, `verdict.level == "alarm"`) has
  never fired on real data. It is coded and unit-reasoned, not witnessed.
Both need a model load big enough to force eviction. Until then, treat the
red/alarm rendering as untested.

**Do not "fix" these, they are deliberate:**
- No per-model pagefile figure. Windows attributes eviction per process, not
  per allocation; the Models view reports the process-level ceiling once
  instead. Adding a per-checkpoint number would be fabrication.
- `evicted_max` is drawn as a dashed hairline over a faint tint, never a filled
  slab. It is an upper bound capped by the system-wide pagefile, most of which
  may belong to other processes.
- The chart palette was validated with the dataviz palette checker (lightness
  band, chroma floor, CVD separation, contrast). Do not nudge those hexes by
  eye - re-run the validator.
- `web/js/memxray.js` imports `../../../scripts/app.js` with THREE levels, not
  the usual two, because the file is served from `/extensions/<pack>/js/`.

Measured facts that are easy to re-derive wrongly, with the numbers:
- A committed-vs-resident gap is not pagefile use. Observed 16.40 GB committed
  / 8.69 GB resident (7.72 GB gap) while the whole system pagefile held
  1.75 GB. The gap was overwhelmingly untouched commit.
- `\Memory\Page Reads/sec` is not a pagefile counter. Importing torch and
  mmap'ing a checkpoint hit 1330 reads/s with zero pagefile growth. Only
  pagefile growth alongside faults escalates the verdict.

## 2026-08-14 19:35 -- MiniMax-H3 session: all four verdict levels witnessed

Eleven H3 generations run against the live panel. Repo at `b8d4757`.
All session data is committed under `analysis/` - 8,645 samples in
`analysis/memxray_runs.db`, raw JSONL and all three ComfyUI log rotations in
`analysis/raw/`. Rebuild with `python analysis/ingest.py`. Start at
`analysis/README.md`.

**Retire these caveats from the 2026-08-13 entry - both are now verified:**
- The Models ledger has been exercised at 39.55 GB across four models
  (MiniMaxH3, MiniMaxH3TEModel_, MiniMaxH3VideoVAE, MiniMaxH3AudioVAE),
  with populate, reorder, partial-release and full-empty transitions all seen.
- `alarm` has fired on real data, including one correctly-attributed pagefile
  thrash: paged_out 42.21 GB, untouched 22.72 GB, leaving 19.49 GB genuinely
  evicted and exactly matching the system pagefile. `ok`, `mapped`, `warn`
  and `alarm` have all now been observed live.

**The thing that matters most, and it is not in the panel at all:**
Commit is the binding constraint on this box, not physical RAM. During run 5
(16:37-16:44, standard resolution, reported as unremarkable at the time)
Windows grew the pagefile from 32 GB to 67 GB and `commit_total` reached
100.0% of `commit_limit` in 8 samples inside a 50 s window. Each time Windows
extended the pagefile, which raised the limit and averted an allocation
failure - a fixed-size pagefile fails there. Peak seen anywhere: 129.9 GB
against a 130.9 GB limit. Watching `phys_avail` and the verdict missed this
entirely; both looked survivable at 91-95%.

**Three open bugs in `memxray/sampler.py`, all with evidence in the findings table:**
- `alarm-latch` (sampler.py:305) - the alarm accepts `evicted_max >=
  PAGED_OUT_ALARM` as an alternative to `pagefile_growing`. `evicted_max` is
  `min(paged_out, pagefile_used)` and the pagefile does not shrink promptly, so
  after any heavy run every later read spike reports "with the pagefile in
  play" while the pagefile is static. 470 of 883 alarms were false by this
  route, including 10 during a completely idle hour with 39 GB free. It does
  self-clear once Windows reclaims pagefile space below the floor.
- `verdict-strobe` - the level is computed per-sample with no hysteresis, so it
  flips alarm/warn/mapped every few seconds under load and the panel strobes.
- `alarm-text-mismatch` - when the alarm is reached via the `evicted_max` floor
  the message still claims pagefile involvement, so text and trigger disagree.

**Environment change made this session, keep it:** `--disable-dynamic-vram` was
removed from the ComfyUI launch flags. With it set, loading was estimate-based
(`loaded partially; 8484 MB loaded, 8832 MB offloaded`), the GPU capped at
9.84 GB, and RAM stayed pinned for minutes after a job - which let
`comfyui-multigpu`'s idle-only 85% check fire and kill the `prompt_worker`
thread via a `None` `model_finalizer` (`comfy/model_management.py:812`,
crash captured at 13:43:07 in `analysis/raw/comfyui_8188.prev.log`). Without
the flag, dynamic VRAM uses the full 15.92 GB card and releases memory *before*
the job ends; eight subsequent runs produced zero trips and zero exceptions.
The MultiGPU crash path is unfixed and still armed - it just no longer gets
the conditions it needs.

Do not conclude a run was healthy from wall-clock alone: the 800 s high-res run
and a 786 s 30-step run are near-identical in duration and opposite in cause
(19.50 GB evicted at 98.9% commit vs 3.36 GB at 90.0%).

`web/js/memxray.js` gained a "What's been happening" recap under the Tiers
lanes (`d4b51c0`). Written and syntax-checked, but never rendered in a browser -
ComfyUI was closed before a refresh. Verify it draws before trusting it.

## 2026-08-15 01:05 -- pinned memory A/B, and the collector that measured it

Recording moved out of ComfyUI. `analysis/collect.py` samples Windows + NVML in
its own process and commits every sample, so it keeps recording when ComfyUI
dies. `analysis/memxray_runs.db` now holds sessions 1-2 (10,455 samples, ~3.1 h)
in `rec_*` tables. `analysis/quickrec.py` is the JSONL stopgap that caught the
first run; `analysis/ingest_quickrec.py` folds it into the same schema.
Raw JSONL for the first capture is `analysis/raw/live_20260814.jsonl.gz`.

**The headline result - pinned memory off is the RAM-only configuration:**

| | pinned ON | pinned OFF |
|---|---|---|
| GPU-busy duration | 385 s (3 runs: 386/392/388) | 450-459 s (5+ runs) |
| ComfyUI shared VRAM | 25.34 GB | 0.26 GB |
| commit peak | 80.4 GB (84%) | 39-46 GB (41-48%) |
| RAM avail floor | 4.29 GB | 34-40 GB |
| pagefile peak | 1.30 GB | 0.25-0.50 GB |
| process working set | 44.1 GB | 6.3-7.7 GB |

Cost of turning pinned off is a stable **+18% wall clock**. Bryan's goal is SSD
endurance, and pinned OFF is the right answer - but NOT because it writes less.
Both configs wrote ~0.02 GB to the pagefile. It wins because it keeps commit at
~44% instead of 84%, so the multi-GB pagefile growth burst (32->67 GB, seen
2026-08-14) never gets triggered.

**Do not repeat this mistake:** the 25 GB of GPU "Shared Usage" was first read
here as the NVIDIA driver's sysmem-fallback spill, because dedicated VRAM hit
95% (15.16 of 15.92 GB) and shared began climbing immediately after. That
timeline fits pinned staging memory equally well, and the pinned-memory toggle
proved it was pinned memory - shared collapsed to 0.26 GB with dedicated
unchanged. Allocation timing cannot separate the two; only the toggle can.

**Measured facts worth not re-deriving:**
- Per-process VRAM must come from `\GPU Process Memory(*)`, NOT NVML. Under
  WDDM every `nvmlDeviceGetComputeRunningProcesses` usedGpuMemory is "not
  available". The PDH set also carries Shared Usage, which NVML has no concept
  of at all.
- `PdhExpandWildCardPathW` returns PDH_MORE_DATA as a DWORD; ctypes hands it
  back signed, so a naive `rc == PDH_MORE_DATA` never matches and every
  wildcard expansion silently returns []. Mask with `& 0xFFFFFFFF`.
- Windows adapter LUIDs are joined to NVML indices by matching dedicated bytes
  at the same instant (256 MB tolerance, greedy, largest first). There is no
  API that states the mapping. All 3 cards matched: 0x133BA=5070 Ti,
  0x14B4D=5060 Ti, 0x1823D=3090.
- Reads do not wear SSDs. G: did 4.5 GB of model reads with zero writes.
- **The recorder was the biggest writer on the box**: ~2 GB/h to E: from
  committing every sample with an fsync plus quickrec's own fsync per line,
  against 25 MB of actual DB growth. If long unattended traces are wanted,
  batch the commits (still unfixed - Bryan has not called it).
  `--gpu-proc-floor` now drops sub-64 MB GPU processes (~90 rows/sample of
  idle desktop apps); ComfyUI is always kept and the sample totals are summed
  before the filter, so they stay exact.

**State on exit:** both recorders STOPPED at Bryan's request. Nothing is
sampling. Restart with
`python analysis/collect.py --label <name>`. ComfyUI still up on 8188 (pid
36268 as of 01:04), pinned memory disabled, `--disable-dynamic-vram` still
absent. Multi-GPU is off the table until comfyui-multigpu works with dynamic
VRAM, so the idle 3090 is deliberate - do not propose it again.

Untouched from the previous entry: the three `memxray/sampler.py` bugs
(`alarm-latch`, `verdict-strobe`, `alarm-text-mismatch`) are still open, and
the panel is still blind to shared VRAM and to commit pressure.

## 2026-08-15 22:52 -- installed on the secondary ComfyUI instance

The pack had only ever been linked into the primary install. Added the same
directory junction on the secondary:
`D:\ComfyUI_Installs\Secondary\ComfyUI\custom_nodes\ComfyUI-MemXray` ->
`E:\vs_code_projects\ComfyUI-MemXray`. Both installs now share one working
tree, so an edit lands in both at once - a Python change needs BOTH servers
restarted before it is fully live.

Ports and interpreters, since they are easy to mix up:
- primary   = port 8188, `D:\ComfyUI_Installs\New Main\ComfyUI\.venv`
- secondary = port 8189, `D:\ComfyUI_Installs\Secondary\ComfyUI\.venv`
The secondary also carries a `standalone-env\python.exe`; that is NOT the
interpreter it runs on. Check the command line, not the directory listing.
`psutil 7.2.2` on Python 3.13.12 is present in the secondary venv, so the
only dependency is already satisfied.

Not yet loaded: the secondary was mid-generation when the junction was made,
so it was left running and `/memxray/status` there still 404s. It needs a
restart before the panel appears on 8189.

`analysis/collect.py` and `analysis/quickrec.py` still default to
`http://127.0.0.1:8188/memxray/stats`. Pass `--url` to record the secondary,
and keep the two instances' samples in separate sessions - the schema has no
notion of which server a row came from.

## 2026-08-16 -- registry prep, deliberately NOT published

Publisher `an80sPWNstar` is claimed and accepted on registry.comfy.org, so the
`PublisherId` in `pyproject.toml` is confirmed correct. Nothing has been
published and Bryan does not want it published yet.

Registry metadata was brought up to spec: `Repository` moved from `[tool.comfy]`
to `[project.urls]` (it is required there, and was effectively missing before),
`license` switched to `{ file = "LICENSE" }`, Windows classifier added.

`.comfyignore` added because the publish archive is built from `git ls-files` -
without it every Manager install would pull `analysis/memxray_runs.db` (51 MB)
and `analysis/raw/` (5 MB). `docs/` is intentionally NOT ignored: the registry
renders the README on the public node page and would show broken images.

`.github/workflows/publish_action.yml` exists but its `push` trigger is
COMMENTED OUT on purpose - `workflow_dispatch` only. Do not uncomment it as a
tidy-up. Publishing is meant to be a deliberate manual act until Bryan says
otherwise, and `pyproject.toml` edits would otherwise fire it.

Two values become permanent at first publish and cannot be changed after:
`name = "comfyui-memxray"` and the publisher id.

Still open before any release: the repo is PRIVATE, so every install path in the
README 404s; and secret `REGISTRY_ACCESS_TOKEN` has not been created.
README was rewritten this session for a public/reddit audience.

## 2026-08-16 19:50 -- pre-publish once-over: verdict bugs fixed, cruft removed

Publish-readiness review of everything shipped in the archive. All three open
`sampler.py` bugs from 2026-08-14 are now FIXED and validated by replaying the
recorded sessions (8,645 samples in `analysis/memxray_runs.db`):
- `alarm-latch`: alarm at >=300 reads/s now requires `pagefile_growing`; a big
  static `evicted_max` gives `warn` with honest text instead. Replay: 883
  recorded alarms -> 48 raw (148 after hold). The old logic replayed to exactly
  883, so the reimplementation is faithful.
- `verdict-strobe`: de-escalation now holds 10 s (`VERDICT_HOLD_SECONDS`,
  escalation still immediate). Replay: level transitions 502 -> 115.
- `alarm-text-mismatch`: gone with the latch; each branch's text matches its
  trigger. README verdict table updated to match.

Other changes, all verified by py_compile + node --check + a live sampler run
(Process V2, 4 threads hammering sample() concurrently, zero errors):
- `sampler.sample()` serialized with `_sample_lock` (HTTP/node on-demand calls
  raced the background thread on `_prev_pagefile`/`_peak_paged_out`);
  `start()` idempotence now lock-guarded; `__import__("sys")` hack removed.
- `__init__.py` no longer calls `logging.basicConfig` (was mutating ComfyUI's
  global logging config).
- `render.py` layout now scales with height (was hardcoded for 720 px; any
  chart under ~600 px crashed PIL with "y1 must be >= y0" - found by smoke
  test, fixed, all sizes 480x320..4096x2160 pass). Chart node min height
  320 -> 480; old saved 320s still render via clamps.
- `memxray.js`: removed dead `tiles.inRam/pagedOut/evictedMax/mapped` aliases,
  the never-used verdict `dot` element + CSS, and the declarative `keybindings`
  block (on builds that wire it, Alt+M fired twice via block + manual listener
  and net no-op'd; manual listener is the single path, command stays for the
  palette). Unreachable-backend message now says Windows-only explicitly.

XSS check: all backend-derived strings (model names, marker labels) go through
textContent/canvas; the one innerHTML in updateTip interpolates only
hardcoded labels and human() output. Clean.

Local-LLM review passes (2x .70, sampler + JS slices, lens-diverse) returned
UNKNOWN - no candidate findings; everything above came from direct read.

NOT committed - working tree also carries the 2026-08-16 registry-prep edits
(README rewrite, pyproject, .comfyignore, .github/). Repo still private,
nothing published, per Bryan's standing instruction.

## 2026-08-17 10:15 -- per-model placement is now MEASURED, not apportioned

Bryan's complaint: the panel could not say whether a model was on RAM, VRAM or
pagefile, and could not show a model split across them. Fixed by measuring it.

**Retire the 2026-08-13 "no per-model pagefile figure" decision.** It was right
for the counters that existed then and wrong as a permanent rule. New
`memxray/placement.py` walks each loaded model's parameters and buffers for the
address range of every storage (`data_ptr`/`nbytes` only - metadata, so probing
never faults an evicted weight back in), then per range:
- `VirtualQuery` -> MEM_PRIVATE (only the pagefile can back it) vs MEM_MAPPED
  (evicts back to the .safetensors). Exact.
- `QueryWorkingSetEx` -> per-page Valid bit, so residency is per model.
Both added to `winmem.py` (`region_at`, `working_set_residency`,
`residency_available`). Four tiers come out: gpu / ram / pagefile / file.

**Measured on this box before wiring it up** (scratch test, all four legs):
private+touched 256 MB = 100% resident, 0.7 ms sampled / 38.8 ms exact;
private committed-never-touched = 0% resident; a 3.16 GB mmap'd safetensors =
kind `mapped`, 0 resident, then exactly 64.1 MiB resident after touching 64 MiB;
probe of the whole 3.16 GB range = 8.2 ms at 256 KB granularity. torch legs:
CPU module 0.125 GB -> all `ram`; after `.to(cuda:0)` -> all `gpu`; a bag of
mmap'd safetensors buffers -> 0.466 GB `file`, 1 MB `ram`.

**Two limits that are NOT bugs - do not "fix" them by making the number
prettier:**
- A page out of the working set may be in RAM on the standby list. No user-mode
  API says which. So every readout says "out of RAM, and this is what backs it",
  never "on disk". Keep that wording.
- A private page committed and never written also reads out-of-RAM. The system
  pagefile size is the cross-check: when the measured per-model total exceeds
  it, the Models view states the excess is untouched commit. That is the whole
  misreading this pack exists to prevent, so it stays visible in the UI.

Cadence: `PLACEMENT_INTERVAL = 5.0` s, own throttle, cached per
`(index, name, total, device_index)`. The 1 Hz sampler is untouched;
`_models()` calls `placement.PLACEMENT.annotate()` which is O(models) when
serving cache. `probe_ms` / `probe_age` ride along in `placement_totals` and the
panel prints them.

UI, per Bryan's choices this session (speed-first wording, grid in Models,
labeled bar in Tiers, all four encodings at once):
- `TIER_DEFS` in memxray.js is the single vocabulary - "fastest (GPU)",
  "fast (RAM)", "slow (disk)", "not loaded (file)". No jargon in user-visible
  text; "out of the working set" and "mmap'd" were explicitly rejected.
- Models view is now a grid (rows = models, columns = the four places) plus a
  labeled bar and a plain sentence per model, plus totals and a capacity row.
  `buildModelRow` and the old two-color row track are gone.
- Tiers view gained a "Where each model is" block ABOVE the lanes; lane captions
  now carry the speed ("VRAM - on the graphics card (fastest)" etc). The flow
  row names the split instead of claiming "parked in system RAM" - which was
  wrong, most of what leaves the card is mmap'd file pages not in RAM at all.
- Every slice carries color + texture + letter chip + text that steps down as it
  narrows; tooltips always hold the figure and the reason.
- Details panel titles/legends reworded to the same vocabulary.

**NOT verified in a browser.** ComfyUI on 8188 was mid-generation (1 job
running) at 10:15, so the Python side was NOT restarted and the panel has never
rendered any of this. Both instances need a restart before it is live (they
share one working tree via the junctions). py_compile + node --check pass, and
the probes are proven by the scratch tests above, but the panel drawing is
unwitnessed - same caveat as the recap block in the 2026-08-14 entry.

Local-LLM review: 2 lens-diverse calls to .70 (boundary conditions on
placement.py, ctypes/API misuse on the winmem slice). placement.py returned
UNKNOWN. The winmem call claimed `QueryWorkingSetEx`'s `cb` should be
`c_size_t`, not `DWORD` - rejected, `DWORD` is the documented signature and the
chunking caps the buffer at 64 KB regardless.

## 2026-08-17 11:20 -- first live reading, and what it says about the old panel

Restarted 8188 (new pid 36336, Process V2) and read a real MiniMax-H3 run.
Three models, ~42.3 GB total. Measured placement, steady across a 24 s window:

  gpu 0.01 | ram 0.16 | pagefile 0.09 | file 37.43 | unknown 0.07  (GB)

**The old panel's headline claim was false, and this is the proof.** It reported
that offload as "parked in system RAM" - it is not in RAM at all. 37.4 GB is
mmap'd .safetensors pages that are not in the working set; they come back from
the model file, not the pagefile. `on_device` was 0.00 GB for all three models
while the 5070 Ti sat at 9.6 GB and 100% utilisation.

**Dynamic VRAM makes per-model VRAM genuinely unattributable, and that is not a
bug to fix.** ComfyUI casts weights to the card layer by layer, so registered
parameters stay on the CPU and `loaded_size()` is 0 even for the model driving
the GPU. A "-" in the GPU column would read as "not on the card at all", the
opposite of the truth. The grid now prints `streamed` for a model whose
`currently_used` is True with no VRAM residency, and says the card's own figure
belongs to the card, not to one model. `currently_used` separates them
correctly on live data (MiniMaxH3 True, the TE model and VAE False).

Fixes made after the reading, both needing a restart to take effect:
- The probe measured 550-670 ms on this load, so it no longer runs on the
  sampler thread: `Placement._kick()` starts a one-shot daemon thread, `_at` is
  stamped at completion so a slow probe cannot queue behind itself, and
  `annotate()` only ever reads the cache. The 1 Hz cadence is untouched.
- Tier totals are reconciled to ComfyUI's own `model_size()`: the walk found
  13.33 GB of a model ComfyUI calls 14.61 GB (weights held outside registered
  parameters/buffers are invisible to it). The shortfall is carried as
  `unknown`, never spread across the measured tiers, so the panel cannot
  disagree with the size everyone already knows.
- Models view now states the standby-cache caveat where the numbers are read:
  with 37 GB reading "not loaded", Windows' cache size is what says how much of
  it comes back at RAM speed. It is system-wide, so it is never attributed.

Still unwitnessed in a browser - Bryan was mid-run, so no second restart and no
refresh. The JS half is live on a Ctrl+F5; the two Python fixes above are not.

## 2026-08-17 14:00 -- 96 GB RAM: what it did and did not buy (3 measured runs)

RAM went 64 -> 96 GB (95.9 GB reported, system-managed 32 GB pagefile, commit
limit 127.9 GB). Three runs of the SAME workload - MiniMax H3, 25 steps, 0.5 MP,
15 s clip, RIFE 2x -> 723 frames every time, so these are directly comparable:

| config | s/it | total | outcome |
|---|---|---|---|
| `--disable-pinned-memory` (11:57) | 29.41 | 14:26 | ok |
| pinned ON + `--reserve-vram 2` (13:30) | 28.19 | - | OOM at VAE decode |
| pinned OFF + `cublas_ops autotune --fast-disk` (13:58) | 28.92 | 14:18 | ok |

**Do not re-enable pinned memory on this box for this pipeline.** It is 4.2%
faster at sampling and then kills the run. Both attempts died the same way, in
`comfy_aimdo/host_buffer.py:109` -> `aimdo: src/hostbuf.c:283 hostbuf_read_file_slice:
device copy failed result=2` (cudaErrorMemoryAllocation) during
`MiniMaxH3VideoVAE` tiled decode - a 64 MB slice with no reserve, a 32 MB slice
with `--reserve-vram 2`. Torch's allocator reported `CUDA OOMs: 0`, because the
failing allocation is aimdo's, not the caching allocator's. Reserve 2 GB was not
enough; chasing it further costs 12 min per attempt for a 4% ceiling.

**The 96 GB is not wasted, it just is not the constraint.** Same pinned-ON
workload that hit 84% of commit and a 4.29 GB RAM floor at 64 GB now peaks at
59% (75.7 GB) with a 36 GB floor and a 0.06 GB idle pagefile. The RAM ceiling is
gone; the 16 GB VRAM ceiling is what binds, and no amount of system RAM moves it.

Flags now in place on BOTH instances (Desktop `installations.json`):
`--listen 0.0.0.0 --fast fp16_accumulation cublas_ops autotune --port <p>
--disable-pinned-memory --fast-disk`
- `fp8_matrix_mult` deliberately NOT enabled: it is the one `--fast` feature that
  can alter output, and the ask was faster, not different.
- `--fast-disk` is within noise here. The Sequential ReadAhead pack still warms
  whole model files into host RAM first (`warm budget 19.53 GiB`), so the
  disk-vs-unpinned-RAM path barely matters. Drop it if it ever confuses a test.
- Editing those args requires Comfy Desktop FULLY quit - it rewrites
  `installations.json` on launch and will clobber a live edit. Helper that
  refuses to run while it is up, with presets: `scratchpad/set_launch_args.py`
  (this session's scratchpad; re-create if gone, it is ~90 lines).

**Why sampling cannot be flag-tuned further:** the sampler model is
`minimax_h3_fl2va_pruned_int8_convrot.safetensors`, 19.53 GB, on a 16.30 GB card.
It is streamed every step (`19995MB Staged`, `Force pre-loaded 210 weights:
1175 KB`), the GPU sits at 100% utilisation, and VRAM in use stays ~9.7 GB
because the streamer keeps a working window plus room for the decode. The only
remaining large lever is a quantisation that FITS in 16 GB (Blackwell has native
fp4/fp8), which ends streaming - not another flag. Untested, and a download
decision Bryan has not made.

ComfyUI update available and deliberately NOT taken: v0.33.1 -> v0.33.2 is 7
commits, all in `comfy_api_nodes/` (ByteDance/FishAudio/credits) plus a frontend
bump 1.48.7 -> 1.49.6, which is 149 frontend commits including assets-mode model
sidebar becoming the default and `widgets_values_named` serialization. Nothing in
`model_management.py`, `comfy_aimdo`, `comfy/ldm/minimax/*` or `nodes.py`, so it
cannot fix the decode OOM and cannot make anything faster.

## 2026-08-17 11:55 -- both fixes verified live; toggle button moved; STATE ON EXIT

Bryan restarted 8188 himself (pid 15540, Process V2). All three things that
needed a live check now have one, on a two-model MiniMax load:
- reconcile: tier sums equal ComfyUI's `model_size()` exactly - TEModel_ 14.61 GB
  vs 14.61 GB, VideoVAE 4.85 vs 4.85, delta +0.000 both. The TE model's walk
  covered 13.33 GB, so 1.28 GB shows as `unknown` ("not located") instead of the
  panel quietly reporting a smaller model than everyone else does.
- off-thread probe: 30 consecutive samples, gaps 0.990-1.010 s, mean 1.000, ZERO
  over 1.2 s, while an 867 ms probe ran every 5 s. Before the change that would
  have stalled a tick every fifth second.
- probe metadata rides along: `probe_ms` 867, `probe_age` 0.07, `measured` true,
  `exact` false (sampled granularity, by design).

Reading at that moment: file 18.01 | unknown 1.28 | pagefile 0.11 | ram 0.06 |
gpu 0.00 GB, with the in-use TE model rendered `streamed` rather than a false
zero in the GPU column.

**Toggle button moved, and this was a UX complaint not a bug:** the floating
"MEM X-RAY" button sat at bottom-right on top of ComfyUI's own canvas toolbar
(select / fit / zoom). Now: on any build where `app.extensionManager
.registerSidebarTab` exists, the floating button is NOT created at all - the left
sidebar tab, Alt+M and the command palette are the ways in. Only sidebar-less
builds get a button, and it is at top-right (`top: 8px`). Do not "restore" the
bottom-right button; it was removed on purpose. README Step 5 rewritten to match.

**STATE ON EXIT (2026-08-17 11:55).** ComfyUI 8188 up, pid 15540, models loaded,
sampling normally. 8189 was NOT restarted this session, so it is still running
the pre-placement Python - restart it before trusting its panel. Nothing is
recording (`analysis/collect.py` not running).

NOT COMMITTED. Working tree carries this session's work plus the older
registry-prep edits: `memxray/placement.py` (new, untracked), `memxray/winmem.py`
(+residency section), `memxray/sampler.py` (one call in `_models`),
`web/js/memxray.js` (tier vocabulary, Models grid, Tiers model bars, Details
relabels, button placement), `README.md`, `HANDOFF.md`. Repo still private,
nothing published, per Bryan's standing instruction.

**The one thing still unverified: nobody has looked at the panel in a browser.**
py_compile, node --check, the probe scratch tests and the live JSON all pass, but
no view has been rendered since the redesign. First job next session: Ctrl+F5 on
8188, then check the Tiers model bars, the Models grid (columns aligned, textures
and chips visible, sentences readable), and that the sidebar tab is actually
present now that the floating button is gone on this build. Screenshot before
believing any of it.

## 2026-08-18 18:50 -- browser check done; Models grid clipping fixed

First browser render since the redesign, via Playwright on 8188 (live, models
loaded, a run in flight during part of the check). Verdict: everything from the
2026-08-17 list passes -- sidebar tab present, Tiers model bars + chips +
textures render, Headroom and Details draw, zero memxray console errors, verdict
line and hero number tracked a live run. The two memxray-root instances in the
DOM are correct: sidebar tab + the always-created hidden floating panel that
Alt+M and the command palette open (comment at the panel-creation site says so).

One defect found and fixed, in `web/js/memxray.js` only:
- Models grid: the full-width rows (placement bar + sentence) spanned the
  grid's scrollable width, so at rest they cut mid-sentence and only horizontal
  scroll revealed the rest. Fix is three parts, all needed:
  (1) `.memxray-grid-span` is now `position: sticky; left: 0` with
      `width: var(--mx-grid-vw)`, fed by a ResizeObserver on
      `.memxray-grid-scroll` (CSS cannot name the scrollport's width).
  (2) `.memxray-grid` min-width changed 330px -> `max-content`: with the fixed
      min-width the tracks (400px) overflowed the grid's own box (330px), which
      gave the sticky rows a too-narrow containing block and silently capped
      their slide -- sticky pinned only partway. Do not put a px back.
  (3) grid header labels now break before "(" via a `white-space: pre-line`
      span, because max-content measures text unwrapped and one-line headers
      ballooned the grid 400 -> 633px of scroll. Now 465px.
  Verified in-browser at rest and at full scrollLeft: bar + sentence stay
  pinned and fully readable, all four columns reachable by scroll.

STILL NOT COMMITTED -- this rides on top of the same uncommitted working tree
as before. Nothing else touched.

## 2026-08-18 19:15 -- placement-tier colors reassigned (Bryan's call)

Bryan rejected the cyan hue-ramp on the placement bars ("two different shades
of blue"). New vocabulary, his spec: red = fastest (GPU), orange = fast (RAM),
yellow = slow (disk), purple "not loaded (file)" unchanged. New COLORS entries
tierGpu/tierRam/tierPf feed --mx-tier-* vars; only the .memxray-tier-gpu/ram/pf
rules moved to them. The resident cyans, --mx-evicted amber and --mx-mapped
purple keep their meanings everywhere else (hero strip, VRAM bars, verdict,
warn-state numbers). Textures on the bars kept. Verified in-browser, Models +
Tiers views. Do not "unify" the tier bars back onto the series palette.

## 2026-08-18 19:55 -- streamed working window now a tier; bars were lying during runs

Bryan caught it live: mid-run the MiniMaxH3 bar sat ~100% purple "not loaded"
while the GPU crunched at 100% with 9 GB on the card. Root cause: the INT8
streamer stages layers into scratch VRAM buffers that are never module
parameters, so the page walk cannot see them - placement.gpu was 1.2 MB
(exactly the force-pre-loaded small weights) while ComfyUI's own `on_device`
said 9.0 GB. Worse, the old "streamed" fallback was gated on `place.gpu <= 0`,
so those pre-loaded 1.2 MB disabled it.

Fix (web/js/memxray.js only, no backend change):
- New STAGED_TIER "streamed (GPU)", chip S, red with horizontal stripes. NOT in
  TIER_DEFS (the backend placement dict has no such key and the grid gets no
  new column); BAR_TIERS = gpu, staged, ram, pagefile, file, unknown feeds the
  bars and the legend.
- placementOf(): for the currently_used model, staged = on_device - walked gpu,
  clamped to `file` and taken out of it (that is where the walk counted those
  bytes), gated above max(32 MB, 4x granularity) so buffer noise on a
  conventionally-loaded model never fabricates a segment. Tier sum unchanged,
  so the model_size() reconcile still holds.
- Models grid: GPU cell shows parked+staged in the streamed style; totals row
  includes it. Sentence leads with "X streamed onto <gpu> as the current run's
  working window"; "parked on" now distinguishes the resident kind. The Tiers
  "shows nothing in VRAM" footnote only fires when staged is ALSO zero.
Verified live right after a run: VideoVAE bar went red-striped "S streamed
(GPU) 4.62 GB - 95%", matching its on_device. Note staged follows
currently_used + on_device, so it persists after the run until ComfyUI unloads
or another model takes over - that is ComfyUI's accounting, not staleness.

STILL NOT COMMITTED - same working tree as the two entries above.

## 2026-08-18 20:12 -- letter chips removed from bar segments (Bryan's call)

Bars now carry color + texture + the size text only; the G/S/R/D/F letters live
in the legend and the Models-grid column headers alone. .memxray-seg-chip CSS
deleted with its last user. Do not put the letters back on segments.

## 2026-08-18 20:30 -- streamed tier merged back into the GPU tier (Bryan's call)

Bryan: on the card is on the card - one red segment, no separate "streamed
(GPU)" tier. placementOf() now folds the measured working window straight into
`gpu` (and still takes it out of `file`), keeping the amount in
`place.streamedPart` - detail only. It surfaces in: the GPU segment's tooltip,
the per-model sentence ("N on <gpu> - M of that is the streamer's working
window, refilled from the model file every step"), and the grid GPU cell's
italic streamed styling + tooltip. STAGED_TIER, BAR_TIERS and the striped CSS
are gone. Verified live: VideoVAE bar is one solid red "fastest (GPU) 4.63 GB -
95%" with the split in the sentence below it.

## 2026-08-18 20:45 -- tier labels flipped to place-first (Bryan's call)

"GPU (fastest)", "RAM (fast)", "disk (slow)", "file (not loaded)"; "not
measured" unchanged. Place leads, speed is the parenthetical - the reverse of
the original speed-first wording; the TIER_DEFS comment block still argues for
speed-first, ignore it on this point. Bars, legend and grid headers all pull
from TIER_DEFS.label so this was a four-string change.

## 2026-08-20 19:05 -- live H3 run: placement is right, the VRAM card is dead data

Checked the panel against a live MiniMax-H3 reference-video + reference-image
job (prompt 6e53e214, ComfyUI 0.33.2, pid 22872, launched `--fast-disk
--disable-pinned-memory`, with comfy-aimdo 0.4.13 and comfy-kitchen 0.2.31
loaded). 40 samples over 134 s, kept in the session scratchpad, each one paired
with an `nvidia-smi` read and a queue read.

**Placement is correct - do not "fix" the near-zero placement.gpu.** MiniMaxH3
reports `on_device` 3.0-3.22 GB while the walk finds `placement.gpu` ~1.2 MB,
because under `--fast-disk` the weights are not registered CUDA params for
`_storage_ranges` to count. `placementOf()` (web/js/memxray.js:895) folds
`on_device - gpu` back into the GPU tier for the running model, so the panel
shows ~3.2 GB on card + ~20.9 GB streamed from file. That is the truth for a
streamed run. Verdict held `warn` throughout; `paged_out` 20.28 GB minus
`untouched_commit` 17.97 GB = 2.31 GB = the whole system pagefile in use.
The arithmetic checks out.

**The GPU list is frozen fake data on this install.** `comfy_probe.torch_devices()`
trusts `torch.cuda.mem_get_info`, and on this stack that call returns a
constant: across all 40 samples `gpu0.free` was bit-identical at 393216000
(exactly 375.0 MiB) and `gpu1.free` at 6485434367 - which is not even page
aligned (`% 4096 == 4095`), something a real driver query never returns. The
cache itself is live: `torch_allocated` moved 1531 -> 1934 MiB over the same
samples. So the numbers are stale by patching, not by throttling.
Consequences, all visible in the panel:
- cuda:1 is reported as an RTX 3090 at 17.96 GB used. The driver had that card
  at 245 MiB used, idle, for every one of the 40 samples.
- cuda:0 is reported at 15.55 GB used against a driver figure oscillating
  15361-15532 MiB - roughly 400 MiB high and never moving.
- The RTX 5060 Ti, holding a steady ~12.2 GB, does not exist as far as the
  panel is concerned: torch only enumerates two devices here.
- The header's VRAM total (memxray.js:1079) sums those two cards, so ~18 GB of
  its "used" is fiction.
ComfyUI's own `/system_stats` returns the identical bogus `vram_free` for
cuda:1, so MemXray is faithfully echoing torch. It is still the panel that
presents it as a live reading.

**The fix is already half-written and unwired.** `memxray/nvml.py` enumerates
all three cards with driver-level per-process VRAM and is imported by
`analysis/collect.py` alone - nothing in `sampler.py`, `routes.py` or the panel
ever calls it. Put `nvml.devices()` in the `/memxray/stats` payload, drive the
GPU rows off the driver, and keep `torch_allocated`/`torch_reserved` as the
per-process overlay on top.

Also still missing, and it is the constraint that actually bites this box:
commit. During this run `commit_total` sat at 82.8-83.2 GB of a 95.88 GB limit
(~87%) and the panel says nothing about it.

## 2026-08-20 19:40 -- NVML wired into the live panel; ComfyUI update vetted

Bryan's H3 reference-video + reference-image job finished clean before any of
this landed (prompt 6e53e214, 1120.6 s, HD output
`videos\minimax-h3\t2v-ref\mmh3-t2v-ref-rife-hd__00069-audio.mp4`).

**Edits made, NOT yet verified live - they need a ComfyUI restart.**
- `comfy_probe.torch_devices()` now also returns `uuid` (from
  `torch.cuda.get_device_properties().uuid`, present on torch 2.13) and its
  docstring records that its total/used/free are NOT to be rendered as truth.
- `nvml.merged_devices(torch_devs)` is new: one row per DRIVER card, pairing
  each to a torch index by UUID first and by (name, total within 2 MB) second,
  never by position - CUDA_VISIBLE_DEVICES and CUDA_DEVICE_ORDER both renumber,
  and on this box torch's cuda:1 is the driver's card 2. Driver figures win for
  total/used/free; torch keeps `torch_allocated`/`torch_reserved`, and its
  rejected claim rides along as `torch_used`/`torch_free`/`torch_disagrees` so
  the panel can name the disagreement instead of silently picking a side.
- `sampler.py` feeds `models["gpus"]` from that, publishes `nvml` /
  `nvml_error` in `static`, and calls `nvml.shutdown()` on stop.
- Panel: GPU rows are driver-sourced, a card CUDA hides is rendered and labelled
  "(hidden)" with its used bytes coloured as somebody else's, `gpuNameFor()`
  matches on `torch_index` instead of array position (positional lookup breaks
  the moment a driver-only card is in the list), and the Models-view GPU
  capacity counts only torch-visible cards while the Tiers VRAM lane counts the
  whole box. `proc_used` splits ComfyUI's own share when NVML reports it, which
  on Windows it never does.

Verified without a restart: `merged_devices` against live NVML in a separate
venv process, both matching paths, pairing torch1 -> driver card 2 (the 3090)
and surfacing the 5060 Ti as visible_to_torch false. Still unverified: the
`/memxray/stats` payload and the rendered panel.

**The pending ComfyUI update is safe for this workflow.** Install is v0.33.2 on
a detached head, diverged: master +36, this branch +9. Checked every file in
those 36 that touches the H3 path. `MiniMaxH3ReferenceToVideo` keeps its schema
(only `_encode_ref_audio` moved to module scope); `MiniMaxH3AddGuide` is new;
`comfy/sd.py`'s new MiniMax branch lives inside `elif "decoder.22.bias" in sd:`
and `minimax_h3_video_vae_fp16.safetensors` has 24 latent channels and no such
key, so it keeps taking the dedicated H3 branch; the new over-long-prompt
ValueError in `text_encoders/minimax.py` is nowhere near a 619-char prompt;
`model_prefetch.py`'s CUDA-graph reset fix helps the `--fast-disk` path.
`comfy/model_management.py` is untouched, so nothing MemXray probes moves.
Commit 5ab2f7a2 forces `CUDA_VISIBLE_DEVICES=0` on Windows multi-GPU ONLY when
none of --cuda-device/--default-device/CUDA_VISIBLE_DEVICES is set; Comfy
Desktop sets `1,0` for this instance and `--disable-pinned-memory` is already
in the launch args, so it does not fire. Update via the Desktop app - a git
pull on a diverged detached head is the wrong tool.

**Local-LLM review of the new nvml code: 0 of 5 findings real.** The lanllm
plugin's judgment profiles leave Qwen thinking enabled and both lens calls
returned empty (whole budget in `reasoning_content`); a hand-rolled retry with
`enable_thinking:false` worked (3251 in / 363 out on .70). Of the five findings,
three claimed `int()` would crash on non-numeric strings that this code's own
producer cannot emit, one claimed a None/int sort-key TypeError that the `or 0`
already prevents, and the last claimed a NaN in the frontend where `num()`
coerces to 0. Do not re-run that review expecting different odds.

## 2026-08-20 19:55 -- STATE ON EXIT: edits on disk, unverified, nothing committed

Session ended before the restart. ComfyUI on 8188 was down at exit (Bryan was
updating through the Desktop app), so the NVML work has never run inside
ComfyUI.

Working tree, all UNCOMMITTED on `main` (last commit still 709d036):
- `memxray/comfy_probe.py`  +25/-3   uuid field, docstring on why torch's VRAM lies
- `memxray/nvml.py`         +173     merged_devices() and its two helpers
- `memxray/sampler.py`      +15/-3   wiring, static nvml flags, shutdown
- `web/js/memxray.js`       +106/-18 driver-sourced GPU rows, hidden-card labels
- `HANDOFF.md`                       this file
Nothing else touched. No commit was made because none was asked for; the branch
is `main`, so branch before committing if that is the call.

**First thing the next session should do, in this order:**
1. `curl http://127.0.0.1:8188/memxray/stats` and check `static.nvml` is true
   with no `nvml_error`, then that `now.models.gpus` has THREE rows: cuda:0
   5070 Ti, cuda:1 3090 (`torch_index` 1, `nvml_index` 2), and a 5060 Ti with
   `visible_to_torch: false`. The pre-change payload had two rows and claimed
   the idle 3090 was 18 GB used - that is the bug being fixed.
2. Confirm `torch_disagrees` is true on the cards where torch's frozen
   `mem_get_info` still contradicts the driver. If it is FALSE everywhere,
   check whether the update fixed `mem_get_info` upstream before assuming the
   merge is broken.
3. Open the panel (browser refresh is enough for the JS) and look at the Tiers
   VRAM lane: three tracks, the 5060 Ti one labelled "(hidden)" and coloured as
   other-apps, and the Models-view GPU capacity counting only the two visible
   cards.
4. Only then trust any of it. Everything above is reasoned and unit-exercised
   against live NVML in a separate process; none of it has been seen in the
   panel.

Also worth a look on the first post-update boot: the ComfyUI log now carries a
startup-warning block for Windows multi-GPU (commit 5ab2f7a2). It should NOT
fire on this instance - `CUDA_VISIBLE_DEVICES=1,0` is set by Comfy Desktop and
`--disable-pinned-memory` is in the launch args. If it does fire, the env var
did not survive the update and ComfyUI is down to GPU 0 only.

Scratch data from this session (live samples, the diff dumps, the review) is in
the session scratchpad and will not survive; the numbers that mattered are
already quoted in the 19:05 and 19:40 entries.

## 2026-08-20 19:50 -- 0.33.3 did NOT make anything faster; 64 GB RAM is deliberate

Bryan asked whether the ComfyUI update caused a job to run "twice as fast".
It did not. Same pipeline, same box, 50 min apart:

| run | s/it | sampling | total |
|---|---|---|---|
| `6e53e214` pre-update, 18:38-18:57 | 32.57-33.12 | 13:34 | 1120.6 s, 247 frames |
| post-update, sampler start 19:30:04 | 30.3-30.6 | - | in flight |

6-7%, inside this box's drift. The update was **0.33.2 -> 0.33.3**, not 0.33.1
-> 0.33.2 (0.33.2 was already installed; see the 19:40 entry).

**s/it is not a speed metric on this box - it is a workload metric.** Every
25-step run on 8188 today, from `comfyui_8188.prev2.log`: 68.53, 33.12, 22.82,
22.53, 20.35, 16.28, 15.93, 12.84, 11.94, 10.37, 7.34, 6.52, 6.07. Same step
count throughout; frame count and resolution set the cost. Any future "is it
faster" claim needs the same prompt re-queued, not a different job compared.
Consider recording both halves of such a pair in `bench_runs`.

**RAM at 64 GB is Bryan's decision, not a fault.** `total RAM 65409 MB` now vs
`98177 MB` in the 08-12/08-13 logs: the extra 32 GB was causing bluescreens and
he pulled it. **The 2026-08-17 14:00 entry's "RAM went 64 -> 96 GB" and its
127.9 GB commit limit are dead** - the real figures are 63.87 GiB phys and a
95.88 GiB commit limit. Do not diagnose the missing 32 GB, and do not plan
anything that needs 96 GB of headroom: Sequential ReadAhead now logs
`warm budget 16.78 GiB` against the 19.53 GiB UNET, so the model never warms
whole. The 16 GB VRAM ceiling on the 5070 Ti remains the only binding limit,
and a quant that fits in 16 GB remains the only large lever.

**Handoff items 1 and 2 from the 19:55 entry are DONE - verified live on 8188
(pid 35416).** `static.nvml` true, `nvml_error` null. `now.models.gpus` has the
three expected rows: 5070 Ti (torch 0 / nvml 0, 100% util, 83 C), 3090
(torch 1 / **nvml 2**, idle, 0% util), 5060 Ti with `visible_to_torch: false`.
`torch_disagrees` is true on both visible cards, so 0.33.3 did not fix torch's
frozen `mem_get_info` upstream - the merge is doing its job. Items 3 and 4, the
rendered panel, are still unwitnessed.

Also new and not from this repo: a fourth Desktop instance **"Bob's AI
Playground", port 8190, on the 5060 Ti, torch 2.12.1**, launched 19:30 with H3
loaded (`installations.json` id `inst-1787107014486`). Its queue was empty
during the run above, so it did not contend. Note it before attributing any
future RAM or disk pressure on this box to 8188 alone.

**Final number for the pair above (run finished 19:44:09).** Post-update run
`a9365e60` = **943 s (00:15:43), 247 frames** - the identical frame count to
`6e53e214`'s 247, so this is a clean matched pair, not two different jobs:

| | pre-update `6e53e214` | post-update `a9365e60` |
|---|---|---|
| s/it | 32.57-33.12 | 30.3-30.6 |
| sampling | 814 s | ~761 s |
| got prompt -> sampler start | 225 s | 98 s |
| total | 1120.6 s | 943.0 s |

**1.19x end-to-end, and most of it is not sampling.** Of the 177 s saved, ~53 s
is sampling and ~127 s is the load/encode phase before the sampler starts. That
phase is standby-cache dependent - the 2026-08-14 bench data in
`analysis/memxray_runs.db` already showed cache state alone moving this model's
end-to-end by 40% (882.7 s -> 528.9 s, bench_runs 2 vs 5). So attribute the
load-phase half to cache, not to 0.33.3, unless a cold-cache pair says
otherwise. Nothing here supports 2x.

## 2026-08-20 20:25 -- the cold pair arrived: 0.33.3 owns the 1.20x, cache does not

**Retracting the "attribute the load-phase half to cache" line above.** A third
run of the same pipeline settled it. All three are now rows 10-12 of
`bench_runs`; all three produced 247 frames, so they are matched work.

| run | s/it | sampling | load phase | total |
|---|---|---|---|---|
| `6e53e214` 18:38, pre-0.33.3 | 32.57 | 814 s | 225 s | 1120.6 s |
| `a9365e60` 19:28, post, warm | 30.49 | 762 s | 98 s | 943.0 s |
| `24a3bee8` 19:56, post, colder | 30.50 | 762 s | 92 s | 931.0 s |

Run 3 is the control. Its text encoder was read **off disk at 0.52 GiB/s**
where run 2 got the same 14.61 GiB **from standby at 2.82 GiB/s**, and its UNET
warm budget was smaller (15.81 vs 16.78 of 19.53 GiB). It still landed 30.50
s/it against 30.49, with a *shorter* load phase. **Sampling on this model is
cache-insensitive, and so is the load phase.** The cache-state effect from
2026-08-14 (bench_runs 2 vs 5) does not reproduce on this pipeline - do not
reach for it as an explanation again without a matched pair.

So 0.33.3 is worth **1.20x end-to-end**: ~7% on sampling plus ~130 s off the
pre-sampler phase. The load-phase gain localises to one stage - VideoVAE-prepared
-> AudioVAE-prepared was **151 s pre-update and ~48 s after, both post runs** -
which is consistent with the `model_prefetch.py` CUDA-graph reset fix on the
`--fast-disk` path that the 19:40 entry vetted. `--fast-disk` is therefore NOT
"within noise" as the 2026-08-17 14:00 entry claims; that line is dead, keep the
flag.

**Attention backend is not a confound.** Bryan moved off SageAttention to the
graph's `ModelAttentionBackend` = "comfy kitchen attention" around 2026-08-17,
days before any of this. Both launches log `Using pytorch attention` globally
with comfy-kitchen 0.2.31, identical either side of the update. SageAttention is
still installed (SeedVR2 reports it available), just not routed through.

**Pinned memory stays disabled, and more RAM was never the question.** The
2026-08-17 failure was a *device* allocation - `hostbuf_read_file_slice: device
copy failed result=2` on the 16 GB 5070 Ti during VideoVAE tiled decode - and it
happened with 96 GB installed. Host RAM is irrelevant to it. One thing did
change though: that A/B ran on 0.33.1, and 0.33.3 touched the same host-buffer
prefetch path, so a retest is now defensible where it was not before. Price is
4% sampling upside against a run that dies ~13 min in. Not taken.

No restart is needed for the NVML work - it is already live from the 19:24
launch. Only the panel JS is stale, and a Ctrl+F5 covers it.

## 2026-08-20 20:45 -- history rewritten: EVERY COMMIT SHA CHANGED

`analysis/memxray_runs.db` is no longer tracked (it is in `.gitignore`) and its
four blobs were purged from all 13 commits with `git filter-branch`, then
force-pushed to `main` and `feat/nvml-driver-gpu-rows`. `.git` went 47 MB ->
5.2 MB. Content at HEAD is byte-identical to before the rewrite; no commit was
pruned and no message changed.

**Any SHA written in an earlier entry of this file is dead.** `709d036` is now
`e4a6b84`, HEAD is `1e3be0f`. Do not try to `git show` an old one - map by
commit subject instead.

The DB still lives at `analysis/memxray_runs.db`, 20 tables / 552,354 rows,
`bench_runs` through row 12. **It is now backup-free inside git** - nothing
restores it if it is deleted, so do not `git clean -x` this worktree.
Pre-rewrite safety copies, outside the repo and not tracked:
`E:\vs_code_projects\ComfyUI-MemXray-git-backup-20260820\`
- `repo-before-rewrite.bundle` (17.8 MB) - clonable, holds the old SHAs
- `db-blobs/` - the four historical DB versions; each verified a strict subset
  of the current file, so they are redundant and can be deleted for space

Watch for one trap that already bit once tonight: while the DB was tracked on
`main` but untracked on the feature branch, a `checkout` + `--ff-only` merge
DELETED the working-tree file (it showed up only as `delete mode 100644` in the
merge output). It was recovered from the blob. Now that nothing tracks it, that
particular failure cannot recur, but it is why the file needs an out-of-repo
copy if it ever matters again.
