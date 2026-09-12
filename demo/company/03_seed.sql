-- Deterministic seed data. No random(), no setseed(): every value is derived
-- by modular arithmetic from a generate_series index, so two runs on two
-- machines produce byte-identical rows and a test can assert exact figures.
--
-- The only thing that moves is the clock: invoices are dated backwards from
-- `now()` so the trailing-window reports have something to find.
--
-- `importe_total` is computed FROM the lines rather than invented, so the
-- header total and the line totals reconcile. A demo whose own numbers do not
-- add up teaches the agent's reader to distrust the right answers.

TRUNCATE factura_lineas, facturas, existencias, articulos, padron_clientes RESTART IDENTITY CASCADE;

-- 20 articles, list price 1137 .. 3740
INSERT INTO articulos (codigo_articulo, detalle, precio_lista)
SELECT
    'ART-' || lpad(i::text, 3, '0'),
    (ARRAY[
        'Gaseosa cola 2.25L', 'Gaseosa lima 2.25L', 'Agua sin gas 2L',
        'Agua con gas 2L', 'Cerveza rubia lata 473ml', 'Cerveza negra lata 473ml',
        'Vino tinto 750ml', 'Vino blanco 750ml', 'Fernet 750ml',
        'Aperitivo 750ml', 'Jugo naranja 1.5L', 'Jugo manzana 1.5L',
        'Isotonica 500ml', 'Energizante 269ml', 'Soda sifon 1.5L',
        'Tonica 1.5L', 'Cidra 720ml', 'Espumante 750ml',
        'Amargo serrano 1L', 'Licor de crema 700ml'
    ])[i],
    1000 + (i * 137)
FROM generate_series(1, 20) AS i;

-- 12 customers across 4 zones and 3 segments
INSERT INTO padron_clientes (nro_cliente, razon_social, localidad, rubro, alta)
SELECT
    i,
    'Cliente ' || lpad(i::text, 2, '0'),
    (ARRAY['Norte', 'Sur', 'Oeste', 'Centro'])[1 + (i % 4)],
    (ARRAY['Almacen', 'Kiosco', 'Supermercado'])[1 + (i % 3)],
    current_date - ((i * 90) || ' days')::interval
FROM generate_series(1, 12) AS i;

-- 360 invoices, one every 1.5 days back from now (~18 months of history).
-- Status mix: every 10th cancelled, every 5th (not already cancelled)
-- pending, the rest invoiced. importe_total is filled in below.
INSERT INTO facturas (nro_factura, nro_cliente, fecha_emision, estado, importe_total)
SELECT
    i,
    1 + (i % 12),
    now() - ((i * 36) || ' hours')::interval,
    CASE
        WHEN i % 10 = 0 THEN 'anulada'
        WHEN i % 5 = 0  THEN 'pendiente'
        ELSE 'facturada'
    END,
    0
FROM generate_series(1, 360) AS i;

-- 2 to 5 lines per invoice
INSERT INTO factura_lineas (nro_factura, codigo_articulo, cantidad, precio_unitario)
SELECT
    src.nro_factura,
    src.codigo_articulo,
    src.cantidad,
    a.precio_lista
FROM (
    SELECT
        f.nro_factura,
        'ART-' || lpad(((((f.nro_factura * 7) + (j * 3)) % 20) + 1)::text, 3, '0')
            AS codigo_articulo,
        1 + ((f.nro_factura + j) % 5) AS cantidad
    FROM facturas f
    CROSS JOIN LATERAL generate_series(1, 2 + (f.nro_factura % 4)) AS j
) src
JOIN articulos a ON a.codigo_articulo = src.codigo_articulo;

-- Header total derived from the lines, never invented.
UPDATE facturas f
SET importe_total = sub.total
FROM (
    SELECT nro_factura, SUM(cantidad * precio_unitario) AS total
    FROM factura_lineas
    GROUP BY nro_factura
) sub
WHERE sub.nro_factura = f.nro_factura;

-- Stock: reorder point 20 for everything, on-hand cycles 0..59 so roughly a
-- third of the catalogue sits at or under the reorder point and `low_stock`
-- has real rows to return.
INSERT INTO existencias (codigo_articulo, cantidad_disponible, punto_pedido)
SELECT
    'ART-' || lpad(i::text, 3, '0'),
    (i * 13) % 60,
    20
FROM generate_series(1, 20) AS i;
