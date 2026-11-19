# VideoFlow natural evidence timing

Each pair runs gated and ungated attribute Worklist from one common Rev.1.
The single Rev.2 edit recolors the anchor. The first arm acquires VLM
evidence concurrently with rendering. The second arm replays that exact
evidence at the recorded offset from edit start. Both arms poll and apply
the evidence as soon as they observe it. Source panels are prepared once
before timing. There is no artificial post-anchor wait or fault injection.

| Story | Prespecified seeds | Required generated references |
| --- | --- | --- |
| Teapot-Chain | 92021, 92022 | S3 → S4 → S6 |
| Toolbox-Fanout | 92031, 92032 | S3 → S4 and S3 → S6 |

The live arm alternates by seed within each story. Use an idle ComfyUI queue
throughout the run. The report records when evidence became available, when
it was applied, when the anchor committed, the first immutable publication,
missing and excess updates, and when rendering drained. An incomplete
immutable revision has no complete-publication time. The later drain is
recorded separately. Report all prespecified seeds, including early evidence,
provider failures, and evidence that failed to select a target. Retain the
per-arm anchor provider observations when interpreting timing and cache effects.

## Run on the GPU node

Requires Python 3.10+, FFmpeg and FFprobe, ComfyUI with the H3 workflow models,
and Ollama with `qwen2.5vl:7b-ctx8k`.

```bash
export PATH="$HOME/apps/videoflow-ffmpeg/ffmpeg-7.0.2-amd64-static:$PATH"
out="runs/egdr-natural-gate-$(date +%Y%m%d-%H%M%S)"
python3 -u VideoFlow_Natural_Gate/run_natural.py --output "$out"
```

Send the top-level `report.json` after the run. The per-arm logs and SQLite
databases stay in the run directory for audit. To reuse a previous CrossStory
Gate v2 source run, add `--reuse-source-from /path/to/run`. This requires the
archived source video URLs to remain readable on the same ComfyUI backend.
The harness verifies the source manifest and archived video hashes on reuse.
Use `--prepare-only` with a fresh output directory to validate all manifests
without contacting the model servers.

## Cache-primed anchor experiment

Add `--prime-anchor-cache` to run the same four paired seeds with an edited
S1 cache prime immediately before each arm. Priming runs one anchor render
in a separate SQLite database. The timed arm starts from the common Rev.1
and submits the same H3 graph with a distinct output prefix. The script
checks that the upstream graphs are identical. VLM acquisition and replay
retain their natural edit-relative timing.

The report lists priming separately from timed provider acceptances. It
records whether ComfyUI reports node 10 (the H3 sampler) cached for the
timed anchor. A missing history is reported as unknown. Keep all runs in
the report, including cache misses. Compare evidence and anchor timings,
first published target coverage, and whether late repairs finished after
publication. Cache priming is an experimental setup. It measures publication
behavior conditional on reuse and does not measure how common reuse is.

## Both arms gated: overlap versus evidence-first

Use `--compare-evidence-first` to test if overlapping the anchor render
with evidence acquisition helps when both policies already protect the
first publication. Both arms use the same planner, publication gate, and
evidence. The serial policy waits until all evidence is applied before
dispatching any render. Once it starts, it has the same in-flight cap as
the overlap policy. Arm order alternates across seeds.

For each seed the VLM is called once, before either timed arm starts.
Both arms replay its actual acquisition duration from edit start, so an
arm's GPU execution cannot shift the other arm's evidence arrival. This
controls the evidence delay. It does not measure contention during live
VLM and renderer execution. Keep seed-level latency, first publication
coverage, provider acceptances, and anchor cache observations. Report the
paired latency difference for complete first publications; retain incomplete
or failed runs separately. Record cache priming as a separate condition
when using `--prime-anchor-cache`; use a fresh output directory for each.
