# V8: attribute-aware baseline and independent-content development stories

## What this tests

`attr_dag` independently computes attribute-aware invalidation using a worklist. It reads the same declared attribute sets, keyed state edges and reference edges; it ignores pure order edges for invalidation. It imports only the Plan result type, not the DAIR selection function. All methods intentionally share schema validation, edit mutation, provider adapter and commit guards. This is a strong planner baseline under the current explicit graph model, not a separately implemented runtime, automatic graph extractor, full build-system reproduction or fingerprint-based early-cutoff implementation.

With complete explicit declarations and the current zero-risk objective, attr_dag and DAIR are expected to choose the same set. Report ties. More content/seed runs cannot establish a selection advantage when the algorithms are equivalent under the tested assumptions. Do not change labels or weaken the baseline to create a gap. The distinction between scheduling and deciding whether to rebuild is established in incremental build-system literature (Mokhov et al., Build Systems a la Carte, 2018, https://www.microsoft.com/en-us/research/publication/build-systems-la-carte/).

If this baseline ties throughout, the paper needs either a clearly differentiated dynamic-runtime contribution, measured under explicit fault/recovery ablations, or a supported broader problem formulation. A stronger novelty claim cannot follow merely from this benchmark. No new mechanism is assumed to outperform the baseline.

## Frozen first-stage matrix

Three newly authored stories: ceramics workshop/teapot, greenhouse/watering can, harbor/backpack. Their shot descriptions differ from the old rooftop story. They deliberately share six-video order topology, with S2→S4→S6 keyed state edges for prop.color. This probes content generalization, not graph-topology generalization, and remains a designed development set, not a blind held-out dataset.

Each story uses base seeds 42, 314, 2718; scene i receives base_seed+97*(i-1). Seeds match across methods. The edit changes a white object to blue at S2, propagating to S4/S6. The author-specified affected set is recorded before running. Someone other than the planner should review those declarations and visual criteria before ground-truth metrics are reported.

Five methods: Full, Suffix-safe, DAG, attr_dag, DAIR. Nine story/seed blocks, forty-five conditions. Method order is pseudorandomized once using RNG seed 20261103 and stored in plan.json. The plan and each manifest/edit are hashed. Do not modify fixtures or regenerate the plan after examining results without creating a new protocol version and retaining failures.

One condition means: complete and archive its own initial six videos, apply the edit, complete and archive the final assets. No reference-conditioned video input is used. Inflight and fault/recovery claims require separate protocols.

Planned counts per story/seed block: Full 12 total video submissions, Suffix-safe 11, DAG 11, attr_dag 9, DAIR 9. The forty-five-condition matrix therefore expects 468 total video submissions, including initial work. At the previously observed roughly 86–90 seconds/video this is approximately 11–12 hours before additional loading/queue/downtime; this is a planning estimate, not measured GPU time. The two-method single-story pilot expects 18 video submissions, approximately 26–30 minutes under the same assumption.

## Run in stages

1. Pull the code only after existing workers/runners finish. New source hashes mean new output directories; do not resume V7 runs with this source.
2. Keep the same ComfyUI cache/startup/model configuration. Warm the provider once before a measurement session, record warmup command/time separately, and exclude it from results. Do not warm only chosen methods. Record server restarts or competing workloads.
3. Run local tests and the synthetic matrix (no playable video or GPU evidence):

```bash
python -m unittest discover -s tests -q
python research/run_matrix.py fixtures/independent_v8/plan/plan.json --output runs/v8-mock-01
```

4. Pilot ceramics/seed42 with just DAIR and attr_dag:

```bash
python -u research/run_matrix.py fixtures/independent_v8/plan/plan.json \
  --stories ceramics --seeds 42 --methods dair attr_dag \
  --remote --server http://127.0.0.1:8188 --output runs/v8-pilot-01
python research/summarize_matrix.py runs/v8-pilot-01 --decode --output runs/v8-pilot-01-summary.json
```

Review complete S2/S4/S6 videos for clearly blue objects after editing and white objects before. Check all shots for malformed objects and motion. Check S1/S3/S5 retain original bytes. Record failures rather than discarding seeds. Decoded equality to Full is a fidelity-to-baseline check, not an independent quality score; the pilot only compares the two selected methods.

5. After the pilot review, run the full frozen matrix under the same stable settings in a new directory:

```bash
python -u research/run_matrix.py fixtures/independent_v8/plan/plan.json \
  --remote --server http://127.0.0.1:8188 --output runs/v8-matrix-01
python research/summarize_matrix.py runs/v8-matrix-01 --decode --output runs/v8-matrix-01-summary.json
```

`--decode` requires FFmpeg. Without it, archived-byte verification and reuse checks still run, but frame equality remains unknown. The runner writes matrix_status.json after each condition and stops on failure/pause. It records no fictitious quality or GPU metric.

To resume a remote matrix, use exactly the same command/filters/output with `--resume`. Source, plan and selections must remain unchanged. Completed conditions are inspected again without resubmitting their generation; unresolved jobs retain the underlying no-automatic-resubmit behavior. A synthetic provider cannot resume between processes; use a fresh mock directory.

## Analysis and stop criteria

Keep per-story paired results; seeds are repetitions nested within story, not nine independent stories or forty-five independent samples. Summarize count differences and client-time means/sample SD across seeds within each story. Three stories are a development-scale start; they do not by themselves establish broad external validity. If equivalent planners differ in selected counts, investigate an implementation/protocol error before claiming benefit.

Preserve prompt/model/seed/source data, failed attempts, provider histories and exact method order. Independent annotation and blinded quality review remain separate. No confidence interval or statistical significance is claimed by the summary tool. Avoid averaging this completed-then-edit matrix together with state-triggered inflight experiments.
