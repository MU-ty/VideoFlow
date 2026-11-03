# Intent executor V3 — exploratory execution-mechanism diagnostic

Status: local unit tests and fake-model integration only. No real-model result yet.
V2 structured self-memory failed its continuation gate (ledger 8/12 versus notes
and explicit undo 10/12). This is a new mechanism probe, not a successful method.

## Comparison

- `state`: strong undo instructions, emits full state.
- `patch`: same instructions, emits literal field assignments.
- `program`: emits set / restore_before / copy_current; a restricted interpreter
  resolves historical values. No arbitrary generated Python execution.

All arms get the same *information categories*: initial state, full instructions,
raw responses, own predicted current state and automatic before/after snapshots.
Histories diverge after their own outputs. No gold snapshots or gold operations
enter model requests. Automatic histories are granted to baselines too; do not
compare V3 directly to V2 as a controlled improvement. There are no retries,
reflection calls, training, or paid APIs required. Same temperature 0, 2048-token
output cap and model; actual token counts and latencies need not be equal.

Restore means assigning selected fields their values immediately before a named
prior round. It is NOT removing an operation and replaying the remaining history.
Copy reads the start-of-current-turn snapshot. Every target field may be written
once per transaction. Invalid output rejects the entire transaction and retains
previous state, but the failed round still occupies a history slot.
This synthetic task uses explicit rounds and fields, no ambiguous requests;
clarification and visual grounding are outside scope.

## Fixtures and frozen gate

4 dev sequences / 19 turns per arm (57 calls); 12 eval sequences / 58 turns per arm
(174 calls). Total 231 calls. Eval contains two surface variants of six families:
control, scoped restoration, undo of undo, snapshot copy, multi-field multi-round
restoration, and no-op history. These are author-written after V2, not independent
user stories, not semantically held-out families, not visual evaluation.
Manual literal gold state updates are separate from interpreter execution and
cross-checked by tests; independent human annotation review is still needed.

`prepare` freezes cases, prompts and executable source hashes. No prompt tuning
between dev and eval in this pass. If a code defect is found, preserve original
outputs and create a new protocol/output directory. Never silently relabel V2.

Continuation is exploratory: program must beat BOTH baselines on eval sequence
success, have positive net paired wins in at least TWO non-control families
against EACH baseline, no control regression and all records returned. Ties fail.
This gate is not a significance test or novelty proof. Report actual costs; even
passing is only justification for independently authored tests and video validation.

## Server commands (Python 3.10+, standard library only)

Keep the existing Ollama server running with 16K context, qwen2.5:14b available.
No ComfyUI/H3 jobs are involved. From the repository root:

```bash
export INTENT_BASE_URL=http://127.0.0.1:11434/v1
export INTENT_MODEL=qwen2.5:14b
export INTENT_API_KEY=ollama
python -m unittest discover -s research/intent_executor_v3 -p 'test_v3.py' -v
python research/intent_executor_v3/run.py prepare --output runs/intent-executor-qwen14b-v3
~/apps/ollama/bin/ollama --version > runs/intent-executor-qwen14b-v3/ollama-version.txt
~/apps/ollama/bin/ollama list > runs/intent-executor-qwen14b-v3/ollama-models.txt
python -u research/intent_executor_v3/run.py run --output runs/intent-executor-qwen14b-v3 --split dev --timeout 600
python research/intent_executor_v3/run.py score --output runs/intent-executor-qwen14b-v3 --split dev
python -u research/intent_executor_v3/run.py run --output runs/intent-executor-qwen14b-v3 --split eval --timeout 600
python research/intent_executor_v3/run.py score --output runs/intent-executor-qwen14b-v3 --split eval
~/apps/ollama/bin/ollama ps > runs/intent-executor-qwen14b-v3/ollama-ps.txt
tar -czf runs/intent-executor-qwen14b-v3-audit.tar.gz runs/intent-executor-qwen14b-v3
```

Stop at any command error. A sequence filename is printed once each sequence
finishes. Do not rerun into a directory with existing records; pending requests
may have reached the server. Preserve partial runs for audit. `score` can inspect
partial runs once configuration.json exists. Reports are text: use `cat`, not
execute their paths. No new model download is needed.

## Interpreting reports

Sequence success requires every turn correct; missing sequences fail the expected
sequence denominator. `expected_turns` gives the full denominator while `turns`
counts turns in existing records (including unreturned turns of partial records).
`local_transition_correct` compares each output with gold operations applied
OFFLINE to that arm's own supplied state/history: distinguishes current-turn
failure from inherited bad state, but does not establish correct intent parsing.
`canonical_operation_matches` is syntax-sensitive (equivalent literal assignments
may not match restore operations), diagnostic only. `operation_rejections` means
invalid schema/target/value/reference, NOT an interpreter implementation failure.
`format_errors` includes these rejections. `new_off_target_changes` measures actual
new changes to unaddressed fields relative to the supplied state, while
`preservation_violations` also counts inherited errors and unscorable fields.
Unscorable fields are reported separately; don't call them observed visual damage.
Token totals are lower bounds if usage is missing; latency includes server caching
and is not GPU time. Raw requests/responses are retained. Model digest/context
must be recorded via Ollama outputs; provider finish_reason is not captured.

Conceptual predecessors include PAL (ICML 2023), selective undo, Edit-R2, and
RefineCut. Structured execution is not itself a new research contribution.
A passing result would still need evidence of a distinct multimedia contribution.
