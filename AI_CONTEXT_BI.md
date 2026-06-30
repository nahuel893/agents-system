# AI Context — Consumir Silver/Gold del Medallion DB

Documento de contexto para una IA que va a **consultar** la base `medallion_db`
(silver + gold). No cubre el ETL ni el bronze — solo el modelo de consumo.

---

## 1. Propósito y scope

- Base: `medallion_db` (PostgreSQL 15+)
- Schemas relevantes: `silver` (normalizado tipado) y `gold` (star schema dimensional)
- **Bronze NO se consulta** (es JSONB crudo, transitorio)
- Read-only desde el proyecto consumer

---

## 2. Conexión

### Variables de entorno (mínimas)

```env
DB_HOST=                  # IP o hostname del Postgres
DB_PORT=5432
DB_NAME=medallion_db
DB_USER=                  # usar el READONLY_USER del ETL
DB_PASSWORD=
```

> **Usar siempre el rol `readonly_user`** (creado por el ETL). Tiene `SELECT` solo
> sobre `gold`. Si necesitás también `silver`, pedir al admin un grant explícito o
> usar otro rol con menor privilegio que `etl_user`.

### Ejemplo de conexión (Python + psycopg2)

```python
import os
import psycopg2
from psycopg2.extras import RealDictCursor

conn = psycopg2.connect(
    host=os.environ["DB_HOST"],
    port=int(os.environ.get("DB_PORT", 5432)),
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    sslmode="disable",  # el ETL no usa SSL
)

with conn.cursor(cursor_factory=RealDictCursor) as cur:
    cur.execute("SELECT * FROM gold.dim_sucursal")
    for row in cur.fetchall():
        print(row)
```

### Ejemplo (SQLAlchemy)

```python
from sqlalchemy import create_engine, text

url = (
    f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}?sslmode=disable"
)
engine = create_engine(url, pool_pre_ping=True)

with engine.connect() as conn:
    rows = conn.execute(text("SELECT * FROM gold.dim_sucursal")).mappings().all()
```

---

## 3. Reglas críticas (LEER ANTES DE QUERYAR)

### 3.1 Composite keys: `id_sucursal` casi siempre va en el JOIN

Los IDs son únicos **por sucursal**, NO globalmente. Si un JOIN involucra
`staff`, `routes`, `dim_vendedor`, `fact_ventas` (en lo que toca vendedor),
**siempre** sumá `AND a.id_sucursal = b.id_sucursal`.

```sql
-- CORRECTO
SELECT *
FROM gold.fact_ventas fv
JOIN gold.dim_vendedor dv
  ON fv.id_vendedor = dv.id_vendedor
 AND fv.id_sucursal = dv.id_sucursal;

-- INCORRECTO (mezcla data entre sucursales — bug silencioso)
SELECT *
FROM gold.fact_ventas fv
JOIN gold.dim_vendedor dv ON fv.id_vendedor = dv.id_vendedor;
```

Tablas afectadas: `silver.staff`, `silver.routes`, `gold.dim_vendedor`,
`gold.fact_comodatos`, y cualquier JOIN que involucre estas.

`dim_cliente`, `dim_articulo`, `dim_deposito`, `dim_sucursal`, `dim_tiempo` SÍ
tienen PK única globalmente — no requieren id_sucursal en el join.

### 3.2 `anulado` — **NO filtrar por defecto**

`fact_ventas.anulado` y `fact_ventas_contabilidad.anulado` traen registros
anulados con `cantidades_total` y `subtotal_*` en cero o negativos. **Convención
del proyecto: NO filtrarlos**, el negocio quiere que figuren para auditoría y
porque ya están reflejados en la facturación neta.

Si tu reporte específicamente requiere "solo ventas vigentes", agregá
`WHERE anulado = false` explícito — pero por default, dejá los anulados adentro.

### 3.3 SCD Type 1 en dimensiones — no hay historia

Todas las dimensiones (`dim_cliente`, `dim_articulo`, `dim_vendedor`, etc.)
sobrescriben en cada corrida. **Reflejan el estado actual**, no el del momento
de la venta.

