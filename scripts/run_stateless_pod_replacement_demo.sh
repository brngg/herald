#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PAYLOAD_FILE="${REPO_ROOT}/payloads/pod_unhealthy_alert.json"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
PROM_URL="${PROMETHEUS_BASE_URL:-http://localhost:9090}"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
ARTIFACT_DIR="${ARTIFACT_DIR:-${REPO_ROOT}/artifacts/pod_replacement/${TIMESTAMP}}"
PLAN_OUTPUT="${ARTIFACT_DIR}/first-pass.json"
APPROVAL_OUTPUT="${ARTIFACT_DIR}/approval-run.json"
REJECTION_OUTPUT="${ARTIFACT_DIR}/rejection-run.json"
TERMINAL_LOG="${ARTIFACT_DIR}/worker-stream.log"
DECISION_MODE="prompt"
DEPLOYMENT="cartservice"
NAMESPACE="default"

while [[ "${#}" -gt 0 ]]; do
  case "$1" in
    --deployment)
      DEPLOYMENT="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --payload-file)
      PAYLOAD_FILE="$2"
      shift 2
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
      echo "Usage: ./scripts/run_stateless_pod_replacement_demo.sh [--deployment NAME] [--namespace NS] [--payload-file FILE] [--auto-approve|--auto-reject] [--artifact-dir DIR]" >&2
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

mkdir -p "${ARTIFACT_DIR}"

DESIRED_REPLICAS="$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
READY_REPLICAS="$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
NON_READY_PODS="$(kubectl get pods -n "${NAMESPACE}" -l "app=${DEPLOYMENT}" --field-selector=status.phase!=Succeeded --no-headers 2>/dev/null | awk '$2 !~ /^[0-9]+\/[0-9]+$/ || $2 !~ /^([0-9]+)\/\1$/ {print $1}')"

echo "== Checking stateless pod replacement preconditions =="
echo "deployment=${DEPLOYMENT} desired_replicas=${DESIRED_REPLICAS:-unknown} ready_replicas=${READY_REPLICAS:-unknown}"
kubectl get pods -n "${NAMESPACE}" -l "app=${DEPLOYMENT}" || true

if [[ -z "${DESIRED_REPLICAS}" || "${DESIRED_REPLICAS}" -le 1 ]]; then
  echo "Error: deployment ${DEPLOYMENT} must have more than one replica for this validation helper." >&2
  exit 1
fi

if [[ -z "${NON_READY_PODS}" ]]; then
  echo "Error: no non-ready pod was found for app=${DEPLOYMENT}. This helper expects a preconditioned isolated unhealthy pod." >&2
  echo "Tip: use the replay scenario for now, or set up a controlled non-ready pod case before running this helper." >&2
  exit 1
fi

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

candidate = payload["hitl_decision"].get("recommended_candidate")
print(candidate.get("candidate_id", "") if isinstance(candidate, dict) else "")
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
  echo "== Running HERALD approval pass from saved first-pass artifact =="
  "${PYTHON_BIN}" -m workflows.recovery_workflow \
    --payload-file "${PAYLOAD_FILE}" \
    --prometheus-base-url "${PROM_URL}" \
    --resume-from-file "${PLAN_OUTPUT}" \
    --approve-action-id "${ACTION_ID}" \
    2> >(tee "${TERMINAL_LOG}" >&2) | tee "${APPROVAL_OUTPUT}"

  echo
  echo "Saved approval-run output to ${APPROVAL_OUTPUT}"
  echo "Saved worker stream to ${TERMINAL_LOG}"
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
