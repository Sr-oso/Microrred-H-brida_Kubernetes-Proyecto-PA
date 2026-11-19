# Diccionario de datos

Convenciones: PK = clave primaria; FK = clave foránea; NN = NOT NULL. "Lineage" indica de dónde sale cada columna. Los timestamps de Bronze son segundos Unix (UTC); en la base se guardan como `timestamptz`.

## 1. Unidades de las variables físicas y evidencia

| Variable (Bronze) | Unidad | Evidencia usada para deducirla |
|---|---|---|
| `read_ts` | s Unix UTC | Segundos fraccionarios; el agrupamiento por día local UTC+8 correlaciona más con los contadores (0,91) que por día UTC (0,85) |
| `pv1power` | W | Integral diaria / contador `pvgeneratenergytoday` = 0,904; máximo observado 3.933 W |
| `outw_a` | W (carga AC de salida) | Integral diaria / contador `usedenergytoday` = 0,994; correlación 0,91 en 190 días completos del inversor 1 |
| `outsumw` | W | Correlación 0,998 con `outw_a`; mediana de la diferencia 0. Se usa para validar `outw_a` (R4) |
| `linepowerw_a` | W (importación de red) | Es 0 cuando `linevolta` = 0 y sube con la carga de batería desde la red. Su integral es ~1,4× la suma de contadores de red: incluye pérdidas y carga de batería |
| `linevolta` | V | ≈ 233 V con red presente; 0 sin red |
| `battvolt` | V | Nominal 51,2 V; rango 0–57,6 |
| `battcurr` | A, con signo | Positivo = descarga, negativo = carga (diccionario oficial) |
| `battchgpower` | W | Correlación −0,98 con `battvolt × battcurr` |
| `battsoc` | % | Rango observado 6–100 |
| `temper1` | °C | Rango −20,3 a 49,3 |
| `gridpowerw_a`, `gridcurr_a` | — | Semántica ambigua (rango −822 a 196 W; corriente casi constante 0,5): **se descartan** |
| `normalloadw_a`, `sir` | — | Siempre 0: se descartan |
| Contadores `statistics` | kWh | `loadconsumlinetday` figura como "component-defined" en el diccionario oficial; por su comportamiento se trata como kWh. Resolución 0,1 kWh, ruidosos y con reinicio cerca del cambio de día: solo se usan como `MAX` por día local en la conciliación |

## 2. Bronze (SQLite, solo lectura, nunca se modifica)

### `analog_measurements` (se leen solo las columnas indicadas)
- **Descripción:** telemetría de los dos inversores. Índice único (`read_ts`, `inverter`).
- **Columnas usadas:** `read_ts`, `inverter`, `pv1power`, `outw_a`, `outsumw`, `linepowerw_a`, `linevolta`, `battvolt`, `battcurr`, `battsoc`, `temper1`.
- **Volumen:** 2.989.759 filas (inv.1: 1.610.276; inv.2: 1.379.483).

### `context`
- **Columnas:** `id` INTEGER PK; `recorded`, `start`, `end` INTEGER (s Unix UTC); `value` TEXT (JSON).
- **Volumen:** 25.178 filas.

### `statistics` (solo conciliación)
- **Columnas usadas:** `pvgeneratenergytoday`, `usedenergytoday`, `loadconsumlinetday`, `linechgenergytday`, `batchgenergytoday`, `batdischgenergytoday`.

## 3. Silver

### `silver.lectura_15min`
- **Descripción:** lecturas limpias agregadas a 15 minutos. **Grano:** un inversor × un intervalo de 15 min con al menos una lectura válida.
- **PK:** (`inversor`, `bin_utc`).

