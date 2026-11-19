#!/usr/bin/env bash
# Run against a disposable, already-created Kind cluster and PostgreSQL database.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
ep="${EP_PATH:?Set EP_PATH to the Execution Plane checkout}"
ep="$(cd "$ep" && pwd)"
revision="${EP_REVISION:?Set EP_REVISION to the resolved Execution Plane commit SHA}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'EP_REVISION must be a full 40-character commit SHA' >&2; exit 1; }
engine="${CONTAINER_ENGINE:-docker}"
# EP tooling shells out to kind, which must use the same engine.
if [[ "$engine" == *podman* ]]; then export KIND_EXPERIMENTAL_PROVIDER=podman; fi
cluster="${EP_KIND_CLUSTER_NAME:-integration-test}"
node="${cluster}-control-plane"
registry="${EP_REGISTRY_NAME:-sdk-compat-registry}"
port="${EP_REGISTRY_PORT:-5001}"
logs="${EP_COMPAT_LOG_DIR:-$root/execution-plane-compat-logs}"
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
runtime_container=""
runtime_started=0
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
      kubectl logs -n execution-plane --all-containers --prefix \
        --tail=-1 -l execution-plane.syntara.io/work-item \
        > "$logs/worker-pod-logs.txt" 2>&1 || true
    fi
    if (( registry_started )); then "$engine" logs "$registry" > "$logs/registry.log" 2>&1 || true; fi
    if (( runtime_started )); then "$engine" logs "$runtime_container" > "$logs/runtime-server.log" 2>&1 || true; fi
    echo "Compatibility test failed; diagnostics: $logs" >&2
  fi
  [[ -z "$api_pid" ]] || kill "$api_pid" 2>/dev/null || true
  # Scan every collected log and result for the credential sentinel. This
  # runs on success and failure alike, after diagnostics collection, so a
  # leak into the worker-pod or runtime logs cannot escape the run. On a
  # hit, redact the value from every affected file before the workflow
  # uploads the log directory as an artifact; the sentinel is plain hex,
  # so it is safe as a sed pattern.
  if [[ -n "${EP_COMPAT_CREDENTIAL_TOKEN:-}" ]] \
    && grep -R -F -q -- "$EP_COMPAT_CREDENTIAL_TOKEN" "$logs" 2>/dev/null; then
    echo 'The credential sentinel value appeared in compatibility logs or test output and was redacted before upload' >&2
    status=1
    grep -R -l -F -- "$EP_COMPAT_CREDENTIAL_TOKEN" "$logs" 2>/dev/null | while read -r affected; do
      sed -i "s/$EP_COMPAT_CREDENTIAL_TOKEN/REDACTED-CREDENTIAL-SENTINEL/g" "$affected" || true
    done
  fi
  rm -f "$test_file"
  if (( secrets_owned )); then
    rm -rf -- "$secrets_dir" || status=1
  fi
  if (( runtime_started )); then "$engine" rm -f "$runtime_container" >/dev/null 2>&1 || true; fi
  if (( registry_started )); then "$engine" rm -f "$registry" >/dev/null 2>&1 || true; fi
  rm -rf "$tmp"
  exit "$status"
}
trap cleanup EXIT

# Do not install either checkout as a package or run its build hooks.
(cd "$root" && UV_PROJECT_ENVIRONMENT="$tmp/sdk-venv" uv sync --locked --all-groups --no-build --no-install-project --no-install-package syntara-plugin-sdk --no-install-package syntara-plugin-contracts)
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv sync --locked --all-groups --no-build --no-install-project --no-install-package execution-plane-client)
export PYTHONPATH="$root/sdks/python/packages/sdk/src:$root/sdks/python/packages/contracts/src:$ep/src"
(cd "$root" && UV_PROJECT_ENVIRONMENT="$tmp/sdk-venv" uv run --no-sync python tests/execution_plane/prepare_image.py "$tmp/context" existing-step)
(cd "$root" && UV_PROJECT_ENVIRONMENT="$tmp/sdk-venv" uv run --no-sync python tests/execution_plane/prepare_image.py "$tmp/credential-context" credential-steps)
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python "$root/tests/execution_plane/verify_checkout.py" "$ep" "$root")

if [[ "${EP_COMPAT_CREATE_DATABASE:-0}" == 1 ]]; then
  psql -d postgres -v ON_ERROR_STOP=1 -c 'CREATE ROLE execution_plane_runtime LOGIN' -c 'CREATE DATABASE execution_plane OWNER postgres'
fi
[[ ! -e "$secrets_dir" && ! -L "$secrets_dir" ]] || { echo "Refusing to overwrite $secrets_dir" >&2; exit 1; }
mkdir -m 700 -- "$secrets_dir"
secrets_owned=1
(cd "$ep" && DATABASE_URL="$EP_TEST_DATABASE_URL" UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync alembic -c alembic.ini upgrade head && make secrets)

