# V4: format-controlled execution probe

Status: code/unit/mock tests only; no real-model result yet. Built after inspecting
V3, so this is a prospective exploratory replication, not an independent benchmark.

## Frozen predecessor

The detailed V3 freeze manifest is retained separately with the original uploaded
archive, rather than published in this repository. It records the archive and
member hashes, original dev/eval reports and the failed gate. V3 strict eval:
state 7/12, patch 8/12, program 8/12. Limited post-hoc fixed-trace normalization
produced 7/12,9/12,12/12 respectively; that is NOT rerun accuracy and does not
replace the original result. V3 inputs/results and executable code are unchanged.

## Fair format control

All three arms call the same Chat Completions endpoint with
`response_format={type:json_schema,json_schema:{name,strict:true,schema}}`.
State uses an exact complete-state object. Patch and program use an exact `ops`
object, including `{"ops":[]}` for no change. Shot IDs and value vocabularies come
only from public initial state and the task definition. Schemas never depend on
gold operations/answers or select the requested target for the model.
Program's extra restore/copy alternatives are the experimental treatment.
The same schema subset validator runs on every response before strict decoding.
No trailing-character repair, stripping, retries, or fallback to unconstrained JSON.
All arms have the same initial state, full raw history, own accepted state and
automatic before/after records. No gold enters requests. Restore-before and
start-of-turn copy semantics are unchanged from V3; causal replay is out of scope.

Official support reference:
https://docs.ollama.com/capabilities/structured-outputs
It documents `response_format` on the OpenAI-compatible API. Local installed
versions are not assumed to work: seven separately logged preflight calls check
both no-op and active outputs for every arm plus a schema-only sentinel in conflict
with its text prompt. A pass is observed compatibility, not proof of every schema
constraint on every possible generation. No-op/active preflight checks test schema
and interpreter acceptance, not benchmark semantic accuracy. Preflight is excluded
from accuracy and timed experiment totals; its usage/time is retained separately.

## New fixtures and pre-run review

4 newly authored dev sequences, 12 turns per arm: 36 calls.
12 newly authored eval sequences, 66 turns per arm: 198 calls.
With 7 preflight calls: **241 total requests** for one complete pass.
The eval sequences are separately written, not ID-renamed copies of V3; however
semantic families intentionally overlap and the author saw V3. Story labels are
mnemonics for symbolic trajectories, NOT independent real video stories.
See `REVIEW.md` for scope/semantic checks. Human independent review is not claimed.

## Run

Use existing local Qwen2.5:14b and Ollama with 16384 context. No H3 invocation,
no extra Python packages, no model download. Python 3.10+ and Bash are sufficient.
From this branch's repository root:

```bash
bash research/intent_executor_v4/run_all.sh
```

The script tests locally, freezes protocol, records Ollama version/models and code
commit/status, runs preflight, dev, checks dev format failures, runs eval, scores,
and archives `runs/intent-executor-qwen14b-v4-audit.tar.gz`. It stops at the first
command error. It refuses an existing output directory. Never remove old attempts
to make reruns succeed. Stop and inspect failed attempts; interrupted requests may
have executed remotely. Single-sequence filenames print as progress indicators.

Optional environment overrides: `INTENT_BASE_URL`, `INTENT_MODEL`,
`INTENT_API_KEY`, `OLLAMA_BIN`. Defaults use local Ollama and the ignored placeholder
key `ollama`. The key is not recorded. Protocol/source hashes and preflight model,
endpoint, token limit and timeout must match the run. Changing model/prompt/code
requires a new protocol/output and disclosure, not selective replacement of results.

Manual scoring, including partial runs once configuration.json exists:

```bash
python research/intent_executor_v4/run.py score \
  --output runs/intent-executor-qwen14b-v4 --split eval
```

## Frozen decision

Program must strictly exceed BOTH state and patch in eval sequence success;
positive paired net wins must appear in at least two non-control families against
each baseline; no control regression, complete records, and zero format rejections
across all arms are required. The original gate is retained with an added format
validity condition. **A tie with patch stops this core-innovation route.**
No further same-route prompt tuning to rescue this evaluation. If the server fails
format constraints, the comparison is invalid for the intended format-controlled
claim; preserve the attempt, diagnose the infrastructure, don't count a selective
repair as method success. A pass is only permission to plan real-video validation,
not a novelty, significance or ICME acceptance claim.

Same model, temperature 0, one attempt per turn, max output 2048 tokens. The same
upper bound does not imply equal actual compute. Report per-arm tokens, latency,
sequence/turn accuracy, paired wins/losses, family differences and error breakdown.
Preflight warms the server and order rotates across sequences; latency is not GPU
time. Context truncation is not measured; retain `ollama ps` (expected 16384).
Provider finish_reason is saved; length finishes are counted. Full request messages,
response_format, raw content, model identity and usage are saved, excluding keys.

`local_transition_correct` is an OFFLINE diagnostic using annotated operations on
the arm's own state/history. `canonical_operation_matches` is syntax-sensitive,
not a complete semantic parsing score. Rejections are output errors, not evidence
of faulty executor code. Preservation violations include inherited and unscorable
errors; `new_off_target_changes` separates accepted new collateral changes.
Missing sequences fail the expected sequence denominator. `expected_turns` is the
full turn denominator. No video quality is measured.

Structured execution and selective undo have predecessors (PAL, selective undo,
Edit-R2, RefineCut). Even a positive V4 result is not sufficient method novelty.
