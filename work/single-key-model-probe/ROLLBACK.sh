#!/usr/bin/env sh
set -eu
target="${1:?rollback target directory is required}"
rm -f -- \
  "$target/probe_models.py" \
  "$target/model_probe_results.json" \
  "$target/context_probe_results.json" \
  "$target/MODEL_PROBE_REPORT.md"
printf 'removed probe artifacts under: %s\n' "$target"
