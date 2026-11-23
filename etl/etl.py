#!/usr/bin/env python3
"""ETL OpenCEM: Bronze (SQLite, solo lectura) -> Silver (15 min) -> Gold (PostgreSQL).

Pasos: DDL -> extraer/limpiar/agregar telemetrÃ­a por inversor y mes -> contexto (JSON) ->
carga transaccional Silver/Gold -> conciliaciÃ³n -> huella de idempotencia -> resumen en consola.
ConfiguraciÃ³n por variables de entorno (ConfigMap/Secret en Kubernetes).
"""
import json
import os
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import Json, execute_values


# ---------------------------------------------------------------- configuraciÃ³n
def env(nombre, defecto=None):
    valor = os.environ.get(nombre, defecto)
    if valor is None:
        sys.exit(f"ERROR: falta la variable de entorno obligatoria {nombre}")
    return valor


SQLITE_PATH = env("SQLITE_PATH", "/data/opencem.sqlite")
PG_HOST = env("PG_HOST", "postgres")
PG_PORT = int(env("PG_PORT", "5432"))
PG_DB = env("PG_DB", "opencem")
PG_USER = env("PG_USER")
PG_PASSWORD = env("PG_PASSWORD")
BIN_MINUTOS = int(env("BIN_MINUTOS", "15"))
TZ_LOCAL = env("TZ_LOCAL", "Asia/Shanghai")
UMBRAL_SPIKE_W = float(env("UMBRAL_SPIKE_W", "1500"))
POTENCIA_MAX_W = float(env("POTENCIA_MAX_W", "8000"))
GAP_MAX_SEG = float(env("GAP_MAX_SEG", "60"))
SOC_BAJO_PCT = float(env("SOC_BAJO_PCT", "20"))
SQL_DIR = Path(env("SQL_DIR", str(Path(__file__).parent / "sql")))

if BIN_MINUTOS != 15:
    sys.exit("ERROR: el modelo (vistas y KPI) estÃ¡ definido para BIN_MINUTOS=15")
BIN_S = BIN_MINUTOS * 60
H = BIN_MINUTOS / 60.0

DDL_FILES = ["01_esquemas.sql", "02_dimensiones.sql", "03_hechos.sql", "04_indices.sql", "05_vistas.sql", "08_permisos.sql"]
CLAVE = ["pv1power", "outw_a", "linepowerw_a", "battsoc", "battvolt"]
NUMERICAS = ["pv1power", "outw_a", "outsumw", "linepowerw_a", "linevolta",
             "battvolt", "battcurr", "battsoc", "temper1"]
SQL_ANALOG = ("SELECT read_ts, inverter, " + ", ".join(NUMERICAS) +
              " FROM analog_measurements WHERE inverter = ? AND read_ts >= ? AND read_ts < ? ORDER BY read_ts")
SQL_STATS = ("SELECT read_ts, inverter, pvgeneratenergytoday, usedenergytoday, loadconsumlinetday, "
             "linechgenergytday FROM statistics WHERE read_ts >= ? AND read_ts < ?")
SILVER_COLS = ["inversor", "bin_utc", "n_crudas", "n_validas", "pv_w", "carga_w", "red_w",
               "bateria_desc_w", "bateria_carga_w", "soc_pct", "temp_c", "red_presente"]

NOMBRE_REGLA = {
    0: "R0 ExtracciÃ³n (filas leÃ­das de analog_measurements)",
    1: "R1 Lecturas a menos de 1 s de la anterior conservada (se descarta la posterior)",
    2: "R2 Todas las variables clave NULL (sin comunicaciÃ³n; se excluye)",
    3: "R3 Valores fuera de rango fÃ­sico (campo a NULL)",
    4: "R4 Inconsistencia |outw_a - outsumw| > umbral (outw_a a NULL)",
    5: "R5 Equipo apagado: battvolt=0 y linevolta=0 (se descarta)",
    6: "R6 Red ausente linevolta=0 (se CONSERVA; red_presente=false; evento operativo)",
    7: "R7 Derivadas y agregaciÃ³n a 15 min (a silver.lectura_15min)",
    10: "C0 ExtracciÃ³n de context",
    11: "C1 Registros de contexto invÃ¡lidos (JSON o tiempos)",
    12: "C2 Intervalos con fin < inicio (se corrige fin = inicio)",
    13: "C3 MinimizaciÃ³n del payload (lista blanca, Ley 1581)",
}

