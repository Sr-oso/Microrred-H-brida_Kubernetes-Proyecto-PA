<!-- docs/01_negocio_y_diagnostico.md -->
# 1. Negocio

## 1.1 Contexto
El campus CUHK-Shenzhen opera una microrred híbrida con dos subsistemas independientes (inversor 1 e inversor 2). Cada uno tiene 26 paneles de 480 W (12,48 kWp), un banco de litio de 10,24 kWh nominales, entrada de red y un inversor híbrido de 8 kW monofásico. Alimenta estaciones de cómputo (CPU/GPU) y climatización. Hoy los datos se exportan a archivos y nadie los convierte en decisiones.

## 1.2 Stakeholders y preguntas de negocio

| Stakeholder | Necesidad | Preguntas de negocio | Indicadores | Herramienta |
|---|---|---|---|---|
| **Dirección de infraestructura** | Decidir inversiones (más paneles, más batería, gestión de cargas) | 1. ¿Qué fracción de la energía de la carga se atiende sin tomarla de la red (autosuficiencia, SSR)? 2. ¿Qué parte la cubre el sol directamente y cuál depende de la batería? 3. ¿Qué franjas horarias y meses dependen más de la red? 4. ¿Cuánto cambió la autosuficiencia frente al mes anterior y entre periodos? 5. ¿Los entrenamientos GPU aumentan la carga de forma relevante? 6. ¿Cuánto mejoraría la autosuficiencia con más PV? | #1, #2, #3, #6, #7, #8, #12 y what-if de PV adicional | Power BI |
| **Mantenimiento** | Ver el estado casi en tiempo real y recibir alertas | 1. ¿Cuál es el SoC ahora y está por debajo del umbral? 2. ¿Está llegando telemetría de cada inversor? 3. ¿Hay red disponible? 4. ¿Cuánto se desgasta la batería (ciclos equivalentes, tiempo con SoC bajo)? 5. ¿Qué tan completos y confiables son los datos? | #4, #5, #9, #10, #11 y alertas | Grafana y Power BI (página 3) |
| **Finanzas / TI** | Cuantificar costos y garantizar que la solución corra de forma reproducible y segura | 1. ¿Cuánto cuesta la energía importada por día y mes? 2. ¿Cuánto se evita comprar gracias al sol y la batería? 3. ¿Cómo cambia el ahorro con la tarifa? 4. ¿La solución corre sobre Kubernetes, se levanta con un comando y protege las credenciales y los datos personales del contexto? | #13, #14, parámetro what-if de tarifa | Power BI, consola `kubectl` |

