# Evidence-Guided Dependency Repair

## Execution contract

The caller specifies the edit attribute, anchor shot and admissible scope. The pilot examines all downstream shots with a compatible prompt binding. The two-input Checklist union supplies candidates. The vision probe checks four original frames at 10%, 35%, 60%, and 85% of duration. It rechecks each positive for object identity, part, and position in a single panel. EGDR adds verified visual positives to the Checklist union. The optional conservative arm also adds unresolved visual judgments. These verdicts are categorical and have no probability calibration. Malformed evidence blocks the paired run.

For each selected consumer, the event carries the committed source input fingerprint and a digest of the evidence bundle. The runtime validates the current project revision, source input fingerprint, forward ordering, and compatible prompt bindings. SQLite persists the new keyed state edges, input mutation, and invalidations together. DAIR computes closure over both original and added edges. Original reference and order edges remain in force. The normal worker records input provenance and checks it at publication. The runtime validates the evidence record's shape and source binding. The research harness archives the bundle and raw replies needed to audit its content.

The first implementation persists accepted state edges, including candidate decisions from the text baseline. A false-positive repair can therefore affect later edits. The pilot is restricted to an authored binding and forward state propagation. It uses exhaustive candidates within that scope. It has no automatic edit parser, learned retrieval, calibration, edge retraction or cross-frame evidence fusion. Visual co-occurrence cannot establish that a downstream renderer consumes an upstream file. Generated-reference edges must come from explicit workflow bindings. The runtime guarantee covers revision and provenance, while semantic completeness still depends on the supplied graph and the quality of inferred state edges.

## E1 and E4 on a CPU

Run the targeted tests and replay the six held-out edits with the frozen verification report.

```bash
python -m unittest discover -s tests -p test_egdr_runtime.py -v
python research/dependency_probe/replay_egdr_runtime.py /path/to/verification-report.json > egdr-replay.json
```

The replay uses the frozen model decisions and a synthetic anchor excluded from scoring. The provider emits prompt bytes rather than playable video. It validates the correspondence between the selected and published shot lineage. The integration tests also exercise a declared generated-reference edge, an order-only edge, source validation, atomic rejection, event replay, restart, and a late obsolete completion.

## E3 on the H3 worker

Use an idle H3/ComfyUI instance, Ollama with the configured models, FFmpeg and FFprobe. First render the common starting project once.

```bash
python -u research/dependency_probe/run_egdr_runtime.py \
  --source-only --seed 88001 --output runs/egdr-press-source-01
```

Review the four clips and contact sheets in `source/`. Before inspecting any model decisions, record which shots actually show the target part. For the original two-input Checklist baseline, save independent observations in a JSON object keyed by shot ID. Use visible descriptions that do not say whether a shot should be regenerated. If observations are omitted, the two Checklist requests receive identical shot text, so this arm should be reported as text-only.

```bash
python -u research/dependency_probe/run_egdr_runtime.py \
  --source-run runs/egdr-press-source-01 --seed 88001 \
  --frame-notes /path/to/frame-notes.json \
  --output runs/egdr-press-paired-01
```

Every arm starts from a copy of the same published database and sees the same edit and seeds. The declared arm edits only the anchor. Checklist, raw union, and EGDR add state edges according to their respective selections. Full applies the same target-specific edit instruction to all shots. To include a conservative arm, pass `--policies declared checklist raw_union egdr egdr_conservative full`. Vary the order across later paired runs with `--policies` and log it before collecting outputs.

Keep the source SHA-256 records, provider history, SQLite ledgers, responses, and post-edit videos. The report records video count, intents, verified reuse, wall-clock publication latency, and provider execution spans when present. Model calls and combined probe wall time are in `evidence.json`. The probe is shared across arms for matching, so that wall time cannot be assigned to a single method. Provider spans include host work. GPU device time is unmeasured. Render timeouts leave the partial ledger and logs in place. Inspect them and use a new output directory for any explicitly recorded rerun.

Blind the method labels for post-edit assessment. Judge the intended change in each affected shot, preservation of excluded objects, and visible consistency of the edited attribute across the final sequence. Report misses and excess selections separately from post-render attribute failures. This pilot has one authored story and is for integration validation.

## Follow-up comparison

For E2, freeze story-level development, calibration, and test partitions before adjusting the verifier. Compare a single sample, four fixed samples, adaptive sampling at the same frame budget, and explicit multi-frame aggregation. Use the same source prompts and visibility criteria, and report error types separately for occlusion, brief visibility, and partial views. Account for frame count and model calls when comparing schedules. Keep the current stitch-selector example in development because it has already informed the design.

For the final E1 and E3, evaluate 4–5 or more independently authored stories, with test stories unseen in tuning. Pair methods within a source revision. Record per-story counts and the cost of inference plus rendering, and use blind output assessment. The CPU replay is not a new selection benchmark. The main efficacy claim requires results from these unseen stories. The H3 pilot tests one edit, so a real in-flight edit and restart with a repaired edge remain a separate E4 check.
