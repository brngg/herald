# Environment Setup

## Prerequisites
- Docker Desktop
- kubectl
- minikube

## Start the cluster
```bash
minikube start --driver=docker
```

## Deploy Google Online Boutique
```bash
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml
```

## Verify pods are running
```bash
kubectl get pods
```

## Access the frontend
```bash
minikube service frontend-external
```
