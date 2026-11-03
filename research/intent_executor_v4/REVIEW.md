# V4 pre-run fixture review — 2026-11-03

Reviewer: the implementing assistant, also the fixture author. This is NOT an
independent annotator review. No V4 model outputs have been inspected.

## Checks completed

- Every restore identifies a prior numbered turn, exact shot and field(s).
- Restore means a field assignment from a prior turn-start snapshot, not removal
  of an operation followed by history replay. Later changes to unaddressed fields
  remain current. All arms receive this definition.
- Copy operations read the current turn's starting snapshot, including swaps.
  No persistent binding or sequential read-after-write is implied.
- No-op rounds occupy a history slot. A no-op assignment can later be the target
  of restore-before. Empty operations must still use an object wrapper.
- Multi-field instructions explicitly name each shot and each historical round.
  No ambiguous shorthand of V2's “字幕仍是晨间” type is present.
- Gold literal updates are written separately from executable reference operations.
  Tests replay the reference operations and compare every field at every round.
- No exact V3 instruction trajectory is reused; semantic families do overlap.
- Mutating gold annotations in tests cannot change model messages or response schema.

## Eval restoration spot checks (literal expected values)

| Sequence | Reviewed transition | Expected result |
|---|---|---|
| gallery | t5 restore S21 light before t3 | sunset; S23 stays day |
| cafe | t5 restore S22 subtitle before t2 | 上午; S21 remains 夜晚 |
| bridge | t6 undo t4 color restore | S23 red; lighting remains sunset |
| radio | t5 restore subtitle before t4 | S22 直播 |
| library | t2 swap start-of-turn subtitles | S21 归还; S22 借阅 |
| library | t5 undo S22 side of t2 | S22 归还 |
| workshop | t4 restore before t3; t5 restore before t1 | S23 blue, then S22 black |
| rooftop | t5 restore color before t3; subtitle before t1 | S21 white, 片头 |
| cinema | t4 restore two attributes from different turns | S22 sunset; S23 正片 |
| rain | t5 restore light before no-op assignment t2 | S23 sunset; S21 雨停 |
| market | t5 restore color before t3; subtitle before t4 | S22 red, 开市 |

Station/stage are controls without historical restoration. They include no-op,
copy, and ordinary fields to detect regressions. Dev has four short new fixtures,
not old eval cases. 7 automated tests cover these literal gold checks, transaction
atomicity and copy snapshots, schema treatment, prompt/gold isolation, API payload
and finish reason, preflight failure/no fallback, and full synthetic score flow
including missing records and the tie-fails gate. Mock results are not accuracy.
