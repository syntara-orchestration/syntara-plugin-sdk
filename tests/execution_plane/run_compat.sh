#!/usr/bin/env bash
# Run against a disposable, already-created Kind cluster and PostgreSQL database.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
ep="${EP_PATH:?Set EP_PATH to the pinned Execution Plane checkout}"
ep="$(cd "$ep" && pwd)"
engine="${CONTAINER_ENGINE:-docker}"
cluster="${EP_KIND_CLUSTER_NAME:-integration-test}"
node="${cluster}-control-plane"
registry="${EP_REGISTRY_NAME:-sdk-compat-registry}"
port="${EP_REGISTRY_PORT:-5001}"
logs="${EP_COMPAT_LOG_DIR:-$root/execution-plane-compat-logs}"
revision=67bea4e48dcb15063fcdce1b98b7bd7d443da961
[[ "$(git -C "$ep" rev-parse HEAD)" == "$revision" ]] || { echo "EP_PATH must be at $revision" >&2; exit 1; }
checkout_status="$(git -C "$ep" status --porcelain=v1 --untracked-files=all --ignored)"
[[ -z "$checkout_status" ]] || { echo 'EP_PATH must be a clean checkout, including ignored files' >&2; exit 1; }
# Establish the cluster identity before enabling diagnostics or running checkout code.
context="$(kubectl config current-context)"
[[ "$context" == "kind-$cluster" ]] || { echo "kubectl context must be kind-$cluster (found $context)" >&2; exit 1; }
kubectl() { command kubectl --context "kind-$cluster" "$@"; }
: "${EP_TEST_DATABASE_URL:?Set EP_TEST_DATABASE_URL to the disposable database}"
export EP_DATABASE_URL="$EP_TEST_DATABASE_URL"
export EP_CREDENTIAL_ENCRYPTION_KEY="${EP_CREDENTIAL_ENCRYPTION_KEY:-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=}"
export EP_KIND_CREDENTIAL_REF="${EP_KIND_CREDENTIAL_REF:-3679517c-ba49-5f27-868e-37e42d1184c1}"
export EP_API_URL="${EP_API_URL:-http://127.0.0.1:8001}"
export EP_DEFAULT_WORKLOAD_NAMESPACE="${EP_DEFAULT_WORKLOAD_NAMESPACE:-execution-plane}"
export EP_AO_JWT_PUBLIC_KEY_PATH="${EP_AO_JWT_PUBLIC_KEY_PATH:-.secrets/ao-jwt-public.pem}"
export EP_AO_JWT_ISSUER="${EP_AO_JWT_ISSUER:-http://localhost:8000}"
export UV_NO_SYNC=true
mkdir -p "$logs"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/sdk-ep-compat.XXXXXXXX")"
test_file="$ep/tests/integration/kind/test_sdk_checkout_compat.py"
[[ ! -e "$test_file" ]] || { echo "Refusing to overwrite $test_file" >&2; rm -rf "$tmp"; exit 1; }
registry_started=0
api_pid=""
secrets_owned=0
secrets_dir="$ep/.secrets"
cleanup() {
  status=$?
  if (( status != 0 )); then
    if [[ "$(command kubectl config current-context 2>/dev/null || true)" == "kind-$cluster" ]]; then
      kubectl get pods -A -o wide > "$logs/pods.txt" 2>&1 || true
      kubectl get events -A --sort-by=.lastTimestamp > "$logs/events.txt" 2>&1 || true
      kubectl describe pods -n execution-plane > "$logs/pod-details.txt" 2>&1 || true
    fi
    if (( registry_started )); then "$engine" logs "$registry" > "$logs/registry.log" 2>&1 || true; fi
    echo "Compatibility test failed; diagnostics: $logs" >&2
  fi
  [[ -z "$api_pid" ]] || kill "$api_pid" 2>/dev/null || true
  rm -f "$test_file"
  if (( secrets_owned )); then
    rm -rf -- "$secrets_dir" || status=1
  fi
  if (( registry_started )); then "$engine" rm -f "$registry" >/dev/null 2>&1 || true; fi
  rm -rf "$tmp"
  exit "$status"
}
trap cleanup EXIT

