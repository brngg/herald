#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PAYLOAD_FILE="${REPO_ROOT}/payloads/crashloop_alert.json"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
PROM_URL="${PROMETHEUS_BASE_URL:-http://localhost:9090}"
PLAN_OUTPUT="${TMPDIR:-/tmp}/herald-crashloop-plan.json"
APPLY_SCENARIO=true

while [[ "${#}" -gt 0 ]]; do
  case "$1" in
    --skip-apply)
      APPLY_SCENARIO=false
      shift
      ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      echo "Usage: ./scripts/run_crashloop_demo.sh [--skip-apply]" >&2
      exit 1
      ;;
  esac
done

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' is not installed." >&2
    exit 1
  fi
}

require_command kubectl

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: Python runner not found at ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -f "${PAYLOAD_FILE}" ]]; then
  echo "Error: payload file not found at ${PAYLOAD_FILE}" >&2
  exit 1
fi

if [[ "${APPLY_SCENARIO}" == "true" ]]; then
  echo "== Applying crashloop scenario =="
  kubectl apply -f "${REPO_ROOT}/k8s/crashloop-cartservice-bad-deploy.yaml"
fi

echo
echo "== Waiting for cartservice to enter a failing state =="
for _ in $(seq 1 30); do
  if kubectl get pods -n default | grep cartservice | grep -E "RunContainerError|CrashLoopBackOff" >/dev/null 2>&1; then
    kubectl get pods -n default | grep cartservice
    break
  fi
  sleep 2
done

echo
echo "== Running HERALD planning pass =="
"${PYTHON_BIN}" -m workflows.recovery_workflow \
  --payload-file "${PAYLOAD_FILE}" \
  --prometheus-base-url "${PROM_URL}" | tee "${PLAN_OUTPUT}"

ACTION_ID="$("${PYTHON_BIN}" - <<'PY' "${PLAN_OUTPUT}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

action = payload["hitl_decision"]["recommended_action"]
print(action["action_id"] if action else "")
PY
)"

echo
echo "Saved first-pass output to ${PLAN_OUTPUT}"

if [[ -z "${ACTION_ID}" ]]; then
  echo "No recommended action was returned. Inspect ${PLAN_OUTPUT} for details."
  exit 0
fi

echo "Recommended action_id: ${ACTION_ID}"
echo
echo "Next step:"
echo "\"${PYTHON_BIN}\" -m workflows.recovery_workflow \\"
echo "  --payload-file \"${PAYLOAD_FILE}\" \\"
echo "  --prometheus-base-url \"${PROM_URL}\" \\"
echo "  --approve-action-id \"${ACTION_ID}\""
