#!/usr/bin/env bash
set -euo pipefail

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' is not installed." >&2
    exit 1
  fi
}

require_command docker
require_command kubectl
require_command minikube

if ! docker info >/dev/null 2>&1; then
  echo "Error: Docker Desktop is not running or the Docker daemon is unavailable." >&2
  exit 1
fi

echo "Starting minikube with the Docker driver..."
minikube start --driver=docker

echo "Verifying cluster connectivity..."
kubectl cluster-info >/dev/null

echo "Minikube is ready."
