# Round 956 · Independent native H3 ownership holdout

Six prespecified new H3 seeds, two each for same-decal, cross-view, and temporary partial occlusion. Each seed has an anchor S1, one same-decal wrong-housing candidate S2, and one correct-housing candidate S3. Three clips per seed, 18 in total. Use the six listed seeds once. Keep failed and excluded seeds in the report. Thresholds and the manual first-frame landmark protocol come from the prior native experiment. Report performance conditional on passing the material screen and report all six seed dispositions.

## Phase 1 · Generate on the school GPU server

Run from the directory containing this README. Requires Python 3.10 and the existing H3 ComfyUI model installation.

```bash
sha256sum -c frozen.sha256
python3 generate.py check
python3 -u generate.py run --output "$HOME/VideoFlow-H3-956"
```

Resume with the identical output directory after a recoverable interruption. The generator preserves accepted task IDs, records terminal failures, and verifies cached clips by SHA-256. Collect `$HOME/VideoFlow-H3-956/round956-h3-source.zip` after completion. Generation and experiment reporting are distinct. The archive includes only source media and their generation journal.

## Phase 2 · Lock the screen before predictions

Requires Python 3.10+, FFmpeg, NumPy, SciPy and Pillow. If using a transferred archive, extract it into the directory supplied as `--source`.

```bash
python3 evaluate.py prepare --source "$HOME/VideoFlow-H3-956" --output review
cp review/screen.template.json review/screen.json
```

Inspect the original H3 clips as well as the contact sheets. The screening instructions are fixed here.

- All three clips must have completed. At the first sampled frame, locate both the dial center and the triangular sticker center. The edit part and decal must be identifiable in every shot, and the first-frame clicks must be unambiguous.
- Confirm S1/S3 show the dial and decal attached to the same identifiable housing, and S2 shows the matching decal on a separate housing while the dial is on the unmarked target. Use the visible footage. Record label ambiguity, an incorrect decal, missing parts, duplicate decals, or generated/merged housings as screening failures. No minimum count of separation-witness frames from the algorithm is used for screening.
- For cross-view seeds, S3 must show a visibly different angle from S1. For partial-occlusion seeds, at least one candidate must show temporary partial overlap from a foreground occluder after both landmarks are visible initially. Record the shot and approximate time of the overlap. Examples where the sticker itself morphs or disappears without a visible occluder do not meet that inclusion criterion.
- Enter an `eligible` Boolean and an observed `reason` for every seed. Set `clicks` for eligible seeds to e.g. `{"S1":[[100,80],[140,70]],"S2":[[90,80],[210,70]],"S3":[[110,90],[180,90]]}` using the corresponding 320×178 `*-first.png` images in `(x,y)` order, dial then decal. Record the screen before running score. Preserve the same screen for all arms.

## Phase 3 · Matched-input scoring

```bash
python3 evaluate.py score --source "$HOME/VideoFlow-H3-956" --screen review/screen.json --output review
```

Marker match accepts both visible identical decals. Framewise geometry consumes the same track, raw votes and gap cues as the temporal method, ignores frame order, and requires 12 directional observations and 75% consensus. The frozen temporal negative uses at least three consecutive gap witnesses, with the same positive consensus and tracking checks. Oracle consumes the screened labels. All policies retain unresolved candidates, and a negative candidate is dropped only when S1 is confirmed.

Report explicit false acceptances on S2 and explicit false rejections on S3 before the anchor gate. Report abstentions separately for S2 and S3, both before and after the gate. The output also gives anchor abstentions, distractors still selected, positives lost, exact shot sets, required-shot coverage, and extra *planned* shot renders. Use the positive and negative candidate counts as denominators. An empty stratum has no rate. Report all values overall and by difficulty stratum. The three planned strata are small, and thresholds are frozen. This evaluation measures ownership and shot selection on H3 source clips. It submits no edit renders or publication requests.