| Columna | Tipo | Restricción | Regla de transformación | Lineage |
|---|---|---|---|---|
| `inversor` | SMALLINT | NN, CHECK IN (1,2), PK | Directa | `analog_measurements.inverter` |
| `bin_utc` | TIMESTAMPTZ | NN, PK | `floor(read_ts / 900) × 900` | `analog_measurements.read_ts` |
| `n_crudas` | INTEGER | NN, ≥ 0 | Lecturas crudas del intervalo (tras R1, antes de R2 a R5) | `analog_measurements` |
| `n_validas` | INTEGER | NN, 0 ≤ n ≤ `n_crudas` | Lecturas que pasan todas las reglas | ídem |
| `pv_w` | REAL | — | Promedio de `pv1power` (W) | `pv1power` |
| `carga_w` | REAL | — | Promedio de `outw_a` (W), tras R4 | `outw_a` |
| `red_w` | REAL | — | Promedio de `linepowerw_a` (W), tras R3 | `linepowerw_a` |
| `bateria_desc_w` | REAL | — | Promedio por lectura de `MAX(battvolt × battcurr, 0)` | `battvolt`, `battcurr` |
| `bateria_carga_w` | REAL | — | Promedio por lectura de `MAX(−battvolt × battcurr, 0)` | ídem |
| `soc_pct` | REAL | CHECK 0–100 | Promedio de `battsoc` | `battsoc` |
| `temp_c` | REAL | — | Promedio de `temper1` | `temper1` |
| `red_presente` | BOOLEAN | — | `true` si más del 50 % de las lecturas del intervalo tienen `linevolta > 0` | `linevolta` |

### `silver.contexto`
- **Descripción:** registros de contexto con tiempos convertidos y JSON parseado. **Grano:** un registro de `context`. **PK:** `id`.

| Columna | Tipo | Restricción | Regla | Lineage |
|---|---|---|---|---|
| `id` | INTEGER | PK | Directa | `context.id` |
| `inicio_utc` | TIMESTAMPTZ | NN | `start` a timestamptz | `context.start` |
| `fin_utc` | TIMESTAMPTZ | NN, CHECK ≥ `inicio_utc` | `end` a timestamptz | `context.end` |
| `registrado_utc` | TIMESTAMPTZ | — | `recorded` a timestamptz | `context.recorded` |
| `inversor` | SMALLINT | — | `inverter` del JSON; NULL si ausente (42 registros) | `value->inverter` |
| `context_type` | TEXT | — | `context_type` del JSON; NULL si ausente (61 registros) | `value->context_type` |
| `payload` | JSONB | NN | JSON parseado (con `json.loads`), filtrado por la lista blanca de la sección 5 | `context.value` |

## 4. Esquema `etl` (evidencia y auditoría)

| Tabla | PK | Columnas | Propósito |
|---|---|---|---|
| `etl.regla_limpieza` | (`run_id`, `orden`) | `run_id` INTEGER, `orden` SMALLINT, `regla` TEXT, `tabla` TEXT, `filas_antes`, `filas_despues`, `filas_afectadas` BIGINT, `ts` TIMESTAMPTZ DEFAULT now() | Conteos antes y después de cada regla (R0 a R7) |
| `etl.bitacora` | `run_id` SERIAL | `inicio` TIMESTAMPTZ, `fin` TIMESTAMPTZ, `estado` TEXT, `filas_crudas`, `filas_silver`, `filas_gold` BIGINT, `detalle` JSONB | Una fila por ejecución del ETL |
| `etl.conciliacion` | (`run_id`, `metrica`, `inversor`) | `run_id` INTEGER, `metrica` TEXT, `inversor` SMALLINT, `valor_fuente`, `valor_destino`, `diferencia`, `diferencia_pct` NUMERIC, `explicacion` TEXT | Comparación de totales contra la fuente |

## 5. Gold — dimensiones

Todas se recargan por completo en cada ejecución (son estáticas y pequeñas).

### `gold.dim_fecha` — PK `fecha_key`
| Columna | Tipo | Regla / lineage |
|---|---|---|
| `fecha_key` | INTEGER | AAAAMMDD de la fecha local. Generada |
| `fecha` | DATE | Fecha local (2025-07-01 a 2026-07-31) |
| `anio` | SMALLINT | Año |
| `trimestre` | SMALLINT | 1–4 |
| `mes` | SMALLINT | 1–12 |
| `nombre_mes` | TEXT | Nombre del mes en español |
| `semana_iso` | SMALLINT | Semana ISO |
| `dia_mes` | SMALLINT | 1–31 |
| `dia_semana` | SMALLINT | 1 (lunes) a 7 (domingo) |
| `nombre_dia` | TEXT | Nombre del día en español |
| `es_fin_de_semana` | BOOLEAN | `dia_semana` ≥ 6 |
| `mes_anio` | TEXT | AAAA-MM (para ordenar) |

### `gold.dim_hora` — PK `hora_key` (24 filas, construida por el equipo)
| Columna | Tipo | Regla / lineage |
|---|---|---|
| `hora_key` | SMALLINT | 0–23 (hora local) |
| `hora` | SMALLINT | Igual a `hora_key` |
| `franja` | TEXT | `madrugada` 0–5, `manana` 6–10, `mediodia_solar` 11–14, `tarde` 15–18, `noche` 19–23 |
| `es_diurno` | BOOLEAN | Horas 6 a 18 |
| `orden_franja` | SMALLINT | 1 a 5, en el orden anterior |

