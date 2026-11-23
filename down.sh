#!/usr/bin/env bash
# down.sh — elimina TODO: el clúster completo (incluye datos de PostgreSQL y recursos).
set -euo pipefail
cd "$(dirname "$0")"
kind delete cluster --name opencem
rm -f .kind-config.generated.yaml
echo "Clúster 'opencem' eliminado."