Implicación práctica:
- Si un cliente cambió de ruta/preventista, todas sus ventas históricas en
  cualquier query que joinee con `dim_cliente.id_personal_fv1` van a mostrar
  la asignación **nueva**, no la del día de la venta.
- Si necesitás el vendedor que **facturó** la venta, usá `fact_ventas.id_vendedor`
  (transaccional). Si necesitás el vendedor **asignado al cliente hoy**, usá
  `dim_cliente.id_personal_fv1/fv4`.

`dim_tiempo` es Type 0 (inmutable).

### 3.4 Hectolitros

- En `fact_ventas` el campo ya viene calculado: `cantidad_total_htls` (no hay
  que multiplicar nada).
- En `dim_articulo.factor_hectolitros` está el factor por unidad.
- Si `factor_hectolitros IS NULL`, el artículo no tiene equivalencia volumétrica
  declarada → `cantidad_total_htls` queda NULL en sus ventas. Filtrá con
  `WHERE cantidad_total_htls IS NOT NULL` si no querés ese ruido.

### 3.5 Identificación de comprobante

La columna se llama **`id_documento`** (VARCHAR), no `id_comprobante`. Y NO es
único globalmente — la clave natural es `(id_documento, serie, nro_doc, id_sucursal)`.

Para contar comprobantes únicos:
```sql
COUNT(DISTINCT (id_documento, serie, nro_doc, id_sucursal))
```

---

## 4. ¿Silver o Gold?

| Caso de uso                                              | Schema | Tabla típica                |
|----------------------------------------------------------|--------|-----------------------------|
| Reportes BI / dashboards                                 | gold   | `fact_ventas` + `dim_*`     |
| Hectolitros vendidos                                     | gold   | `fact_ventas.cantidad_total_htls` |
| Cobertura (clientes compradores por marca/genérico/mes)  | gold   | `cob_*`                     |
| Cupos / objetivos comerciales                            | gold   | `fact_cupos`, `fact_cupos_cobertura` |
| Stock por depósito + día                                 | gold   | `fact_stock`                |
| Comodatos (equipos en clientes)                          | gold   | `fact_comodatos`            |
| Detalle contable (IVA, percepciones, subtotales brutos)  | gold   | `fact_ventas_contabilidad`  |
| Datos de cliente / artículo / ruta sin agregar           | silver | `clients`, `articles`, `routes` |
| Listas de precios y vigencias                            | silver | `price_lists`, `price_list_items` |
| Asignación cliente ↔ ruta histórica                      | silver | `client_forces`             |

> **Regla simple**: gold para analytics; silver solo si gold no tiene la columna
> que necesitás (típicamente: impuestos, vigencias de precios, asignaciones de
> ruta con fechas).

---

## 5. Catálogo de tablas — GOLD

### 5.1 Dimensiones

#### `gold.dim_tiempo`
PK: `fecha`. SCD Type 0.
```
fecha (DATE) | dia | dia_semana | nombre_dia | semana | mes | nombre_mes | trimestre | anio
```

#### `gold.dim_sucursal`
PK: `id_sucursal`.
```
id_sucursal | descripcion
```

#### `gold.dim_deposito`
PK: `id_deposito`. Tiene jerarquía a sucursal denormalizada.
```
id_deposito | descripcion | id_sucursal | des_sucursal
```

#### `gold.dim_vendedor`
**PK compuesta: `(id_vendedor, id_sucursal)`**.
```
id_vendedor | des_vendedor | id_fuerza_ventas | id_sucursal | des_sucursal | supervisor
```

#### `gold.dim_articulo`
PK: `id_articulo`.
```
id_articulo | des_articulo | marca | generico | calibre | proveedor
            | unidad_negocio | factor_hectolitros
```

#### `gold.dim_cliente`
PK: `id_cliente`. Denormaliza ruta + preventista actual de FV1 y FV4 + marketing.
```
id_cliente | razon_social | fantasia | id_sucursal | des_sucursal
           | id_canal_mkt | des_canal_mkt | id_segmento_mkt | des_segmento_mkt
           | id_subcanal_mkt | des_subcanal_mkt
           | id_ruta_fv1 | des_personal_fv1 | id_personal_fv1
           | id_ruta_fv4 | des_personal_fv4 | id_personal_fv4
           | id_ramo | des_ramo | id_localidad | des_localidad
           | id_provincia | des_provincia | latitud | longitud
           | id_lista_precio | des_lista_precio
           | telefono_fijo | telefono_movil | anulado
```

