#!/usr/bin/env python3
"""Simulador de lecturas casi en tiempo real.

Cada ejecución (CronJob, 1 por minuto) inserta UNA lectura por inversor en
gold.fact_lectura_reciente. El modo lo controla SIM_MODO (ConfigMap):
  normal | bateria_baja | sin_red | silencio
"""
import math
import os
import random
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2

PG_HOST = os.environ.get("PG_HOST", "postgres")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "opencem")
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
SIM_MODO = os.environ.get("SIM_MODO", "normal").strip().lower()
SIM_TZ = os.environ.get("SIM_TZ", "Asia/Shanghai")
PV_PICO_W = float(os.environ.get("SIM_PV_PICO_W", "2000"))
ACEL = float(os.environ.get("SIM_ACEL", "10"))   # acelera el cambio de SoC para que la demo se vea
BATERIA_KWH = 10.24
MODOS = {"normal", "bateria_baja", "sin_red", "silencio"}


def generar(inv, ahora_utc, soc_prev):
    local = ahora_utc.astimezone(ZoneInfo(SIM_TZ))
    h = local.hour + local.minute / 60.0
    pv = max(0.0, math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0
    pv = pv * PV_PICO_W * (1 + random.uniform(-0.1, 0.1))

    carga = random.uniform(250, 700)
    if random.random() < 0.15:                      # picos de carga
        carga = random.uniform(900, 1300)
    if inv == 2:
        carga *= 1.5

    soc = soc_prev
    red_presente = SIM_MODO != "sin_red"
    deficit = carga - pv

    if SIM_MODO == "bateria_baja":
        desc, carga_bat = 3000.0, 0.0
        soc = max(5.0, soc - 15.0)                  # descarga forzada hasta SoC < 20 %
    elif deficit > 0:
        desc, carga_bat = deficit, 0.0
        soc = soc - (desc / 60.0 / 1000.0) / BATERIA_KWH * 100.0 * ACEL
    else:
        desc, carga_bat = 0.0, -deficit
        soc = soc + (carga_bat / 60.0 / 1000.0) / BATERIA_KWH * 100.0 * ACEL

    red = 0.0
    if red_presente:
        red = max(0.0, carga - pv - desc)
        if SIM_MODO == "normal" and soc < 20:       # recuperación desde la red
            red += 1000.0
            carga_bat += 1000.0
            soc += 2.0
    soc = min(100.0, max(5.0, soc))
    bateria_w = desc - carga_bat                    # positivo = descarga, negativo = carga
    return pv, carga, red, bateria_w, soc, red_presente


def main():
    if SIM_MODO not in MODOS:
        sys.exit(f"ERROR: SIM_MODO inválido '{SIM_MODO}'. Use: {sorted(MODOS)}")
    if SIM_MODO == "silencio":
        print("[simulador] modo silencio: no se escribe nada")
        return
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD)
    try:
        with conn, conn.cursor() as cur:
            for inv in (1, 2):
                cur.execute("SELECT soc_pct FROM gold.fact_lectura_reciente "
                            "WHERE inversor_key = %s ORDER BY ts_utc DESC LIMIT 1", (inv,))
                fila = cur.fetchone()
                soc_prev = float(fila[0]) if fila and fila[0] is not None else 70.0
                pv, carga, red, bat, soc, rp = generar(inv, ahora, soc_prev)
                cur.execute(
                    "INSERT INTO gold.fact_lectura_reciente "
                    "(inversor_key, ts_utc, pv_w, carga_w, red_w, bateria_w, soc_pct, red_presente, origen) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'simulador') "
                    "ON CONFLICT (inversor_key, ts_utc) DO NOTHING",
                    (inv, ahora, round(pv, 1), round(carga, 1), round(red, 1), round(bat, 1), round(soc, 2), rp))
                print(f"[simulador] {ahora.isoformat()} modo={SIM_MODO} inv={inv} pv={pv:.0f}W "
                      f"carga={carga:.0f}W red={red:.0f}W bat={bat:.0f}W soc={soc:.1f}% red_presente={rp}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()