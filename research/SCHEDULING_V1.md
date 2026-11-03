# Scheduling falsification probe v1

This is an exploratory symbolic discrete-event experiment. It never calls ComfyUI, a model, or a GPU. It is not a demonstrated new algorithm or a substitute for real-provider measurements.

## Fair shared environment

- The same independent attribute-aware planner selects changed assets for every policy.
- Six assets begin absent. Edits arrive at fixed absolute wall-clock times independent of method progress. Initial work is included.
- One nonpreemptive FIFO simulated server; at most two submitted-but-unfinished remote jobs, including queued or obsolete jobs. There is no queue deletion or cancellation.
- All methods coalesce unsubmitted versions, cache exact symbolic generation inputs, and may use an obsolete result after an undo if its complete symbolic input key matches the current request. This cross-revision rebinding is shared by all policies but is not implemented by the current production runtime's revision guard.
- Unrelated assets and semantic state relations do not consume predecessor video outputs. The independent profile allows resolved prompts to generate independently. The ordered profile gates each submission on all preceding assets being current. Every method receives the same profile for each trace. The independent profile differs from current production readiness; results are not a drop-in GPU prediction.
- Service estimates per asset lie in [55,120] declared simulation seconds. Actual service is a deterministic keyed +/-15% perturbation, identical for matching inputs within a trace across methods. A policy knows the estimate, not future service completions.
- Same-time ordering is fixed: edits, completions, then submissions. Current input keys guard result publication. All remote work drains before simulation ends.

## Policies and tuning

Greedy baselines use immediate FIFO, latest-change-first, and shortest-estimated-service-first. Waiting policies reset only affected assets' deadlines and share a maximum pending wait of 120 simulation seconds.

The fixed baseline searches 8 windows (0,2,5,10,20,40,80,120) times 4 dispatch orders (FIFO/latest/shortest/stability heuristic), 32 configurations. The adaptive candidate searches 4 initial waits (0,5,20,40), 2 gap multipliers (.5,1.5), and the same 4 orders, also 32 configurations. It uses only previously observed per-asset inter-edit gaps, expires burst evidence after a long silence, and limits waits by estimated service cost. It never reads the next edit time or final user stop time.

Five synthetic workload families cover single edits, bursts, spaced edits, mixed local/global edits and undo. Each has 4 development and 4 held-out generated seeds by default: 20 development plus 20 evaluation traces. Each family contains both readiness profiles. This is a same-generator seed split, not real user data or an independently collected benchmark. The evaluator and generator are authored by the method developer.

Both policy families are selected on development mean objective only. Selection and source hashes are written before the first held-out simulation. The frozen objective is:

`last-edit-to-final-visible latency + 0.25 * unconsumed service + 0.1 * average unavailable-asset exposure`.

These coefficients are declared design assumptions, not elicited user utility. Components and per-family/profile slices are reported separately. The held-out set must not be used to keep adjusting the candidate. Once results have been inspected, these traces are disclosed exploratory evidence; later method changes require a new development/evaluation protocol rather than relabeling these as unseen tests.

## Metrics

Unconsumed service counts simulated executions whose output was never used for any current asset, even via later undo/cache reuse. A separate metric counts service obsolete at completion, which may later become useful. Exposure integrates the fraction of assets without current outputs over time. Final-visible time differs from complete remote drainage. All policies must end with identical final symbolic keys and no unresolved jobs.

Video quality, dollar charges and measured GPU time are null. The simulation does not model frame quality, actual visual identity, backend failures, cancellation, checkpoint recovery or multiple GPU workers.

## Reproduce

Run from the repository root:

```bash
python -m unittest discover -s tests -q
python research/run_schedule_benchmark.py --output runs/schedule-v1
```

Use a fresh directory. Outputs: traces.json, selection.json with all development trials, report.json/report.md, and per-policy/per-trace audit logs. No real video-generation process needs to be running. No installation beyond Python/standard library is required by this probe. It is compatible with the project's Python 3.10 environment.

## Recorded initial outcome

Default v1 run, 20 development and 20 evaluation traces; lower is better:

| Policy | Mean objective | Final-edit latency | Unconsumed service | Mean submissions |
|---|---:|---:|---:|---:|
| Immediate FIFO | 652.794 | 521.414 | 281.491 | 10.45 |
| Latest first | 692.549 | 550.074 | 306.851 | 10.75 |
| Shortest first | 639.906 | 517.838 | 245.358 | 10.70 |
| Tuned fixed wait | 607.474 | 498.559 | 203.393 | 10.40 |
| Tuned adaptive candidate | 609.937 | 497.220 | 215.779 | 10.45 |

Fixed selected wait=20/order=stable; adaptive selected initial wait=40/factor=1.5/order=stable. Adaptive has a 0.405% worse mean objective, 0.269% lower mean tail latency and 6.090% more unconsumed service. Paired objective outcomes are 3 wins, 9 ties, 8 losses versus fixed. No confidence interval or significance claim is made.

The candidate improves in some slices but regresses in others. In particular, the mean objective is better in the independent profile and worse in the ordered profile. That is a disclosed observation, not permission to discard the ordered cases. Any profile-conditioned follow-up must be preregistered and evaluated on fresh trajectories.

The initial candidate does not pass the proposed gate of beating the tuned fixed baseline. Do not launch a real-GPU campaign or claim a scheduling innovation from this result. Keep the result as negative evidence and decide whether an independently motivated new method merits a fresh protocol.
