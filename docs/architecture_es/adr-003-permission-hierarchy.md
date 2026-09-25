# ADR-003 — Jerarquía de permisos

**Estado:** Aceptado · **Fecha:** 2026-09-25 · **Enmienda:** ADR-002 C.10, C.11, C.12 y AD-5

## Resumen

Los permisos se representan internamente mediante una jerarquía explícita de clases `Permission` y un `Tier` ordinal (`T0` a `T3`); los wire names permanecen como cadenas en los límites de manifiestos, YAML, logs y auditoría. La jerarquía reemplaza la inferencia por prefijos como autoridad de seguridad, exige registro explícito y conserva un techo de grants de despliegue para la revalidación de Capa 2.

## Contexto

ADR-002 introdujo los tiers de capacidad, la barrera `untrusted_input`, las herramientas de comando declarativas y el auto-grant de arranque AD-5. La implementación entregada necesitaba un mecanismo de clasificación más fuerte que la ortografía del permiso: una acción nueva debe poder declarar su peligro de forma estructural, y los grants no deben ampliarse al conjunto completo de permisos declarados por el rol durante un turno.

Los valores de tier no cambian: T0 es acceso inherente, T1 lectura acotada, T2 escritura/envío acotado y T3 ejecución en el host. Este ADR registra el comportamiento entregado, no el diseño propuesto anteriormente.

## Decisión

### Modelo de clases y errores

`Permission` es una raíz abstracta sin tier. Las subclases concretas llevan un atributo de clase `Tier`. La jerarquía de errores entregada está enraizada en `AgentPermissionError` (no en `PermissionError`, para evitar la subclase incorporada de `OSError`) e incluye los fallos de tier inválido, registro, R2a/R2b y entrada no confiable.

R1 se aplica en el momento de definir la clase mediante `Permission.__init_subclass__`: una hija puede heredar el tier de su padre, conservarlo o escalarlo, pero no declarar uno inferior. Todas las comparaciones de tier usan el mapeo ordinal explícito `tier_rank` / `_RANK`, no comparaciones directas de enums.

Las familias de acción incorporadas son `Read` (T0), `Write` (T2), `Send` (T2), `Exec` (T3), `Run` (T2) y `Spawn` (T2). Los nombres con alcance de recurso resuelven a subclases dedicadas. `ReadFiles(Read)` escala explícitamente `read:files` a T3.

### Aplicación en herramientas y grants

`ToolSpec.__post_init__` difiere su importación de `evaluate_tool_spec` hasta la construcción. Esto evita el ciclo de importación entre `permissions` y `harness`: `permissions.base` importa `Tier` desde `harness.registry`, y `harness/__init__.py` importa `harness.loader` de forma anticipada, por lo que `harness.loader` también difiere sus imports de permisos al ámbito de función. Los demás módulos del harness (`injector`, `factory`, `interceptor`) no forman parte de esa cadena de inicialización del paquete e importan `agents_system.permissions` a nivel de módulo.

`evaluate_tool_spec` aplica ambos predicados:

- **R2a, techo:** cada clase de permiso requerida debe satisfacer `tier de la herramienta >= tier del permiso`.
- **R2b, piso:** una herramienta T2 o T3 debe tener al menos un permiso requerido cuyo tier alcance el de la herramienta. Las herramientas T0 y T1 no tienen requisito de piso.

R3 define la cobertura de un grant: una clase otorgada cubre una clase requerida solo cuando `issubclass(required, granted)` y el tier requerido no supera al otorgado. Por lo tanto, un descendiente de igual tier puede quedar cubierto por su ancestro; un descendiente escalado requiere un grant explícito.

R4 prohíbe que un rol con `untrusted_input` posea cualquier permiso T3, independientemente de su wire name. La validación del loader resuelve los permisos del manifiesto y rechaza una clase T3 al cargar la definición con `UntrustedInputGrantError`; `build_runtime` rechaza de forma independiente un grant T3 para ese rol. El injector también conserva su barrera de herramientas T3 como defensa en profundidad.

`build_runtime` acepta grants tanto por wire name registrado como por clase, y normaliza ambos a la misma `frozenset[type[Permission]]`. Antes de persistir `EquippedRuntime.deploy_grant_ceiling`, conserva solo las clases otorgadas que cubren mediante R3 un permiso declarado por el rol. Por lo tanto, un grant bruto no es por sí mismo el techo desplegado salvo que cubra la superficie declarada por el rol.

