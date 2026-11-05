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