> `id_personal_fv1/fv4` apuntan a `dim_vendedor.id_vendedor` con el mismo
> `id_sucursal` que el cliente.

### 5.2 Hechos

#### `gold.fact_ventas` — grain: línea de comprobante

Columnas clave:
```
id_cliente | id_articulo | id_vendedor | id_sucursal
fecha_comprobante | fecha_pedido
id_documento (VARCHAR) | letra | serie | nro_doc | anulado

cantidades_con_cargo | cantidades_sin_cargo | cantidades_total
precio_unitario_bruto | bonificacion (%)
subtotal_neto | subtotal_final
facturacion_neta = cantidades_total * ABS(precio_unitario_bruto)
descuentos      = facturacion_neta * (bonificacion / 100)
cantidad_total_htls (calculado contra dim_articulo.factor_hectolitros)
```

Índices: `fecha_comprobante`, `id_cliente`, `id_articulo`, `id_vendedor`,
`id_sucursal`. Filtros por fecha son muy rápidos.

#### `gold.fact_ventas_contabilidad` — grain: línea de comprobante (data mart contable)

Igual grano que `fact_ventas` pero con TODOS los campos del silver: impuestos
(IVA21/27/105/2, internos, percepciones IIBB, per3337, per212), subtotal_bruto,
subtotal_bonificado, datos de cuenta contable, asiento, plan contable, fechas
adicionales (alta, entrega, vencimiento, caja, anulación, pago, liquidación).

Usar este solo para reportes contables/fiscales. Para BI comercial usar
`fact_ventas`.

#### `gold.fact_stock` — grain: snapshot diario × depósito × artículo
```
date_stock | id_deposito | id_articulo | cant_bultos | cant_unidades | cantidad_total_htls
```
UNIQUE: `(date_stock, id_deposito, id_articulo)`.

#### `gold.fact_comodatos` — grain: equipo prestado vigente
```
comprobante | desc_comprobante | id_sucursal | numero | fecha | linea
id_cliente | id_articulo | unidad_negocio | saldo
```
UNIQUE: `(comprobante, id_sucursal, numero, linea)`. Full DELETE + INSERT por
corrida (snapshot del estado vigente).

#### `gold.fact_cupos` — grain: objetivos de venta por proveedor × ruta × mes
```
periodo (VARCHAR 'YYYY-MM') | proveedor | id_sucursal | sucursal
id_ruta | descripcion | preventista | generico | desagregado | cupo
```

#### `gold.fact_cupos_cobertura` — grain: objetivos de cobertura por ruta × mes
```
periodo (VARCHAR 'YYYY-MM') | tipo_apertura ('marca'|'generico')
id_sucursal | sucursal | id_ruta | descripcion_ruta | preventista
categoria | cupo
```

### 5.3 Tablas de cobertura (`cob_*`) — agregaciones mensuales

Grano: `periodo` (DATE, primer día del mes) + dimensiones específicas.
Métricas siempre: `clientes_compradores`, `volumen_total`.

| Tabla                          | Grain                                                                  |
|--------------------------------|------------------------------------------------------------------------|
| `cob_preventista_marca`        | periodo, fuerza_venta, vendedor, ruta, sucursal, **marca**             |
| `cob_preventista_generico`     | periodo, fuerza_venta, vendedor, ruta, sucursal, **generico**          |
| `cob_sucursal_marca`           | periodo, fuerza_venta, sucursal, **marca**                             |
| `cob_sucursal_generico`        | periodo, fuerza_venta, sucursal, **generico**                          |
| `cob_sucursal_aguas`           | periodo, fuerza_venta, sucursal, **subdivision_aguas** (AGUAS DANONE)  |
| `cob_sucursal_lista_marca`     | periodo, fuerza_venta, sucursal, marca, **lista_precio**               |
| `cob_sucursal_lista_generico`  | periodo, fuerza_venta, sucursal, generico, **lista_precio**            |
| `cob_preventista_articulo`     | periodo, fuerza_venta, vendedor, ruta, sucursal, **nombre_grupo**      |
| `cob_sucursal_articulo`        | periodo, fuerza_venta, sucursal, **nombre_grupo**                      |

