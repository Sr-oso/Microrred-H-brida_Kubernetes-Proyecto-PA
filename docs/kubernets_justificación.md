# Objetos de Kubernetes: justificación

| Necesidad | Objeto elegido | Por qué (y frente a qué alternativa) |
|---|---|---|
| Aislar el proyecto | Namespace `opencem` | Agrupa y borra todo junto; evita mezclar con otros recursos del clúster. |
| BD que no pierde datos | StatefulSet `postgres` + PVC de 2 Gi | Un Deployment da identidad y almacenamiento no estables; el StatefulSet recrea `postgres-0` con el mismo volumen. Una sola réplica: el taller no pide alta disponibilidad. |
| Credenciales fuera del código | Secret `opencem-credenciales` | Se crea desde un `.env` no versionado; en el repo solo hay plantillas. Nota: Base64 no es cifrado; en producción se usaría un gestor de secretos o cifrado en reposo. |
| Configuración no sensible | ConfigMap `opencem-config` | Umbrales, intervalos y nombres cambian sin reconstruir la imagen (clave para la demo de cambio de umbral). |
| Comunicación entre servicios | Service `postgres` (ClusterIP) | DNS interno: ETL, simulador y Grafana usan el nombre, no una IP que cambia al recrear el pod. |
| Acceso desde el anfitrión | Service `postgres-externo` (NodePort 30543) + mapeo de kind a `localhost:5433` | `kubectl port-forward` obliga a mantener una terminal abierta; el NodePort con mapeo es permanente. Solo escucha en 127.0.0.1. |
| Carga histórica | Job `etl-historico` | Corre una vez y termina; deja logs y filas en `etl.*`. Un Deployment lo reiniciaría sin fin; un CronJob lo repetiría sin necesidad. |
| Entrada del SQLite | Volumen `hostPath` de solo lectura | Es un clúster local de un solo nodo; evita copiar 4,4 GB a un PVC. En producción se usaría almacenamiento compartido. |
| Salud | `readinessProbe` y `livenessProbe` con `pg_isready -h 127.0.0.1` | Readiness evita enviar tráfico a una base que aún inicia; liveness reinicia el contenedor si se cuelga. |
| No agotar el anfitrión | `requests` y `limits` en cada contenedor | PostgreSQL 250m/512Mi → 1 CPU/1 Gi; ETL 250m/512Mi → 1 CPU/1,5 Gi. |
| Imágenes propias | `kind load docker-image` | Sin registry remoto, con tag fijo (`1.0`) e `imagePullPolicy: IfNotPresent`. |
| Distribución | kind (un nodo) | Permite montar una carpeta del anfitrión y mapear puertos; frente a minikube y k3d, la configuración es un único YAML. |