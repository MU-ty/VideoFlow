# Round 957 · Reference-conditioned H3 ownership pilot

Round 956 text-to-video produced 18 clips but yielded zero eligible seeds. This new cohort uses three prespecified scene seeds and nine H3 image-to-video tasks. The nine supplied 864×480 Blender renders fix the starting landmarks and their attachment. Each scene uses one enclosure across S1 and S3. S2 places the same blue decal on a physically separate enclosure while the dial stays on the unmarked main enclosure. The partial-occlusion reference starts with both marks visible. The H3 prompt requests an occluder to pass later. Reference-conditioned data are a controlled pilot and are reported separately from the text-to-video cohort.

| Scene | Intended condition |
| --- | --- |
| Voltage indicator 95711 | Same decal on a separate housing |
| Signal router 95721 | Oblique S3 relative to S1, with a continued camera orbit |
| Interval controller 95731 | Partial sticker occlusion after initialization |

## Generate on the GPU host

Run from the extracted bundle directory. Use the supplied PNGs. The Blender script records how they were made.

```bash
sha256sum -c frozen.sha256
python3 generate.py check
python3 -u generate.py run --output "$HOME/VideoFlow-H3-957"
```

The generator validates the H3 image socket and uploads and reads back each hashed reference before submitting a job. Each task ID, reference hash, provider image path, failure, and resulting video hash is logged. Rerun with the same output directory to resume a recoverable interruption. Return `$HOME/VideoFlow-H3-957/round957-h3-source.zip` upon completion.

## Screen and score

Follow the frozen source screening criteria below using generated video. A reference alone provides no proof that the video preserved the landmarks. Do not substitute seeds or initialize from later frames. Record all exclusions before generating scores.

1. All three clips must complete. The dial center and triangle center must both be unambiguously visible in the first sampled frame of each clip.
2. Verify that the dial and blue triangle share a housing in S1 and S3. Verify that the matching blue triangle in S2 belongs to a separate housing and that the dial remains on the unmarked main housing. Record label ambiguity, absent landmarks, duplicates, and merged housings as failures. The screen uses no classifier votes or gap counts.
3. In the cross-view condition, S3 must visibly differ in angle from S1. In the partial-occlusion condition, at least one candidate must show temporary partial overlap by a visible foreground occluder after both landmarks are initially visible. Record the affected shot and approximate time. Sticker morphing or disappearance without a visible occluder fails this criterion.
4. Write an `eligible` Boolean and a `reason` for every seed. For an eligible seed supply `clicks` with one entry each for S1/S2/S3. Each entry is two integer `[x,y]` points, dial then triangle, in the 320×178 first-frame PNGs. Use the same screen and frozen settings for all arms.

```bash
python3 evaluate.py prepare --source "$HOME/VideoFlow-H3-957" --output review
cp review/screen.template.json review/screen.json
# Complete screen.json after viewing the source clips.
python3 evaluate.py score --source "$HOME/VideoFlow-H3-957" --screen review/screen.json --output review
```

The generator uses only the Python 3.10+ standard library. Scoring additionally uses FFmpeg, NumPy, SciPy and Pillow. Marker match, framewise geometry, temporal geometry and Oracle share the same clips. Report explicit false acceptance on S2, explicit false rejection on S3, and abstentions by ground-truth class. Report after-anchor abstentions and retained distractors separately. Use positive and negative candidate counts as denominators. Mark empty strata N/A. This evaluates ownership and shot selection. It measures no downstream edit-render cost.
