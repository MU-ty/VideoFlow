# Real ComfyUI partial-observability experiment

Run on the school GPU worker with the existing MiniMax H3 workflow and an
idle, dedicated ComfyUI queue. The script uses only the Python standard library.
Confirm ComfyUI responds before launching:

    python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:8188/system_stats", timeout=10).status)'

Run a one-cell pilot first, from the repository root:

    python -u research/recovery_observability_comfy.py \
      --server http://127.0.0.1:8188 --output runs/comfy-observability-pilot-01 \
      --pilot --poll-seconds 5 --horizon-seconds 1200

After the pilot passes and the queue is idle, run the four-policy matched
experiment. Use fresh output directories for every invocation:

    python -u research/recovery_observability_comfy.py \
      --server http://127.0.0.1:8188 --output runs/comfy-observability-p0-01 \
      --repetitions 2 --poll-seconds 5 --horizon-seconds 1200

A repetition has eight cells and can take substantial GPU time. Two
repetitions use matched seeds within each repetition and reverse the cell
order in the second repetition. The opaque/wait cell can intentionally run
until the full observation horizon. The pilot is outside the measured runs.

## Protocol

The experiment submits three initial jobs and confirms that all are running
or queued. A transport shim delivers the real S4 request upstream, records the
server acknowledgement separately, and then drops the response to the worker.
The worker applies one local S2 edit and reopens SQLite. The original S6
remains independent of the edit. The worker sees the first two S4 inspections
as unknown in the transient arm and all S4 inspections as unknown in the opaque
arm. Other HTTP traffic uses ComfyUI as usual. The separate auditor reads
unmasked provider state and records evidence, without committing artifacts.

The policies are project-version invalidation, indefinite wait, local
replacement on the first unknown poll, and local replacement on the third
consecutive unknown poll. All share the asset planner and commit checks.
The ledger limit is three jobs, including superseded but unresolved remote
attempts. The treatment is client-transport fault injection on a real
generation backend. SQLite reopening is the client recovery boundary.

## Evidence

Return the whole output directory, including summary.json, protocol.json,
and each cell's boundary.json, worker.jsonl, project.db, and report.json.
Each report separates deadline-censored publication, stale publications,
provider acceptances per shot, S6 reuse, client-tracked unresolved attempts,
peak occupancy, and independent provider observations. Inspect sampler node
10 in provider history for execution evidence. Provider acceptances include
possible cache hits, and backend execution spans include host work.

## v2.6 extensions

Keep the original 16-run archive. The following block adds repetition IDs
3, 4, and 5 to the original four-policy, two-fault comparison, using the same
seed derivation, poll sleep, deadline, and forward/reverse ordering. Every
repetition has a separate output directory so a failed repetition can be
rerun without replacing the completed ones. A rerun needs a fresh path.

    for rep in 3 4 5; do
      python -u research/recovery_observability_comfy.py \
        --suite primary --start-repetition "$rep" --repetitions 1 \
        --server http://127.0.0.1:8188 \
        --output "runs/comfy-observability-v26-r$rep" \
        --poll-seconds 5 --horizon-seconds 1200 || break
    done

Optional matched blocks, with two repetitions each. Run them separately on
the same idle server and retain each batch:

    python -u research/recovery_observability_comfy.py \
      --suite capacity --repetitions 2 --output runs/comfy-observability-v26-capacity

    python -u research/recovery_observability_comfy.py \
      --suite backoff --repetitions 2 --output runs/comfy-observability-v26-backoff

    python -u research/recovery_observability_comfy.py \
      --suite ordered --repetitions 2 --output runs/comfy-observability-v26-ordered

- **Capacity (16 runs).** Compare project-version invalidation and three-poll
  local replacement under both original fault conditions. Submit the same
  three initial jobs, then apply cap 2 or 4 at the edit/reopen boundary.
  Cap 3 results for repetitions 1 and 2 come from the original primary batch.
  A cap of 2 drains inherited occupancy of 3 before admitting new jobs.
  This isolates the recovery admission limit; it is not a cap of 2 throughout
  the entire trial. Old unresolved attempts still consume capacity.
- **Backoff (8 runs).** Compare fixed five-second polling and exponential
  backoff for unknown tasks (5, 10, 20, 40 seconds, capped at 40), both
  replacing after three consecutive unknown observations. A skipped probe
  does not count as an observation. Other jobs retain ordinary polling.
  S4 visibility returns after a shared 12-second wall-clock mask or remains
  masked for the full window. This block uses its own matched fixed-poll
  comparator because the original transient arm was defined by poll count.
- **Ordered story (8 runs).** An outdoor gear trace adds an order-only edge
  S2 → S6, uses different prompts, and starts with S2 and S4 in flight. S6
  cannot start until the edited S2 resolves. The two policies are paired
  within this story. The block does not isolate topology from scene content
  and initial occupancy.

Summarize the five matched primary repetitions from the full output
directories. Add optional batch directories to the same command if available.
The summary reports raw results, success rate, latency median and inclusive
IQR for successful publications, unresolved and stale counts, and
within-repetition differences. A deadline-censored latency stays null.
A duplicate repetition or incompatible batch is rejected.

    python research/summarize_comfy_observability.py \
      runs/comfy-observability-p0-01 \
      runs/comfy-observability-v26-r3 runs/comfy-observability-v26-r4 \
      runs/comfy-observability-v26-r5 > runs/comfy-observability-v26-summary.json

### Ordered-story recovery policies

Run the ordered outdoor-gear story with cap three and pair Wait, immediate
replacement, and three-poll replacement in two seed configurations under
both the first-two-polls and persistent masks. S6 has an order-only edge
from S2 and starts after the edited S2 completes. S6 therefore measures
downstream progress and cannot demonstrate reuse of a previously admitted
S6. Keep the original independent-story results separate.

    python -u research/recovery_observability_comfy.py --suite ordered_recovery --repetitions 2 --server http://127.0.0.1:8188 --output runs/comfy-observability-v28-ordered-01 --poll-seconds 5 --horizon-seconds 1200
    python research/summarize_comfy_observability.py runs/comfy-observability-v28-ordered-01 > runs/comfy-observability-v28-ordered-01/paired-summary.json

There are 12 trials. A persistently masked Wait trial can take the full
1200-second horizon even after all upstream jobs have completed.