# Do not install either checkout as a package or run its build hooks.
(cd "$root" && UV_PROJECT_ENVIRONMENT="$tmp/sdk-venv" uv sync --locked --all-groups --no-build --no-install-project --no-install-package syntara-plugin-sdk --no-install-package syntara-plugin-contracts)
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv sync --locked --all-groups --no-build --no-install-project --no-install-package execution-plane-client)
export PYTHONPATH="$root/sdks/python/packages/sdk/src:$root/sdks/python/packages/contracts/src:$ep/src"
(cd "$root" && UV_PROJECT_ENVIRONMENT="$tmp/sdk-venv" uv run --no-sync python tests/execution_plane/prepare_image.py "$tmp/context")
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python "$root/tests/execution_plane/verify_checkout.py" "$ep" "$root")

if [[ "${EP_COMPAT_CREATE_DATABASE:-0}" == 1 ]]; then
  psql -d postgres -v ON_ERROR_STOP=1 -c 'CREATE ROLE execution_plane_runtime LOGIN' -c 'CREATE DATABASE execution_plane OWNER postgres'
fi
[[ ! -e "$secrets_dir" && ! -L "$secrets_dir" ]] || { echo "Refusing to overwrite $secrets_dir" >&2; exit 1; }
mkdir -m 700 -- "$secrets_dir"
secrets_owned=1
(cd "$ep" && DATABASE_URL="$EP_TEST_DATABASE_URL" UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync alembic -c alembic.ini upgrade head && make secrets)

# Host and node can reach the registry; workload pods are isolated below.
"$engine" run -d --name "$registry" -p "127.0.0.1:$port:5000" 'registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373' >/dev/null
registry_started=1
"$engine" network connect kind "$registry"
"$engine" exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:$port"
printf 'server = "http://localhost:%s"\n[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' "$port" "$registry" | "$engine" exec -i "$node" tee "/etc/containerd/certs.d/localhost:$port/hosts.toml" >/dev/null
image="localhost:$port/sdk-existing-step:compat"
"$engine" build -f "$tmp/context/Containerfile" -t "$image" "$tmp/context"
push_output="$("$engine" push "$image")"
printf '%s\n' "$push_output"
digest="$(printf '%s\n' "$push_output" | sed -nE 's/^.*digest: (sha256:[0-9a-f]{64}).*$/\1/p' | tail -n 1)"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Registry did not return a valid immutable image digest' >&2; exit 1; }
export EP_COMPAT_IMAGE="localhost:$port/sdk-existing-step@$digest"
export EP_COMPAT_REGISTRATION="$tmp/context/registration.json"
echo "Dispatching checkout image $EP_COMPAT_IMAGE"

CONTAINER_ENGINE="$engine" EP_KIND_CLUSTER_NAME="$cluster" bash "$root/tests/execution_plane/restrict_egress.sh"
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python -c 'from execution_plane.api.main import main; main()') > "$logs/api.log" 2>&1 &
api_pid=$!
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python -m tools.deploy_kind_execution_target --cluster-name "$cluster" --credential-ref "$EP_KIND_CREDENTIAL_REF" --wait-for-api 60)
kubectl patch serviceaccount default -n execution-plane --type=merge -p '{"automountServiceAccountToken":false}'
export EP_IT_K8S_ENDPOINT="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
token="$(kubectl create token syntara-dispatcher -n execution-plane --duration=2h)"
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then printf '::add-mask::%s\n' "$token"; fi
export EP_IT_K8S_TOKEN="$token"
unset token
export EP_IT_K8S_NAMESPACE=execution-plane
export EP_IT_K8S_CLUSTER_NAME="$cluster"
export EP_IT_K8S_CA_CERT="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)"
# Never print the token or write it to a workflow environment file.
cp "$root/tests/execution_plane/test_compat.py" "$test_file"
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync pytest "$test_file" -v --timeout=600 --junitxml="$logs/results.xml")
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python "$root/tests/execution_plane/check_results.py" "$logs/results.xml")
