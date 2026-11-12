# Prospective EGDR identity holdout

Freeze this protocol before inspecting any of the new source clips. The source
and paired runs record the protocol and implementation hashes. The archived
Diamond Canister, Lunchbox and Lamp examples were used for diagnosis and are
outside this evaluation.

| Story | Seeds | S1/S3 target marker and edited part | S2 distractor | S4 |
|---|---|---|---|---|
| survey-case | 90501, 90502, 90503 | Orange stripe on gray survey case; silver side latch | Blue stripe | Closed black field notebook |
| desk-timer | 90601, 90602, 90603 | White hexagon on gray timer; silver control knob | White circle | Empty clear glass |
| greenhouse-sprayer | 90701, 90702, 90703 | Two black neck rings on green sprayer; silver spray nozzle | One black neck ring | Closed green seed packet |

S1 is declared. S3 is the omitted target, S2 is a hard negative and S4 is an
unrelated negative. Freeze the blue-then-red edit, the first-frame reference,
the four arm order, and the model and sampling settings before rendering.

## Frozen evidence policy

Use the legacy detector and verifier on the same four source checkpoints
(10%, 35%, 60%, 85%). Keep its clear negatives. For its retained candidates,
probe pairs (1,2) and (3,4) with the structured schema and store both replies.
The evaluated online policy records marker location as diagnostic and decides
using marker shape, color, required ring count, edited part visibility and
same-instance linkage. A target match on any sampled frame wins. Exclusion
requires at least one explicitly excluded identity plus complete coverage
of all four frames, without a target or unresolved frame. Missing, malformed
or unresolved evidence preserves the legacy decision. The strict-location
variant uses the same replies and requires the body location for the host
marker. Record its selection separately as a paired selection-only control.
No new render is generated for that control. A refusal or model error stays
in the log. Oracle labels never enter a probe prompt.

## Source eligibility

Inspect each full source clip and the four checkpoints before running any edit
arm. For S1 and S3, frame 0 must show the full edited part and its correct
identity marker on the same host. S1/S3 must show the target in at least
three of the four checkpoints including 85%. S2 must show the distractor and
its matching part in at least three checkpoints including 85%, with no
qualifying target instance anywhere. S4 must show its unrelated object
without a qualifying target. Log any failed seed and why, and keep its
exclusion common to all arms. No substitutions after seeing source results.
Record infrastructure failures separately from source eligibility failures.

For an eligible seed, record the reviewer, source notes, all four SHA-256
values from the manifest, and S1/S3 polygons around the complete editable
part at decoded frame 0. Coordinates are integer pairs in the original
864 by 480 image. Exclude body and identity marker from the polygons.

## Execution and outcomes

Run Worklist, EGDR-Oracle, EGDR-VLM and Full from the same frozen Rev.1,
using the existing continuous-edit runner and the same target reference
transform. The two EGDR policies receive their respective evidence after
the red-edit S1 submission. Report paired missed/excess counts and
confirmed/fallback decisions, actual calls and timing, submission intents,
observed provider acceptances, and final-edit-to-publication latency.
Report all nine seed outcomes including eligibility and infrastructure
failures. Provider execution spans include host work. GPU seconds are
unavailable unless measured independently.

For completed runs, compare target edit correctness, temporal stability
and unintended changes in method-blinded review by two independent humans.
Use the criteria in the Diamond Canister rubric with the part name replaced
for each story. Preserve both raw forms and the adjudication of disagreements.
Unblind only after scoring. AI notes are auxiliary.

## Run on the school server

Start with a source-only run. Verify eligibility before creating its annotation.

```bash
python3 -u research/dependency_probe/run_egdr_identity_holdout.py source \
  --story survey-case --seed 90501 --output runs/identity-survey-case-90501-source
```

An annotation is a JSON object with `source_eligible` (boolean), `reviewer`
and `notes` (strings), `source_sha256` (shot to video digest), and
`frame0_polygons` (S1 and S3 to lists of integer coordinate pairs).
Use the same checkout for source and paired phases.

```bash
python3 -u research/dependency_probe/run_egdr_identity_holdout.py paired \
  --story survey-case --seed 90501 \
  --source-run runs/identity-survey-case-90501-source \
  --annotation runs/identity-survey-case-90501-source/annotation.json \
  --output runs/identity-survey-case-90501-paired
```
