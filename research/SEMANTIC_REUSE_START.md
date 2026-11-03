# Research start: semantic reuse under incomplete edit footprints

Status: falsifiable candidate and offline scaffold, not an established ICME contribution. No video quality or GPU savings result. Existing complete-graph DAIR results and negative scheduling results remain unchanged.

## Motivation and rejection criteria

Complete explicit attribute dependencies determine the required repair closure. An independent attribute-aware planner already recovers it; graph traversal, Router/Worker organization and commit guards do not establish a new selection method. A candidate needs a changed, justified problem setting: some semantic edit footprints are unknown before looking at the existing video. Examples include an entity mentioned in a prompt but outside the frame, or a visible recurring entity absent from metadata. Neither phenomenon has yet been established at useful frequency in the current designed stories.

Proposed question: with the same frozen visual scorer and graph, can we reduce total inspection plus generation cost at a controlled rate of leaving semantically invalid old clips untouched? Evaluate semantic obligations specified by users, not pixel differences alone.

## DeepSeek architecture: borrowed system layer

Source inspected at commit `6337fc823ce20251c8de8085948165c81aaa701e` (2026-10-31):

- [README](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/README.md): research prototype, not identical to the internal system or design article.
- [Domain model](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md): immutable triggers, tickets, assignments, subscriptions and policy versions.
- [Execution](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md): separate router and worker sessions, foreground/background execution and recovery.
- [Architecture](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/007-architecture.md): transactional state and side effects after commit.

Map edits/results to Triggers, a bounded production request to a Ticket, and a generation attempt to an Assignment. Keep interaction responsive while a remote GPU task runs. Route follow-ups to the same production context via subscriptions. Persist outputs independently of the model's conversational memory. An optional external supervisor could inspect semantic progress, but the inspected public architecture has no standalone Watchdog module; a timeout or a polling loop is not evidence of that capability. The X design article was inaccessible directly (403), so no article-specific implementation claim is inferred from secondary summaries.

These are attributed engineering foundations. They are not claimed as VideoFlow's core algorithmic novelty, and no performance number from coding-agent workloads transfers to video generation.

## Candidate formalization

For one story/edit episode let `H` be mandatory repairs, `E_ref` hard generated-reference dependencies, `c_i > 0` declared repair costs, and `q_i in [0,1]` frozen visual disagreement scores. Scores need not be calibrated probabilities. A label `y_i=1` means reusing the old clip violates the NEW semantic contract; labels cannot be supplied to deployment selection.

Build a nested ladder of repair sets beginning with `closure(H)`. A simple provisional ranking selects the additional seed with largest `sum(q_i over newly covered closure) / sum(c_i over that closure)`. Index the ladder by cumulative declared cost fraction. This accounts for expensive descendants and shared closures, but it is a conventional greedy heuristic, not a new optimal algorithm. Compare it against score-only and score-per-own-cost rankings, each with the identical hard closure.

Episode loss is `L = 1{any invalid OLD clip is reused}`. It is nonincreasing along the ladder. With scorer, ranking and grid fixed BEFORE calibration, use the existing Conformal Risk Control correction `(sum calibration losses + 1)/(n+1) <= alpha` to choose a conservativeness parameter. The implementation has a deterministic all-repair fallback.

