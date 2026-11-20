# Table VI anchor-pair development diagnostic

This fixed-budget replay uses the archived 2×2 source sheets from the
three historically rendered Table VI seeds. Both arms receive the same
side-by-side frames, with S1 on the left and either S2 or S3 on the right.
The schedule takes panels 1, 2, and 4, totaling six single-image model
calls per seed per arm. The ordinal arm order alternates by input.
The baseline is direct Checklist on the new paired inputs. The other arm
records separate structured observations and derives the verdict in code.
Both aggregate with two consistent votes and no opposing vote, and keep
uncertain candidates. No expected shot labels are sent to the model.

The input ZIP is `egdr-strong-source-20261118-120156-screen.zip`.
Pass its extracted root, containing `source-report.json` and `screen/`.
Use Python 3, FFmpeg and the Ollama model already used for the VLM work.

```bash
python3 research/dependency_probe/run_anchor_pair.py \
  --source /path/to/extracted-source-sheets \
  --output runs/table-vi-anchor-diagnostic
```

`--prepare-only` validates original sheet checksums and generates the
paired inputs without querying Ollama. Use a fresh output directory for
inference. Archive `report.json`, `calls.jsonl`, `inputs.json` and
`cases.json`. Runs with malformed replies or API failures log errors and
exit nonzero, so they can be distinguished from valid runs.

This is a development check, not a new Table VI online result. Both arms
use paired views and six calls under this protocol, but the old online
experiment used different input and aggregation rules. If the diagnostic
is useful, freeze the procedure and use a new, prespecified source cohort.
The optional `--cases` JSON maps block names to `{"part": "...", "targets":
["S2"]}` or other required S2/S3 subsets, with the same archived-sheet and
checksum layout. A successful selection result should be followed by a
matched online run with the same publication gate before reporting render
or first-manifest improvements.
