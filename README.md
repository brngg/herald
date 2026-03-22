# HERALD
### Human-Evaluated Remediation Agent with Logged Decisions

> An agentic infrastructure operations platform where AI detects, diagnoses,
> and recommends remediations — but a human operator always has the final word.

---

## The Problem

Modern distributed systems fail constantly. Kubernetes pods crash, memory spikes,
services return 500s. The monitoring exists — Prometheus fires the alert — but
recovery is still manual, slow, and undocumented. On-call engineers are woken up
at 2am to run the same runbook they ran last week.

The gap isn't detection. The gap is **safe, explainable, auditable recovery.**

---

## The Solution

HERALD is an agentic platform that sits between your alerting system and your
operators. When an incident fires, HERALD investigates, reasons about the cause,
evaluates its own plan for safety, and brings a fully contextualized recommendation
to a human operator for approval. The operator decides. HERALD executes and verifies.

No autonomous actions. No black boxes. No unverified fixes.

---

## What HERALD Does

HERALD is an agent-assisted remediation platform built around three principles:

- **Human authority** — a human operator always approves before anything executes
- **Auditability** — every agent decision, judge verdict, and human approval is logged
- **Verified recovery** — pre-check and post-check on every remediation, with automatic rollback if verification fails

---

## Tech Stack

| Layer | Technology |
|---|---|
| Target system | Google Online Boutique (microservices demo) |
| Chaos injection | Chaos Mesh |
| Metrics & alerting | Prometheus + Grafana + Alertmanager |
| Agent orchestration | LangGraph |
| Workflow execution | Temporal.io |
| LLM evaluation/tracing | Langfuse |
| Human approval | Slack API |
| Infrastructure | Kubernetes (minikube) |

---

## Incident Scenarios

HERALD is evaluated against scripted incident scenarios:

| Scenario | Failure Type |
|---|---|
| Bad deployment crash loop | CrashLoopBackOff on `cartservice` |
| CPU saturation | High CPU / response-time degradation on `frontend` |
| Frontend bad cart config | User-facing HTTP failures from a bad `CART_SERVICE_ADDR` config |
| Dependency network disruption | Network partition from `frontend` to `cartservice` |

Live-verified:
- `crashloop-cartservice-bad-deploy.yaml`
- `chaos-frontend-cpu-saturation.yaml`
- `chaos-cartservice-network-partition.yaml`

“Pending final live verification:”
- `frontend-bad-cart-config.yaml` should keep the frontend process healthy while making `/cart` return deterministic user-visible errors.

---

## Project Status
“Current implementation is strongest in local environment setup, alert generation/routing, and Alertmanager-to-Incident ingestion. Fixer, Judge, HITL Gate, execution workflow, and evaluation harness are not yet implemented.”

- [x] Phase 1 — Environment: Google Online Boutique on minikube
- [x] Phase 2 — Observability: Prometheus + Grafana + Alertmanager
- [x] Phase 3 — Chaos: Chaos Mesh incident scenarios
- [ ] Phase 4 — Fixer Agent: LangGraph diagnosis + confidence scoring
- [ ] Phase 5 — Judge Layer: LLM safety evaluation + Langfuse tracing
- [ ] Phase 6 — Temporal Workflows: execution + pre/post checks + rollback
- [ ] Phase 7 — HITL: Slack approval integration
