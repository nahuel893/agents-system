-- The contract, satisfied.
--
-- These four views are the ENTIRE integration surface between a company's
-- database and `agentsys.connectors.sales_reports`. Every difference between
-- this fake distributor's schema and the platform's expectations is absorbed
-- here; not one line of the platform's SQL knows this company exists.
--
-- A real deployment writes a file like this once. The column lists must match
-- `sales_reports.CONTRACT_VIEWS` exactly — `demo/verify_contract.py` checks
-- that against the live database rather than trusting this comment.

-- customer_id, name, zone, segment
CREATE VIEW agentsys_customers AS
SELECT
    nro_cliente  AS customer_id,
    razon_social AS name,
    localidad    AS zone,
    rubro        AS segment
FROM padron_clientes;


-- sale_id, sold_at, customer_id, status, amount
--
-- The status mapping is the part that is easy to get wrong and impossible to
-- notice: every report's status filter and every `statuses_included`
-- disclosure is downstream of it. An unmapped value would silently fall out
-- of every filtered report — the rows would not error, they would just be
-- absent — so anything unrecognized is mapped to 'cancelled', the
-- conservative choice: excluded from revenue by default and visible in
-- `status_summary`, rather than quietly inflating sales.
CREATE VIEW agentsys_sales AS
SELECT
    nro_factura   AS sale_id,
    fecha_emision AS sold_at,
    nro_cliente   AS customer_id,
    CASE estado
        WHEN 'facturada' THEN 'confirmed'
        WHEN 'pendiente' THEN 'pending'
        WHEN 'anulada'   THEN 'cancelled'
        ELSE 'cancelled'
    END           AS status,
    importe_total AS amount
FROM facturas;


-- sale_id, sku, description, quantity, amount
--
-- `amount` does not exist in this company's schema: a line stores quantity
-- and unit price. The contract wants the line total, so it is computed here.
-- This is why the contract is views rather than a name-to-name mapping — a
-- mapping table could not express arithmetic.
CREATE VIEW agentsys_sale_items AS
SELECT
    l.nro_factura                        AS sale_id,
    l.codigo_articulo                    AS sku,
    a.detalle                            AS description,
    l.cantidad                           AS quantity,
    (l.cantidad * l.precio_unitario)     AS amount
FROM factura_lineas l
JOIN articulos a ON a.codigo_articulo = l.codigo_articulo;


-- sku, description, on_hand, reorder_point
CREATE VIEW agentsys_stock AS
SELECT
    e.codigo_articulo     AS sku,
    a.detalle             AS description,
    e.cantidad_disponible AS on_hand,
    e.punto_pedido        AS reorder_point
FROM existencias e
JOIN articulos a ON a.codigo_articulo = e.codigo_articulo;
