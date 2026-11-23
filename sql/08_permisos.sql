-- sql/08_permisos.sql
-- Usuario de solo lectura para Power BI y Grafana. El rol lo crea el script de inicialización
-- de PostgreSQL en Kubernetes; en docker-compose no existe y este bloque no hace nada.
-- Se reaplica en cada corrida porque 05_vistas.sql recrea las vistas.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opencem_ro') THEN
    GRANT USAGE ON SCHEMA gold TO opencem_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA gold TO opencem_ro;
    -- sin permisos sobre silver ni etl
  END IF;
END $$;