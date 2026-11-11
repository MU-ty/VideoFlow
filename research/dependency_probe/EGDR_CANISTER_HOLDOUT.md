# Diamond Canister held-out experiment

Fix the story, seed 90421, source eligibility, reference frame, arm order, and scoring rules before generation. Run one case with one paired seed. Archive the source eligibility decision and source digests before running the visual model or any edit arm. If the generated source fails eligibility, record the failure and stop. A documented infrastructure interruption can be retried with the same configuration in a new directory.

| Shot | Required pre-edit content | Expected final change |
| --- | --- | --- |
| S1 | Teal canister with its own white diamond and broad yellow lid, viewed from above the front | Entire visible lid becomes scarlet red |
| S2 | Similar teal canister with its own white circle and broad yellow lid | Preserve the circle, body, and lid |
| S3 | Diamond-marked canister with a broad yellow lid, seen from a three-quarter view | Entire visible lid becomes scarlet red |
| S4 | A single gray stone on a plain studio backdrop | Preserve the stone and backdrop |

S1 is the declared target. The property dependency from S1 to S3 is deliberately omitted from the authored graph. S2 is the hard negative, and S4 is the unrelated negative. Check all four clips. At frame 0 of each target shot, the full lid must be visible with its silhouette uncropped, its top and rim distinguishable, and its diamond visible on the same canister. Require a visible yellow lid and diamond together at three of the four checkpoints (10%, 35%, 60%, 85%), including 85%. Require the visible circle and yellow lid on S2 at three of those checkpoints including 85%, with no qualifying diamond-marked canister anywhere. S4 must contain a single stone with no other distinct object throughout.

## Execution

Use the same frozen Rev.1 source, shot seeds, H3 configuration and references across arms. Use frame 0 for both target references. Draw one polygon per target on that frame before VLM evidence collection, tracing the complete visible lid, including the rim, while excluding the body and marker. Apply the same recoloring transform to each target's polygon. The reference plan records source shot, zero-based frame index, source video SHA-256, decoded frame SHA-256, and edited reference PNG SHA-256. Only final target renders use these corrected references. The oracle labels are tied to the source video digests. The visual verifier receives the source panels and question.

Run Worklist, EGDR-Oracle, EGDR-VLM, and Full in that order. Start each arm from the frozen source. Acknowledge the blue-edit S1 submission, reopen SQLite, issue the red edit and submit the current S1. Deliver the stored source-only evidence to the appropriate EGDR arm after the red-edit submission. Use the existing two-stage VLM detector and verifier with Qwen2.5-VL 7B at 8k context and the existing Unknown fallback. Record missed and excess final selections, and whether S3 was selected by a confirmed repair or fallback.

Check publication history, obsolete render rejection, revision-scoped evidence rejection in both EGDR arms, and exact reference binding against the planned edited PNG. Report submission intents, observed provider execution spans, edit-to-publication wall time, VLM calls and probe wall time. Provider spans include host work. GPU-only seconds are recorded as unavailable with the current instrumentation.

## Reviewer rubric

Two independent human reviewers score the randomized clips without method names. Watch each full output and compare it to the source with the same shot ID. Inspect 10%, 35%, 60%, and 85% of the output duration. Record timestamps and observations for any fail, yes, or uncertain rating.

- **Edit** (S1 and S3) measures attribute fidelity. Enter `pass` if the whole visible lid, including the rim, is scarlet red at every assessable checkpoint, with at least three assessable checkpoints including 85%, and no contrary frame. Enter `fail` for a clearly discernible yellow or blue patch on the lid, including a residual rim strip, a visibly wrong edit region, or a visible missing lid. Enter `uncertain` if assessable evidence is insufficient. Use `na` for S2 and S4.
- **Temporal** (S1 and S3) measures stability of the intended attribute. Enter `pass` if the visible lid color is stable across at least three assessable checkpoints including 85% and no contrary frame. Enter `fail` if the visible lid changes color, flickers, or alternates between old and edited colors. A consistently wrong color can be stable. Use `uncertain` when stability is not assessable and `na` for S2 and S4.
- **Unintended** (all shots) measures preservation. Enter `yes` for a clearly added or missing object, changed mark or body color, or a non-target lid recolor. Enter `no` when those elements are assessable and preserved, or `uncertain` when evidence is insufficient. Ignore ordinary motion, illumination and compression.

## Reporting

Keep the original reviews. After both forms are locked, flag any different ratings by a reviewer for clips of the same shot with identical decoded frame hashes. Keep the flagged values and use a separate adjudication record. Report strict attribute fidelity, temporal consistency, and non-target preservation separately from selection. Report publication safety from the runtime records. A single case supports a case-level comparison. Use the Oracle arm to assess attainable editing quality under the same renderer and selected references.

## School server

Render the shared source once with the branch commit used to freeze this protocol.

```bash
export PATH="$HOME/apps/videoflow-ffmpeg/ffmpeg-7.0.2-amd64-static:$PATH"
python3 -u research/dependency_probe/run_egdr_runtime.py \
  --source-only --story diamond-canister --seed 90421 --output runs/egdr-canister-source-01
python3 research/dependency_probe/egdr_radio_holdout.py frames \
  runs/egdr-canister-source-01 runs/egdr-canister-source-01/frame0
```

After eligibility review, create `annotation.json` with `reviewer`, `source_eligible`, `notes`, and `frame0_lid_polygons`. Put the S1 and S3 integer vertex lists under `frame0_lid_polygons`. Document the observed identities for all four shots in the notes. Then run:

```bash
python3 research/dependency_probe/egdr_radio_holdout.py bind \
  runs/egdr-canister-source-01 runs/egdr-canister-source-01/annotation.json \
  runs/egdr-canister-labels-01
python3 -u research/dependency_probe/run_egdr_runtime.py \
  --story diamond-canister --seed 90421 --source-run runs/egdr-canister-source-01 \
  --continuous --policies worklist oracle_egdr online_egdr full \
  --vision-model qwen2.5vl:7b-ctx8k \
  --oracle-labels runs/egdr-canister-labels-01/oracle_labels.json \
  --reference-regions runs/egdr-canister-labels-01/reference_regions.json \
  --output runs/egdr-canister-four-arm-01
python3 research/dependency_probe/egdr_radio_holdout.py blind \
  runs/egdr-canister-four-arm-01 runs/egdr-canister-review-01 runs/egdr-canister-private-key-01.json
python3 research/dependency_probe/egdr_radio_holdout.py scores \
  runs/egdr-canister-four-arm-01 runs/egdr-canister-private-key-01.json \
  reviewer1.csv reviewer2.csv > runs/egdr-canister-scores-01.json
```
