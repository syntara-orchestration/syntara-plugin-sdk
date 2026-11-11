#!/usr/bin/env bash
# Run the step types compatibility check locally, the way CI runs it.
#
#   make test-step-types                              # every step type at the pinned revision
#   make test-step-types STEP_TYPES_PLUGIN=http-request
#   make test-step-types STEP_TYPES_REF=devel          # any commit, branch, or tag
#   make test-step-types STEP_TYPES_MODE=report-only:dispatch   # report without failing
#
# Needs uv, git, make, jq, and podman. Works in .cache/step-types-compat/.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
workflow="$root/.github/workflows/step-types-compat.yml"
cache="$root/.cache/step-types-compat"
checkout="$cache/step-types"
wheels="$cache/wheels"
reports="$cache/reports"
here="$root/tests/step_types"

pinned="$(sed -n "s/.*STEP_TYPES_REF: .*|| '\([0-9a-f]\{40\}\)'.*/\1/p" "$workflow")"
ref="${STEP_TYPES_REF:-$pinned}"
if [[ -z "$ref" ]]; then
  echo "could not read the pinned STEP_TYPES_REF from $workflow" >&2
  exit 2
fi

mkdir -p "$cache"
if [[ ! -d "$checkout/.git" ]]; then
  git clone --quiet https://github.com/syntara-orchestration/syntara-step-types.git "$checkout"
fi
git -C "$checkout" fetch --quiet origin "$ref"
git -C "$checkout" checkout --quiet --force --detach FETCH_HEAD
git -C "$checkout" clean -fdq

rm -rf "$wheels" "$reports"
mkdir -p "$wheels" "$reports"
dirty=""
if [[ -n "$(git -C "$root" status --porcelain --untracked-files=no)" ]]; then
  dirty=" plus uncommitted changes"
fi
echo "Building candidate wheels from the working tree (HEAD $(git -C "$root" rev-parse --short HEAD)$dirty)..."
(cd "$root" && uv build --all-packages --out-dir "$wheels" --quiet)

tooling=success
echo "Running the tooling tests..."
if ! (cd "$root" && uv run --no-project --no-build --python 3.12 --with 'pytest>=8' \
    python -m pytest -q -p no:cacheprovider tests/step_types); then
  tooling=failure
fi

if [[ -n "${STEP_TYPES_PLUGIN:-}" ]]; then
  plugins_json="[\"$STEP_TYPES_PLUGIN\"]"
else
  plugins_json="$(uv run --no-project --no-build --python 3.12 python "$here/discover.py" "$checkout" | tail -n 1)"
fi
plugins="$(printf '%s' "$plugins_json" | uv run --no-project --no-build --python 3.12 python -c \
  'import json, sys; print(" ".join(json.load(sys.stdin)))')"

compat=success
for plugin in $plugins; do
  git -C "$checkout" reset --quiet --hard
  git -C "$checkout" clean -fdq
  if ! uv run --no-project --no-build --python 3.12 python "$here/driver.py" \
      --plugin "$plugin" \
      --step-types-dir "$checkout" \
      --wheels-dir "$wheels" \
      --report-dir "$reports" \
      --mode "${STEP_TYPES_MODE:-enforcing}"; then
    compat=failure
  fi
done

uv run --no-project --no-build --python 3.12 python "$here/report.py" \
  --reports-dir "$reports" \
  --plugins "$plugins_json" \
  --changes success \
  --run-compat true \
  --discover success \
  --tooling "$tooling" \
  --compat "$compat"
