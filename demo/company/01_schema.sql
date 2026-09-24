-- A fake distributor, deliberately NOT shaped like this repository's own
-- `orders` / `order_items` / `clients` tables.
--
-- The point of this schema is to be plausibly different in the three ways a
-- real client's database differs, so that `connectors/sales_reports.py`
-- running against it unchanged actually proves something:
--
--   1. NAMES      — Spanish tables and columns throughout.
--   2. VOCABULARY — statuses are 'facturada' / 'pendiente' / 'anulada',
--                   not 'confirmed' / 'pending' / 'cancelled'.
--   3. DERIVATION — a line has `cantidad` and `precio_unitario` but NO line
--                   total, so the contract's `amount` has to be computed in
--                   the view rather than renamed.
--
-- `02_views.sql` absorbs all three. Nothing in the platform's SQL changes.

-- The loader's signature, and the FIRST thing created.
--
-- `demo/load_demo_company.py` refuses to touch a database that holds objects
-- but not this table. Identifying the target by marker rather than by the table
-- names below is deliberate: those names imitate a real Argentine distributor's
-- ERP, so "the tables look like the demo's" is exactly the evidence a real
-- company's database would also produce, moments before this file dropped it.
--
-- Created before the DROPs so that an interrupted first load still leaves the
-- database marked as the loader's own, and the retry is allowed through.
CREATE TABLE IF NOT EXISTS agents_system_demo_marker (
    note       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Cleared first: the loader is meant to be re-run, and an INSERT alone would
-- stack one row per load, making the marker a growing log instead of a fact.
DELETE FROM agents_system_demo_marker;

INSERT INTO agents_system_demo_marker (note)
VALUES ('Created by demo/load_demo_company.py. This database is disposable.');

DROP VIEW IF EXISTS agents_system_stock;
DROP VIEW IF EXISTS agents_system_sale_items;
DROP VIEW IF EXISTS agents_system_sales;
DROP VIEW IF EXISTS agents_system_customers;

DROP TABLE IF EXISTS factura_lineas;
DROP TABLE IF EXISTS facturas;
DROP TABLE IF EXISTS existencias;
DROP TABLE IF EXISTS articulos;
DROP TABLE IF EXISTS padron_clientes;


CREATE TABLE padron_clientes (
    nro_cliente   integer PRIMARY KEY,
    razon_social  text        NOT NULL,
    localidad     text        NOT NULL,   -- the contract calls this "zone"
    rubro         text        NOT NULL,   -- the contract calls this "segment"
    alta          date        NOT NULL
);

CREATE TABLE articulos (
    codigo_articulo  text PRIMARY KEY,
    detalle          text           NOT NULL,
    precio_lista     numeric(12, 2) NOT NULL
);

CREATE TABLE facturas (
    nro_factura     integer PRIMARY KEY,
    nro_cliente     integer     NOT NULL REFERENCES padron_clientes (nro_cliente),
    fecha_emision   timestamptz NOT NULL,   -- the contract calls this "sold_at"
    estado          text        NOT NULL,   -- 'facturada' | 'pendiente' | 'anulada'
    importe_total   numeric(12, 2) NOT NULL
);

CREATE TABLE factura_lineas (
    id               serial PRIMARY KEY,
    nro_factura      integer NOT NULL REFERENCES facturas (nro_factura),
    codigo_articulo  text    NOT NULL REFERENCES articulos (codigo_articulo),
    cantidad         integer NOT NULL,
    precio_unitario  numeric(12, 2) NOT NULL
    -- No line total column. On purpose.
);

CREATE TABLE existencias (
    codigo_articulo     text PRIMARY KEY REFERENCES articulos (codigo_articulo),
    cantidad_disponible integer     NOT NULL,
    punto_pedido        integer     NOT NULL,
    actualizado         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON facturas (fecha_emision);
CREATE INDEX ON facturas (nro_cliente);
CREATE INDEX ON factura_lineas (nro_factura);