### `gold.dim_inversor` — PK `inversor_key`
| Columna | Tipo | Valor / lineage |
|---|---|---|
| `inversor_key` | SMALLINT | 1, 2 y 0 ("No aplica / ambos") |
| `nombre` | TEXT | "Inversor 1", "Inversor 2", "No aplica / ambos" |
| `kwp_instalado` | NUMERIC(6,2) | 12,48 (26 × 480 W) |
| `bateria_kwh_nominal` | NUMERIC(6,2) | 10,24 (200 Ah × 51,2 V) |
| `potencia_nominal_kw` | NUMERIC(5,2) | 8,0 |
| `n_paneles` | SMALLINT | 26 |
| `potencia_panel_w` | SMALLINT | 480 |
| `sitio` | TEXT | "Campus CUHK-Shenzhen" |

Para la clave 0 los atributos físicos son NULL. Lineage: documentación del dataset y `settings` (`battpackrateah` = 200).

### `gold.dim_tipo_evento` — PK `tipo_evento_key`
| Columna | Tipo | Regla / lineage |
|---|---|---|
| `tipo_evento_key` | SERIAL | Sustituta |
| `context_type` | TEXT, UNIQUE | Texto exacto del JSON; 'SIN_TIPO' si es nulo |
| `categoria` | TEXT | Mapeo propuesto: `carga_computo`, `sistema_ti`, `energia_operativo`, `calendario_solar`, `calidad_datos`, `otro` |
| `es_critico` | BOOLEAN | `true` para `grid_unavailable_or_islanded`, `unexpected_shutdown`, `data_gap`, `power_transition`, `sin_red_detectado` |
| `origen` | TEXT | 'contexto' o 'derivado_telemetria' |

## 6. Gold — hechos

### `gold.fact_energia_15min`
- **Grano:** un inversor × un intervalo de 15 min. **PK:** (`inversor_key`, `bin_utc`). Carga idempotente con `INSERT … ON CONFLICT DO UPDATE`.

| Columna | Tipo | Restricción | Regla / lineage |
|---|---|---|---|
| `inversor_key` | SMALLINT | PK, FK → `dim_inversor` | `silver.lectura_15min.inversor` |
| `bin_utc` | TIMESTAMPTZ | PK | `silver.lectura_15min.bin_utc` |
| `fecha_key` | INTEGER | FK → `dim_fecha` | AAAAMMDD de `bin_utc` en Asia/Shanghai |
| `hora_key` | SMALLINT | FK → `dim_hora` | Hora de `bin_utc` en Asia/Shanghai |
| `bin_local` | TIMESTAMP | — | `bin_utc` en hora local, sin zona |
| `pv_kwh` | DOUBLE PRECISION | ≥ 0 | `pv_w × 0,25 / 1000`; NULL si `pv_w` es NULL |
| `carga_kwh` | DOUBLE PRECISION | ≥ 0 | `carga_w × 0,25 / 1000` |
| `red_kwh` | DOUBLE PRECISION | ≥ 0 | `red_w × 0,25 / 1000` |
| `bateria_descarga_kwh` | DOUBLE PRECISION | ≥ 0 | `bateria_desc_w × 0,25 / 1000` |
| `bateria_carga_kwh` | DOUBLE PRECISION | ≥ 0 | `bateria_carga_w × 0,25 / 1000` |
| `pv_directo_kwh` | DOUBLE PRECISION | ≥ 0 | `LEAST(pv_kwh, carga_kwh)`; NULL si alguno es NULL |
| `red_a_carga_kwh` | DOUBLE PRECISION | ≥ 0 | `LEAST(red_kwh, carga_kwh)`; NULL si alguno es NULL |
| `soc_prom` | REAL | 0–100 | `silver.soc_pct` |
| `temp_prom_c` | REAL | — | `silver.temp_c` |
| `red_presente` | BOOLEAN | — | `silver.red_presente` |
| `n_crudas` | INTEGER | ≥ 0 | `silver.n_crudas` |
| `n_validas` | INTEGER | ≥ 0 | `silver.n_validas` |

### `gold.fact_evento_operativo`
- **Grano:** un evento por inversor. **PK:** `evento_key`.

