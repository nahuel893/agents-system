# Contexto de Consulta: dim_articulo & dim_cliente

**Schema:** `gold` | **Base:** medallion ETL | **SCD:** Type 1 (overwrite)

---

## gold.dim_articulo

Dimensión de artículos con atributos desnormalizados. PK: `id_articulo`.

| Columna | Tipo | Descripcion |
|---------|------|-------------|
| `id_articulo` | INTEGER PK | ID unico del articulo |
| `des_articulo` | VARCHAR(200) | Nombre del articulo |
| `marca` | VARCHAR(150) | Ej: QUILMES, BRAHMA, STELLA ARTOIS |
| `generico` | VARCHAR(150) | Agrupacion generica. Ej: CERVEZAS, AGUAS DANONE, GASEOSAS |
| `calibre` | VARCHAR(150) | Formato/tamanio. Ej: LATA 473, RETORNABLE 1000 |
| `proveedor` | VARCHAR(150) | Ej: CCU, BRANCA |
| `unidad_negocio` | VARCHAR(150) | Ej: CERVEZA, NO ALCOHOL |
| `factor_hectolitros` | NUMERIC(12,8) | Factor de conversion a hectolitros |

**Indices:** marca, proveedor

### Origen de datos

```
silver.articles (maestro)
  + silver.article_groupings (pivot MARCA/GENERICO/CALIBRE/PROVEED/UNIDAD DE NEGOCIO)
  + silver.hectolitros (factor htls)
  → gold.dim_articulo (desnormalizado via MAX CASE WHEN)
```

### Queries utiles

```sql
-- Articulos por marca
SELECT marca, COUNT(*) as qty
FROM gold.dim_articulo
GROUP BY marca ORDER BY qty DESC;

-- Articulos por generico y marca
SELECT generico, marca, COUNT(*) as qty
FROM gold.dim_articulo
GROUP BY generico, marca ORDER BY generico, qty DESC;

-- Buscar articulo por nombre
SELECT id_articulo, des_articulo, marca, generico, calibre
FROM gold.dim_articulo
WHERE des_articulo ILIKE '%quilmes%';

-- Articulos con factor de hectolitros
SELECT id_articulo, des_articulo, factor_hectolitros
FROM gold.dim_articulo
WHERE factor_hectolitros IS NOT NULL
ORDER BY factor_hectolitros DESC;

-- Articulos sin clasificar (falta alguna agrupacion)
SELECT id_articulo, des_articulo, marca, generico, calibre, proveedor
FROM gold.dim_articulo
WHERE marca IS NULL OR generico IS NULL;
```

---

## gold.dim_cliente

Dimension de clientes desnormalizada. PK: `id_cliente`. Incluye sucursal, marketing, rutas/preventistas por fuerza de venta, clasificacion y geolocalizacion.

| Columna | Tipo | Descripcion |
|---------|------|-------------|
| **Identificacion** | | |
| `id_cliente` | INTEGER PK | ID unico del cliente |
| `razon_social` | VARCHAR(150) | Razon social legal |
| `fantasia` | VARCHAR(150) | Nombre de fantasia |
| **Sucursal** | | |
| `id_sucursal` | INTEGER | Sucursal asignada |
| `des_sucursal` | VARCHAR(100) | Nombre de la sucursal |
| **Marketing** | | |
| `id_canal_mkt` | INTEGER | Canal de marketing |
| `des_canal_mkt` | VARCHAR(100) | Ej: AUTOSERVICIO, ALMACEN, KIOSCO |
| `id_segmento_mkt` | INTEGER | Segmento de marketing |
| `des_segmento_mkt` | VARCHAR(100) | Ej: TRADICIONAL, MODERNO |
| `id_subcanal_mkt` | INTEGER | Subcanal de marketing |
| `des_subcanal_mkt` | VARCHAR(100) | Subdivision del canal |
| **Ruta FV1 (Fuerza Ventas 1)** | | |
| `id_ruta_fv1` | INTEGER | Ruta asignada en FV1 |
| `id_personal_fv1` | INTEGER | ID del preventista FV1 |
| `des_personal_fv1` | VARCHAR(150) | Nombre del preventista FV1 |
| **Ruta FV4 (Fuerza Ventas 4)** | | |
| `id_ruta_fv4` | INTEGER | Ruta asignada en FV4 |
| `id_personal_fv4` | INTEGER | ID del preventista FV4 |
| `des_personal_fv4` | VARCHAR(150) | Nombre del preventista FV4 |
| **Clasificacion** | | |
| `id_ramo` | INTEGER | Ramo comercial |
| `des_ramo` | VARCHAR(100) | Ej: ALMACEN, BAR, RESTAURANT |
| `id_localidad` | INTEGER | Localidad |
| `des_localidad` | VARCHAR(100) | Nombre localidad |
| `id_provincia` | VARCHAR(10) | Codigo provincia |
| `des_provincia` | VARCHAR(100) | Nombre provincia |
| **Geolocalizacion** | | |
| `latitud` | NUMERIC(15,6) | Latitud GPS |
| `longitud` | NUMERIC(15,6) | Longitud GPS |
| **Comercial** | | |
| `id_lista_precio` | INTEGER | Lista de precio asignada |
| `des_lista_precio` | VARCHAR(100) | Nombre lista de precio |
| `telefono_fijo` | VARCHAR(50) | Telefono fijo |
| `telefono_movil` | VARCHAR(50) | Telefono movil |
| `anulado` | BOOLEAN | Cliente dado de baja (default false) |

