# ADR-007 — Herramienta SQL de solo lectura

**Estado:** Aceptado · **Fecha:** 2026-09-26 · **Enmienda:** AD-2 (motor de reportes, D-023) · **Issue:** #80

## Resumen

La plataforma incorpora `sql_query`, una herramienta que ejecuta SQL **escrito por el modelo**. Eso es exactamente lo que AD-2 prohíbe, por lo que este ADR enmienda AD-2 para esta única herramienta y traslada el límite de confianza: el SQL escrito por el modelo solo se ejecuta dentro de límites que **aplica el propio PostgreSQL** — un rol dedicado que no puede leer nada salvo una lista permitida de vistas ni escribir absolutamente nada, dentro de una transacción `READ ONLY` con un timeout de sentencia del lado del servidor. Por encima se ubican un guard en la capa de aplicación, un permiso T2 dedicado y textos de error fijos, y cada capa está pensada para sostenerse por sí sola. AD-2 sigue siendo absoluto para todo lo demás.

## Contexto

**Dónde vive AD-2.** AD-2 está enunciado en el docstring del módulo `src/agents_system/services/reports.py` (el motor de reportes D-023): la plataforma nunca permite que quien llama — humano, modelo u otro — construya texto SQL; el SQL de cada reporte es texto fijo a nivel de módulo con parámetros enlazados, validados antes de que nada llegue a la base de datos, y ni siquiera un nombre de tabla o columna se arma por plantilla. La herramienta `run_report` (`connectors/report_connector.py`) era la única vía de consulta de los agentes, sobre un catálogo cerrado de siete reportes, con el rol `bi_readonly` como barrera que se sostiene si fallan tanto la validación como el interceptor de Capa 2.

**Por qué cambiarlo.** El plan de pruebas en vivo (`docs/delivery_es/live-test-plan.md`) necesita preguntas ad hoc de solo lectura que el catálogo de reportes no puede responder, y el 2026-09-25 el responsable decidió construirlo como funcionalidad real: rol de base de datos dedicado de solo lectura, solo vistas permitidas, límite de filas y timeout de sentencia, permiso propio, sin DDL/DML, una única sentencia, resultados acotados y tier recomendado T2.

**Numeración.** ADR-004 corresponde a library-first agents (`openspec/changes/library-first-agents/`), ADR-005 a la hoja de ruta de seguridad operativa, y el plan de pruebas en vivo reserva tentativamente ADR-006 para la delegación. Este es ADR-007.

## Decisión

### La enmienda a AD-2

- **AD-2 no cambia para `services/reports.py`** ni para ninguna sentencia que escriba la propia plataforma — incluidas las sentencias de preparación y de catálogo de esta herramienta (`SET TRANSACTION READ ONLY`, `set_config(...)` con valores enlazados, las consultas de verificación del rol), que son texto estático.
- **Una excepción:** la herramienta `sql_query` ejecuta texto SQL escrito por el modelo.
- **El nuevo límite de confianza:** el SQL escrito por el modelo solo se ejecuta dentro de límites que aplica la base de datos. La aplicación puede rechazar más, pero nunca es lo que vuelve imposible una escritura.

**Por qué el límite es la base de datos y no la aplicación.** La aplicación ve texto; la base de datos ejecuta objetos. Un chequeo de privilegios dentro de PostgreSQL se evalúa sobre la relación, la función y el operador que el servidor efectivamente resolvió, después de expandir vistas, buscar en `search_path` y aplicar cada reescritura — por lo que ningún desacuerdo entre parsers, tipo de nodo omitido ni bug del guard puede convertir una lectura en una escritura. Inspeccionar texto influido por un atacante es, en esencia, una lista de lo prohibido, y esas listas envejecen mal; el conjunto de privilegios de un rol es una lista de lo permitido, aplicada por el componente que ejecuta la sentencia. Además, el rol se puede verificar: la herramienta le pregunta al servidor qué puede hacer su rol en cada llamada y se niega a ejecutar cuando la respuesta no es segura. La capa de aplicación se mantiene porque falla temprano, barato y con un mensaje útil, y porque bloquea lo que los privilegios no pueden expresar (esperas, locks consultivos, joins desbocados); es la segunda línea, no la primera.

