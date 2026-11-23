#!/usr/bin/env bash
# up.sh — levanta TODO desde cero: clúster kind, imágenes, manifiestos y carga histórica.
# Requisitos: Docker (abierto), kind, kubectl, archivo .env y datos/opencem-v1.0.0.sqlite.
set -euo pipefail
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1     # Git Bash: no reescribir rutas tipo /data

CLUSTER=opencem
NS=opencem
SQLITE=datos/opencem-v1.0.0.sqlite
KIND_CFG=.kind-config.generated.yaml

for cmd in docker kind kubectl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: falta '$cmd' en el PATH"; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "ERROR: Docker no está corriendo"; exit 1; }
[ -f .env ] || { echo "ERROR: falta .env (copie .env.example a .env y edite las claves)"; exit 1; }
[ -f "$SQLITE" ] || { echo "ERROR: falta $SQLITE (descomprima el .zst, ver README)"; exit 1; }

# Ruta absoluta de datos/ (en Git Bash, 'pwd -W' da C:/ruta; en Linux/WSL2 se usa pwd)
if pwd -W >/dev/null 2>&1; then DATOS_DIR="$(pwd -W)/datos"; else DATOS_DIR="$(pwd)/datos"; fi

echo "==> 1/7 Clúster kind '$CLUSTER'"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "    ya existe; se reutiliza"
else
  if docker ps --format '{{.Ports}}' | grep -q ':5433->'; then
    echo "ERROR: el puerto 5433 está ocupado (¿docker compose?). Ejecute: docker compose down"
    exit 1
  fi
  sed "s|__DATOS_DIR__|$DATOS_DIR|" k8s/kind-config.yaml > "$KIND_CFG"
  kind create cluster --name "$CLUSTER" --config "$KIND_CFG"
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "==> 2/7 Construcción de imágenes propias"
docker build -f etl/Dockerfile -t opencem-etl:1.0 .
if [ -f simulador/Dockerfile ]; then
  docker build -f simulador/Dockerfile -t opencem-simulador:1.0 .
fi

echo "==> 3/7 Carga de imágenes al clúster (sin registry remoto)"
kind load docker-image opencem-etl:1.0 --name "$CLUSTER"
if docker image inspect opencem-simulador:1.0 >/dev/null 2>&1; then
  kind load docker-image opencem-simulador:1.0 --name "$CLUSTER"
fi

echo "==> 4/7 Namespace, Secret (desde .env) y ConfigMap"
kubectl apply -f k8s/00-namespace.yaml
kubectl create secret generic opencem-credenciales --from-env-file=.env -n "$NS" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/10-config.yaml

echo "==> 5/7 PostgreSQL (StatefulSet + PVC + Services)"
kubectl apply -f k8s/20-postgres.yaml
kubectl rollout status statefulset/postgres -n "$NS" --timeout=300s

echo "==> 6/7 Job del ETL (carga histórica)"
kubectl delete job etl-historico -n "$NS" --ignore-not-found
kubectl apply -f k8s/30-etl-job.yaml
if ! kubectl wait --for=condition=complete job/etl-historico -n "$NS" --timeout=1500s; then
  echo "ERROR: el ETL no terminó bien. Últimas líneas del log:"
  kubectl logs job/etl-historico -n "$NS" --tail=40 || true
  exit 1
fi
kubectl logs job/etl-historico -n "$NS" --tail=60

echo "==> 7/7 Grafana y simulador (si existen sus manifiestos)"
shopt -s nullglob
for f in k8s/40-*.yaml k8s/50-*.yaml; do kubectl apply -f "$f"; done

echo
echo "Listo. PostgreSQL desde el anfitrión: localhost:5433 (base opencem)."
echo "Estado: kubectl get all -n $NS"