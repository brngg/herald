#!/usr/bin/env bash
set -euo pipefail

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' is not installed." >&2
    exit 1
  fi
}

require_command kubectl
require_command minikube

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "Error: kubectl is not connected to a cluster. Run scripts/start_minikube.sh first." >&2
  exit 1
fi

printf '== Minikube status ==\n'
minikube status

printf '\n== Nodes ==\n'
kubectl get nodes -o wide

printf '\n== Pods ==\n'
kubectl get pods -n default

printf '\n== Services ==\n'
kubectl get services -n default

printf '\n== Frontend URL ==\n'
if ! minikube service frontend-external --url; then
  echo "frontend-external is not ready yet." >&2
fi
