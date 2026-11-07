#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
runs="runs/grace15-$(date +%Y%m%d-%H%M%S)"
python -u research/recovery_observability_comfy.py \
  --suite grace --repetitions "${1:-2}" --server "${COMFY_SERVER:-http://127.0.0.1:8188}" \
  --output "$runs" --poll-seconds 5 --horizon-seconds 1200
python research/summarize_comfy_observability.py \
  "$runs" >"$runs/paired-summary.json"
python -m zipfile -c "$runs.zip" "$runs"
printf 'Upload %s.zip\n' "$runs"
