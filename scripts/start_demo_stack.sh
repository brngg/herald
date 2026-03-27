#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' is not installed." >&2
    exit 1
  fi
}

start_port_forward() {
  local namespace="$1"
  local resource="$2"
  local binding="$3"
  local label="$4"
  local logfile="${TMPDIR:-/tmp}/herald-${label}.log"

  echo "Starting ${label} port-forward on ${binding}..."
  kubectl port-forward -n "${namespace}" "${resource}" "${binding}" >"${logfile}" 2>&1 &
  local pid=$!
  PORT_FORWARD_PIDS+=("${pid}")
  PORT_FORWARD_LOGS+=("${logfile}")
}

cleanup() {
  local pid
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
}

require_command kubectl

echo "== Starting or resuming minikube =="
"${SCRIPT_DIR}/start_minikube.sh"

echo
echo "== Deploying Google Online Boutique =="
"${SCRIPT_DIR}/deploy_boutique.sh"

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "Error: kubectl is not connected to a cluster." >&2
  exit 1
fi

declare -a PORT_FORWARD_PIDS=()
declare -a PORT_FORWARD_LOGS=()
trap cleanup EXIT INT TERM

echo
echo "== Starting monitoring port-forwards =="
start_port_forward "monitoring" "svc/monitoring-kube-prometheus-prometheus" "9090:9090" "prometheus"
start_port_forward "monitoring" "svc/monitoring-kube-prometheus-alertmanager" "9093:9093" "alertmanager"
start_port_forward "monitoring" "svc/monitoring-grafana" "3000:80" "grafana"

sleep 3

echo
echo "Monitoring UIs are available while this script is running:"
echo "  Prometheus:   http://localhost:9090"
echo "  Alertmanager: http://localhost:9093"
echo "  Grafana:      http://localhost:3000"
echo
echo "Suggested shell export for the recovery workflow:"
echo "  export PROMETHEUS_BASE_URL=http://localhost:9090"
echo
echo "Press Ctrl-C to stop all port-forwards."

for pid in "${PORT_FORWARD_PIDS[@]}"; do
  if ! kill -0 "${pid}" >/dev/null 2>&1; then
    echo "Error: one or more port-forwards exited early." >&2
    echo "Recent logs:" >&2
    for logfile in "${PORT_FORWARD_LOGS[@]}"; do
      echo "--- ${logfile} ---" >&2
      tail -n 20 "${logfile}" >&2 || true
    done
    exit 1
  fi
done

wait
