#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' is not installed." >&2
    exit 1
  fi
}

wait_for_rollout() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  kubectl rollout status "${kind}/${name}" -n "${namespace}" --timeout=300s
}

require_command docker
require_command kubectl
require_command minikube
require_command helm

echo "== Starting or resuming minikube =="
"${SCRIPT_DIR}/start_minikube.sh"

echo
echo "== Deploying Google Online Boutique =="
"${SCRIPT_DIR}/deploy_boutique.sh"

echo
echo "== Installing kube-prometheus-stack =="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace

echo "Waiting for monitoring components..."
wait_for_rollout deployment monitoring-kube-prometheus-operator monitoring
wait_for_rollout deployment monitoring-grafana monitoring
wait_for_rollout statefulset prometheus-monitoring-kube-prometheus-prometheus monitoring
wait_for_rollout statefulset alertmanager-monitoring-kube-prometheus-alertmanager monitoring

echo
echo "== Installing Chaos Mesh =="
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh \
  --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=true

echo "Waiting for Chaos Mesh components..."
wait_for_rollout deployment chaos-controller-manager chaos-mesh
wait_for_rollout deployment chaos-dashboard chaos-mesh
kubectl rollout status daemonset/chaos-daemon -n chaos-mesh --timeout=300s

echo
echo "== Applying HERALD-specific manifests =="
kubectl apply -f "${REPO_ROOT}/k8s/chaos-mesh-rbac.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/prometheus/herald-blackbox-exporter.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/prometheus/herald-frontend-cart-probe.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/prometheus/herald-alert-rules.yaml"

echo "Waiting for HERALD probe components..."
wait_for_rollout deployment herald-blackbox-exporter monitoring

echo
echo "== Final cluster check =="
"${SCRIPT_DIR}/check_cluster.sh"

cat <<'EOF'

Environment is ready.

This script is safe to rerun when your minikube cluster already exists.
If Docker Desktop was stopped, start Docker again and rerun this script to
reconcile the cluster, redeploy workloads, and reapply the HERALD manifests.

Useful follow-up commands:
  kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
  kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093
  kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
  kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333
EOF
