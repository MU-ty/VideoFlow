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

## Versioned evidence replay

Run this separate CPU experiment from the repository root.

```bash
mkdir -p runs
python3 research/dependency_probe/replay_versioned_egdr.py > runs/egdr-versioned-replay-v1.json
python3 -m unittest discover -s tests -p test_egdr_versioned_replay.py -v
```

The three traces use the archived Blender press control for source clip identities. They add a synthetic S5 reference consumer of S2. Evidence verdicts and arrival times are scripted. The trace uses a single authored edit target and attribute, an explicit candidate set, fixed service times, and two non-preemptive slots. The publication barrier waits for all current candidate verdicts and the planned inputs to be committed. An unknown verdict leaves the barrier in place. Each current candidate accepts at most one definitive verdict. Late responses for another edit or source digest are rejected. The replay does not call a VLM, generate videos, or exercise the production SQLite worker.

All policies share the same evidence and publication barrier. The waiting Worklist starts submitting only after the barrier has enough evidence. The online Worklist replans using the complete current accepted edge set and reuses matching input fingerprints. EGDR starts with the declared closure and expands from each newly confirmed consumer. Both online policies retain input-compatible work. The JSON includes each evidence decision, submission, completion and publication. Gold sets are used only for scoring the final published selection.

| Trace | Wait Worklist | Online Worklist | EGDR delta |
| --- | ---: | ---: | ---: |
| Late confirmation | 13 / 3 | 9 / 3 | 9 / 3 |
| Lookalike counterevidence | 14 / 3 | 8 / 3 | 8 / 3 |
| Superseded evidence | 11 / 3 | 10 / 4 | 10 / 4 |

Cells give simulated ticks from the last edit to publication / submission intents after the initial fixture. All nine policy runs publish the expected final shot set. In the superseded trace, both online policies spend one extra intent on an obsolete render. The online policies have identical event logs apart from their policy names. These cases demonstrate the trade-off of overlapping evidence acquisition with rendering. A comparative EGDR benefit would require evidence that improves the selection or arrives with lower verification cost on independent stories, or a broader execution experiment with a measured difference under the same evidence.

## Reusing visual evidence across edits

The experiment compares independent policies over a shared capture of fresh visual replies. All policies use the same source-only question and verify a positive before selecting a shot. The destination color is kept outside that question. The exact-source policy caches verified positives for the same story, shot, question, and ordered source panels. The witness policy also permits reuse after a source change if the single frame independently verified for identity, part, and position is still present with identical bytes. Its claim is existential visibility within the supplied frames. Negatives and unknowns are rechecked. The experiment never reuses a witness for a different shot or question.

A retrospective replay of the existing microscope and sewing-machine logs checks eight source sheet hashes. It replays the six original edits and two unchanged-source destination-color changes. The two added edits replay identical requests and recorded replies. They are not independent model observations. Across this schedule, fresh probing accounts for 55 calls, including format retries. Each reuse policy accounts for 47 calls with 4 cache hits and the same 2 misses, 4 excess selections, and 8 unresolved pairs. The archival verifier used whole sheets, so these data only test exact-source reuse. The repeated stitch-selector edit retains the original model's incorrect selection.

To capture the new boundary controls on the school server, use Ollama and FFmpeg. The script unpacks the repository's four-shot Blender control. It records paired responses for six conditions covering a destination-color change, a target switch, replacement of a nonwitness panel, replacement of a shot's source, and complete occlusion of a shot's sampled panels. It uses a separate single-frame verification call for new positive witnesses.

```bash
python3 -u research/dependency_probe/vision_evidence_reuse.py live \
  --server http://127.0.0.1:11434 --model qwen2.5vl:7b-ctx8k \
  --output runs/egdr-evidence-reuse-live-01
```

`responses.jsonl` stores actual detector and verifier replies. `report.json` scores each arm and reports the model calls each policy would require with those replies. The capture deliberately queries even requests a cache would skip, so the totals are counterfactual call counts. This protocol measures visibility and selection for the supplied panels. H3 rendering and the production runtime are outside the experiment.

## Two-story decoded-frame resampling

The frozen bundle contains six newly authored Blender clips. Station Intercom uses a
triangular badge to identify the red pushbutton's intercom. Weather Beacon uses three
dots to identify the yellow indicator's housing. S1 and S2 are positives and S3 is a
similar-looking device with a different marker in each story. Both stories use three
distinct four-second clips at 12 fps. Ground truth follows the rendered geometry and
is never sent to the model.

The experiment decodes each source video to 48 frames and uniformly samples four
frame indices from a specified trim window. The four scored trim windows were drawn
before any model evaluation with `random.Random(20261110).sample` from the 24 ordered
head/tail trim pairs in [0, 4] frames, excluding (0, 0). The remaining windows are
covered in the geometry-only sweep. Each matched control changes three sampled
indices. One control retains the original verified frame and the other retains a
different original frame. If the original probe has no verified frame, the controls
use a nominal index and exclude that probe from witness-survival counts.

Each resampling starts with the same original capture for its story and a fresh
copy of each policy's cache. Costs for the six alternatives exclude that shared
original capture, reported separately. The paired capture queries all inputs and
then replays three policies over the recorded replies. Selection scores and estimated
calls require model replies. The geometry-only sweep measures the chance to retain
a particular frame under this trim grid and does not measure editing-workload frequency.

```bash
python3 -u research/dependency_probe/vision_evidence_resampling.py \
  --server http://127.0.0.1:11434 --model qwen2.5vl:7b-ctx8k \
  --output runs/egdr-evidence-resampling-01
```

Use `--story station_intercom` or `--story weather_beacon` to capture separately.
Use `--prepare-only` to generate `sampling.json` and the decoded frames without an
Ollama instance. A live run writes `report.json` and `responses.jsonl`. The resampled
frame indices, source digests and selected witness digests are included for audit.

A malformed detector or single-frame verifier reply gets one format retry. Each attempt
is counted and archived. If both replies are invalid, the shot remains unresolved with
no reusable witness. The probe retains the validation failures in `invalid_replies`.