# Host and node can reach the registry; workload pods are isolated below.
# The registry joins the Kind network at creation: engines based on rootless
# networking cannot hot-attach a container that publishes ports.
"$engine" run -d --name "$registry" --network kind -p "127.0.0.1:$port:5000" 'registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373' >/dev/null
registry_started=1
"$engine" exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:$port"
printf 'server = "http://localhost:%s"\n[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' "$port" "$registry" | "$engine" exec -i "$node" tee "/etc/containerd/certs.d/localhost:$port/hosts.toml" >/dev/null

# Push one prepared context by immutable digest and export its reference.
push_image() {
  local context="$1" name="$2" export_name="$3"
  local image push_output digest
  local push_flags=()
  # Podman does not treat a localhost registry as insecure by default (docker
  # does and rejects the flag), and prints no digest line to parse
  # (--digestfile writes it instead).
  if [[ "$engine" == *podman* ]]; then
    push_flags=(--tls-verify=false --digestfile "$tmp/$name.push-digest")
  fi
  image="localhost:$port/$name:compat"
  "$engine" build -f "$context/Containerfile" -t "$image" "$context"
  push_output="$("$engine" push "${push_flags[@]}" "$image")"
  printf '%s\n' "$push_output"
  if [[ "$engine" == *podman* ]]; then
    digest="$(<"$tmp/$name.push-digest")"
  else
    digest="$(printf '%s\n' "$push_output" | sed -nE 's/^.*digest: (sha256:[0-9a-f]{64}).*$/\1/p' | tail -n 1)"
  fi
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Registry did not return a valid immutable image digest' >&2; return 1; }
  export "$export_name=localhost:$port/$name@$digest"
}

push_image "$tmp/context" sdk-existing-step EP_COMPAT_IMAGE
export EP_COMPAT_REGISTRATION="$tmp/context/registration.json"
echo "Dispatching checkout image $EP_COMPAT_IMAGE"
push_image "$tmp/credential-context" sdk-credential-steps EP_COMPAT_CREDENTIAL_IMAGE
export EP_COMPAT_CREDENTIAL_REGISTRATION="$tmp/credential-context/registration.json"
echo "Dispatching credential checkout image $EP_COMPAT_CREDENTIAL_IMAGE"

# Confirm that a staged checkout image can start its runtime server before
# Kubernetes owns and promptly cleans up a failed worker pod.
check_runtime_container() {
  local image="$1"
  local run_image="$image"
  # Podman cannot resolve a digest-pinned registry reference from the local
  # store and would try to pull it over TLS; the local tag is the same image.
  if [[ "$engine" == *podman* ]]; then run_image="${image%@sha256:*}:compat"; fi
  runtime_container="sdk-compat-runtime-${RANDOM}"
  "$engine" run -d --name "$runtime_container" "$run_image" >/dev/null
  runtime_started=1
  sleep 1
  if [[ "$("$engine" inspect --format '{{.State.Running}}' "$runtime_container")" != true ]]; then
    echo 'Checkout runtime image exited during startup' >&2
    return 1
  fi
  if ! "$engine" exec "$runtime_container" python3.12 -c '
import grpc
channel = grpc.insecure_channel("127.0.0.1:50051")
grpc.channel_ready_future(channel).result(timeout=5)
'; then
    echo 'Checkout runtime image did not accept a gRPC connection through its compatibility bridge' >&2
    return 1
  fi
  "$engine" rm -f "$runtime_container" >/dev/null
  runtime_started=0
}

check_runtime_container "$EP_COMPAT_IMAGE"
check_runtime_container "$EP_COMPAT_CREDENTIAL_IMAGE"

CONTAINER_ENGINE="$engine" EP_KIND_CLUSTER_NAME="$cluster" bash "$root/tests/execution_plane/restrict_egress.sh"
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python -c 'from execution_plane.api.main import main; main()') > "$logs/api.log" 2>&1 &
api_pid=$!
target_flags=(--cluster-name "$cluster" --credential-ref "$EP_KIND_CREDENTIAL_REF" --wait-for-api 60)
if [[ "$engine" == *podman* ]]; then
  # A rootless kind network is not routable from the host; register the
  # kubeconfig's host endpoint instead of the node IP.
  target_flags+=(--host-worker)
fi
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python -m tools.deploy_kind_execution_target "${target_flags[@]}")
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

# The synthetic credential value for the credential dispatch cases: random
# per run, never written to disk, masked in CI, and scanned for in every
# collected log and result afterwards.
sentinel="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
[[ ${#sentinel} -ge 16 ]] || { echo 'Could not generate a credential sentinel' >&2; exit 1; }
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then printf '::add-mask::%s\n' "$sentinel"; fi
export EP_COMPAT_CREDENTIAL_TOKEN="$sentinel"
unset sentinel

cp "$root/tests/execution_plane/test_compat.py" "$test_file"
compat_status=0
if ! (cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync pytest "$test_file" -v --timeout=600 --junitxml="$logs/results.xml"); then
  compat_status=1
fi
(cd "$ep" && UV_PROJECT_ENVIRONMENT="$tmp/ep-venv" uv run --no-sync python "$root/tests/execution_plane/check_results.py" "$logs/results.xml")
# The sentinel scan lives in the exit trap, after diagnostics collection.
exit "$compat_status"
