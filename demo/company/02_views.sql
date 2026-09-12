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
-- disclosure is downstream of it.
--
-- The ELSE is the interesting line. It used to say 'cancelled', on the
-- reasoning that excluding an unrecognized status from revenue is the
-- conservative choice. That is only half a decision, and the other half is a
-- fabrication: `status_summary` does NOT filter by status, so a new source word
-- like 'en_proceso' came back counted as a CANCELLATION that no invoice ever
-- recorded. The agent would then report a cancellation figure with total
-- confidence and no row behind it.
--
-- So unrecognized values map to 'unknown' — `sales_reports.UNMAPPED_STATUS`,
-- reserved and outside the canonical vocabulary. Both halves stay honest: every
-- status-filtered report still excludes these rows (they match none of the
-- three), and `status_summary` shows them under their own name, so whoever can
-- fix this view can see that it needs fixing. `demo/load_demo_company.py`
-- additionally fails the load when any row lands here, so a new status word is
-- caught at load time rather than in a report months later.
CREATE VIEW agentsys_sales AS
SELECT
    nro_factura   AS sale_id,
    fecha_emision AS sold_at,
    nro_cliente   AS customer_id,
    CASE estado
        WHEN 'facturada' THEN 'confirmed'
        WHEN 'pendiente' THEN 'pending'
        WHEN 'anulada'   THEN 'cancelled'
        ELSE 'unknown'
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