### Tres capas, cada una pensada para sostenerse sola

| Capa | Mecanismo | Qué detiene |
|---|---|---|
| **1. Base de datos (el límite)** | Rol `sql_readonly` creado por `scripts/provision_sql_readonly.sql`: solo `LOGIN` (sin superusuario, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS` ni pertenencia a otros roles), `default_transaction_read_only = on`, timeouts del lado del servidor, todos los privilegios revocados y `SELECT` otorgado exactamente sobre las vistas permitidas. En cada llamada, dentro de la misma transacción y antes de la consulta del modelo: `SET TRANSACTION READ ONLY`; `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, un `search_path` vacío y `standard_conforming_strings = on`, locales a la transacción; `check_query_role` (solo lectura por defecto, sin atributos elevados, sin pertenencia a ningún otro rol, sin `CREATE` sobre la base de datos, sin privilegio de escritura sobre ninguna relación, secuencia o esquema, sin funciones `SECURITY DEFINER` ejecutables fuera de los esquemas del sistema, nada legible fuera de la lista permitida, cada relación permitida es una vista). Siempre se hace rollback. | Cualquier escritura o DDL (transacción de solo lectura **y** falta de privilegios); lecturas fuera de la lista permitida (falta de privilegios); consultas desbocadas (timeout del servidor); cambios de grants posteriores al arranque (chequeo en cada llamada). |
| **2. Guard de aplicación** (`services/sql_guard.py`) | Se parsea con el dialecto PostgreSQL de sqlglot, nunca por coincidencia de texto. Exactamente una sentencia; la raíz debe ser `SELECT` o una operación de conjuntos de `SELECT`s; ningún DML, DDL, `SELECT … INTO`, `FOR UPDATE/SHARE`, CTE que modifique datos ni sentencia de control en ninguna parte del árbol. Cada relación se resuelve con las propias reglas de PostgreSQL (sin comillas pasa a minúsculas, con comillas es exacta, un nombre de CTE solo oculta dentro de su alcance) a un `esquema.vista` permitido. Cada función es un nombre sin calificar de una lista permitida de funciones; sin llamadas calificadas, `OPERATOR(...)`, parámetros enlazados ni casts a tipos no incorporados; solo literales de cadena `'...'` simples. Longitud del texto y tamaño del árbol acotados (10.000 caracteres, 2.500 nodos sintácticos); cada chequeo es una única pasada lineal — los nombres de funciones y los tipos se chequean mientras la sentencia se renderiza una sola vez — y el conector ejecuta el guard en un hilo de trabajo (`asyncio.to_thread`), fuera del event loop. **Lo que se ejecuta es la representación canónica del árbol validado** — relaciones calificadas con su esquema, identificadores entre comillas tal como se resolvieron, comentarios eliminados — envuelta como `SELECT * FROM (…) LIMIT n + 1` y enviada por el protocolo extendido del driver (asyncpg prepara cada sentencia), que rechaza una segunda sentencia de forma independiente. Esa sentencia va a su vez envuelta en una compuerta de bytes: una suma acumulada del tamaño de cada fila (`octet_length` de su forma de texto) en la base de datos, que envía como NULL toda fila que pase `byte_limit`; luego el conector corta las filas JSON en el mismo presupuesto e informa `truncated_bytes`. | Escrituras y DDL antes de llegar al servidor; contrabando de varias sentencias, incluidos comentarios que ocultan una sentencia; relaciones fuera de la lista permitida; funciones con efectos laterales o que evalúan SQL (`pg_sleep`, `set_config`, locks consultivos, `dblink`, `lo_*`, `query_to_xml`); resultados excesivos (nunca se leen más de `n + 1` filas, y nunca se envía una fila que pase `byte_limit` bytes). |
| **3. Superficie de la herramienta** | Permiso propio `query:sql` en una nueva familia de primer nivel `Query`, T2, de modo que la Capa 2 revalida cada llamada y ningún grant `read:*` la cubre. Textos de error fijos elegidos por clase de SQLSTATE; ninguno incluye el error SQL, la excepción del driver ni detalles de conexión. Ningún rol predefinido la equipa. | Ampliación silenciosa de capacidades a través de un grant existente; filtración de datos, esquema o credenciales por el texto de error. |

### Elección del parser

Se consideraron dos parsers reales. `pglast` envuelve libpg_query — la gramática del propio servidor — y eliminaría por completo las diferencias entre parsers, pero su licencia es GPL-3.0-or-later, y depender de él desde esta librería con licencia MIT es una decisión de licenciamiento, no técnica. sqlglot (MIT) parsea bien el dialecto PostgreSQL pero no es la gramática del servidor, por lo que es posible una construcción que lea distinto que PostgreSQL. El diseño cierra esa brecha en lugar de suponer que no existe. El guard nunca reenvía el texto del modelo, solo su representación del árbol que validó, de modo que lo que se ejecuta es lo que se verificó — y esa representación se vuelve a parsear, se vuelve a validar y debe producir exactamente el mismo texto antes de poder ejecutarse. La representación se limita a un subconjunto léxico en el que sqlglot y PostgreSQL coinciden: identificadores entre comillas, cadenas estándar con comillas duplicadas (con `standard_conforming_strings` fijado en on), sin comentarios ni dollar quoting. Las cadenas con escape (`E'...'`) y las demás formas de literal no estándar se rechazan, porque la revisión encontró allí una fuga real: `E'\\'` (una barra invertida) se representaba como `e'\'`, que PostgreSQL lee como una comilla escapada, lo que permitía que un literal de cadena posterior se convirtiera en SQL activo que el guard nunca verificó. La suite de integración ejecuta consultas representativas de ambas formas — a través del guard y como el texto original del modelo — y exige filas idénticas, así que una representación que cambie el significado de una consulta hace fallar CI. Y la capa de base de datos se sostiene de todos modos.

### Tier: T2, `query:sql`, una nueva familia `Query`

- **No es T1.** T1 es una lectura acotada cuya consulta escribió la plataforma (`run_report`). Aquí la escribe el modelo: su alcance es cada fila de cada vista permitida con cualquier forma, más tiempo de servidor. T2 vuelve estructural la revalidación en tiempo de llamada, en lugar del `always_revalidate` opcional, y una familia separada evita que cualquier grant `read:*` la cubra (la cobertura R3 funciona por herencia de clases).
- **R2a / R2b.** Una herramienta T2 que requiere el `query:sql` T2 cumple ambas. Una herramienta T1 que lo requiera incumple R2a (el permiso supera a la herramienta); una herramienta T3 apoyada solo en él incumple R2b (ningún permiso alcanza T3). Ambos casos están fijados por tests.
- **No es T3.** T3 significa ejecución en el host; aquí nada se ejecuta en el host, y cada efecto está acotado por los privilegios de la base de datos. Declararla T3 describiría mal el peligro y tomaría prestada la barrera de R4 por una razón para la que R4 no fue escrita.
- **Consecuencia de R4.** Al ser T2, R4 no impide que un rol `untrusted_input` tenga `query:sql`. Otorgarlo a un rol así (un canal de cara a clientes) permite que cualquiera que hable con el agente dirija consultas sobre cada vista permitida, inyección de prompt incluida. Hágalo solo cuando cada vista permitida sea apta para esa audiencia; de lo contrario, no otorgue `query:sql` a ese rol.

### Modelo de amenazas

| Amenaza | La detiene | Residual |
|---|---|---|
| Se le pide al modelo (o se le inyecta) escribir, eliminar o alterar | El guard la rechaza; si se lo omite, la transacción de solo lectura la rechaza (`25006`) y la falta de privilegios también (`42501`), incluso dentro de una transacción `READ WRITE` explícita | Ninguno detectado; la suite de integración ejecuta cada escritura a través del rol con el guard omitido |
| Segunda sentencia contrabandeada (`; DROP`, comentarios de línea o de bloque anidados) | La regla de sentencia única del guard; comentarios eliminados de la representación; el protocolo extendido rechaza varios comandos; los privilegios | — |
| Diferencia entre parsers: texto que el guard lee de una forma y PostgreSQL de otra (comillas, escapes, comentarios) | Solo se ejecuta la representación del guard, que debe revalidarse hasta un punto fijo; se rechazan los literales no estándar; `standard_conforming_strings` fijado | Una diferencia aún desconocida sigue acotada por la capa de base de datos: una lectura oculta necesita un privilegio que el rol no tiene |
| Lectura de tablas, otras vistas o catálogos | Lista permitida de relaciones; el rol tiene `SELECT` solo sobre las vistas permitidas | `pg_catalog` sigue siendo legible para cualquier rol, como en todo PostgreSQL: se ven **nombres** de objetos y columnas, nunca datos fuera de la lista permitida |
| Funciones con efectos laterales: esperas, configuración, locks consultivos, archivos, objetos grandes, SQL dentro de cadenas | Lista permitida de funciones; `statement_timeout`; las funciones de archivos y de programas del servidor requieren roles que este nunca recibe; las funciones que evalúan SQL corren con los privilegios de este rol | Con el guard omitido, un lock consultivo de sesión sobreviviría a la transacción revertida en una conexión del pool — use un engine dedicado para la herramienta |
| Agotamiento de recursos: joins enormes, ordenamientos, `generate_series` | `statement_timeout` local a la transacción; `LIMIT n + 1` del lado del servidor; el timeout por llamada a herramienta del harness | Cualquier sesión puede cambiar `statement_timeout`; se sostiene frente al modelo porque el guard rechaza `SET` y `set_config`, y el valor por defecto del rol (10 s) actúa como respaldo |
| Texto armado para que la propia validación sea lenta (una cadena larga de `AND`/`OR`) | Chequeos lineales: el nombre de una función se lee del único render de la sentencia, nunca renderizando cada nodo por separado (lo que volvía cuadrática una cadena booleana: 30 s en el límite de longitud, sobre el event loop); un presupuesto de 2.500 nodos sobre el árbol parseado; el guard corre en un hilo de trabajo, así que el event loop y el timeout por llamada del harness conservan el control | Un hilo no se puede matar: tras un timeout el guard igual termina, pero su trabajo está acotado (muy por debajo de un segundo en el límite de longitud) |
| Resultados excesivos dentro del tope de filas: `rpad`, `lpad`, `string_agg`, `array_agg` ponen cientos de MB en un solo valor (hasta 1 GB por valor en PostgreSQL), que la aplicación almacenaba entero y el harness serializaba en un solo mensaje de herramienta | Compuerta de bytes en la base de datos: las filas que pasan `byte_limit` (64 KiB por defecto, 1 MiB como máximo) salen de PostgreSQL como NULL; el conector corta las filas JSON en el mismo presupuesto y lo informa (`truncated_bytes`, una `note` fija) | Medir cada fila es trabajo del servidor: una consulta que arma valores enormes igual le cuesta a la base de datos hasta su `statement_timeout`, nunca memoria a la aplicación |
| Cambios de grants después del arranque (alguien otorga `INSERT`, `SELECT` sobre otra vista, `CREATE` sobre la base de datos o pertenencia a un rol — `pg_execute_server_program`, `pg_read_server_files`, `pg_signal_backend` otorgan capacidades del lado del servidor que ninguna transacción de solo lectura contiene) | `check_query_role` en cada llamada: la herramienta se niega a ejecutar; volver a correr el script de aprovisionamiento elimina el cambio | — |
| Una función propia del despliegue alcanzada con una llamada calificada (`SELECT * FROM esquema.fn(...)`), por ejemplo una función `SECURITY DEFINER` que lee lo que el rol no puede | El guard rechaza las llamadas calificadas con esquema en cualquier posición, `FROM` incluido; el `search_path` vacío mantiene los nombres simples en las funciones incorporadas; `check_query_role` se niega a ejecutar mientras el rol pueda ejecutar alguna función `SECURITY DEFINER` fuera de `pg_catalog` e `information_schema` | Una función `SECURITY INVOKER` corre con los privilegios de este mismo rol y no alcanza nada más. Las funciones son ejecutables por `PUBLIC` por defecto: un despliegue con una función `SECURITY DEFINER` en un esquema que el rol puede usar debe hacer `REVOKE EXECUTE … FROM PUBLIC` sobre ella, o la herramienta se niega a ejecutar |
| Texto de error que filtra datos, esquema o credenciales | Textos fijos por clase de SQLSTATE; para errores de consulta los logs guardan solo el SQLSTATE | — |
| Tablas temporales | Las rechazan la transacción de solo lectura y el guard | El rol conserva el privilegio `TEMPORARY` de PUBLIC; con ambos omitidos podría crear tablas temporales privadas de la sesión, sin tocar datos persistentes. Endurecimiento opcional: `REVOKE TEMPORARY ON DATABASE … FROM PUBLIC` |

El despliegue más fuerte apunta la herramienta a una réplica física de lectura: un hot standby rechaza toda escritura, sin importar la configuración del rol.

### Aprovisionamiento y conexión

1. Cree las vistas que la herramienta puede leer. Solo vistas (o vistas materializadas): la verificación rechaza una tabla base en la lista permitida.
2. Aprovisione el rol como superusuario o como dueño de las vistas; el script es atómico e idempotente, y volver a ejecutarlo corrige desvíos:

   ```bash
   psql "$ADMIN_DATABASE_URL" \
     -v sql_password=change-me \
     -v sql_views=reporting.sales_v,reporting.clients_v \
     -f scripts/provision_sql_readonly.sql
   ```

3. Cree un engine **dedicado** para `sql_readonly` — nunca el engine de lectura-escritura de la aplicación ni la sesión del turno.
4. Configure la herramienta con la misma lista de vistas y regístrela:

   ```python
   from agents_system.connectors.sql_query_connector import (
       SqlQueryConfig,
       build_sql_query_tool_spec,
   )

   config = SqlQueryConfig(
       views={
           "reporting.sales_v": "One row per invoice line: sold_on, product, units, amount.",
           "reporting.clients_v": "One row per client: id, name, region.",
       }
   )
   registry.register(build_sql_query_tool_spec(sql_engine, config))
   ```

5. Al arrancar, `await verify_query_role(sql_engine, config.policy().allowed_relations)` (`services/db_role.py`): `False` significa que el rol no es seguro — no arranque o deje la herramienta sin engine. `None` significa que la base de datos no pudo responder; la herramienta vuelve a verificar en cada llamada de todos modos.
6. Otorgue `query:sql` mediante `DEPLOY_GRANTS` a los roles que declaran `sql_query`.

Valores por defecto: 100 filas por llamada (como máximo el `HARD_ROW_CEILING` de la plataforma, 500), 64 KiB de filas por llamada (`byte_limit`, de 1 KiB a 1 MiB) y un timeout de sentencia de 5 segundos (como máximo 30 s); manténgalo por debajo del timeout por llamada a herramienta del harness.

## Consecuencias

- La forma absoluta de AD-2 tiene ahora una excepción documentada, a cargo de este ADR; `services/reports.py` remite aquí.
- `sqlglot` pasa a ser una dependencia de ejecución.
- El job de CI `bi-readonly` también aprovisiona `sql_readonly` y demuestra, sobre un PostgreSQL real, que el rol rechaza escrituras con el guard de aplicación omitido.
- Los logs registran códigos de motivo y SQLSTATEs, no el texto de las consultas.

## Seguimientos

- **Qué roles predefinidos equipan `sql_query`.** Ninguno en este cambio: agregar una herramienta a un rol entregado cambia lo que reciben quienes importan la librería y es una decisión propia, relevante para SemVer.
- **Escenarios en vivo a través de la aplicación real.** `tests/test_live_eval_sql_query.py` ya ejecuta la herramienta contra un modelo real (una pregunta ad hoc, y un pedido de borrado que debe dejar los datos intactos) mediante un rol exclusivo de test y el runtime directo; ejecutarla a través de la API HTTP necesita un rol que declare la herramienta y el harness en vivo de la Fase 0 (#78).
- **Reevaluar `pglast`** si su licencia llegara a ser compatible con la librería, para eliminar la diferencia entre parsers en su origen.
