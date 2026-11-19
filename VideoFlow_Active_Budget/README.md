# Equal-budget verification allocation

Every policy receives the first view of every candidate under an identical
structured prompt and model configuration. Remaining calls are allocated
between candidates. Each candidate's view order is fixed. The primary
comparison uses three calls per seed (two first views, one recheck). Also
report budgets 4, 5 and 6 when the four-view bank is available. Predictions
conservatively retain unresolved candidates. A positive verdict requires two
target observations and no linked distinct evidence. A negative verdict
requires linked distinct evidence from two views and no positive evidence.

- **Fixed** distributes calls in rotation, counterbalancing the first recheck
  across seeds. Both fixed starting shots are also reported as sensitivity
  controls.
- **Uncertainty** prioritizes unclear or conflicting evidence, with a penalty
  for candidates already checked more often.
- **Graph-aware** multiplies that score by the marginal number of affected
  shots using declared edges and currently confirmed candidates.
- **Graph-static** allocates calls by declared closure size and call count,
  without viewing earlier responses.

Each bank entry records source and input hashes, exact prompt, raw reply
and wall time. The evaluation exposes a bank entry only when that policy
spends a call. Actual bank calls are reported separately. This is an offline
allocation comparison. First publication, rerender count and wall time in a
live runtime require a separate execution experiment. Closure excess in
this report is a graph-level count.

## Development finding

On the eight archived 923 seeds, the two archived repeats agreed. At three
calls, fixed rotation selected an excess candidate in 5/8 seeds, and both
uncertainty arms did so in 2/8. The fixed S2-first sensitivity control also
had 2/8. At four calls all arms had 2/8. Every arm retained S3 on every seed
under conservative fallback. The replay motivates an independent test and
does not isolate an incremental benefit of the graph-aware score.

## Reuse the 925 sources for a quick diagnostic

From the repository checkout, with FFmpeg/ffprobe on PATH and the frozen
Qwen model available in Ollama:

```bash
src="$(find "$HOME/VideoFlow-workflow-pair/runs" -maxdepth 1 -type d -name 'egdr-strong-source-*' | sort | tail -n 1)"
test -n "$src" && test -f "$src/source-report.json" &&
python3 -u VideoFlow_Active_Budget/active_budget.py \
  --source-run "$src" \
  --protocol VideoFlow_Active_Budget/reused_925_protocol.json \
  --output "runs/egdr-active-925-$(date +%Y%m%d-%H%M%S)"
```

This includes all completed source seeds and separately reports the four
previously eligible seeds. A full bank has eight calls per seed. Archived
source clips are required. A ZIP of just the screening panels is insufficient.

## Independent holdout

The new cohort freezes eight seeds across four stories. For every story,
one seed assigns the reference descendants to S3 and one assigns them to S2.
The graph therefore does not always assign the larger closure to the target.

```bash
src="runs/egdr-active-source-926"
python3 -u VideoFlow_Active_Budget/generate_sources.py \
  --protocol VideoFlow_Active_Budget/fresh_926_protocol.json --output "$src"
```

The generator uses the existing H3/ComfyUI runtime and requires an idle queue.
`--prepare-only` validates manifests without rendering and should use a
different output directory. Keep all eight planned source records. Before
running the VLM, fill `$src/source-screen.csv` using the S1/S2/S3 contact sheets
in each block's `frozen/` folder. Record the count of panels (0–4) where the
specified marker and edited part are visible and can both be attributed to
the same object. S2 uses the distractor marker and S1/S3 use the target
marker. At least one readable linked panel per shot qualifies a seed.

```bash
src="runs/egdr-active-source-926"
python3 -u VideoFlow_Active_Budget/active_budget.py \
  --source-run "$src" --screen "$src/source-screen.csv" \
  --protocol VideoFlow_Active_Budget/fresh_926_protocol.json \
  --output "runs/egdr-active-holdout-926"
```

Keep the frozen protocol, source report, screening table and run report.
The collection can resume in the same output directory with the same
protocol, source, screen, and model identity. Invalid replies and request
failures consume their assigned budget and are recorded as unknown evidence.
Inspect paired records and source eligibility before interpreting the totals.