> **Regla de negocio**: estas tablas usan la asignación ruta/preventista
> **actual** del cliente (`dim_cliente.id_ruta_fv1`, `id_personal_fv1`), NO la
> que estaba en `fact_ventas` al momento de la venta.

---

## 6. Catálogo de tablas — SILVER (solo lo más usado)

### `silver.clients`
PK: `id_cliente`. Atributos crudos del cliente (razón social, fantasia, CUIT,
domicilio, condición fiscal, etc.). Útil cuando `dim_cliente` no tiene el campo.

### `silver.articles`
PK: `id_articulo`. Catálogo completo de artículos (más campos que `dim_articulo`).

### `silver.staff`
UNIQUE: `(id_personal, id_sucursal)`. **Composite key.**

### `silver.routes`
UNIQUE: `(id_ruta, id_sucursal, id_fuerza_ventas)`. **Composite key.**
Solo rutas activas (`fecha_hasta = '9999-12-31'`).

### `silver.client_forces`
UNIQUE: `(id_cliente, id_ruta, fecha_inicio)`. **NO tiene `id_sucursal`** — la
sucursal viene implícita por la ruta (JOIN con `silver.routes`).

### `silver.price_lists` + `silver.price_list_items` + `silver.price_list_sucursales`
- `price_lists`: header (id_lista, id_vigencia, fechas, vigente boolean)
- `price_list_items`: detalle por artículo (precio, precio_final, IVA, internos,
  bonificación, márgenes, segmentación por marca/proveedor)
- `price_list_sucursales`: bridge N-N lista ↔ sucursal

> Para "precio actual de un artículo en una sucursal X":
> ```sql
> FROM silver.price_lists pl
> JOIN silver.price_list_sucursales pls USING (id_lista, id_vigencia)
> JOIN silver.price_list_items pli USING (id_lista, id_vigencia)
> WHERE pl.vigente = true
>   AND pls.id_sucursal = :id_sucursal
>   AND pli.id_articulo = :id_articulo
>   AND pli.anulado = false;
> ```

### `silver.hectolitros`
UNIQUE: `(id_articulo)`. Si necesitás el factor crudo (en gold ya está en `dim_articulo`).

### `silver.fact_ventas`
Misma grain que gold pero con todos los campos (incluye fechas operativas,
impuestos, percepciones, datos contables, segmentación marketing N3, etc.).
Útil cuando gold no alcanza.

### `silver.fact_stock`
Igual a `gold.fact_stock` pero con `ds_articulo` denormalizado y `fec_vto_lote`.

### `silver.deposits`
UNIQUE: `(id_deposito)`. Mapeo depósito → sucursal.

---

## 7. Recetas de JOIN

### Ventas con todas las dimensiones
```sql
SELECT
    dt.fecha,
    ds.descripcion        AS sucursal,
    dv.des_vendedor       AS vendedor,
    dc.razon_social       AS cliente,
    da.des_articulo       AS articulo,
    da.marca,
    fv.cantidades_total,
    fv.cantidad_total_htls,
    fv.facturacion_neta
FROM gold.fact_ventas fv
JOIN gold.dim_tiempo   dt ON dt.fecha       = fv.fecha_comprobante
JOIN gold.dim_sucursal ds ON ds.id_sucursal = fv.id_sucursal
JOIN gold.dim_vendedor dv ON dv.id_vendedor = fv.id_vendedor
                         AND dv.id_sucursal = fv.id_sucursal   -- ⚠️ COMPOSITE
JOIN gold.dim_cliente  dc ON dc.id_cliente  = fv.id_cliente
JOIN gold.dim_articulo da ON da.id_articulo = fv.id_articulo
WHERE fv.fecha_comprobante BETWEEN :desde AND :hasta;
```

