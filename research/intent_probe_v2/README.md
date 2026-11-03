# Intent probe v2: stronger controls after the v1 audit

## Question and status

V1's uploaded traces show history leaving S6 red, state returning it to the initial white, and ledger returning the correct pre-edit blue in the same scoped-undo template across three contexts. This is a post-V1, author-designed exploratory test, not confirmation of a novel method. No real-model result for v2 has been measured by the implementer.

Four arms use the same Qwen model, temperature, full interaction history, initial state, most recent predicted state, and maximum output allowance (2048 tokens by default):

- `state`: strong state-table prompt.
- `notes`: same, with self-generated free prose notes stored for later turns.
- `undo`: same state table with an explicit prompt to find the specified turn's **before** value, rather than initial or after value.
- `ledger`: same state table with self-generated structured before/after records.

Notes and ledger both receive their own memory explicitly and inside the full raw-answer history, matching memory duplication in v1. Gold records are never supplied. All arms output full state and a memory object; memory contents are not enforced beyond being a JSON object, so adherence to the respective recording instructions must be inspected. Notes and ledger have equal output caps, **not equal realized token cost**; account for actual usage and latency. This does not isolate structure from all possible computation/verbosity effects.

## Cases and frozen decision

Four development sequences (14 turns, 56 calls) and twelve evaluation sequences (50 turns, 200 calls). Evaluation includes two simple controls plus scoped subtitle/light/color undo, undo of undo, no-op history, copy followed by undo, two-round restoration, later overrides, multi-field partial undo and a partial slice of a global edit. Scene IDs and initial values differ from v1. Every undo has a specified round and field; snapshot copy and undo semantics are public to all methods. These annotations still need independent human review.

The author inspected v1 and wrote v2. Some semantic families overlap, and all cases remain synthetic state tasks. Do not call the evaluation independently authored user behavior, a video-quality test, or evidence of broad generalization. The local unit tests check representative exact expected values, prompt isolation, tamper detection, complete fake-call flow, no-retry protection and conservative incomplete scoring. Fake-call tests are not model accuracy.

Before first model calls, freeze case/prompt/runner hashes via `prepare`. Make no prompt adjustments between dev and eval for this pass. If dev exposes an implementation error, preserve the failed pass, fix under a new version, and document any reused evaluation data. Do not retune on v2 eval after seeing results.

**Exploratory continuation gate:** ledger must beat each of state, notes and undo in evaluation sequence successes, with no control regression and all records complete. Tying either stronger control fails this gate. All costs and individual paired wins/losses must be reported. Passing is only a reason for independent validation, not statistical significance or proof of novelty. If a trivial undo prompt or free notes catches up, pause structured-memory development as a distinct contribution. If all methods are nearly perfect, do not manufacture harder examples and reuse the same split as confirmation.

## Run on the existing local Ollama service

Python 3.10+, standard library only. No ComfyUI/H3 invocation. Keep `ollama serve` running with the previously configured 16K context. Ensure the model is still `qwen2.5:14b`; record `ollama --version`, `ollama list`, and `ollama ps` alongside the outputs. The runner records requested model and returned model names but not a complete binary/model digest or server context configuration; retain these manually. Server model caching affects latency; totals are not calibrated GPU time.

From repository root:

```bash
export INTENT_BASE_URL=http://127.0.0.1:11434/v1
export INTENT_MODEL=qwen2.5:14b
export INTENT_API_KEY=ollama
python -m unittest discover -s research/intent_probe_v2 -p 'test_v2.py' -v
python research/intent_probe_v2/run.py prepare --output runs/intent-qwen14b-v2
python -u research/intent_probe_v2/run.py run --output runs/intent-qwen14b-v2 --split dev --timeout 600
python research/intent_probe_v2/run.py score --output runs/intent-qwen14b-v2 --split dev
python -u research/intent_probe_v2/run.py run --output runs/intent-qwen14b-v2 --split eval --timeout 600
python research/intent_probe_v2/run.py score --output runs/intent-qwen14b-v2 --split eval
```

The local key is an ignored placeholder, not a paid API credential. Each arm/sequence result prints only when complete; quiet periods during inference are expected. No automatic resume or retry: existing record files, including pending requests, block reruns. On any failure stop, score partial output if useful, retain all files, and inspect before creating a fresh version/directory. An interrupted request may already have executed remotely. Do not overwrite original attempts or retain only successful reruns. Strict JSON formatting failures count as errors.

Results: `dev-run/report.txt` and `eval-run/report.txt` contain sequence/turn correctness, undo-turn correctness, off-target-field mismatch, format/request errors, latency, available token totals and pairwise ledger comparisons against all three controls. Missing sequences count against the expected sequence denominator; incomplete observed-turn totals are not a full-run denominator. Off-target mismatches may be inherited errors rather than current-turn changes. Missing usage makes token totals an observed lower bound. No independent inference call is made during scoring.

To submit the complete audit:

```bash
tar -czf runs/intent-qwen14b-v2-audit.tar.gz runs/intent-qwen14b-v2
```

Preserve v1 and its original archive. Do not combine development and evaluation into an independent sample count.
