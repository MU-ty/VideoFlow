# Equal-budget identity verification ablation

Run this selection-only ablation from a VideoFlow checkout. It compares two
direct VLM calls per candidate with two archived structured calls per
candidate on the same frames. Two candidates per seed and two seeds yield
eight new calls. It uses Python 3, FFmpeg, and the Ollama model
`qwen2.5vl:7b-ctx8k`. No video generation is required.

Use the two original paired-run folders from the 90601 and 90602 archives,
or the `experiments/inputs/90601` and `experiments/inputs/90602` folders
from the VideoFlow v4.1 90602 LaTeX package. Pass those paths to the script:

```bash
export PATH="$HOME/apps/videoflow-ffmpeg/ffmpeg-7.0.2-amd64-static:$PATH"
python3 -u research/dependency_probe/equal_budget.py \
  --server http://127.0.0.1:11434 \
  --output "runs/egdr-equal-budget-$(date +%Y%m%d-%H%M%S)" \
  /path/to/inputs/90601 /path/to/inputs/90602
```

Add `--prepare-only` to verify frame and response hashes and assemble the
eight inputs without connecting to Ollama. The full run writes `plan.json`,
`responses.jsonl`, and `report.json`. Compare missed and excess shots and
check `direct_failed_calls` before interpreting the results. The report
uses archived structured responses. The same model tag alone cannot
establish that the archived and current runners use identical weights or
inference runtimes.
