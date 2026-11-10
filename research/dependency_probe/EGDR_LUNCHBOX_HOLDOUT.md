# Star Lunchbox holdout protocol

This is a new, single-seed case after the Cross Radio seed 89101 source eligibility failure. The earlier case stays excluded and its failure stays in the experimental record. Freeze the story, seed 89201, arm order, and rubric before requesting any H3 output. Preserve any source eligibility failure and stop before model inference or experimental rendering. Retry only a documented infrastructure failure with identical configuration in a new output folder.

| Shot | Pre-edit source requirement | Rev.3 intention |
| --- | --- | --- |
| S1 | Deep-purple hard-shell lunchbox with a white five-point star on its own front and an attached thick yellow handle | Change only the handle to scarlet red |
| S2 | Similar lunchbox with a white triangle on its own front and an attached thick yellow handle | Preserve the triangle, yellow handle, and purple shell |
| S3 | Star-marked lunchbox from a three-quarter view | Change only the handle to scarlet red |
| S4 | One empty white ceramic bowl against a seamless pale gray surface and backdrop | Preserve the bowl and keep the shot free of additional objects |

The declared target is S1. The keyed S1→S3 state dependency is intentionally absent from the authored graph. S2 is a lookalike and S4 is unrelated. Check all four source clips before any model queries. In S1 and S3, require the yellow handle and white star to be visibly on the same lunchbox at frame 0 and at least three of the four rubric checkpoints, including 85%. In S2 require the distinguishable triangle and attached yellow handle at least three of the four checkpoints, including 85%, and no qualifying star-marked lunchbox anywhere. S4 must show a single bowl without other distinct or ambiguous objects throughout. Record exact visual failure reasons and source hashes before stopping. Do not infer eligibility from prompts.

All arms start from the same frozen Rev.1. Submit S1 for the first blue edit and wait for its acknowledgement. Reopen SQLite, apply the red edit, submit the current S1, then deliver the archived source-only verdicts in the two EGDR arms. Keep the two-slot in-flight cap, the same H3 configuration, and Qwen2.5-VL 7B with 8k context. Worklist uses the declared graph, Oracle uses source-hash-bound human labels, EGDR-VLM uses the existing source-only verifier and explicit fallback for Unknown, and Full regenerates every shot. The order is Worklist, Oracle, EGDR-VLM, Full. Trace stale results and verify that only Rev.1 and Rev.3 publish.

As in the lamp trial, draw first-frame polygons around the target part in S1 and S3 before running any arm. Preserve the polygons and source hashes. The same recoloring transform, final prompt and reference image apply whenever any arm schedules either target shot. These reference images and gold labels never enter the VLM question or source frames. S2 and S4 receive no corrected reference. This is an oracle-assisted renderer with source-only VLM selection and controlled evidence delivery. Report inference wall time separately from edit-to-publication. Provider execution spans include host work.

## Reviewer rubric

Use the source clip with the same shot ID. Play each final clip in full and inspect frames at 10%, 35%, 60%, and 85% of its duration. Record timestamps and a short observation for every fail, yes, or uncertain judgment. Use the provided CSV and keep method identities hidden until both reviewers finish.

- **Edit** applies only to S1 and S3. Enter `pass` if the attached carry handle is scarlet red wherever clearly visible, with at least three assessable checkpoints including 85%, and there is no definite contrary frame. Enter `fail` for clearly yellow, blue or mixed target coloring, a visibly wrong part, or an observable disappearance of the target handle. Enter `uncertain` for insufficient visibility when no definite failure is observable. Enter `na` for S2 and S4.
- **Unintended** applies to all shots. Enter `yes` for a definite new or missing object, a changed identity mark, an unintended change to the lunchbox shell, bowl or other salient object, or a non-target handle changing color. Changes to the requested target handle are scored under Edit. Ignore normal motion, viewpoint, lighting, and compression unless the relevant object or attribute visibly changes. Enter `no` when the relevant attributes are assessable and preserved, and `uncertain` when they cannot be verified and no definite change is visible. A frame with a clear violation anywhere in the clip is sufficient for `yes`.
- Score the output as shown even if it seems identical to another clip. The two reviewers should complete their forms independently.

## Reporting

Keep reviewer-level values and disagreements. Report confirmed edit passes out of two targets and confirmed unintended changes out of two non-target shots, with the uncertain counts and affected shot IDs. Keep target-shot unintended changes separately. Report missed and excess selections, and whether S3 was confirmed as a keyed edge or was selected by fallback. The main comparison with Worklist is recovery of S3 and the paired difference in non-target changes. Oracle separates incorrect evidence and fallback from the effect of selecting the intended dependency. This single story supports a case-level comparison.

## School server

Run these commands from the VideoFlow repository at the pinned holdout branch commit. Use a dedicated idle ComfyUI queue and the existing 8k Ollama model.

```bash
export PATH="$HOME/apps/videoflow-ffmpeg/ffmpeg-7.0.2-amd64-static:$PATH"
python3 -u research/dependency_probe/run_egdr_runtime.py \
  --source-only --story star-lunchbox --seed 89201 \
  --output runs/egdr-lunchbox-source-01
python3 research/dependency_probe/egdr_radio_holdout.py frames \
  runs/egdr-lunchbox-source-01 runs/egdr-lunchbox-source-01/frame0
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

After freezing the eligible source annotation as `runs/egdr-lunchbox-source-01/annotation.json`, run the paired experiment.

```bash
python3 research/dependency_probe/egdr_radio_holdout.py bind \
  runs/egdr-lunchbox-source-01 runs/egdr-lunchbox-source-01/annotation.json \
  runs/egdr-lunchbox-labels-01
python3 -u research/dependency_probe/run_egdr_runtime.py \
  --story star-lunchbox --seed 89201 --source-run runs/egdr-lunchbox-source-01 \
  --continuous --policies worklist oracle_egdr online_egdr full \
  --vision-model qwen2.5vl:7b-ctx8k \
  --oracle-labels runs/egdr-lunchbox-labels-01/oracle_labels.json \
  --reference-regions runs/egdr-lunchbox-labels-01/reference_regions.json \
  --output runs/egdr-lunchbox-four-arm-01
python3 research/dependency_probe/egdr_radio_holdout.py blind \
  runs/egdr-lunchbox-four-arm-01 runs/egdr-lunchbox-review-01 runs/egdr-lunchbox-private-key-01.json
```

Send only the review directory to two independent reviewers. Keep the private key and runtime reports away from reviewers. Save the two completed CSVs and score them together.

```bash
python3 research/dependency_probe/egdr_radio_holdout.py scores \
  runs/egdr-lunchbox-four-arm-01 runs/egdr-lunchbox-private-key-01.json \
  reviewer1.csv reviewer2.csv > runs/egdr-lunchbox-scores-01.json
```
