#!/usr/bin/env bash
# Enforce and verify isolation before any untrusted workload starts.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
engine="${CONTAINER_ENGINE:-docker}"
cluster="${EP_KIND_CLUSTER_NAME:-integration-test}"
node="${cluster}-control-plane"
context="$(kubectl config current-context)"
[[ "$context" == "kind-$cluster" ]] || { echo "kubectl context must be kind-$cluster (found $context)" >&2; exit 1; }
kubectl() { command kubectl --context "kind-$cluster" "$@"; }
kubectl apply -f "$root/tests/execution_plane/network-probe.yaml"
kubectl wait --for=condition=Ready pod/compat-network-probe --timeout=120s
kubectl wait --for=condition=Ready pod/compat-network-peer --timeout=120s
peer_index="$(kubectl exec compat-network-probe -- cat /sys/class/net/eth0/iflink | tr -d '\r')"
"$engine" exec "$node" ip -o link show | grep -Eq "^${peer_index}: veth[^:]*:"
node_ip="$(kubectl get node "$node" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
api_ip="$(kubectl get service kubernetes -o jsonpath='{.spec.clusterIP}')"
peer_ip="$(kubectl get pod compat-network-peer -o jsonpath='{.status.podIP}')"
kubectl exec compat-network-probe -- nc -z -w 3 "$node_ip" 6443
kubectl exec compat-network-probe -- nc -z -w 3 "$api_ip" 443
kubectl exec compat-network-probe -- nc -z -w 3 "$peer_ip" 80

# Block forwarded traffic and node-local sockets, including IPv6 link-local.
"$engine" exec "$node" iptables -N COMPAT-EGRESS
"$engine" exec "$node" iptables -A COMPAT-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j RETURN
"$engine" exec "$node" iptables -A COMPAT-EGRESS -j REJECT
"$engine" exec "$node" iptables -I FORWARD 1 -s 10.244.0.0/16 -j COMPAT-EGRESS
"$engine" exec "$node" iptables -I FORWARD 1 -i veth+ -j COMPAT-EGRESS
"$engine" exec "$node" iptables -I INPUT 1 -s 10.244.0.0/16 -j REJECT
"$engine" exec "$node" iptables -I INPUT 1 -i veth+ -j REJECT
"$engine" exec "$node" ip6tables -I INPUT 1 -i veth+ -j REJECT
"$engine" exec "$node" ip6tables -I FORWARD 1 -i veth+ -j REJECT
# Allow replies to node-initiated connections such as kubelet probes.
"$engine" exec "$node" iptables -I INPUT 1 -s 10.244.0.0/16 -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
"$engine" exec "$node" iptables -I INPUT 1 -i veth+ -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
"$engine" exec "$node" ip6tables -I INPUT 1 -i veth+ -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
if kubectl exec compat-network-probe -- nc -z -w 3 "$node_ip" 6443; then
  echo 'Pod unexpectedly reached the node API endpoint' >&2
  exit 1
fi
if kubectl exec compat-network-probe -- nc -z -w 3 "$api_ip" 443; then
  echo 'Pod unexpectedly reached the Kubernetes API service' >&2
  exit 1
fi
if kubectl exec compat-network-probe -- nc -z -w 3 "$peer_ip" 80; then
  echo 'Pod unexpectedly reached another pod' >&2
  exit 1
fi
kubectl delete pod compat-network-probe compat-network-peer --wait=false
