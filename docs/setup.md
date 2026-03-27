# Environment Setup

## Prerequisites
- Docker Desktop
- kubectl
- minikube
- Helm

## One-command bootstrap
```bash
./scripts/bootstrap_herald_env.sh
```

This script:
- starts minikube
- deploys Google Online Boutique
- installs Prometheus, Grafana, and Alertmanager via `kube-prometheus-stack`
- installs Chaos Mesh
- applies the HERALD-specific alert rules and synthetic probe manifests

It is also safe to rerun when your minikube cluster already exists.
If Docker Desktop was stopped, start Docker again and rerun:

```bash
./scripts/bootstrap_herald_env.sh
```

The script is intended to reconcile the existing cluster back to the expected local HERALD state, not only to perform first-time setup.

## Start Demo Stack And Expose Monitoring UIs
```bash
./scripts/start_demo_stack.sh
```

This launcher:
- starts or resumes minikube
- redeploys Google Online Boutique
- starts local port-forwards for Prometheus, Alertmanager, and Grafana
- prints the localhost URLs
- keeps the port-forwards alive until you press `Ctrl-C`

## Start the cluster
```bash
./scripts/start_minikube.sh
```

## Deploy Google Online Boutique
```bash
./scripts/deploy_boutique.sh
```

## Verify pods are running
```bash
./scripts/check_cluster.sh
```

## Access the frontend
```bash
minikube service frontend-external
```

## Notes
- `scripts/deploy_boutique.sh` applies the upstream Google Online Boutique manifest from GitHub.
- If you want to use a different manifest source, set `BOUTIQUE_MANIFEST_URL` before running the deploy script.
- `scripts/bootstrap_herald_env.sh` is the fastest way to reproduce the full local HERALD evaluation environment end to end.
- `scripts/start_demo_stack.sh` is the fastest way to start the demo cluster path and expose the monitoring dashboards locally without reinstalling monitoring.