La Capa 2 corrige el issue #38 al verificar llamadas sensibles contra el `deploy_grant_ceiling` persistido, intersectado con los permisos actuales, en lugar de contra todo el conjunto declarado por el rol. `AgentRuntime.run_turn` toma por defecto ese techo persistido, no `definition.permissions`.

### Registro y formato de cable

`PermissionRegistry` mapea un wire name registrado a una clase de permiso y una clase registrada a su único nombre canónico. No contiene inferencia por prefijo, subcadena ni otra forma basada en el nombre. Un único lock protege lecturas y escrituras. Su operación `get_or_register` es atómica: verifica si existe una clase, crea una solo cuando falta y la registra dentro de una adquisición del lock, evitando una carrera de resolver y luego registrar.

Los permisos `run:` declarados en manifiestos se registran automáticamente como subclases de `Run` mediante esa operación atómica. Todas las representaciones externas de permisos siguen siendo wire names de cadena; la resolución a clases es un límite interno de política.

### Decisiones resueltas entregadas con este modelo

1. **Escalamiento de `ReadFiles`.** `read:files` resuelve a `ReadFiles(Read)` en T3, satisfaciendo R2a y R2b para el acceso al sistema de archivos del host.
2. **Familia `Spawn`.** `Spawn` es una nueva familia `Permission` de nivel superior T2 para los permisos `spawn:*` del orquestador.
3. **Techo de herramientas de comando.** Los `command_tools` declarativos se limitan exactamente a T2; T3 ya no se acepta.
4. **Evals excluidos.** El valor por defecto de grant completo implícito de evals queda fuera de este cambio.
5. **Grants explícitos al arrancar.** `DEPLOY_GRANTS` es la fuente de grants de despliegue de `main.py`. Cada `model_id` configurado requiere una entrada; el arranque falla en lugar de otorgar automáticamente `definition.permissions`.

### Enmiendas a ADR-002

Este ADR conserva el texto histórico de ADR-002, pero reemplaza las siguientes afirmaciones sobre el comportamiento entregado:

- **C.10 — tiers de capacidad:** los valores de tier se mantienen en T0–T3, pero la clasificación y aplicación pasan de coincidencia por prefijo a despacho basado en clases.
- **C.11 — `untrusted_input`:** el invariante se generaliza desde la ortografía `exec:*` a toda clase de tier T3 y genera `UntrustedInputGrantError`.
- **C.12 — `command_tools` declarativos:** el techo de tier permitido se reduce a T2 únicamente; las declaraciones T3 se rechazan.
- **Auto-grant AD-5:** AD-5 se elimina. `main.py` ya no otorga `definition.permissions`; exige una entrada explícita en `DEPLOY_GRANTS` por `model_id` o se niega a arrancar.

## Seguimientos abiertos

- **Issue #47:** los evals conservan un valor por defecto implícito de grant completo. La Decisión resuelta 4 excluye deliberadamente ese comportamiento de este ADR.
- **Grants por clase en librería:** `build_runtime(..., granted_permissions=[SomeUnregisteredClass])` no falla de forma universal al otorgar. Una clase sin registrar que cubre mediante R3 un permiso declarado por el rol puede entrar en `deploy_grant_ceiling`; cuando `AgentRuntime.run_turn` usa sus permisos por defecto, resuelve esa clase a la inversa y genera `UnknownPermissionNameError` antes de ejecutar el grafo o llamar una herramienta. Una clase sin registrar que no cubre ningún permiso declarado se omite del techo y no activa ese fallo diferido. Los llamadores que pasan permisos de turno explícitos evitan la ruta de resolución inversa por defecto. Este caso solo de librería debe aplicar el requisito de registro antes.

## Correcciones

- La raíz de error entregada es `AgentPermissionError`, no `PermissionError`.
- El orden de tiers y el get-or-create del registro son explícitos y están protegidos por lock, respectivamente; ninguno depende del orden incidental de enums ni de una secuencia separada de resolver y luego registrar.
- El techo de grants persistido está limitado por la cobertura R3 de los permisos declarados por el rol, no se normaliza simplemente a partir de la lista de grants bruta.
