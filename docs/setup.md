# Environment Setup

## Prerequisites
- Docker Desktop
- kubectl
- minikube

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
