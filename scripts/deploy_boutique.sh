#!/usr/bin/env bash
set -euo pipefail

MANIFEST_URL="${BOUTIQUE_MANIFEST_URL:-https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' is not installed." >&2
    exit 1
  fi
}

require_command kubectl

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "Error: kubectl is not connected to a cluster. Run scripts/start_minikube.sh first." >&2
  exit 1
fi

echo "Applying Google Online Boutique manifests..."
kubectl apply -f "$MANIFEST_URL"

echo "Waiting for Online Boutique deployments to roll out..."
deployments=()
while IFS= read -r deployment; do
  [[ -n "$deployment" ]] || continue
  deployments+=("$deployment")
done < <(kubectl get deployments -n default -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

if [[ "${#deployments[@]}" -eq 0 ]]; then
  echo "Error: no deployments were found in the default namespace after apply." >&2
  exit 1
fi

for deployment in "${deployments[@]}"; do
  [[ -n "$deployment" ]] || continue
  kubectl rollout status "deployment/${deployment}" -n default --timeout=300s
done

echo "Google Online Boutique is deployed."
