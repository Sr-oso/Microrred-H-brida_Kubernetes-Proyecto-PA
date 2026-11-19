# diagnostico_sqlite.py
#!/usr/bin/env python3
"""Diagnóstico de SOLO LECTURA de la fuente Bronze (OpenCEM SQLite).

Uso:
    python diagnostico_sqlite.py --sqlite opencem-v1.0.0.sqlite
    python diagnostico_sqlite.py --sqlite opencem-v1.0.0.sqlite --salida docs/evidencias/diagnostico_sqlite.txt

Qué hace: inventario de tablas, cobertura y cadencia por mes e inversor,
nulos, huecos, lecturas casi duplicadas, valores fuera de rango, red en 0 V
y análisis del contexto (JSON). Compara con las cifras de referencia del taller.
La base se abre con mode=ro: nunca se modifica.
"""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

CLAVE = ["pv1power", "outw_a", "linepowerw_a", "battsoc", "battvolt"]
COLUMNAS = ["read_ts", "inverter", "pv1power", "outw_a", "outsumw",
            "linepowerw_a", "linevolta", "battvolt", "battsoc"]

REF = {
    "filas analog_measurements": 2989759,
    "filas inversor 1": 1610276,
    "filas inversor 2": 1379483,
    "casi duplicadas (<1 s) inv.1": 19865,
    "casi duplicadas (<1 s) inv.2": 514,
    "filas con todas las claves NULL": 55658,
    "linepowerw_a negativo": 9,
    "battvolt=0 y linevolta=0": 3,
    "linevolta=0": 342593,
    "outw_a > 2500 W": 53,
    "huecos > 1 h inv.1": 16,
    "huecos > 1 h inv.2": 15,
    "registros de contexto": 25178,
    "contexto grid_unavailable_or_islanded": 56,
}

LINEAS = []


def p(texto=""):
    print(texto)
    LINEAS.append(str(texto))


def seccion(titulo):
    p()
    p("=" * 78)
    p(titulo)
    p("=" * 78)


def comparar(etiqueta, medido, clave_ref):
    ref = REF[clave_ref]
    medido = int(medido)
    estado = "OK" if medido == ref else f"REVISAR (dif={medido - ref:+,})"
    p(f"{etiqueta:<46} medido={medido:>12,}  ref={ref:>12,}  {estado}")