# ---------------------------------------------------------------- contexto: mapeo y lista blanca
CATEGORIAS = {
    "carga_computo": [
        "cpu_compilation_workload", "gpu_cnn_training_workload", "benchmark_or_profiling_activity",
        "build_or_test_activity", "compiler_invocation_burst", "compiler_analysis_activity",
        "interactive_development_window", "repository_activity", "package_or_environment_setup",
        "python_environment_or_script_activity", "dataset_processing_or_python_activity",
        "database_export_or_conversion_activity", "data_transfer_or_archive_activity",
        "legacy_shell_activity", "background_or_long_running_session",
        "remote_shell_or_key_setup_activity", "system_monitoring_activity", "high_ac_load"],
    "sistema_ti": [
        "windows_update_maintenance_window", "windows_update_activity", "wsl_virtualization_network_activity",
        "virtualized_disk_io_summary", "interactive_or_remote_logon", "remote_access_or_ssh_activity",
        "remote_desktop_disconnect", "remote_desktop_reconnect", "remote_desktop_logon",
        "remote_desktop_logoff", "system_time_adjustment", "storage_health_or_error_window",
        "storage_capacity_pressure", "system_boot_eventlog_started", "system_shutdown_eventlog_stopped",
        "scheduled_reboot_or_shutdown", "winlogon_issue", "service_failure_or_restart_window",
        "system_stability_report", "hardware_error_report", "virtual_disk_io_latency_window",
        "manual_workload_termination", "system_control_activity"],
    "energia_operativo": [
        "battery_charging", "battery_discharging", "pv_generation_active", "power_transition",
        "grid_unavailable_or_islanded", "unexpected_shutdown",
        "sin_red_detectado", "bateria_baja_detectada"],
    "calendario_solar": [
        "calendar_day", "solar_civil_twilight_morning", "solar_daylight",
        "solar_civil_twilight_evening", "SIN_TIPO"],
    "calidad_datos": ["data_gap", "data_coverage_summary", "telemetry_quality_note"],
    "otro": ["installation_location"],
}
MAPEO_CATEGORIA = {tipo: cat for cat, tipos in CATEGORIAS.items() for tipo in tipos}
CRITICOS = {"grid_unavailable_or_islanded", "unexpected_shutdown", "data_gap", "power_transition",
            "sin_red_detectado"}
DERIVADOS = {"sin_red_detectado", "bateria_baja_detectada"}
CLAVES_PERMITIDAS = ["source", "device", "context_type", "task_type", "inverter", "model",
                     "dataset", "epochs", "batch_size", "gap_seconds"]
CLAVES_TEXTO = ["textual_description", "summary"]  # se conservan truncadas a 300 caracteres


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- acumulador de conteos
class Acumulador:
    def __init__(self):
        self.tot = defaultdict(lambda: [0, 0, 0])                           # orden -> antes, despues, afectadas
        self.inv = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))      # orden -> inversor -> ...
        self.r3_campos = Counter()
        self.integrales = defaultdict(float)                                # (inversor, magnitud) -> kWh

    def reg(self, orden, inv, antes, despues, afectadas):
        for fila in (self.tot[orden], self.inv[orden][inv]):
            fila[0] += int(antes)
            fila[1] += int(despues)
            fila[2] += int(afectadas)


# ---------------------------------------------------------------- limpieza de telemetrÃ­a
def mascara_r1(ts, ultimo):
    """False para lecturas a < 1 s de la Ãºltima lectura CONSERVADA (arrastra entre meses)."""
    keep = np.ones(len(ts), dtype=bool)
    for i, t in enumerate(ts.tolist()):
        if ultimo is not None and t - ultimo < 1.0:
            keep[i] = False
        else:
            ultimo = t
    return keep, ultimo


