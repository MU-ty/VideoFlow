# Cross Radio holdout protocol

Freeze this protocol before requesting any H3 outputs. The story, seed 89101, arm order, and rubric are fixed. This is one newly authored story and one paired seed. Do not tune prompts, seed, verifier, thresholds, masks, or eligibility after seeing any VLM verdict or final video. Preserve a failed eligibility check as a source-generation failure. Retry only a documented infrastructure failure with the identical configuration and a new output folder.

| Shot | Pre-edit source requirement | Rev.3 intention |
| --- | --- | --- |
| S1 | Mint-green radio, white X on its own front, yellow carry handle attached to the same radio | Change only the handle to scarlet red |
| S2 | Similar radio with a white circle, yellow carry handle, no qualifying X-marked radio | Preserve the circle-marked radio and its yellow handle |
| S3 | Same qualifying X-marked radio from a three-quarter angle | Change only the handle to scarlet red |
| S4 | Closed maroon notebook alone, no radio | Preserve the notebook and keep the shot free of radios |

The declared target is S1. The keyed S1→S3 state dependency is intentionally absent from the authored graph. S2 is a lookalike, and S4 is unrelated. A human checks the full four source clips before any model queries. In S1 and S3, require the handle and white X to be visibly on the same radio in frame 0 and at least three of the four rubric checkpoints, including 85%. In S2 require the distinguishable circle and attached yellow handle at the same number of checkpoints and no qualifying X radio anywhere. S4 must meet its requirement throughout. If a source fails, record the exact reason and stop. Do not infer eligibility from prompts.

All arms start from the same frozen Rev.1. Submit S1 for the first blue edit and wait for its acknowledgement. Reopen SQLite, apply the red edit, submit the current S1, then deliver the archived source-only verdicts in the two EGDR arms. Keep the two-slot in-flight cap, the same H3 configuration, and Qwen2.5-VL 7B with 8k context. Worklist uses the declared graph, Oracle uses source-hash-bound human labels, EGDR-VLM uses the existing source-only verifier and explicit fallback for Unknown, and Full regenerates every shot. The order is Worklist, Oracle, EGDR-VLM, Full. Trace stale results and verify that only Rev.1 and Rev.3 publish.

As in the lamp trial, draw first-frame polygons around the target part in S1 and S3 before running any arm. Preserve the polygons and source hashes. The same recoloring transform, final prompt and reference image apply whenever any arm schedules either target shot. These reference images and gold labels never enter the VLM question or source frames. S2 and S4 receive no corrected reference. This is an oracle-assisted renderer with source-only VLM selection and controlled evidence delivery. Report inference wall time separately from edit-to-publication. Provider execution spans include host work.

## Reviewer rubric

Use the source clip with the same shot ID. Play each final clip in full and inspect frames at 10%, 35%, 60%, and 85% of its duration. Record timestamps and a short observation for every fail, yes, or uncertain judgment. Use the provided CSV and keep method identities hidden until both reviewers finish.

- **Edit** applies only to S1 and S3. Enter `pass` if the attached carry handle is scarlet red wherever clearly visible, with at least three assessable checkpoints including 85%, and there is no definite contrary frame. Enter `fail` for clearly yellow, blue or mixed target coloring, a visibly wrong part, or an observable disappearance of the target handle. Enter `uncertain` for insufficient visibility when no definite failure is observable. Enter `na` for S2 and S4.
- **Unintended** applies to all shots. Enter `yes` for a definite new or missing object, a changed identity mark, an unintended change to the radio body, notebook or other salient object, or a non-target handle changing color. Changes to the requested target handle are scored under Edit. Ignore normal motion, viewpoint, lighting, and compression unless the relevant object or attribute visibly changes. Enter `no` when the relevant attributes are assessable and preserved, and `uncertain` when they cannot be verified and no definite change is visible. A frame with a clear violation anywhere in the clip is sufficient for `yes`.
- Score the output as shown even if it seems identical to another clip. The two reviewers should complete their forms independently.

## Reporting

Keep reviewer-level values and disagreements. Report confirmed edit passes out of two targets and confirmed unintended changes out of two non-target shots, with the uncertain counts and affected shot IDs. Keep target-shot unintended changes separately. Report missed and excess selections, and whether S3 was confirmed as a keyed edge or was selected by fallback. The main comparison with Worklist is recovery of S3 and the paired difference in non-target changes. Oracle separates incorrect evidence and fallback from the effect of selecting the intended dependency. This single story supports a case-level comparison.

## School server

Run these commands from the VideoFlow repository at the pinned holdout branch commit. Use a dedicated idle ComfyUI queue and the existing 8k Ollama model.

```bash
export PATH="$HOME/apps/videoflow-ffmpeg/ffmpeg-7.0.2-amd64-static:$PATH"
python3 -u research/dependency_probe/run_egdr_runtime.py \
  --source-only --story cross-radio --seed 89101 \
  --output runs/egdr-radio-source-01
python3 research/dependency_probe/egdr_radio_holdout.py frames \
  runs/egdr-radio-source-01 runs/egdr-radio-source-01/frame0
```

Review the four full videos under `source/` and the four PNGs under `frame0/`. Record an eligible source annotation in the following format, replacing the empty polygons with ordered integer [x, y] contour points in the 864×480 frame coordinates. Trace only the colored handle material, leaving the background and the interior gap outside the polygon. The example is deliberately ineligible until reviewed. Preserve the annotation if the source fails.

```json
{
  "reviewer": "",
  "source_eligible": false,
  "notes": "",
  "frame0_handle_polygons": {"S1": [], "S3": []}
}
```

After freezing the eligible source annotation as `runs/egdr-radio-source-01/annotation.json`, run the paired experiment.

```bash
python3 research/dependency_probe/egdr_radio_holdout.py bind \
  runs/egdr-radio-source-01 runs/egdr-radio-source-01/annotation.json \
  runs/egdr-radio-labels-01
python3 -u research/dependency_probe/run_egdr_runtime.py \
  --story cross-radio --seed 89101 --source-run runs/egdr-radio-source-01 \
  --continuous --policies worklist oracle_egdr online_egdr full \
  --vision-model qwen2.5vl:7b-ctx8k \
  --oracle-labels runs/egdr-radio-labels-01/oracle_labels.json \
  --reference-regions runs/egdr-radio-labels-01/reference_regions.json \
  --output runs/egdr-radio-four-arm-01
python3 research/dependency_probe/egdr_radio_holdout.py blind \
  runs/egdr-radio-four-arm-01 runs/egdr-radio-review-01 runs/egdr-radio-private-key-01.json
```

Send only the review directory to two independent reviewers. Keep the private key and runtime reports away from reviewers. Save the two completed CSVs and score them together.

```bash
python3 research/dependency_probe/egdr_radio_holdout.py scores \
  runs/egdr-radio-four-arm-01 runs/egdr-radio-private-key-01.json \
  reviewer1.csv reviewer2.csv > runs/egdr-radio-scores-01.json
```
