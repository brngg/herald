#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PAYLOAD_FILE="${REPO_ROOT}/payloads/readiness_shortfall_alert.json"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
PROM_URL="${PROMETHEUS_BASE_URL:-http://localhost:9090}"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
ARTIFACT_DIR="${ARTIFACT_DIR:-${REPO_ROOT}/artifacts/readiness_shortfall/${TIMESTAMP}}"
PLAN_OUTPUT="${ARTIFACT_DIR}/first-pass.json"
APPROVAL_OUTPUT="${ARTIFACT_DIR}/approval-run.json"
REJECTION_OUTPUT="${ARTIFACT_DIR}/rejection-run.json"
TERMINAL_LOG="${ARTIFACT_DIR}/worker-stream.log"
APPLY_SCENARIO=true
DECISION_MODE="prompt"
PROM_QUERY='sum(kube_pod_status_ready{namespace="default",condition="true",pod=~"frontend-.*"})'
DEPLOYMENT="frontend"
NAMESPACE="default"

while [[ "${#}" -gt 0 ]]; do
  case "$1" in
    --skip-apply)
      APPLY_SCENARIO=false
      shift
      ;;
    --auto-approve)
      DECISION_MODE="approve"
      shift
      ;;
    --auto-reject)
      DECISION_MODE="reject"
      shift
      ;;
    --artifact-dir)
      ARTIFACT_DIR="$2"
      PLAN_OUTPUT="${ARTIFACT_DIR}/first-pass.json"
      APPROVAL_OUTPUT="${ARTIFACT_DIR}/approval-run.json"
      REJECTION_OUTPUT="${ARTIFACT_DIR}/rejection-run.json"
      TERMINAL_LOG="${ARTIFACT_DIR}/worker-stream.log"
      shift 2
      ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      echo "Usage: ./scripts/run_scale_shortfall_demo.sh [--skip-apply] [--auto-approve|--auto-reject] [--artifact-dir DIR]" >&2
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
require_command curl

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: Python runner not found at ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -f "${PAYLOAD_FILE}" ]]; then
  echo "Error: payload file not found at ${PAYLOAD_FILE}" >&2
  exit 1
fi

mkdir -p "${ARTIFACT_DIR}"

if [[ "${APPLY_SCENARIO}" == "true" ]]; then
  echo "== Applying readiness shortfall scenario =="
  kubectl scale "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --replicas=0
fi

echo
echo "== Waiting for frontend ready replicas to reach zero =="
for _ in $(seq 1 30); do
  READY_REPLICAS="$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  READY_REPLICAS="${READY_REPLICAS:-0}"
  if [[ "${READY_REPLICAS}" == "0" ]]; then
    kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}"
    break
  fi
  sleep 2
done

wait_for_prometheus_shortfall() {
  echo
  echo "== Waiting for Prometheus readiness shortfall signal to catch up =="
  for _ in $(seq 1 30); do
    RESPONSE="$(curl -sG "${PROM_URL%/}/api/v1/query" --data-urlencode "query=${PROM_QUERY}" || true)"
    VALUE="$("${PYTHON_BIN}" - <<'PY' "${RESPONSE}"
import json
import sys

payload_text = sys.argv[1]
if not payload_text.strip():
    print("unknown")
    raise SystemExit(0)

try:
    payload = json.loads(payload_text)
except json.JSONDecodeError:
    print("unknown")
    raise SystemExit(0)

result = payload.get("data", {}).get("result", [])
if not result:
    print("0")
    raise SystemExit(0)

sample = result[0].get("value", [None, "0"])[1]
print(sample)
PY
)"
    if [[ "${VALUE}" == "0" || "${VALUE}" == "0.0" ]]; then
      echo "Prometheus readiness query is now ${VALUE}"
      return 0
    fi
    sleep 2
  done

  echo "Warning: Prometheus readiness shortfall signal did not turn zero before timeout." >&2
  return 1
}

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

hitl_decision = payload["hitl_decision"]
candidate = hitl_decision.get("recommended_candidate")
if isinstance(candidate, dict):
    print(candidate.get("candidate_id", ""))
else:
    print("")
PY
)"

echo
echo "Saved first-pass output to ${PLAN_OUTPUT}"
echo "Artifacts directory: ${ARTIFACT_DIR}"

if [[ -z "${ACTION_ID}" ]]; then
  echo "No recommended approval target was returned. Inspect ${PLAN_OUTPUT} for details."
  exit 0
fi

echo "Recommended approval id: ${ACTION_ID}"
echo

if [[ "${DECISION_MODE}" == "prompt" ]]; then
  printf "Choose next step: 1=approve / 2=reject / anything else=stop: "
  read -r DECISION_INPUT
  case "${DECISION_INPUT}" in
    1)
      DECISION_MODE="approve"
      ;;
    2)
      DECISION_MODE="reject"
      ;;
    *)
      DECISION_MODE="stop"
      ;;
  esac
fi

if [[ "${DECISION_MODE}" == "approve" ]]; then
  if ! wait_for_prometheus_shortfall; then
    echo "Refusing to auto-approve because Prometheus never confirmed the readiness shortfall." >&2
    exit 1
  fi
  echo "== Running HERALD approval pass from saved first-pass artifact =="
  "${PYTHON_BIN}" -m workflows.recovery_workflow \
    --payload-file "${PAYLOAD_FILE}" \
    --prometheus-base-url "${PROM_URL}" \
    --resume-from-file "${PLAN_OUTPUT}" \
    --approve-action-id "${ACTION_ID}" \
    2> >(tee "${TERMINAL_LOG}" >&2) | tee "${APPROVAL_OUTPUT}"

  FINAL_STATE="$("${PYTHON_BIN}" - <<'PY' "${APPROVAL_OUTPUT}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

print(payload["decision_trace"]["final_state"])
PY
)"

  echo
  echo "Saved approval-run output to ${APPROVAL_OUTPUT}"
  echo "Saved worker stream to ${TERMINAL_LOG}"
  echo "Final DecisionTrace state: ${FINAL_STATE}"
  exit 0
fi

if [[ "${DECISION_MODE}" == "reject" ]]; then
  echo "== Running HERALD rejection pass from saved first-pass artifact =="
  "${PYTHON_BIN}" -m workflows.recovery_workflow \
    --payload-file "${PAYLOAD_FILE}" \
    --prometheus-base-url "${PROM_URL}" \
    --resume-from-file "${PLAN_OUTPUT}" \
    --reject-action-id "${ACTION_ID}" | tee "${REJECTION_OUTPUT}"

  echo
  echo "Saved rejection-run output to ${REJECTION_OUTPUT}"
  exit 0
fi

echo "Next step:"
echo "\"${PYTHON_BIN}\" -m workflows.recovery_workflow \\"
echo "  --payload-file \"${PAYLOAD_FILE}\" \\"
echo "  --prometheus-base-url \"${PROM_URL}\" \\"
echo "  --resume-from-file \"${PLAN_OUTPUT}\" \\"
echo "  --approve-action-id \"${ACTION_ID}\""