def limpiar_y_agregar(df, inv, ultimo, acum):
    for c in NUMERICAS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    n0 = len(df)
    acum.reg(0, inv, n0, n0, 0)

    # R1
    keep, ultimo = mascara_r1(df["read_ts"].to_numpy(), ultimo)
    df1 = df.loc[keep].copy()
    acum.reg(1, inv, n0, len(df1), n0 - len(df1))
    df1["bin_s"] = (df1["read_ts"] // BIN_S * BIN_S).astype("int64")
    crudas = df1.groupby("bin_s").size()

    # R2
    m2 = df1[CLAVE].isna().all(axis=1)
    df2 = df1.loc[~m2].copy()
    acum.reg(2, inv, len(df1), len(df2), m2.sum())

    # R3 (se anula el campo, no la lectura)
    rangos = {"battsoc": (0, 100), "pv1power": (0, POTENCIA_MAX_W), "outw_a": (0, POTENCIA_MAX_W),
              "linepowerw_a": (0, POTENCIA_MAX_W), "battvolt": (0, 60)}
    m3_any = pd.Series(False, index=df2.index)
    for col, (lo, hi) in rangos.items():
        m = (df2[col] < lo) | (df2[col] > hi)
        acum.r3_campos[col] += int(m.sum())
        df2.loc[m, col] = np.nan
        m3_any |= m
    acum.reg(3, inv, len(df2), len(df2), m3_any.sum())

    # R4
    m4 = (df2["outw_a"] - df2["outsumw"]).abs() > UMBRAL_SPIKE_W
    df2.loc[m4, "outw_a"] = np.nan
    acum.reg(4, inv, len(df2), len(df2), m4.sum())

    # R5
    m5 = (df2["battvolt"] == 0) & (df2["linevolta"] == 0)
    df3 = df2.loc[~m5].copy()
    acum.reg(5, inv, len(df2), len(df3), m5.sum())

    # R6 (solo se registra: la red ausente es un evento operativo, no se descarta)
    acum.reg(6, inv, len(df3), len(df3), int((df3["linevolta"] == 0).sum()))

    if df3.empty:
        acum.reg(7, inv, 0, 0, 0)
        return None, ultimo

    # Integral directa sobre la fuente (para conciliaciÃ³n): potencia x min(dt a la siguiente, tope)
    dt = (df3["read_ts"].shift(-1) - df3["read_ts"]).clip(upper=GAP_MAX_SEG).fillna(GAP_MAX_SEG)
    for nombre, col in (("pv", "pv1power"), ("carga", "outw_a"), ("red", "linepowerw_a")):
        acum.integrales[(inv, nombre)] += float((df3[col].fillna(0) * dt).sum()) / 3.6e6

    # R7: derivadas y agregaciÃ³n
    bat = df3["battvolt"] * df3["battcurr"]
    df3["bateria_desc_w"] = bat.clip(lower=0)
    df3["bateria_carga_w"] = (-bat).clip(lower=0)
    df3["red_flag"] = np.where(df3["linevolta"].notna(), (df3["linevolta"] > 0).astype(float), np.nan)

    agg = df3.groupby("bin_s").agg(
        n_validas=("read_ts", "size"), pv_w=("pv1power", "mean"), carga_w=("outw_a", "mean"),
        red_w=("linepowerw_a", "mean"), bateria_desc_w=("bateria_desc_w", "mean"),
        bateria_carga_w=("bateria_carga_w", "mean"), soc_pct=("battsoc", "mean"),
        temp_c=("temper1", "mean"), red_frac=("red_flag", "mean"))
    agg["n_crudas"] = crudas.reindex(agg.index).astype(int)
    agg["red_presente"] = agg["red_frac"].apply(lambda x: None if pd.isna(x) else bool(x > 0.5))
    agg = agg.reset_index()
    agg["bin_utc"] = pd.to_datetime(agg["bin_s"], unit="s", utc=True)
    agg["inversor"] = inv
    acum.reg(7, inv, len(df3), len(agg), len(df3) - len(agg))
    return agg[SILVER_COLS], ultimo


def rangos_mensuales(minimo, maximo):
    ini = pd.Timestamp(minimo, unit="s", tz="UTC").normalize().replace(day=1)
    fin = pd.Timestamp(maximo, unit="s", tz="UTC")
    rangos = []
    while ini <= fin:
        sig = ini + pd.offsets.MonthBegin(1)
        rangos.append((ini.timestamp(), sig.timestamp()))
        ini = sig
    return rangos


def procesar_telemetria(sq, acum, meses):
    frames = []
    for inv in (1, 2):
        ultimo = None
        for (t0, t1) in meses:
            df = pd.read_sql_query(SQL_ANALOG, sq, params=(inv, t0, t1))
            if df.empty:
                continue
            silver, ultimo = limpiar_y_agregar(df, inv, ultimo, acum)
            if silver is not None and len(silver):
                frames.append(silver)
        log(f"TelemetrÃ­a del inversor {inv} procesada")
    return pd.concat(frames, ignore_index=True)


def leer_contadores(sq, meses):
    """Contadores diarios de statistics: MAX por inversor y dÃ­a local (solo para conciliar)."""
    partes = []
    for (t0, t1) in meses:
        df = pd.read_sql_query(SQL_STATS, sq, params=(t0, t1))
        if df.empty:
            continue
        for c in df.columns[2:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        loc = pd.to_datetime(df["read_ts"], unit="s", utc=True).dt.tz_convert(TZ_LOCAL).dt.tz_localize(None)
        df["fecha_key"] = loc.dt.year * 10000 + loc.dt.month * 100 + loc.dt.day
        partes.append(df.groupby(["inverter", "fecha_key"])[list(df.columns[2:6])].max())
    if not partes:
        return pd.DataFrame()
    return pd.concat(partes).groupby(level=[0, 1]).max().reset_index()


# ---------------------------------------------------------------- contexto (JSON)
def procesar_contexto(sq):
    filas = sq.execute('SELECT id, recorded, "start", "end", value FROM context ORDER BY id').fetchall()
    salida, tipos = [], set()
    invalidos = fin_corregido = con_descartes = 0
    claves_descartadas = Counter()

    def ts(x):
        return None if x is None else datetime.fromtimestamp(float(x), tz=timezone.utc)

    for (cid, rec, ini, fin, valor) in filas:
        try:
            d = json.loads(valor)
            if not isinstance(d, dict) or ini is None or fin is None:
                raise ValueError("registro invÃ¡lido")
        except (TypeError, ValueError):
            invalidos += 1
            continue
        if fin < ini:
            fin = ini
            fin_corregido += 1
        payload = {k: d[k] for k in CLAVES_PERMITIDAS if k in d}
        for k in CLAVES_TEXTO:
            if d.get(k) is not None:
                payload[k] = str(d[k])[:300]
        descartadas = [k for k in d if k not in CLAVES_PERMITIDAS and k not in CLAVES_TEXTO]
        if descartadas:
            con_descartes += 1
            claves_descartadas.update(descartadas)
        try:
            inv = int(d["inverter"]) if d.get("inverter") is not None else None
        except (TypeError, ValueError):
            inv = None
        if inv not in (1, 2):
            inv = None
        tipo = d.get("context_type") or None
        tipos.add(tipo or "SIN_TIPO")
        salida.append((cid, ts(ini), ts(fin), ts(rec), inv, tipo, Json(payload)))
    return {"filas": salida, "tipos": tipos, "total": len(filas), "invalidos": invalidos,
            "fin_corregido": fin_corregido, "con_descartes": con_descartes,
            "claves_descartadas": dict(claves_descartadas)}


# ---------------------------------------------------------------- SQL de carga (Gold)
SQL_GOLD_ENERGIA = """
INSERT INTO gold.fact_energia_15min
  (inversor_key, fecha_key, hora_key, bin_utc, bin_local, pv_kwh, carga_kwh, red_kwh,
   bateria_descarga_kwh, bateria_carga_kwh, pv_directo_kwh, red_a_carga_kwh,
   soc_prom, temp_prom_c, red_presente, n_crudas, n_validas)
SELECT s.inversor,
       to_char(l.bin_local, 'YYYYMMDD')::int,
       EXTRACT(HOUR FROM l.bin_local)::smallint,
       s.bin_utc, l.bin_local,
       s.pv_w::double precision * %(h)s / 1000.0,
       s.carga_w::double precision * %(h)s / 1000.0,
       s.red_w::double precision * %(h)s / 1000.0,
       s.bateria_desc_w::double precision * %(h)s / 1000.0,
       s.bateria_carga_w::double precision * %(h)s / 1000.0,
       CASE WHEN s.pv_w IS NULL OR s.carga_w IS NULL THEN NULL
            ELSE LEAST(s.pv_w, s.carga_w)::double precision * %(h)s / 1000.0 END,
       CASE WHEN s.red_w IS NULL OR s.carga_w IS NULL THEN NULL
            ELSE LEAST(s.red_w, s.carga_w)::double precision * %(h)s / 1000.0 END,
       s.soc_pct, s.temp_c, s.red_presente, s.n_crudas, s.n_validas
FROM silver.lectura_15min s
CROSS JOIN LATERAL (SELECT (s.bin_utc AT TIME ZONE %(tz)s) AS bin_local) l
ON CONFLICT (inversor_key, bin_utc) DO UPDATE SET
  fecha_key = EXCLUDED.fecha_key, hora_key = EXCLUDED.hora_key, bin_local = EXCLUDED.bin_local,
  pv_kwh = EXCLUDED.pv_kwh, carga_kwh = EXCLUDED.carga_kwh, red_kwh = EXCLUDED.red_kwh,
  bateria_descarga_kwh = EXCLUDED.bateria_descarga_kwh, bateria_carga_kwh = EXCLUDED.bateria_carga_kwh,
  pv_directo_kwh = EXCLUDED.pv_directo_kwh, red_a_carga_kwh = EXCLUDED.red_a_carga_kwh,
  soc_prom = EXCLUDED.soc_prom, temp_prom_c = EXCLUDED.temp_prom_c, red_presente = EXCLUDED.red_presente,
  n_crudas = EXCLUDED.n_crudas, n_validas = EXCLUDED.n_validas
"""

SQL_GOLD_HUERFANAS_ENERGIA = """
DELETE FROM gold.fact_energia_15min f
WHERE NOT EXISTS (SELECT 1 FROM silver.lectura_15min s
                  WHERE s.inversor = f.inversor_key AND s.bin_utc = f.bin_utc)
"""

SQL_GOLD_CONTEXTO = """
INSERT INTO gold.fact_evento_operativo
  (evento_id_fuente, inversor_key, tipo_evento_key, fecha_inicio_key, inicio_utc, fin_utc,
   duracion_min, fuente, descripcion, payload, origen)
SELECT c.id, COALESCE(c.inversor, 0), d.tipo_evento_key,
       to_char(c.inicio_utc AT TIME ZONE %(tz)s, 'YYYYMMDD')::int,
       c.inicio_utc, c.fin_utc,
       EXTRACT(EPOCH FROM (c.fin_utc - c.inicio_utc)) / 60.0,
       c.payload->>'source',
       LEFT(COALESCE(c.payload->>'textual_description', c.payload->>'summary'), 300),
       c.payload, 'contexto'
FROM silver.contexto c
JOIN gold.dim_tipo_evento d ON d.context_type = COALESCE(c.context_type, 'SIN_TIPO')
ON CONFLICT (evento_id_fuente) DO UPDATE SET
  inversor_key = EXCLUDED.inversor_key, tipo_evento_key = EXCLUDED.tipo_evento_key,
  fecha_inicio_key = EXCLUDED.fecha_inicio_key, inicio_utc = EXCLUDED.inicio_utc,
  fin_utc = EXCLUDED.fin_utc, duracion_min = EXCLUDED.duracion_min, fuente = EXCLUDED.fuente,
  descripcion = EXCLUDED.descripcion, payload = EXCLUDED.payload, origen = EXCLUDED.origen
"""

SQL_GOLD_HUERFANOS_EVENTO = """
DELETE FROM gold.fact_evento_operativo e
WHERE e.origen = 'contexto'
  AND NOT EXISTS (SELECT 1 FROM silver.contexto c WHERE c.id = e.evento_id_fuente)
"""

# Gaps and islands: bin_index - ROW_NUMBER() es constante dentro de una racha de intervalos consecutivos.
SQL_ISLAS = """
INSERT INTO gold.fact_evento_operativo
  (evento_id_fuente, inversor_key, tipo_evento_key, fecha_inicio_key, inicio_utc, fin_utc,
   duracion_min, fuente, descripcion, payload, origen)
SELECT NULL::integer, i.inversor::smallint, d.tipo_evento_key,
       to_char(i.inicio_utc AT TIME ZONE %(tz)s, 'YYYYMMDD')::int,
       i.inicio_utc, i.fin_utc,
       EXTRACT(EPOCH FROM (i.fin_utc - i.inicio_utc)) / 60.0,
       'silver.lectura_15min', %(desc)s,
       jsonb_build_object('source', 'derived_telemetry', 'context_type', d.context_type, 'inverter', i.inversor),
       'derivado_telemetria'
FROM (
  SELECT inversor, MIN(bin_utc) AS inicio_utc, MAX(bin_utc) + interval '15 minutes' AS fin_utc
  FROM (
    SELECT inversor, bin_utc,
           (EXTRACT(EPOCH FROM bin_utc)::bigint / 900)
             - ROW_NUMBER() OVER (PARTITION BY inversor ORDER BY bin_utc) AS grp
    FROM silver.lectura_15min
    WHERE {cond}
  ) t
  GROUP BY inversor, grp
) i
JOIN gold.dim_tipo_evento d ON d.context_type = %(tipo)s
"""

SQL_AMPLIAR_FECHAS = """
SELECT gold.cargar_dim_fecha(
  LEAST(DATE '2024-04-01',
        (SELECT MIN(inicio_utc AT TIME ZONE %(tz)s)::date FROM silver.contexto),
        (SELECT MIN(bin_utc AT TIME ZONE %(tz)s)::date FROM silver.lectura_15min)),
  GREATEST(DATE '2026-07-31',
        (SELECT MAX(inicio_utc AT TIME ZONE %(tz)s)::date FROM silver.contexto),
        (SELECT MAX(bin_utc AT TIME ZONE %(tz)s)::date FROM silver.lectura_15min)))
"""

HUELLAS = {
    "dimensiones": ("SELECT (SELECT COUNT(*) FROM gold.dim_fecha), (SELECT COUNT(*) FROM gold.dim_hora), "
                    "(SELECT COUNT(*) FROM gold.dim_inversor), (SELECT COUNT(*) FROM gold.dim_tipo_evento)"),
    "silver_lectura": (
        "SELECT COUNT(*), ROUND(SUM(pv_w)::numeric, 3), ROUND(SUM(carga_w)::numeric, 3), "
        "ROUND(SUM(red_w)::numeric, 3), md5(COALESCE(string_agg(concat_ws('|', inversor, bin_utc, n_crudas, "
        "n_validas, ROUND(pv_w::numeric, 3), ROUND(carga_w::numeric, 3), ROUND(red_w::numeric, 3), "
        "ROUND(soc_pct::numeric, 3), red_presente), ';' ORDER BY inversor, bin_utc), '')) "
        "FROM silver.lectura_15min"),
    "gold_energia": (
        "SELECT COUNT(*), ROUND(SUM(pv_kwh)::numeric, 4), ROUND(SUM(carga_kwh)::numeric, 4), "
        "ROUND(SUM(red_kwh)::numeric, 4), ROUND(SUM(red_a_carga_kwh)::numeric, 4), "
        "md5(COALESCE(string_agg(concat_ws('|', inversor_key, bin_utc, fecha_key, hora_key, "
        "ROUND(pv_kwh::numeric, 5), ROUND(carga_kwh::numeric, 5), ROUND(red_kwh::numeric, 5), "
        "ROUND(red_a_carga_kwh::numeric, 5), ROUND(pv_directo_kwh::numeric, 5)), ';' "
        "ORDER BY inversor_key, bin_utc), '')) FROM gold.fact_energia_15min"),
    "gold_evento": (
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE origen = 'contexto'), "
        "COUNT(*) FILTER (WHERE origen = 'derivado_telemetria'), "
        "md5(COALESCE(string_agg(concat_ws('|', COALESCE(evento_id_fuente::text, '-'), inversor_key, "
        "tipo_evento_key, inicio_utc, fin_utc, origen, payload::text), ';' "
        "ORDER BY origen, evento_id_fuente NULLS FIRST, inversor_key, inicio_utc, tipo_evento_key), '')) "
        "FROM gold.fact_evento_operativo"),
}


# ---------------------------------------------------------------- piezas de carga
def aplicar_ddl(conn):
    with conn.cursor() as cur:
        for nombre in DDL_FILES:
            cur.execute((SQL_DIR / nombre).read_text(encoding="utf-8"))
            log(f"DDL aplicado: {nombre}")


def filas_para_insertar(df):
    return df.astype(object).where(df.notna(), None).values.tolist()


def cargar_dim_tipo_evento(cur, tipos_vistos):
    todos = set(MAPEO_CATEGORIA) | set(tipos_vistos)
    filas = [(t, MAPEO_CATEGORIA.get(t, "otro"), t in CRITICOS,
              "derivado_telemetria" if t in DERIVADOS else "contexto") for t in sorted(todos)]
    execute_values(cur,
                   "INSERT INTO gold.dim_tipo_evento (context_type, categoria, es_critico, origen) VALUES %s "
                   "ON CONFLICT (context_type) DO UPDATE SET categoria = EXCLUDED.categoria, "
                   "es_critico = EXCLUDED.es_critico, origen = EXCLUDED.origen", filas)
    return len(filas)


def cargar_transaccion(conn, run_id, silver_df, ctx, acum):
    """Una sola transacciÃ³n: si algo falla, no queda nada a medias."""
    with conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE silver.lectura_15min, silver.contexto")
            execute_values(cur, "INSERT INTO silver.lectura_15min (" + ",".join(SILVER_COLS) + ") VALUES %s",
                           filas_para_insertar(silver_df), page_size=5000)
            execute_values(cur, "INSERT INTO silver.contexto (id, inicio_utc, fin_utc, registrado_utc, "
                                "inversor, context_type, payload) VALUES %s", ctx["filas"], page_size=5000)
            log(f"Silver cargado: {len(silver_df):,} intervalos y {len(ctx['filas']):,} registros de contexto")

            cur.execute(SQL_AMPLIAR_FECHAS, {"tz": TZ_LOCAL})
            n_tipos = cargar_dim_tipo_evento(cur, ctx["tipos"])
            log(f"dim_tipo_evento: {n_tipos} tipos")

            cur.execute(SQL_GOLD_ENERGIA, {"h": H, "tz": TZ_LOCAL})
            cur.execute(SQL_GOLD_HUERFANAS_ENERGIA)
            cur.execute(SQL_GOLD_CONTEXTO, {"tz": TZ_LOCAL})
            cur.execute(SQL_GOLD_HUERFANOS_EVENTO)
            cur.execute("DELETE FROM gold.fact_evento_operativo WHERE origen = 'derivado_telemetria'")
            cur.execute(SQL_ISLAS.format(cond="red_presente = false"),
                        {"tz": TZ_LOCAL, "tipo": "sin_red_detectado",
                         "desc": "Racha de intervalos de 15 min sin red (linevolta = 0)"})
            cur.execute(SQL_ISLAS.format(cond="soc_pct < %(soc)s"),
                        {"tz": TZ_LOCAL, "tipo": "bateria_baja_detectada", "soc": SOC_BAJO_PCT,
                         "desc": f"Racha de intervalos de 15 min con SoC < {SOC_BAJO_PCT:g} %"})
            log("Gold cargado (energÃ­a, eventos de contexto y eventos derivados)")

            filas = []
            for orden in sorted(acum.tot):
                a, d, f = acum.tot[orden]
                filas.append((run_id, orden, NOMBRE_REGLA[orden], "analog_measurements", a, d, f))
            c = ctx
            filas += [
                (run_id, 10, NOMBRE_REGLA[10], "context", c["total"], c["total"], 0),
                (run_id, 11, NOMBRE_REGLA[11], "context", c["total"], c["total"] - c["invalidos"], c["invalidos"]),
                (run_id, 12, NOMBRE_REGLA[12], "context", c["total"] - c["invalidos"],
                 c["total"] - c["invalidos"], c["fin_corregido"]),
                (run_id, 13, NOMBRE_REGLA[13], "context", c["total"] - c["invalidos"],
                 c["total"] - c["invalidos"], c["con_descartes"]),
            ]
            execute_values(cur, "INSERT INTO etl.regla_limpieza (run_id, orden, regla, tabla, filas_antes, "
                                "filas_despues, filas_afectadas) VALUES %s", filas)


def conciliar(conn, run_id, acum, contadores, n_ctx_fuente):
    filas = []

    def agregar(metrica, inv, fuente, destino, explicacion):
        fuente, destino = float(fuente), float(destino)
        dif = destino - fuente
        pct = (100.0 * dif / fuente) if fuente else None
        filas.append((run_id, metrica, inv, fuente, destino, dif, pct, explicacion))

    with conn:
        with conn.cursor() as cur:
            # (a) lecturas vÃ¡lidas: filas fuente - descartes R1, R2, R5 vs Î£ n_validas
            cur.execute("SELECT inversor, SUM(n_validas) FROM silver.lectura_15min GROUP BY inversor")
            validas = dict(cur.fetchall())
            for inv in (1, 2):
                leidas = acum.inv[0][inv][0]
                descartes = sum(acum.inv[o][inv][2] for o in (1, 2, 5))
                agregar("lecturas_validas", inv, leidas - descartes, validas.get(inv, 0),
                        "Filas SQLite - descartes R1, R2 y R5 vs Î£ n_validas de Silver (debe cuadrar exacto).")

            # (b) integrales directas vs Gold
            cur.execute("SELECT inversor_key, SUM(pv_kwh), SUM(carga_kwh), SUM(red_kwh) "
                        "FROM gold.fact_energia_15min GROUP BY inversor_key")
            gold = {r[0]: r[1:] for r in cur.fetchall()}
            expl = (f"Integral con tope de {GAP_MAX_SEG:g} s por lectura vs Î£ de intervalos de 15 min. Con cadencia "
                    f"> 60 s (2025-07 a 2025-09) el tope subestima la fuente; en Gold cada intervalo con datos "
                    f"cuenta 15 min completos aunque estÃ© parcialmente cubierto (huecos dentro del intervalo).")
            for inv in (1, 2):
                if inv not in gold:
                    continue
                for i, (nombre, etiqueta) in enumerate((("pv", "pv"), ("carga", "carga"), ("red", "red"))):
                    agregar(f"energia_{etiqueta}_kwh_integral", inv, acum.integrales[(inv, nombre)],
                            gold[inv][i] or 0, expl)

            # (c) contadores diarios de statistics vs Gold (solo dÃ­as completos, >= 90 intervalos)
            if len(contadores):
                cur.execute("SELECT inversor_key, fecha_key, COUNT(*), SUM(pv_kwh), SUM(carga_kwh), SUM(red_kwh) "
                            "FROM gold.fact_energia_15min GROUP BY inversor_key, fecha_key")
                dia = pd.DataFrame(cur.fetchall(), columns=["inverter", "fecha_key", "n", "pv", "carga", "red"])
                for c in ("pv", "carga", "red"):
                    dia[c] = pd.to_numeric(dia[c], errors="coerce")
                m = dia.merge(contadores, on=["inverter", "fecha_key"])
                m = m[m["n"] >= 90]
                m["cont_red"] = m["loadconsumlinetday"].fillna(0) + m["linechgenergytday"].fillna(0)
                casos = (("carga_vs_contador_usedenergytoday", "carga", "usedenergytoday",
                          "Esperado â‰ˆ 0,99 (MAX diario local UTC+8)."),
                         ("pv_vs_contador_pvgeneratenergytoday", "pv", "pvgeneratenergytoday",
                          "Esperado â‰ˆ 0,90 (el contador usa otra referencia de mediciÃ³n)."),
                         ("red_vs_contadores_red", "red", "cont_red",
                          "Esperado â‰ˆ 1,4: linepowerw_a incluye pÃ©rdidas de conversiÃ³n y carga de baterÃ­a desde la red."))
                for metrica, col_gold, col_cont, nota in casos:
                    for inv in (1, 2):
                        sel = m[(m["inverter"] == inv) & (m[col_cont] > 0)]
                        if sel.empty:
                            continue
                        f, d = sel[col_cont].sum(), sel[col_gold].sum()
                        agregar(metrica, inv, f, d, f"{len(sel)} dÃ­as completos; razÃ³n Gold/contador = "
                                                    f"{d / f:.3f}. {nota}")

            # (d) contexto
            cur.execute("SELECT COUNT(*) FROM gold.fact_evento_operativo WHERE origen = 'contexto'")
            agregar("registros_contexto", 0, n_ctx_fuente, cur.fetchone()[0],
                    "Registros de context en SQLite vs eventos de contexto en Gold (debe ser 25.178).")

            execute_values(cur, "INSERT INTO etl.conciliacion (run_id, metrica, inversor, valor_fuente, "
                                "valor_destino, diferencia, diferencia_pct, explicacion) VALUES %s", filas)
    return filas


def calcular_huella(conn):
    huella = {}
    with conn.cursor() as cur:
        for nombre, sql in HUELLAS.items():
            cur.execute(sql)
            huella[nombre] = [str(x) for x in cur.fetchone()]
    return json.loads(json.dumps(huella))


def resumen(run_id, acum, conc, huella, previa, idempotente, segundos):
    print("\n" + "=" * 100)
    print(f"RESUMEN ETL (run_id={run_id}, {segundos:.0f} s)")
    print("=" * 100)
    print(f"{'orden':<6}{'regla':<82}{'antes':>10}{'despuÃ©s':>10}{'afectadas':>11}")
    for orden in sorted(acum.tot):
        a, d, f = acum.tot[orden]
        print(f"{orden:<6}{NOMBRE_REGLA[orden][:80]:<82}{a:>10,}{d:>10,}{f:>11,}")
    print(f"R3 por campo: {dict(acum.r3_campos)}")
    print("\nConciliaciÃ³n:")
    for (_, metrica, inv, fuente, destino, dif, pct, expl) in conc:
        pct_txt = f"{pct:+.2f} %" if pct is not None else "n/d"
        print(f"  inv {inv} {metrica:<40} fuente={fuente:>14,.2f} destino={destino:>14,.2f} dif={pct_txt}")
    print("\nHuella (conteos y md5):")
    for k, v in huella.items():
        print(f"  {k:<16} {v}")
    if previa is None:
        print("\nIDEMPOTENCIA: no hay ejecuciÃ³n anterior con la cual comparar (primera corrida).")
    elif idempotente:
        print(f"\nIDEMPOTENCIA: huella IDÃ‰NTICA a la ejecuciÃ³n anterior (run {previa[0]}).  âœ”")
    else:
        print(f"\nIDEMPOTENCIA: la huella DIFIERE de la ejecuciÃ³n anterior (run {previa[0]}).  âœ˜")
        for k in huella:
            if huella[k] != previa[1].get(k):
                print(f"  difiere {k}: antes={previa[1].get(k)} ahora={huella[k]}")
    print("=" * 100 + "\n")


# ---------------------------------------------------------------- principal
def main():
    t0 = time.time()
    log(f"Inicio ETL. SQLite={SQLITE_PATH}; PostgreSQL={PG_HOST}:{PG_PORT}/{PG_DB}")
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD)
    conn.autocommit = True
    aplicar_ddl(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO etl.bitacora (estado) VALUES ('EN_CURSO') RETURNING run_id")
        run_id = cur.fetchone()[0]
    log(f"run_id = {run_id}")

    sq = None
    try:
        sq = sqlite3.connect(f"{Path(SQLITE_PATH).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        acum = Acumulador()
        mn, mx = sq.execute("SELECT MIN(read_ts), MAX(read_ts) FROM analog_measurements").fetchone()
        meses = rangos_mensuales(mn, mx)
        log(f"Rango de telemetrÃ­a en {len(meses)} bloques mensuales (UTC)")

        silver_df = procesar_telemetria(sq, acum, meses)
        log(f"Silver en memoria: {len(silver_df):,} intervalos de 15 min")
        ctx = procesar_contexto(sq)
        log(f"Contexto leÃ­do: {ctx['total']:,} registros ({ctx['invalidos']} invÃ¡lidos)")
        contadores = leer_contadores(sq, meses)
        log(f"Contadores diarios de statistics: {len(contadores):,} (inversor, dÃ­a)")

        conn.autocommit = False
        cargar_transaccion(conn, run_id, silver_df, ctx, acum)
        conc = conciliar(conn, run_id, acum, contadores, ctx["total"])
        huella = calcular_huella(conn)
        conn.commit()  # Finaliza la transacción abierta por los SELECT de calcular_huella.

        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT run_id, detalle->'huella' FROM etl.bitacora "
                        "WHERE estado = 'OK' AND run_id < %s ORDER BY run_id DESC LIMIT 1", (run_id,))
            previa = cur.fetchone()
            idempotente = None if previa is None else (previa[1] == huella)
            cur.execute("SELECT (SELECT COUNT(*) FROM gold.fact_energia_15min) + "
                        "(SELECT COUNT(*) FROM gold.fact_evento_operativo)")
            filas_gold = cur.fetchone()[0]
            detalle = {
                "por_inversor": {str(o): {str(i): v for i, v in acum.inv[o].items()} for o in acum.inv},
                "r3_por_campo": dict(acum.r3_campos),
                "claves_descartadas_contexto": ctx["claves_descartadas"],
                "huella": huella,
                "idempotente_vs_anterior": idempotente,
                "config": {"bin_minutos": BIN_MINUTOS, "tz_local": TZ_LOCAL, "umbral_spike_w": UMBRAL_SPIKE_W,
                           "potencia_max_w": POTENCIA_MAX_W, "gap_max_seg": GAP_MAX_SEG,
                           "soc_bajo_pct": SOC_BAJO_PCT},
                "segundos": round(time.time() - t0, 1),
            }
            cur.execute("UPDATE etl.bitacora SET fin = now(), estado = 'OK', filas_crudas = %s, filas_silver = %s, "
                        "filas_gold = %s, detalle = %s WHERE run_id = %s",
                        (acum.tot[0][0], len(silver_df), filas_gold, Json(detalle), run_id))
        resumen(run_id, acum, conc, huella, previa, idempotente, time.time() - t0)
    except Exception as exc:
        traceback.print_exc()
        try:
            conn.rollback()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("UPDATE etl.bitacora SET fin = now(), estado = 'ERROR', detalle = %s WHERE run_id = %s",
                            (Json({"error": str(exc)}), run_id))
        finally:
            sys.exit(1)
    finally:
        if sq is not None:
            sq.close()
        conn.close()


if __name__ == "__main__":
    main()
