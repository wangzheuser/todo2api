#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
target="${1:?usage: ROLLBACK.sh TARGET_FILE}"
cp "$root/ORIGINAL_FILE" "$target"
sha256sum "$target"