**Indices:** id_sucursal, id_canal_mkt, id_segmento_mkt

### Origen de datos

```
silver.clients (maestro)
  + silver.branches (sucursal)
  + silver.marketing_channels / segments / subchannels
  + silver.client_forces + silver.routes + silver.staff (rutas/preventistas FV1 y FV4)
  → gold.dim_cliente (desnormalizado via CTEs rutas_fv1 + rutas_fv4)
```

### Regla critica: dos vendedores distintos

| Fuente | Significado |
|--------|-------------|
| `fact_ventas.id_vendedor` → `dim_vendedor` | Vendedor que **facturo** la venta (verdad transaccional) |
| `dim_cliente.id_personal_fv1/fv4` | Vendedor **actualmente asignado** al cliente (asignacion dimensional) |

Pueden diferir: un cliente puede comprar a un vendedor de otra ruta (cross-selling, urgencia, etc.).

- **Usar `dim_vendedor`** cuando: analisis de lo que vendio cada vendedor, estructura comercial
- **Usar `dim_cliente.id_personal_fv1/fv4`** cuando: cobertura, cartera de clientes por preventista

### Queries utiles

```sql
-- Clientes por sucursal
SELECT des_sucursal, COUNT(*) as qty
FROM gold.dim_cliente
WHERE anulado = false
GROUP BY des_sucursal ORDER BY qty DESC;

-- Clientes por canal de marketing
SELECT des_canal_mkt, COUNT(*) as qty
FROM gold.dim_cliente
WHERE anulado = false
GROUP BY des_canal_mkt ORDER BY qty DESC;

-- Cartera de un preventista FV1
SELECT id_cliente, razon_social, fantasia, des_ramo, des_localidad
FROM gold.dim_cliente
WHERE id_personal_fv1 = 123 AND anulado = false;

-- Clientes por ruta FV1 con nombre de preventista
SELECT id_ruta_fv1, des_personal_fv1, COUNT(*) as clientes
FROM gold.dim_cliente
WHERE anulado = false AND id_ruta_fv1 IS NOT NULL
GROUP BY id_ruta_fv1, des_personal_fv1
ORDER BY clientes DESC;

-- Buscar cliente por nombre
SELECT id_cliente, razon_social, fantasia, des_sucursal, des_canal_mkt
FROM gold.dim_cliente
WHERE razon_social ILIKE '%supermercado%' OR fantasia ILIKE '%supermercado%';

-- Clientes sin ruta asignada en FV1
SELECT id_cliente, razon_social, des_sucursal
FROM gold.dim_cliente
WHERE id_ruta_fv1 IS NULL AND anulado = false;

-- Clientes con geolocalizacion
SELECT id_cliente, fantasia, latitud, longitud
FROM gold.dim_cliente
WHERE latitud IS NOT NULL AND longitud IS NOT NULL AND anulado = false;
```

---

## JOINs con fact tables

```sql
-- Ventas por marca y canal de cliente
SELECT da.marca, dc.des_canal_mkt, SUM(fv.cantidades_total) as bultos
FROM gold.fact_ventas fv
JOIN gold.dim_articulo da ON fv.id_articulo = da.id_articulo
JOIN gold.dim_cliente dc ON fv.id_cliente = dc.id_cliente
WHERE fv.fecha_comprobante >= '2026-01-01'
GROUP BY da.marca, dc.des_canal_mkt
ORDER BY bultos DESC;

-- Stock por generico y deposito
SELECT da.generico, fs.id_deposito, SUM(fs.cant_bultos) as bultos
FROM gold.fact_stock fs
JOIN gold.dim_articulo da ON fs.id_articulo = da.id_articulo
WHERE fs.date_stock = CURRENT_DATE
GROUP BY da.generico, fs.id_deposito
ORDER BY bultos DESC;

-- Ventas en hectolitros por proveedor
SELECT da.proveedor, SUM(fv.cantidad_total_htls) as htls
FROM gold.fact_ventas fv
JOIN gold.dim_articulo da ON fv.id_articulo = da.id_articulo
WHERE fv.fecha_comprobante BETWEEN '2026-01-01' AND '2026-03-31'
GROUP BY da.proveedor ORDER BY htls DESC;
```

### Regla de composite keys

Los IDs son unicos **por sucursal**, no globalmente. Todo JOIN con `dim_vendedor` DEBE incluir `id_sucursal`:

```sql
-- CORRECTO
JOIN gold.dim_vendedor dv ON fv.id_vendedor = dv.id_vendedor AND fv.id_sucursal = dv.id_sucursal

-- INCORRECTO (mezcla datos entre sucursales)
JOIN gold.dim_vendedor dv ON fv.id_vendedor = dv.id_vendedor
```

Esto NO aplica a `dim_articulo` ni `dim_cliente` (sus PKs son globalmente unicas).