| Columna | Tipo | Restricción | Regla / lineage |
|---|---|---|---|
| `evento_key` | BIGSERIAL | PK | Sustituta |
| `evento_id_fuente` | INTEGER | NULL, UNIQUE cuando no es NULL | `context.id`; NULL en eventos derivados |
| `inversor_key` | SMALLINT | FK → `dim_inversor` | `inverter`; 0 si es nulo |
| `tipo_evento_key` | INTEGER | FK → `dim_tipo_evento` | Según `context_type` ('SIN_TIPO' si nulo) |
| `fecha_inicio_key` | INTEGER | FK → `dim_fecha` | Fecha local de `inicio_utc` |
| `inicio_utc`, `fin_utc` | TIMESTAMPTZ | NN, `fin_utc` ≥ `inicio_utc` | De `start` y `end` |
| `duracion_min` | DOUBLE PRECISION | ≥ 0 | `(fin − inicio) / 60` |
| `fuente` | TEXT | — | `payload->>'source'` |
| `descripcion` | TEXT | máx. 300 caracteres | `textual_description` o, si no existe, `summary` |
| `payload` | JSONB | NN | Solo claves de la lista blanca |
| `origen` | TEXT | — | 'contexto' o 'derivado_telemetria' |

**Lista blanca del `payload` (Ley 1581):** `source`, `device`, `context_type`, `task_type`, `inverter`, `model`, `dataset`, `epochs`, `batch_size`, `gap_seconds`, y `textual_description` o `summary` truncados. Se **descartan** `command`, `directory`, `windows_event`, `task_files`, `library` y cualquier otra clave.

### `gold.fact_lectura_reciente`
- **Grano:** un inversor × una lectura de ~1 minuto. **PK:** (`inversor_key`, `ts_utc`). Alimentada por el simulador (y opcionalmente por una carga histórica de arranque).

| Columna | Tipo | Restricción | Descripción |
|---|---|---|---|
| `inversor_key` | SMALLINT | PK, FK → `dim_inversor` | 1 o 2 |
| `ts_utc` | TIMESTAMPTZ | PK | Instante de la lectura |
| `pv_w`, `carga_w`, `red_w` | REAL | ≥ 0 | Potencias en W |
| `bateria_w` | REAL | — | Positivo = descarga, negativo = carga (W) |
| `soc_pct` | REAL | 0–100 | Estado de carga (%) |
| `red_presente` | BOOLEAN | — | Disponibilidad de red |
| `origen` | TEXT | 'simulador' o 'historico' | Procedencia |

### `gold.parametro`
| Columna | Tipo | Restricción | Descripción |
|---|---|---|---|
| `nombre` | TEXT | PK | Nombre del parámetro |
| `valor` | NUMERIC | NN | Valor. Incluye `tarifa_cop_kwh` = 900 (ejemplo, no oficial) |

## 7. Vistas

| Vista | Grano | Columnas principales |
|---|---|---|
| `gold.vw_kpi_dia` | inversor × día local | `inversor_key`, `fecha_key`, `fecha`, `pv_kwh`, `carga_kwh`, `red_kwh`, `red_a_carga_kwh`, `pv_directo_kwh`, `ssr`, `ssr_con_red`, `cobertura_solar_directa`, `disponibilidad_pct`, `datos_validos_pct`, `soc_min`, `ciclos_equivalentes`, `horas_sin_red`, `costo_importacion_cop`, `costo_evitado_cop` |
| `gold.vw_kpi_mes` | inversor × mes | Las mismas columnas agregadas por `mes_anio` |

## 8. Conciliación (referencia)

| Métrica | Cómo se compara | Resultado esperado |
|---|---|---|
| Lecturas válidas | Filas SQLite − filas descartadas por regla | Cuadra exacto |
| Energía de carga, PV y red | Integral directa sobre la fuente (potencia × diferencia de tiempo hasta la siguiente lectura, tope 60 s) vs Σ de Gold | Diferencia pequeña, explicada por ventanas de 15 min, huecos y el tope de 60 s |
| Carga vs `usedenergytoday` | `MAX` diario local (UTC+8) | Razón ≈ 0,99 |
| PV vs `pvgeneratenergytoday` | ídem | Razón ≈ 0,90 |
| Red vs contadores | ídem | ≈ 1,4× (por la definición de `linepowerw_a`) |
| Registros de contexto | Conteo en destino | 25.178 |