[Angelopoulos et al., Conformal Risk Control](https://arxiv.org/abs/2208.02814), ICLR 2024, is the statistical source. This correction and its guarantee are borrowed. Conditional on the required fixed-policy/exchangeability assumptions, CRC controls expected episode loss marginally over calibration and test draws. It is not a high-probability per-project promise, not an error rate conditional on being reused, and not a guarantee for nonexchangeable future edits, unseen generators or distribution shifts. Story-level independence cannot be obtained by treating neighboring frames, repeated edits or multiple seeds of one story as independent calibration units.

Importantly, the loss concerns old-asset omission. Regenerating a clip does not make it correct. Final edited clips and newly formed transitions still need independent validation, and all repair failures and retries count in end-to-end cost and final quality. The current scaffold does not implement or certify that stage.

## Implementation boundaries

`risk_reuse_probe.py` implements nested closure-aware choices, the calibration calculation. It makes no VLM or GPU calls and is not wired to `AssetRuntime.complete`. Existing stale-result guards must not be relaxed. Semantic reuse would require a separate, explicit approval/provenance record bound to input media hashes, new semantic contract, project epoch, scorer/prompt version and calibration artifact. A new edit invalidates the old evidence. No new production authorization or commit mechanism is implemented.

The current `asset_worker.build_h3` rejects references; `h3_workflow` names an image-to-video node without providing a source-image input. A public [Comfy reference workflow](https://comfy.org/hub/workflows/minimax-h3-reference-to-video-46a303cbccf9/) demonstrates model-family capabilities, not installed nodes on the user's server. A text-encoder checkpoint alone is not an available VLM verification API. No new backend capability is claimed.

## Closest work and competitive requirements

| Primary work | Existing overlap | What must be distinguished and tested |
|---|---|---|
| [CoAgent](https://arxiv.org/abs/2512.22536) | Shared memory, verifier-guided selective regeneration | Comparing only with open-loop generation is inadequate |
| [Thinking on Shots / MMLVE-Agent](https://arxiv.org/abs/2608.26809) | Shot-wise on-demand editing with 3-vote entity checks and editing feedback | Reproduce a compatible voting/threshold footprint selector with the same scorer; do not claim on-demand editing itself |
| [Aurora](https://arxiv.org/abs/2605.18748) | VLM interpretation and construction of editing conditions | Condition construction alone is already occupied |
| [ContextMaster](https://arxiv.org/abs/2608.04956) | Interactive creation with sparse context under a fixed budget | Differentiate update/reuse decisions from context-token routing; abstract-level screening only here |
| [Conformal Risk Control](https://arxiv.org/abs/2208.02814) | Risk calibration of nested decisions | A direct application of CRC is a mandatory strong baseline, not a new theorem |
| [SCoRE](https://arxiv.org/abs/2603.24704) | Selective trust with risk control among trusted cases | Do not conflate our marginal episode loss with selective conditional error control |

This is targeted screening, not a proof that the candidate is novel or an exhaustive literature review. Latest versions, code availability, licenses and implementable baseline protocols need further checking before a full campaign.

## Locked sequence and stop gates

1. Inventory installed interfaces and annotate NEW-contract validity of OLD clips. Use independent stories for development/calibration/test. Existing quality acceptance of edited clips is not an old-clip validity label. No training/calibration from the three already accepted edited examples.
2. Before GPU: establish that naturally incomplete footprints actually occur and the scorer identifies them. If a complete attribute graph already solves the target workload, stop; artificial removal of metadata can be a stress test, not the main evidence.
3. Freeze scorer/prompt, costs, grid and candidate ranking on development data. Compare full repair, complete-annotation oracle, deployed attribute planner, development-tuned score and 3-vote policies, score+project-CRC, score-per-cost+project-CRC, and closure-gain+project-CRC. Give every semantic method the same observations and charge identical inspection costs. Do not pick the ranking on the calibration set.
4. Calibrate on separate story groups. Run all held-out policies once with locked choices. Count episode omission risk, conditional invalid-reuse rate, reuse coverage, total observed generation/inspection cost, final edit fidelity, human acceptance and final consistency. Report stories and repeated seeds separately.
5. A proposed engineering continuation gate, to lock before collecting held-out results: at least 10% lower total observed cost than the best quality-matched semantic baseline, no more than 5 percentage points reduction in final human acceptance, and episode omission within the declared tolerance with uncertainty reported. These are chosen go/no-go thresholds, not established ICME standards or a guarantee of acceptance. Underpowered intervals mean unresolved, not success.
6. If cost advantages vanish after equalizing verifier information, or only exist against a weaker attribute baseline, pause. CPU unit tests only validate the scaffold. No numeric gain or novelty claim follows from them.

## Run the offline checks

```bash
python -m unittest tests.test_risk_reuse_probe -v
```

The seven checks cover monotone graph closure, closure-cost ranking, small-sample fallback, calibration arithmetic, no benefit when the hard graph covers everything, label isolation and input validation. They are deterministic implementation checks, not a scheduling, visual-quality or statistical-coverage benchmark.
