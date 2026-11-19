#!/usr/bin/env bash
# Local composition evidence for the ANSTRAT-2347 node-sdk POC (Phase 2).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EXAMPLE="${ROOT}/examples/subworkflow-composition"
INPUT="$(mktemp)"
trap 'rm -f "${INPUT}"' EXIT

cd "${ROOT}"

echo "==> pytest examples/subworkflow-composition"
uv run --no-sync pytest examples/subworkflow-composition -q

echo "==> syntara-plugin run (parent Control stand-in)"
printf '%s\n' '{"artifact":"syntara-cli","channel":"stable"}' > "${INPUT}"
uv run --no-sync syntara-plugin run \
  --manifest "${EXAMPLE}/plugin.yaml" \
  call-publish-artifact \
  --input-file "${INPUT}"

echo "==> Phase 2 local composition evidence complete"
