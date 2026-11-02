# Designed diagnostics and post-generation comparison

The suite contains subtitle, isolated local costume, keyed state propagation, global illumination, reference invalidation, and inflight consecutive-edit cases. These are designed mechanism diagnostics, not a held-out benchmark. Each method shares declared inputs and hard constraints. Global illumination affects every video and provides a no-video-saving case.

Run the synthetic suite first (no playable videos or GPU measurements):

```bash
python -m unittest discover -s tests -q
python research/run_suite.py fixtures/diagnostics_v7 --output runs/diagnostics-v7-mock-01
```

Run real H3 cases sequentially, after existing tasks have finished:

```bash
python -u research/run_suite.py fixtures/diagnostics_v7 \
  --cases subtitle local_costume global_light \
  --remote --server http://127.0.0.1:8188 \
  --output runs/diagnostics-v7-real-01
```

Fresh suite output directories are required. Each condition generates its own initial project. The suite stops if a condition fails or pauses; inspect its report. It does not overwrite existing run directories. The reference case is explicitly skipped because the H3 adapter has no reference loader; planning and refusal behavior are tested separately.

Compare the four completed post-generation conditions for one case:

```bash
python research/compare_post.py \
  runs/diagnostics-v7-real-01/local_costume/full \
  runs/diagnostics-v7-real-01/local_costume/suffix_safe \
  runs/diagnostics-v7-real-01/local_costume/dag \
  runs/diagnostics-v7-real-01/local_costume/dair \
  --decode --output runs/local-costume-comparison.json \
  --markdown runs/local-costume-comparison.md
```

`--decode` requires FFmpeg and checks full decoded video streams, excluding audio. Omitting it still checks both artifact archives and actual reuse. Missing server spans remain null. Counts and wall-clock times are not GPU duration or monetary cost.

Freeze all four methods in a fresh directory outside the source:

```bash
python research/freeze_group.py runs/diagnostics-v7-real-01/local_costume \
  --output runs/frozen/local-costume --decode
python -c 'from research.freeze_case import verify; print(verify("runs/frozen/local-costume")["inventory_hash"])'
```

Stop writers and checkpoint SQLite before freezing. The freezer rejects WAL/SHM sidecars, detects changed files during copying, and refuses overwrite.

For consecutive edits, use a separate suite output:

```bash
python -u research/run_suite.py fixtures/diagnostics_v7 \
  --cases inflight_costume --remote --server http://127.0.0.1:8188 \
  --output runs/diagnostics-v7-inflight-real-01
```

Use compare_baselines.py for dynamic reports and compare_post.py for completed-then-edit reports. New fixtures can be generated with make_suite.py using a supplied manifest and a fresh output path. Real quality assessment, multiple independent stories/seeds, randomized method order, stronger incremental baselines, and GPU instrumentation remain separate experiments. Raw videos, databases, receipts, private reports and paper drafts are not part of this code change.
