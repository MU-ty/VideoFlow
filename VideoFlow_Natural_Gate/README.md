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