def ts_texto(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return pd.to_datetime(x, unit="s", utc=True).strftime("%Y-%m-%d %H:%M:%S")


def conectar(ruta):
    uri = Path(ruta).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def inventario(con):
    seccion("1. INVENTARIO DE TABLAS")
    tablas = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    p(f"Tablas: {', '.join(tablas)}")
    for t in tablas:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        extra = ""
        if "read_ts" in cols:
            mn, mx = con.execute(f'SELECT MIN(read_ts), MAX(read_ts) FROM "{t}"').fetchone()
            extra = f"read_ts {ts_texto(mn)} -> {ts_texto(mx)}"
        elif t == "context":
            r = con.execute('SELECT MIN(recorded), MAX(recorded), MIN("start"), MAX("end") FROM context').fetchone()
            extra = (f"recorded {ts_texto(r[0])} -> {ts_texto(r[1])}; "
                     f"start>= {ts_texto(r[2])}; end<= {ts_texto(r[3])}")
        p(f"- {t:<22} filas={n:>10,}  columnas={len(cols):>3}  {extra}")


def cargar_telemetria(con):
    df = pd.read_sql_query(f"SELECT {', '.join(COLUMNAS)} FROM analog_measurements", con)
    df["inverter"] = df["inverter"].astype(int)
    df = df.sort_values(["inverter", "read_ts"]).reset_index(drop=True)
    df["dt"] = df.groupby("inverter")["read_ts"].diff()
    df["mes_utc"] = pd.to_datetime(df["read_ts"], unit="s", utc=True).dt.strftime("%Y-%m")
    return df


def cobertura(df):
    seccion("2. COBERTURA Y CADENCIA (mes UTC)")
    comparar("Filas analog_measurements", len(df), "filas analog_measurements")
    comparar("Filas inversor 1", (df.inverter == 1).sum(), "filas inversor 1")
    comparar("Filas inversor 2", (df.inverter == 2).sum(), "filas inversor 2")
    p()
    p("Filas por mes e inversor:")
    p(df.groupby(["mes_utc", "inverter"]).size().unstack(fill_value=0).to_string())
    p()
    p("Cadencia: mediana de diferencia entre lecturas consecutivas (s):")
    p(df.groupby(["mes_utc", "inverter"])["dt"].median().unstack().round(1).to_string())
    p()
    p("Último timestamp con alguna variable clave no nula, por inversor:")
    for inv, g in df.groupby("inverter"):
        con_dato = g[g[CLAVE].notna().any(axis=1)]
        p(f"  inversor {inv}: {ts_texto(con_dato['read_ts'].max())} UTC")


def huecos(df):
    seccion("3. HUECOS DE TELEMETRÍA (> 1 h entre lecturas consecutivas)")
    total_dia = 0
    for inv, g in df.groupby("inverter"):
        h = g[g["dt"] > 3600].copy()
        h["horas"] = h["dt"] / 3600
        h["inicio_utc"] = (h["read_ts"] - h["dt"]).map(ts_texto)
        h["fin_utc"] = h["read_ts"].map(ts_texto)
        total_dia += int((h["horas"] > 24).sum())
        comparar(f"Huecos > 1 h inversor {inv}", len(h), f"huecos > 1 h inv.{inv}")
        p(f"  Horas totales en huecos > 1 h: {h['horas'].sum():,.0f} h")
        p(h.sort_values("horas", ascending=False).head(8)[["inicio_utc", "fin_utc", "horas"]]
          .round(1).to_string(index=False))
    p(f"\nHuecos > 1 día (suma de ambos inversores): {total_dia} (ref. 6 con ambos inversores contados por separado)")


def calidad(df):
    seccion("4. CALIDAD DE LECTURAS")
    for inv in (1, 2):
        n = int(((df.inverter == inv) & (df["dt"] < 1)).sum())
        comparar(f"Lecturas a < 1 s de la anterior, inv.{inv}", n, f"casi duplicadas (<1 s) inv.{inv}")
    p()
    todas_nulas = df[CLAVE].isna().all(axis=1)
    comparar("Filas con todas las variables clave NULL", todas_nulas.sum(), "filas con todas las claves NULL")
    p("  por inversor: " + str(todas_nulas.groupby(df["inverter"]).sum().to_dict()))
    p("  por mes (solo meses con casos): "
      + str(todas_nulas.groupby(df["mes_utc"]).sum().loc[lambda s: s > 0].to_dict()))
    p()
    p("NULL por columna clave (total / %):")
    for c in CLAVE:
        n = int(df[c].isna().sum())
        p(f"  {c:<14} {n:>9,}  {100 * n / len(df):5.2f} %")
    p()
    p("Valores fuera de rango físico (conteo):")
    fuera = {
        "battsoc fuera de 0-100": int(((df.battsoc < 0) | (df.battsoc > 100)).sum()),
        "pv1power < 0 o > 8000": int(((df.pv1power < 0) | (df.pv1power > 8000)).sum()),
        "outw_a < 0 o > 8000": int(((df.outw_a < 0) | (df.outw_a > 8000)).sum()),
        "linepowerw_a > 8000": int((df.linepowerw_a > 8000).sum()),
        "battvolt fuera de 0-60": int(((df.battvolt < 0) | (df.battvolt > 60)).sum()),
    }
    for k, v in fuera.items():
        p(f"  {k:<28} {v:>8,}")
    comparar("linepowerw_a negativo", (df.linepowerw_a < 0).sum(), "linepowerw_a negativo")
    p(f"  mínimo de linepowerw_a: {df.linepowerw_a.min():.1f} W")
    p()
    comparar("outw_a > 2500 W", (df.outw_a > 2500).sum(), "outw_a > 2500 W")
    incons = (df.outw_a - df.outsumw).abs() > 1500
    p(f"Regla R4: |outw_a - outsumw| > 1500 -> {int(incons.sum()):,} lecturas "
      f"(por inversor: {incons.groupby(df['inverter']).sum().to_dict()})")
    p()
    apagado = (df.battvolt == 0) & (df.linevolta == 0)
    comparar("battvolt=0 y linevolta=0 simultáneos", apagado.sum(), "battvolt=0 y linevolta=0")


def red_ausente(df):
    seccion("5. RED EN 0 V (evento operativo, NO dato inválido)")
    sin_red = df.linevolta == 0
    comparar("Filas con linevolta = 0", sin_red.sum(), "linevolta=0")
    for inv, g in df.groupby("inverter"):
        z = g[g.linevolta == 0]
        if z.empty:
            p(f"  inversor {inv}: sin filas con red en 0 V")
            continue
        ultima_con_red = g.loc[g.linevolta > 0, "read_ts"].max()
        despues = int((g.read_ts > ultima_con_red).sum())
        sin_red_despues = int(((g.read_ts > ultima_con_red) & (g.linevolta == 0)).sum())
        p(f"  inversor {inv}: filas con 0 V = {len(z):,}; primera = {ts_texto(z.read_ts.min())} UTC; "
          f"última con red > 0 V = {ts_texto(ultima_con_red)} UTC")
        p(f"      tras la última lectura con red: {despues:,} filas, de las cuales {sin_red_despues:,} con 0 V")
        p(f"      carga media con red en 0 V: {z.outw_a.mean():.0f} W; PV medio: {z.pv1power.mean():.0f} W; "
          f"linepowerw_a medio: {z.linepowerw_a.mean():.1f} W")
    p()
    p("SoC mínimo por mes e inversor (%):")
    p(df.groupby(["mes_utc", "inverter"])["battsoc"].min().unstack().round(1).to_string())


def contexto(con, df):
    seccion("6. CONTEXTO (JSON)")
    filas = con.execute('SELECT id, recorded, "start", "end", value FROM context').fetchall()
    comparar("Registros de contexto", len(filas), "registros de contexto")
    claves, sources, tipos, inversores, tareas = Counter(), Counter(), Counter(), Counter(), Counter()
    invalidos, duraciones, grid = 0, [], []
    for (_id, _rec, ini, fin, valor) in filas:
        duraciones.append(fin - ini)
        try:
            d = json.loads(valor)
        except (TypeError, ValueError):
            invalidos += 1
            continue
        if not isinstance(d, dict):
            invalidos += 1
            continue
        claves.update(d.keys())
        sources[d.get("source", "SIN_SOURCE")] += 1
        tipos[d.get("context_type") or "SIN_TIPO"] += 1
        inversores[d.get("inverter", "ausente")] += 1
        tareas[d.get("task_type") or "SIN_VALOR"] += 1
        if d.get("context_type") == "grid_unavailable_or_islanded":
            grid.append((ini, fin, d.get("inverter")))
    p(f"JSON inválido o no objeto: {invalidos}")
    p("\nClaves del JSON (frecuencia):")
    for k, v in claves.most_common():
        p(f"  {k:<28} {v:>7,}")
    p("\nValores de source:")
    for k, v in sources.most_common():
        p(f"  {k:<28} {v:>7,}")
    p("\nValores de task_type:")
    for k, v in tareas.most_common():
        p(f"  {k:<28} {v:>7,}")
    p("\ninverter dentro del JSON:")
    for k, v in inversores.most_common():
        p(f"  {str(k):<28} {v:>7,}")
    p("\ncontext_type (top 40):")
    for k, v in tipos.most_common(40):
        p(f"  {k:<42} {v:>7,}")
    p(f"  ... total de tipos distintos: {len(tipos)}")
    d = np.array(duraciones, dtype=float)
    p("\nDuración (end - start):")
    p(f"  instantáneos (end = start): {int((d == 0).sum()):,}")
    p(f"  mediana: {np.median(d) / 60:.1f} min; p99: {np.percentile(d, 99) / 3600:.1f} h; "
      f"máximo: {d.max() / 3600:,.0f} h")
    inicio_tel = df["read_ts"].min()
    previos = sum(1 for f in filas if f[2] < inicio_tel)
    p(f"  registros con start anterior al inicio de la telemetría: {previos:,}")
    p()
    comparar("Contexto grid_unavailable_or_islanded", len(grid), "contexto grid_unavailable_or_islanded")
    if grid:
        p(f"  primer start: {ts_texto(min(g[0] for g in grid))} UTC; último end: {ts_texto(max(g[1] for g in grid))} UTC")
        p(f"  por inversor: {dict(Counter(g[2] for g in grid))}")
        p("  -> contrastar con la primera fila con linevolta = 0 (sección 5) y documentar la conclusión.")


def main():
    ap = argparse.ArgumentParser(description="Diagnóstico de solo lectura del SQLite de OpenCEM")
    ap.add_argument("--sqlite", required=True, help="ruta al archivo opencem-v1.0.0.sqlite")
    ap.add_argument("--salida", help="ruta de un .txt para guardar la salida (evidencia para el informe)")
    args = ap.parse_args()

    con = conectar(args.sqlite)
    inventario(con)
    p("\nCargando analog_measurements (9 columnas) en memoria...")
    df = cargar_telemetria(con)
    cobertura(df)
    huecos(df)
    calidad(df)
    red_ausente(df)
    contexto(con, df)
    con.close()
    seccion("FIN: las líneas con 'REVISAR' indican diferencias frente a las cifras de referencia")
    if args.salida:
        Path(args.salida).parent.mkdir(parents=True, exist_ok=True)
        Path(args.salida).write_text("\n".join(LINEAS), encoding="utf-8")
        print(f"\nSalida guardada en {args.salida}")


if __name__ == "__main__":
    main()