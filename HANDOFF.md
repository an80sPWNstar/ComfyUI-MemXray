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
