#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PAYLOAD_FILE="${REPO_ROOT}/payloads/unknown_dependency_alert.json"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
PROM_URL="${PROMETHEUS_BASE_URL:-http://localhost:9090}"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
ARTIFACT_DIR="${ARTIFACT_DIR:-${REPO_ROOT}/artifacts/unknown_dependency/${TIMESTAMP}}"
PLAN_OUTPUT="${ARTIFACT_DIR}/first-pass.json"
APPROVAL_OUTPUT="${ARTIFACT_DIR}/approval-run.json"
DECISION_MODE="prompt"

while [[ "${#}" -gt 0 ]]; do
  case "$1" in
    --auto-approve)
      DECISION_MODE="approve"
      shift
      ;;
    --artifact-dir)
      ARTIFACT_DIR="$2"
      PLAN_OUTPUT="${ARTIFACT_DIR}/first-pass.json"
      APPROVAL_OUTPUT="${ARTIFACT_DIR}/approval-run.json"
      shift 2
      ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      echo "Usage: ./scripts/run_unknown_dependency_escalation_demo.sh [--auto-approve] [--artifact-dir DIR]" >&2
      exit 1
      ;;
  esac
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: Python runner not found at ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -f "${PAYLOAD_FILE}" ]]; then
  echo "Error: payload file not found at ${PAYLOAD_FILE}" >&2
  exit 1
fi

mkdir -p "${ARTIFACT_DIR}"

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
  printf "Approve the escalation candidate now? 1=yes / anything else=stop: "
  read -r DECISION_INPUT
  if [[ "${DECISION_INPUT}" == "1" ]]; then
    DECISION_MODE="approve"
  else
    DECISION_MODE="stop"
  fi
fi

if [[ "${DECISION_MODE}" == "approve" ]]; then
  echo "== Running HERALD approval pass from saved first-pass artifact =="
  "${PYTHON_BIN}" -m workflows.recovery_workflow \
    --payload-file "${PAYLOAD_FILE}" \
    --prometheus-base-url "${PROM_URL}" \
    --resume-from-file "${PLAN_OUTPUT}" \
    --approve-action-id "${ACTION_ID}" | tee "${APPROVAL_OUTPUT}"

  echo
  echo "Saved approval-run output to ${APPROVAL_OUTPUT}"
  exit 0
fi

echo "Next step:"
echo "\"${PYTHON_BIN}\" -m workflows.recovery_workflow \\"
echo "  --payload-file \"${PAYLOAD_FILE}\" \\"
echo "  --prometheus-base-url \"${PROM_URL}\" \\"
echo "  --resume-from-file \"${PLAN_OUTPUT}\" \\"
echo "  --approve-action-id \"${ACTION_ID}\""
