# Live H3 reference fault experiment

The graph is S1 → S2, S1 → S3, S2 → S4, with independent S5. Each trial starts
from an archived revision 1. All arms use the same attribute-worklist edit
to make S1 blue. The setup admits S1, then S2, and stops before admitting S3.
The shim hides the accepted revision-2 S2 ACK and inspection. The independent
observer must see that S2 is still nonterminal. The ledger must have exactly
one unresolved task at this barrier, and S2 must bind revision-2 S1.
The second edit changes S1 to red and starts the measured interval.

| Condition | Obsolete revision-2 S2 | First current S2 |
| --- | --- | --- |
| `obsolete_only` | Mask until 12 seconds after edit 2 | Visible |
| `joint_opaque` | Same 12-second mask | ACK and observations hidden for the remainder of the run |

The independent observer records real provider history in both conditions.
It does not release worker slots or publish output. An unobservable task
continues to occupy a worker slot until the worker observes a terminal state.
The optional post-deadline drain queries the provider without publishing or
retrying.

| Policy | Second-edit invalidation | Reconciliation | Local replacement |
| --- | --- | --- | --- |
| `project_version_grace` | Full project | Fingerprint | 15 seconds since first unknown, checked at a fresh unknown probe |
| `worklist_grace` | Attribute Worklist | Fingerprint | Same grace policy |
| `videoflow` | DAIR | Job ledger | Three unknown probes |

All policies share SQLite, version and publication guards, admission limits,
and exponential polling backoff. The project-version arm has the same local
replacement as Worklist to isolate the cost of full-project invalidation.
This differs from the wait-only variants in the earlier fault matrix.

## Run on an idle, dedicated worker

Pilot both fault conditions at seed 3107. This is six trials and checks the
protocol before collecting the three-seed batch. A fresh output directory is
required, and both ffmpeg and ffprobe must be available.

```bash
export PATH="$HOME/apps/videoflow-ffmpeg:$PATH"
python -u research/recovery_reference_fault_comfy.py \
  --server http://127.0.0.1:8188 --seeds 3107 \
  --output runs/joint-reference-fault-pilot
```

After reviewing the pilot, use the following fixed seeds and defaults for
the 18-trial paired batch. Keep the pilot separate. The policy order rotates
across seed and fault cells. Keep all attempted runs, including setup failures
and missed deadlines. The publication deadline is 1,800 seconds after edit 2.

```bash
python -u research/recovery_reference_fault_comfy.py \
  --server http://127.0.0.1:8188 --seeds 7200 11293 15386 \
  --output runs/joint-reference-fault-paired3
```

Archive the whole output directory, including incomplete runs. Each trial
preserves its barrier, worker passes, SQLite ledger, reference frames, and
available manifests and media. The top-level summary retains previously
completed rows when a setup or provider error interrupts a batch.

Compare within each seed/fault cell:

- Correct publication by the deadline and latency from edit 2, retaining misses.
- Provider acceptances and repeated acceptances of the same renderer workflow
  for the same shot, ignoring only the output filename prefix. Also report
  replacement attempts separately from repeat work caused by full invalidation.
- Rejection of the obsolete S2 result, the S4 reservation after the selected
  S2 commit, and all manifest-to-reference bindings.
- Byte-for-byte reuse of S5, unresolved worker tasks, and independent provider
  terminal states. Provider execution spans include host work.

The joint condition is expected to test local replacement and downstream
readiness under partial observation. Equal results between Worklist and
VideoFlow are a valid outcome. The synthetic HTTP integration tests exercise
both fault conditions and all three strategies without GPU hardware.
