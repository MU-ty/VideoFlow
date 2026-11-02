# Baselines and evidence protocol v0.5

Use freeze_case.py to preserve a completed development pair locally. Raw experiment data and case receipts are not included in this code change. New protocol code must use fresh run directories because source fingerprints changed. Previously running jobs should be reconciled before switching code or directories.

## Executable methods

All methods consume the same structured edit, declared read sets, costs and versioned assets. They share the zero-risk hard constraints used by the runtime, and never consume benchmark ground-truth labels.

| method | Selection |
| --- | --- |
| dair | Attribute-sensitive invalidation, state/reference closure; order edges do not invalidate |
| full | All assets |
| suffix_safe | Target asset and suffix, union required edit/provenance invalidations, then untyped closure |
| dag | Required explicit edit/provenance invalidations, then traverse all dependency types, including order |

`suffix_safe` is intentionally not a naive suffix baseline: a global edit may affect an earlier asset and reference compatibility must hold. `dag` shares explicit semantic read declarations with DAIR; its difference is conservative untyped propagation, not automatic semantic understanding. The shared hard-constraint computation must be disclosed. This implementation does not isolate a planner CPU-speed advantage.

The v3 graph has no edges, so DAG and DAIR may tie. Report ties. Typed-edge benefit needs independent examples with meaningful state/reference/order edges. The current H3 adapter refuses reference inputs; reference execution experiments require a functioning loader before real video evaluation.

Run additional methods on your locally prepared v3 inputs (not included in this code change), one process at a time:

```bash
python research/run_dynamic.py --manifest fixtures/assets_v3.json --schedule fixtures/dynamic_edits_v3.json --method suffix_safe --server http://127.0.0.1:8188 --output runs/protocol-v5/suffix-safe --max-inflight 2
python research/run_dynamic.py --manifest fixtures/assets_v3.json --schedule fixtures/dynamic_edits_v3.json --method dag --server http://127.0.0.1:8188 --output runs/protocol-v5/dag --max-inflight 2
```

To compare all conditions, Full and DAIR must also be rerun with this exact source version into separate directories. Existing v3 results cannot be mixed with new-code conditions. Use `--max-passes 0` for local initialization without worker execution.

```bash
python research/run_dynamic.py --manifest fixtures/assets_v3.json --schedule fixtures/dynamic_edits_v3.json --method full --server http://127.0.0.1:8188 --output runs/protocol-v5/full --max-inflight 2
python research/run_dynamic.py --manifest fixtures/assets_v3.json --schedule fixtures/dynamic_edits_v3.json --method dair --server http://127.0.0.1:8188 --output runs/protocol-v5/dair --max-inflight 2
python research/compare_baselines.py runs/protocol-v5/full runs/protocol-v5/suffix-safe runs/protocol-v5/dag runs/protocol-v5/dair --output runs/protocol-v5/comparison.json
```

The new protocol was validated with mock providers and local HTTP servers; no new real-provider run has been performed.

## Raw provider observations

At each runner session, `/system_stats` is recorded with server versions, startup arguments and available device information; errors are saved explicitly. The API does not expose every server environment variable or model-weight checksum, so record those separately when needed.

ComfyUI inspections now save the exact observed history entry and this prompt's matching queue records to SQLite `vf_provider_inspections`. Identical observations are deduplicated, and each distinct payload retains its first client observation time. A partial or failed query is not invented as a successful observation.

`report.json.provider_observations` contains:

- Cache node IDs reported by `execution_cached`.
- Started node IDs only if the provider actually includes `executing` messages. Missing messages mean unknown.
- Server `execution_start` to `execution_success`/`execution_error` span when both timestamps exist.
- Explicit null GPU time.

A history span includes model loading, CPU processing and I/O. Cache node IDs show provider cache reports, not fractions of GPU computation. A short span alone is not evidence of a cache hit. No monetary cost or GPU time is inferred from submission counts. Mock providers lacking history remain without provider observations, rather than receiving synthetic measurements.

Query observations read-only after a run:

```bash
python - <<'PY'
import json, sqlite3
from pathlib import Path
p=Path('runs/protocol-v5/dair/project.db').resolve()
db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
for job,payload in db.execute('SELECT job,payload FROM vf_provider_inspections ORDER BY id'):
    observation=json.loads(payload)
    if observation.get('history_entry') is not None:
        print(job,json.dumps(observation,ensure_ascii=False))
db.close()
PY
```

## Before larger generation batches

1. Keep one fixed cache policy and record the exact ComfyUI startup command/version, model identifiers, execution parameters and device. Document warmup separately. Do not silently change cache policy or use a changed seed solely to force baseline work.
2. Use matched seeds per scene/condition; run counterbalanced orders across repeats. Keep state-trigger and wall-clock-trigger results distinct; current runner uses state triggers only.
3. Freeze development/test splits and label affected assets independently of the algorithm. v3 has already informed prompt development and must remain a development case.
4. Include local, global and state-propagation edits; report unsupported cases. Do not batch reference-based real video tests until reference consumption is implemented.
5. Keep blocked/failed attempts and their reasons. The complete-run comparator refuses incomplete pairs, so publish the attempt inventory separately rather than silently dropping failures.
6. Measure semantic edit fidelity and identity/quality through independent evaluation. Fewer planned tasks and identical final outputs on a development sample are insufficient alone.

## Validation

Tests cover executable conservative baselines, global earlier-scene dependencies, dynamic execution, immutable case verification/tamper detection, original DB preservation, provider identity binding, cache message retention, missing-history behavior and actual adapter-to-local-HTTP persistence. Real GPU validation of the new protocol remains pending.
