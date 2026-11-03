#!/usr/bin/env bash
# Run from any directory. Abort on the first error; never overwrite old runs.
set -euo pipefail
cd "$(dirname "$0")/../.."
output="${1:-runs/intent-executor-qwen14b-v4}"
if [ -e "$output" ]; then
  echo "Output already exists: $output. Preserve it; no automatic retry/resume." >&2
  exit 1
fi
export INTENT_BASE_URL="${INTENT_BASE_URL:-http://127.0.0.1:11434/v1}"
export INTENT_MODEL="${INTENT_MODEL:-qwen2.5:14b}"
export INTENT_API_KEY="${INTENT_API_KEY:-ollama}"
ollama_bin="${OLLAMA_BIN:-$HOME/apps/ollama/bin/ollama}"
python -m unittest discover -s research/intent_executor_v4 -p 'test_v4.py' -v
python research/intent_executor_v4/run.py prepare --output "$output"
"$ollama_bin" --version > "$output/ollama-version.txt"
"$ollama_bin" list > "$output/ollama-models.txt"
git rev-parse HEAD > "$output/code-commit.txt"
git status --porcelain > "$output/worktree-status.txt"
python -u research/intent_executor_v4/run.py preflight --output "$output" --timeout 600
python -u research/intent_executor_v4/run.py run --output "$output" --split dev --timeout 600
python research/intent_executor_v4/run.py score --output "$output" --split dev
"$ollama_bin" ps > "$output/ollama-ps-dev.txt"
python - "$output" <<'PY'
import json,sys
from pathlib import Path
report=json.loads((Path(sys.argv[1])/'dev-run/report.txt').read_text())
if any(m['counts'].get('format_errors',0) or m['counts'].get('unreturned_turns',0) for m in report['metrics'].values()):
    raise SystemExit('Dev format/transport failures: stop and inspect. Do not silently repair or rerun.')
PY
python -u research/intent_executor_v4/run.py run --output "$output" --split eval --timeout 600
python research/intent_executor_v4/run.py score --output "$output" --split eval
"$ollama_bin" ps > "$output/ollama-ps-eval.txt"
archive="${output}-audit.tar.gz"
if [ -e "$archive" ]; then echo "Archive already exists: $archive" >&2; exit 1; fi
tar -czf "$archive" "$output"
echo "Completed. Upload: $archive"