### Cobertura: clientes que compraron marca X en un mes
```sql
SELECT
    cob.periodo,
    ds.descripcion AS sucursal,
    cob.marca,
    cob.clientes_compradores,
    cob.volumen_total
FROM gold.cob_sucursal_marca cob
JOIN gold.dim_sucursal ds ON ds.id_sucursal = cob.id_sucursal
WHERE cob.periodo = DATE '2026-05-01'
  AND cob.marca = 'SCHNEIDER'
ORDER BY cob.volumen_total DESC;
```

### Cumplimiento de cupo (real vs objetivo) por ruta
```sql
WITH real AS (
    SELECT
        TO_CHAR(fv.fecha_comprobante, 'YYYY-MM') AS periodo,
        dc.id_ruta_fv1 AS id_ruta,
        fv.id_sucursal,
        da.generico,
        SUM(fv.cantidad_total_htls) AS htls_reales
    FROM gold.fact_ventas fv
    JOIN gold.dim_cliente  dc ON dc.id_cliente  = fv.id_cliente
    JOIN gold.dim_articulo da ON da.id_articulo = fv.id_articulo
    WHERE fv.fecha_comprobante >= DATE '2026-05-01'
      AND fv.fecha_comprobante <  DATE '2026-06-01'
    GROUP BY 1, 2, 3, 4
)
SELECT
    fc.periodo,
    fc.sucursal,
    fc.id_ruta,
    fc.generico,
    fc.cupo                                      AS objetivo,
    COALESCE(real.htls_reales, 0)                AS real,
    ROUND(100.0 * COALESCE(real.htls_reales,0) / NULLIF(fc.cupo, 0), 2) AS cumplimiento_pct
FROM gold.fact_cupos fc
LEFT JOIN real
       ON real.periodo     = fc.periodo
      AND real.id_ruta     = fc.id_ruta
      AND real.id_sucursal = fc.id_sucursal
      AND real.generico    = fc.generico
WHERE fc.periodo = '2026-05';
```

### Stock actual por sucursal de un artículo
```sql
SELECT
    ds.descripcion AS sucursal,
    SUM(fs.cant_unidades)        AS unidades,
    SUM(fs.cantidad_total_htls)  AS htls
FROM gold.fact_stock fs
JOIN gold.dim_deposito dd ON dd.id_deposito = fs.id_deposito
JOIN gold.dim_sucursal ds ON ds.id_sucursal = dd.id_sucursal
WHERE fs.date_stock  = (SELECT MAX(date_stock) FROM gold.fact_stock)
  AND fs.id_articulo = :id_articulo
GROUP BY ds.descripcion
ORDER BY 1;
```

### Comodatos vigentes por cliente
```sql
SELECT
    dc.razon_social,
    da.des_articulo,
    fc.unidad_negocio,
    fc.saldo
FROM gold.fact_comodatos fc
JOIN gold.dim_cliente  dc ON dc.id_cliente  = fc.id_cliente
JOIN gold.dim_articulo da ON da.id_articulo = fc.id_articulo
WHERE fc.saldo > 0
ORDER BY dc.razon_social, da.des_articulo;
```

---

## 8. Glosario de negocio

| Término          | Significado                                                                           |
|------------------|---------------------------------------------------------------------------------------|
| **FV / Fuerza de venta** | Canal comercial. `FV1` = preventa principal. `FV4` = canal alternativo (típicamente). `dim_cliente` denormaliza ambas asignaciones. |
| **Preventista**  | Vendedor asignado a una ruta (`dim_vendedor` filtrado por `id_fuerza_ventas`).        |
| **Ruta**         | Agrupación geográfica/comercial de clientes. Única por `(id_ruta, id_sucursal, id_fuerza_ventas)`. |
| **Sucursal**     | Filial física/operativa. CASA CENTRAL = id 1. Importante: **VALLE SALTA es sub-zona de CASA CENTRAL**, no sucursal aparte. |
| **Genérico**     | Categoría de producto (ej. "CERVEZAS", "AGUAS DANONE", "VINOS").                      |
| **Marca**        | SCHNEIDER, SALTA, HEINEKEN, etc.                                                      |
| **Hectolitros**  | Unidad volumétrica. `cantidades_total * factor_hectolitros` ya viene calculado.       |
| **Cobertura**    | Cantidad de clientes que compraron al menos 1 unidad de una marca/genérico en el mes. |
| **Cupo**         | Objetivo comercial (mensual, por ruta o por sucursal). Por proveedor (CCU, BRANCA).   |
| **Comodato**     | Equipo (heladera, dispenser) prestado a un cliente. Saldo = cantidad en su poder.     |
| **Anulado**      | Comprobante invalidado. Convención: **NO filtrarlo** salvo necesidad explícita.       |
| **CCU**          | Proveedor mayorista. Sus marcas: SCHNEIDER, SALTA, HEINEKEN, AMSTEL, etc.             |
| **BRANCA**       | Otro proveedor (Fratelli Branca). Sus marcas: FERNET BRANCA, etc.                     |