## 1.3 Preguntas guía del enfoque SolarBI
La pregunta central es: *¿qué fracción de la energía que consume la carga se atiende sin tomarla de la red, y qué la explica?* Se responde con el SSR (#6), el SSR con red disponible (#7), la cobertura solar directa (#8) y su desglose por hora, mes, inversor y eventos.

# 2. Diagnóstico de la fuente

## 2.1 Origen y verificación de integridad
Dataset OpenCEM v1.0.0 (CC BY 4.0). Se usa el SQLite (`opencem-v1.0.0.sqlite.zst`, 302,1 MB; descomprimido ≈ 4,4 GB). Las imágenes de cielo quedan fuera de alcance.

```bash
# Linux/macOS/WSL2
sha256sum -c opencem-v1.0.0-SHA256SUMS.txt --ignore-missing
zstd -d opencem-v1.0.0.sqlite.zst -o opencem-v1.0.0.sqlite
# Windows PowerShell
Get-FileHash -Algorithm SHA256 .\opencem-v1.0.0.sqlite.zst
```

SHA-256 esperado del `.zst`: `8e38f98209029367a3775799ee9a7c0757cc7e8f2742e4df4eccc45dd4b27517`.
**Evidencia a pegar en el informe:** salida de `sha256sum -c` (o `Get-FileHash`) y de `sqlite3 opencem-v1.0.0.sqlite ".tables"`.

## 2.2 Estructura (Bronze)
Todas las tablas de telemetría tienen índice único (`read_ts`, `inverter`); `read_ts` son segundos Unix fraccionarios en UTC, `inverter` ∈ {1, 2} y NULL significa dato faltante (la fuente no imputa).

| Tabla | Filas | Cols | Periodo (UTC) | Uso |
|---|---|---|---|---|
| `analog_measurements` | 2.989.759 (inv.1: 1.610.276; inv.2: 1.379.483) | 44 | 2025-07-12 13:31:35 → 2026-07-13 05:13:53 | Principal |
| `statistics` | 2.285.904 | 31 | 2025-07-12 → 2026-07-13 | Solo conciliación (contadores diarios en kWh) |
| `status` | 2.240.052 | 20 | idem | Opcional |
| `totals` | 2.279.918 | 21 | idem | No se usa |
| `settings` | 2.284.775 | 176 | idem | Solo parámetros de batería |
| `specs` | 1.333.174 | 16 | 2025-07-12 → 2026-02-02 | No se usa |
| `fault_history` | 2.129.517 | 66 | 2025-07-12 → 2026-07-13 | No se usa |
| `context` | 25.178 | 5 | `start` desde 2024-04-30; `end` hasta 2026-07-22 | Eventos (JSON) |

El SQLite ya fue podado de columnas vacías (44 columnas frente a las 82 del CSV espejo): se eliminaron canales PV2/PV3 y fases B/C porque la instalación es monofásica.

## 2.3 Cobertura y cadencia
La cadencia cambia con el tiempo: ~366 s (2025-07), ~111 s (2025-08/09), ~12,6 s (2025-10 a 2026-05) y ~10,7 s (2026-06/07). Esto justifica agregar a 15 minutos en Silver.
El inversor 2 deja de reportar valores desde mayo de 2026 y no hay datos después del 2026-06-22. Los meses completos y comparables son 2025-10 a 2026-04.

## 2.4 Problemas de calidad (referencia para validar el ETL)

| # | Hallazgo | Cifra de referencia | Tratamiento previsto |
|---|---|---|---|
| 1 | Cadencia irregular | 10,7 s a 366 s | Agregar a 15 min |
| 2 | Huecos de telemetría | 6 huecos > 1 día (el mayor 627,5 h); 31 huecos > 1 h (16 + 15) | Intervalos sin fila; cuentan en disponibilidad |
| 3 | Lecturas a menos de 1 s | 19.865 (inv.1) + 514 (inv.2) | R1: se descarta la posterior |
| 4 | Todas las variables clave NULL | 55.658 (55.657 del inv.2) | R2: excluir; es "sin comunicación", no cero |
| 5 | NULL esporádicos | ~58 mil por columna (≈1,9 %) | Se tratan como faltantes |
| 6 | Picos de carga inconsistentes | 53 lecturas con `outw_a` > 2.500 W | R4: invalidar si `|outw_a − outsumw|` > 1.500 |
| 7 | `linepowerw_a` negativo | 9 filas | R3: campo a NULL |
| 8 | `battvolt` = 0 con `linevolta` = 0 | 3 filas | R5: descartar lectura |
| 9 | Red en 0 V | 342.593 filas, desde 2026-05-03 ~07:50 UTC (inv.1) | R6: se conserva; es un **evento operativo**, no un error |
| 10 | SoC mínimo de 10 % del inv.2 (2026-01 a 04) | — | Insumo del análisis de batería |
| 11 | Contexto JSON | 25.178 registros, JSON válido; 42 sin inversor; 61 sin `context_type` | Mapeo a `dim_tipo_evento` |
| 12 | Unidades y semántica ambiguas | `loadconsumlinetday`, `gridpowerw_a` | Se documenta la evidencia; `gridpowerw_a` se descarta |
| 13 | Columnas de texto/arreglos JSON (`*total`, `*last7day`) | — | No se usan |

## 2.5 Limitaciones
1. No hay irradiancia ni temperatura de módulo, así que **no se puede calcular el Performance Ratio** (se usa rendimiento específico en kWh/kWp y una envolvente p95 como proxy).
2. La generación solo se mide en CC del canal PV1 (12,48 kWp instalados frente a 3,93 kW observados como máximo).
3. `linepowerw_a` sobreestima la importación frente a los contadores (≈1,4×): incluye pérdidas y carga de batería.
4. Los contadores diarios de `statistics` son ruidosos (resolución 0,1 kWh, no monótonos), por eso solo se usan para conciliar.
5. Desde mayo de 2026 la red está en 0 V (inversor 1): el SSR es trivialmente 100 % y no es comparable con el periodo anterior, por lo que se reporta también el SSR con red disponible.
6. El inversor 2 no tiene datos útiles tras el 2026-06-22.
7. Los timestamps están en UTC y el análisis se hace en hora local (UTC+8).