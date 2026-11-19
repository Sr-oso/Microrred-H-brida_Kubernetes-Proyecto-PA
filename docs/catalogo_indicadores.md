# Catálogo de indicadores — SolarBI

Enfoque: **autosuficiencia energética y fracción de la carga atendida sin la red**.
Pregunta central: *¿qué fracción de la energía que consume la carga se atiende sin tomarla de la red, y qué la explica (sol directo, batería, hora, mes, inversor, eventos)?*

## 1. Definiciones físicas (por intervalo de 15 min, `h = 0,25`)

Fuente de todas las potencias: `silver.lectura_15min` (promedios de potencia por intervalo, ya limpios).

| Magnitud | Fórmula | Unidad |
|---|---|---|
| `pv_kwh` | `pv_w × h / 1000` | kWh |
| `carga_kwh` | `carga_w × h / 1000` | kWh |
| `red_kwh` | `red_w × h / 1000` | kWh |
| `bateria_descarga_kwh` | `bateria_desc_w × h / 1000` | kWh |
| `bateria_carga_kwh` | `bateria_carga_w × h / 1000` | kWh |
| `red_a_carga_kwh` | `LEAST(red_kwh, carga_kwh)` | kWh |
| `pv_directo_kwh` | `LEAST(pv_kwh, carga_kwh)` | kWh |

Reglas:

1. `red_a_carga_kwh` y `pv_directo_kwh` se calculan **por intervalo** y luego se suman. Nunca se calculan con totales, porque un exceso en un intervalo compensaría un déficit en otro.
2. `LEAST(red_kwh, carga_kwh)`: el exceso de `red_kwh` sobre la carga se va a cargar la batería o son pérdidas de conversión (por eso la importación de `linepowerw_a` es ~1,4 veces la de los contadores).
3. **Un dato faltante no es cero.** Si un intervalo no tiene lecturas válidas, no existe fila. Si una variable del intervalo es NULL (por ejemplo `outw_a` invalidada por R4), su energía queda NULL y no se convierte en 0.
4. **Consistencia de denominadores:** el SSR (#6, #7) y la cobertura solar directa (#8) solo suman intervalos donde `carga_kwh` **y** `red_kwh` (o `pv_kwh`, según el caso) no son NULL. Así numerador y denominador se calculan sobre el mismo conjunto de intervalos.

## 2. Catálogo

Convención de fuente: todos salen de `gold.fact_energia_15min` salvo indicación contraria. Las metas marcadas "(propuesta)" no vienen de la fuente y las define el equipo.

| # | Indicador | Fórmula | Unidad | Granularidad | Meta / umbral | Fuente | Usuario |
|---|---|---|---|---|---|---|---|
| 1 | Energía solar generada | Σ `pv_kwh` | kWh | intervalo, día, mes, periodo, inversor | Informativo | `fact_energia_15min` | Dirección |
| 2 | Energía entregada a la carga | Σ `carga_kwh` | kWh | ídem | Informativo | ídem | Dirección |
| 3 | Energía importada de la red | Σ `red_kwh` | kWh | ídem | Informativo (menor es mejor) | ídem | Dirección / Finanzas |
| 4 | Disponibilidad del registro | Intervalos de 15 min con ≥ 1 lectura válida / intervalos esperados (96 por día local, dentro del rango operativo del inversor) | % | día, mes, inversor | ≥ 95 % (propuesta) | `fact_energia_15min` | Mantenimiento |
| 5 | % de datos válidos | Lecturas crudas que pasan todas las reglas / lecturas crudas totales (Σ `n_validas` / Σ `n_crudas`) | % | día, mes, inversor | ≥ 95 % (propuesta) | `fact_energia_15min` (conteos de Silver) | Mantenimiento / BI |
| 6 | **Autosuficiencia energética (SSR)** | `1 − Σ red_a_carga_kwh / Σ carga_kwh` | % (0–100) | día, mes, periodo, inversor, hora | Meta ≥ 50 %; alerta amarilla < 35 % | ídem | Dirección |
| 7 | **Autosuficiencia con red disponible** | Igual que #6 solo sobre intervalos con `red_presente = true` | % | ídem | Mismas metas que #6 | ídem | Dirección |
| 8 | **Cobertura solar directa** (híbrido) | Σ `pv_directo_kwh` / Σ `carga_kwh` | % | ídem | Informativo; referencia observada 22–32 % | ídem | Dirección |
| 9 | **Ciclos equivalentes de batería** (híbrido) | Σ `bateria_descarga_kwh` / 10,24 kWh | ciclos | día, mes | Informativo; alerta > 1 ciclo/día (propuesta) | ídem | Mantenimiento |
| 10 | **Tiempo con SoC bajo** (híbrido) | Intervalos con `soc_prom` < 20 % × 15 min / minutos con dato de SoC | % o min | día, mes | < 5 % del tiempo (propuesta) | ídem | Mantenimiento |
| 11 | **Horas sin red / isla** (híbrido) | Intervalos con `red_presente = false` × 0,25 | h | día, mes | Informativo | ídem | Dirección / Mantenimiento |
| 12 | Factor de utilización solar | Σ `pv_kwh` / Σ `carga_kwh` | razón | mes | > 1 indica excedente de generación | ídem | Dirección |
| 13 | **Costo de la energía importada** (económico) | Σ `red_kwh` × tarifa | COP | día, mes | Informativo | `fact_energia_15min` + `gold.parametro` | Finanzas |
| 14 | **Costo evitado por autosuficiencia** (económico) | (Σ `carga_kwh` − Σ `red_a_carga_kwh`) × tarifa | COP | mes | Informativo | ídem | Dirección |
| 15 | Rendimiento específico | Σ `pv_kwh` / 12,48 kWp | kWh/kWp | día, mes | Informativo | `fact_energia_15min` + `dim_inversor.kwp_instalado` | Dirección |
| 16 | PR relativo a la envolvente (proxy) | Σ `pv_kwh` real / Σ `pv_kwh` de la envolvente p95 por hora y mes | razón | mes | Informativo | `fact_energia_15min` | Dirección |

### Justificación de los indicadores híbridos propios (#7 a #11)

- **#7 Autosuficiencia con red disponible.** Desde 2026-05-03 la red está en 0 V en el inversor 1, así que el SSR es trivialmente 100 % y no se puede comparar con el periodo anterior. Este indicador restringe el cálculo a intervalos con red y mantiene la comparabilidad en el tiempo.
- **#8 Cobertura solar directa.** Separa la carga que cubre el sol sin pasar por la batería de la que depende de la batería o la red. Permite decidir si conviene más PV, más batería o mover cargas a horas solares.
- **#9 Ciclos equivalentes.** Un ciclo equivalente es una descarga acumulada igual a la capacidad nominal. Mide el desgaste del banco de litio y orienta el mantenimiento.
- **#10 Tiempo con SoC bajo.** El umbral de 20 % es una pre-alerta por encima de la alarma del inversor (15 %) y de la parada (5 %). Indica riesgo de corte y estrés de la batería (en el inversor 2 el SoC mínimo llegó a 10 %).
- **#11 Horas sin red.** Cuantifica la operación en isla, un evento operativo real (no un error de datos), y la contrasta con el contexto `grid_unavailable_or_islanded`.

## 3. Indicador económico y tarifa

- **Parámetro:** `gold.parametro.tarifa_cop_kwh`, con valor de ejemplo de **900 COP/kWh**.
- **Importante:** 900 COP/kWh es un valor de ejemplo, **no una cifra oficial**. El equipo debe sustituirlo por la tarifa de su factura o la de su operador de energía para el sector que corresponda, y justificar el origen (operador, mes y estrato o sector) en el informe.
- **Por qué una tarifa única:** los datos son de un campus en Shenzhen (China) y el taller pide el costo en COP. Se usa una tarifa plana de referencia y se analiza su efecto con el parámetro what-if `Tarifa COP/kWh` (400–1.600) en Power BI.
- **Limitación:** la tarifa real puede variar por franja horaria y por componentes (cargo fijo, contribuciones). El indicador estima el costo de la energía importada, no una factura completa.

## 4. Análisis crítico: indicador clásico que no se puede calcular

**Performance Ratio (PR)** y el índice de rendimiento corregido por irradiancia.

- **Por qué no:** requieren irradiancia en el plano del arreglo (W/m²) y temperatura del módulo. Ninguna existe en la fuente: no hay piranómetro y las imágenes de cielo están fuera del alcance.
- **Qué se hace:**
  1. Calcular el **rendimiento específico** (#15, kWh/kWp con 12,48 kWp), que sí es calculable.
  2. Usar como *proxy* la **envolvente de máxima generación** observada (percentil 95 de `pv_w` por hora y mes) y calcular un **PR relativo a la envolvente** (#16).
  3. Declarar la limitación explícitamente en el informe y los tableros.
  4. Recomendar instalar un sensor de irradiancia o usar un modelo de cielo despejado.
- **Otras limitaciones de la fuente:**
  - La generación solo se mide en CC del canal PV1. No existe energía exportada en esta instalación.
  - La potencia observada máxima de PV (3,93 kW) es muy inferior a la nominal (12,48 kWp). El canal PV del inversor limita la entrada, y no se puede distinguir limitación del equipo de sombreado o suciedad.
  - La importación de red está sobreestimada frente a los contadores (razón ~1,4) porque `linepowerw_a` incluye pérdidas y carga de batería.

## 5. Valores de referencia para validar el ETL

Medidos en la exploración (lecturas ponderadas por duración con tope de 60 s). El ETL con ventanas de 15 min debe dar valores parecidos, no idénticos.

| Magnitud | Inversor 1 | Inversor 2 |
|---|---|---|
| SSR del periodo completo | ≈ 0,53 | ≈ 0,39 |
| SSR mensual 2025-09 a 2026-04 | ≈ 0,40–0,47 | ≈ 0,35–0,49 (2025-07 a 2026-03); ≈ 0,18 en 2026-04 |
| SSR 2026-05 / 2026-06-07 | ≈ 0,91 / 1,00 (red ausente) | — |
| Cobertura solar directa | ≈ 0,27–0,32 | ≈ 0,22–0,26 |
| Energía total de carga | ≈ 1.743 kWh | ≈ 3.258 kWh |
| Energía PV | ≈ 880 kWh | ≈ 1.103 kWh |

El SSR calculado con contadores (inversor 1, 190 días) es ≈ 0,54, consistente con el cálculo por potencias.