---

## 9. Anti-patterns (no hacer esto)

| ❌ Mal                                                          | ✅ Bien                                                                |
|----------------------------------------------------------------|-----------------------------------------------------------------------|
| `JOIN dim_vendedor USING(id_vendedor)`                         | `JOIN dim_vendedor ON id_vendedor AND id_sucursal`                    |
| `WHERE anulado = false` por defecto                            | No filtrar `anulado` salvo que el reporte lo pida                     |
| `cantidades_total * factor_hectolitros` manual                 | Usar `cantidad_total_htls` que ya viene calculado                     |
| `COUNT(DISTINCT id_documento)`                                 | `COUNT(DISTINCT (id_documento, serie, nro_doc, id_sucursal))`         |
| Calcular cobertura ad-hoc desde `fact_ventas`                  | Usar `cob_*` (ya agregada, mucho más rápida)                          |
| Asumir que `id_vendedor` en `fact_ventas` = vendedor del cliente | Son distintos: uno facturó, otro tiene asignada la cartera          |
| Joinear `dim_cliente.id_personal_fv1` sin `id_sucursal`        | Siempre llevar `id_sucursal` al join contra `dim_vendedor`            |
| Filtrar artículos sin htls con `factor_hectolitros = 0`        | Usar `IS NULL` (puede ser NULL, no 0)                                 |

---

## 10. Performance tips

- Filtrá siempre por `fecha_comprobante` (rango) en `fact_ventas`. Hay índice.
- Las tablas `cob_*` ya están agregadas — usá esas en lugar de re-agregar desde
  `fact_ventas` cuando el grano coincida.
- `fact_ventas_contabilidad` es más pesada que `fact_ventas`. Usar solo para
  reportes contables.
- `fact_stock`: pedir siempre un `date_stock` específico (o MAX) — sin filtro
  podés traer cientos de snapshots.
- Para "último estado conocido" en `fact_stock` usar:
  `WHERE date_stock = (SELECT MAX(date_stock) FROM gold.fact_stock)`.

---

## 11. Cosas que NO están en gold (van a silver)

- Impuestos detallados (IVA21/27/105/2, internos, percepciones IIBB) → `gold.fact_ventas_contabilidad` o `silver.fact_ventas`
- Vigencias de listas de precios con fechas → `silver.price_lists` + `silver.price_list_items`
- Asignaciones históricas cliente↔ruta con `fecha_inicio`/`fecha_fin` → `silver.client_forces`
- Lotes y vencimientos de stock → `silver.fact_stock.fec_vto_lote`
- Domicilio / CUIT / contacto detallado del cliente → `silver.clients`
- Segmentación marketing nivel 3 (subcanal) detallada → `silver.marketing_subchannels` + bridge

---

## 12. Multi-tenant (importante si te conectás a varias empresas)

Cada empresa tiene su propio Postgres / propia DB. **El schema es idéntico**
entre empresas. Si un proyecto consumer atiende varias empresas, abrí una
conexión por empresa (distinto `DB_HOST` o `DB_NAME`) y no mezcles datos.

Una empresa puede no tener cargada una feature (cupos, hectolitros, comodatos,
listas de precios). En ese caso las tablas correspondientes existen pero están
**vacías**. Diseñar el consumer para tolerar tablas vacías sin crashear.
