# ADR-004 — Agentes personalizados library-first

**Estado:** Aceptado · **Fecha:** 2026-09-26 · **Enmienda:** ADR-002 A.1, D, C.15, E.18, F

## Resumen

`agents_system` ya no sirve únicamente sus 8 roles predefinidos empaquetados.
Quien importa la librería puede construir un `Agent` personalizado — desde su
propia carpeta, desde parámetros de Python, o ambos — que extiende al agente
genérico o a cualquier rol predefinido, se resuelve mediante el mismo
pipeline `AgentDefinition`/`AgentRuntime`, y hereda gratis todo invariante de
la librería (el contrato base, los tiers de capacidad, la barrera
`untrusted_input`⊥T3, el techo de grants de despliegue). `create_app`
registra agentes bajo ids elegidos por quien despliega, en lugar de
extraerlos de una cadena `{deployment}__{role}`. Esto se entregó en diez PRs
(#68, #70, #85, #91, #92, #95, #98, #101, #103, más este), dos de ellos (#94
para el issue #93, #97 para el issue #75) endureciendo el diseño de abajo en
respuesta a una revisión de seguridad, no siguiéndolo tal como fue escrito
originalmente.

## Contexto

Antes de este cambio, toda ruta de resolución en `harness/loader.py` y el
esquema de runtime id en `main.py`/`integration/openai_adapter.py` asumía de
forma rígida que "un rol es un nombre bajo `platform_root/roles`". ADR-003
(permission-model, publicado como v0.2.0) acababa de entregar la capa de
seguridad — la jerarquía `Permission`/`Tier` y el techo explícito de
`DEPLOY_GRANTS` en tiempo de despliegue — que un agente definido por quien
importa necesitaba heredar sin código específico para agentes. Este ADR
registra lo que efectivamente se entregó para lograrlo, corrigiendo
`design.md`/`proposal.md` donde la implementación fusionada se apartó del
plan original.

## Decisión

### D1 — `RoleLocator`: nombre de rol de plataforma | carpeta de quien importa | definición inline

`harness/loader.py` incorpora `RoleLocator = str | FolderLocator |
InlineLocator`. Un `str` simple queda sin cambios — ningún sitio de llamada
de un rol de plataforma existente necesitó editarse. `FolderLocator(path,
root, overrides)` lee la carpeta de quien importa (`role.md`/`manifest.md`/
`policy.md`) de la misma forma en que se lee la de un rol de plataforma;
`InlineLocator(raw, parent)` envuelve una `RawDefinition` ya construida y no
toca disco. `_load_role_files`, `_resolve_role_chain`, `load_generic` y
`resolve()` despachan sobre la unión; `merge()`, el injector y
`_append_base_contract` quedan intactos, que es precisamente por qué todo
invariante de la librería se aplica igual sin importar la forma del agente
(#68).

### D2 — `extends:` falla de forma ruidosa en lugar de colapsar en silencio

`_extends_target` ya no recorta en silencio un valor irresoluble hasta un
rol de plataforma con el mismo nombre. Un valor se ubica en exactamente uno
de dos espacios — el árbol de roles de plataforma, o la propia raíz de
quien importa del agente actual — o genera `DefinitionError`; una ruta
absoluta, o una cuyo destino resuelto cae fuera de esa raíz, se rechaza de
la misma forma, verificado después de la resolución y no rechazando un
segmento `..` a simple vista (una referencia a un hermano como
`../base-support` dentro de la misma raíz de quien importa es legítima). Una
desviación respecto del propio bloque de código de `design.md`: un valor
relativo de `extends:` se resuelve contra la propia carpeta del agente que
lo declara, no contra toda la raíz de quien importa que se le pasó a
`resolve()` — el ejemplo trabajado del diseño ya asumía esto; el bloque de
código que también incluía nunca se corrigió (#70).

### D3 — `Agent`: un constructor de Python congelado, inmutable en profundidad

`agent/spec.py` define `Agent` (exportado como `agents_system.Agent`), que
construye un agente a partir de parámetros de Python, de
`Agent.from_folder(path, **overrides)`, o de ambos — `overrides` reemplaza,
nunca combina, el propio campo de la carpeta. `extends` acepta el nombre de
un rol predefinido (resuelto de forma perezosa, con el algoritmo de D2) u
otro `Agent` (resuelto de forma inmediata — libre de ciclos por
construcción, ya que Python no puede construir un objeto antes de que
existan sus propias dependencias). Se entrega genuinamente congelado: cada
campo contenedor se copia en profundidad a una forma inmutable en el
momento de construcción, no solo declarado `frozen=True` en el nivel
superior (#85).

Toda definición autorada por quien importa en cualquier punto de una cadena
`extends:` — no solo en la hoja — queda sujeta al `autonomy`,
`execution_limits`, redacción de auditoría y condiciones de escalamiento
efectivos de su padre antes de plegarse (`_apply_importer_ceiling`); una
raíz de quien importa sin padre queda sujeta a los valores por defecto de la
plataforma. Esto cerró un hallazgo de severidad ALTA en la revisión de #85:
un agente personalizado podía, de otro modo, ampliar su alcance a través de
`extends` de una forma que un hijo de un rol predefinido nunca podría — los
plegados entre roles de plataforma siguen siendo solo aditivos, sin
cambios. El issue #93 encontró después que ese techo cubría solo `autonomy`/
`execution_limits`, y que los propios parámetros de `Agent(...)` se saltaban
por completo las validaciones de tipo de la ruta YAML — una cadena simple
para `tools` se iteraba carácter por carácter, y una directiva de
eliminación de `permissions` sin `inherit: true` resolvía en silencio al
conjunto completo del padre. Ninguna de las dos brechas está descripta en la
sección D3 de `design.md`, que es anterior a ambas: `_checked` ahora corre
cada campo de `Agent`/override de `from_folder` por los mismos parsers que
usan `policy.md`/`manifest.md`, y el techo también cubre la redacción de
auditoría y une las condiciones de escalamiento en lugar de reemplazarlas
(#94).

### D4 — Skills: texto inline, luego la propia carpeta del agente, luego el despliegue

`_load_skills` resuelve cada nombre de skill declarado, en este orden: el
texto inline de `Agent(skill_contents={...})`, la propia `path/skills/` de
`Agent.from_folder(path)`, y luego — sin cambios — la carpeta `skills/` de
un despliegue de cliente para un rol predefinido. La frase "no existen
skills genéricas" de `docs/platform/deployment.md` y la mención aspiracional
de `skills/` en `docs/platform/role.md` se corrigen para reflejar esto
(#91).

### D5 — Registro: ids elegidos por quien despliega reemplazan `{deployment}__{role}`

`create_app` incorpora `agents: Mapping[str, Agent | str] | None`,
`grants: Mapping[str, Sequence[str]] | None` y
`clients: Mapping[str, str] | None`. Todo id es opaco — como máximo 64
caracteres válidos según `_SAFE_SEGMENT`, nunca se parsea — y todo id que
nombren `WHATSAPP_RUNTIME_ID` o `ADAPTER_RUNTIMES` debe estar registrado, o
el arranque falla nombrándolo. `clients` se rechaza contra un id cuyo valor
es un `Agent`: los overrides de despliegue siguen siendo exclusivos de roles
predefinidos. Cuando se omite `agents`, `lifespan()` recurre al nuevo campo
`Settings.agent_registrations` (`AGENT_REGISTRATIONS`, `{id: "role"}` o
`{id: "role@client"}`), validado por la misma ruta (#92, #95, #98).
`to_model_id`/`parse_model_id` y el parser inline `model_id.split("__", 1)`
se eliminan, no se consolidan; `tests/test_no_duplicated_runtime_id_parsing.py`
es un escaneo AST que fija que ninguna tercera copia de ese parseo
reaparezca.

### D6 — Validación del runtime id, y dónde vive el código nuevo

`_validate_runtime_id` (`main.py`) reutiliza la clase de caracteres de
`loader._SAFE_SEGMENT` para todo id, venga de `agents`, `grants`, `clients`
o `agent_registrations`. `Agent` vive en `src/agents_system/agent/spec.py`,
junto a `agent/graph.py` — no en un archivo `agent.py` hermano, que no
puede coexistir con el paquete `agent/` ya existente.

### D7 — `examples/demo/app.py`, no un punto de entrada empaquetado

`src/agents_system/demo.py` se mudó a `examples/demo/app.py`, idéntico byte
a byte salvo su docstring de módulo. `examples/` no incluye `__init__.py` y
queda fuera del paquete instalado, así que la invocación documentada es un
simple `python examples/demo/app.py`, no una entrada `python -m` (#101). La
propia llamada `build_app` de `demo.py` no necesitó cambios — ya omite
`agents`, así que sigue usando la ruta de reserva `AGENT_REGISTRATIONS`.

### D8 — Guard 1: una instantánea revisada, más un check de CI obligatorio

La superficie de `tools`/`permissions` de un rol predefinido es una base de
seguridad compartida sobre la que construyen las cadenas `extends:` de otros
agentes, así que un cambio en ella se exige como cambio MAYOR (disruptivo) a
través de dos mitades independientes, porque ambas verificaciones corren en
momentos distintos del pipeline real de este repositorio.
`RoleGovernanceSnapshot`/`EXPECTED_ROLE_SURFACE`
(`tests/platform_role_contract.py`) es un literal revisado y versionado por
rol predefinido; el test de contrato en
`tests/test_role_governance_contract.py` falla cuando el `(tools,
permissions)` resuelto de un rol diverge de su instantánea, a menos que el
propio `version:` del frontmatter del rol haya recibido un bump MAYOR. Por
separado, el job `predefined-role-governance-marker`
(`.github/workflows/ci.yml`, `scripts/check_role_governance_marker.py`)
exige un marcador de conventional-commit `BREAKING CHANGE:`/`!` en todo PR
que toque `platform/roles/**/manifest.md` — una entrada en `CHANGELOG.md`
todavía no puede existir en el momento del PR, ya que release-please solo
escribe una en su propio PR de release posterior, así que el marcador es el
equivalente exigible y correctamente temporizado. #103 entregó este job
corriendo, pero no obligatorio: la protección de rama de `main` todavía
listaba solo `ci`/`secret-scan`/`dependency-audit`, así que un cambio de
manifiesto sin el marcador todavía podía fusionarse — solo la mitad de
pytest lo bloqueaba. El issue #104 cerró esta brecha como un cambio
exclusivo de configuración del repositorio, sin PR
(`required_status_checks.contexts` ahora también lista
`predefined-role-governance-marker`, confirmado leyendo de vuelta la
protección de rama). **Tanto `ci` como `predefined-role-governance-marker`
son checks de estado obligatorios de GitHub en `main` hoy.** Un `Agent`
definido por quien importa que extiende un rol predefinido queda
explícitamente fuera del alcance de Guard 1: no tiene nombre en
`PINNED_ROLES`, así que no es, ni puede llegar a ser, sujeto de esta
gobernanza.

## Enmiendas a ADR-002

Este ADR agrega una nota breve en cada una de las siguientes secciones de
ADR-002, sin borrar ni reescribir su texto original — ese texto sigue
siendo el registro histórico del mundo de solo-ocho-roles-predefinidos; este
ADR es lo que realmente se entrega por encima de él ahora:

- **A.1** — rol = clase ahora incluye a un `Agent` autorado por quien
  importa, no solo a un rol predefinido.
- **D** — el árbol de herencia que una cadena `extends:` puede alcanzar
  ahora incluye una carpeta de quien importa o una definición inline, no
  solo los 8 roles predefinidos.
- **C.15** — los backends de referencia genéricos de la plataforma son
  agnósticos a la forma del agente, disponibles para un `Agent`
  personalizado exactamente igual que para un rol predefinido.
- **E.18** — el pipeline de evaluación en vivo corre contra una
  `AgentDefinition` resuelta, no contra un nombre de rol, así que se aplica
  sin cambios a un agente personalizado.
- **F** — la corrección de ubicación de skills (D4) y el renombrado de
  terminología de roles predefinidos (#101) tocan los mismos archivos que
  el trabajo de higiene documental de esta sección.

## Seguimientos abiertos

- `Agent` no expone `command_tools` declarativos (ADR-002 C.12) — un agente
  de quien importa no puede declarar uno. Queda para un cambio de
  seguimiento (las propias Open Questions de `design.md`).
- La identidad por principal (issue #15, ADR-002 A.2) sigue acotada a
  rol/despliegue; sin tocar por este cambio.
- El spec de capacidad `permission-hierarchy` no se promovió desde su
  carpeta de cambio archivada hacia `openspec/specs/` — un vacío solo de
  proceso, no resuelto aquí.

## Correcciones

- El propio bloque de código de la sección D2 de `design.md` resuelve un
  valor relativo de `extends:` contra toda la raíz de quien importa; la
  regla entregada lo resuelve primero contra la propia carpeta del agente
  que lo declara (#70) — ver D2 arriba. El bloque de código nunca se
  actualizó para coincidir.
- La sección D3 de `design.md` es anterior al alcance completo del techo de
  seguridad de quien importa y al endurecimiento de validación de tipos de
  los parámetros de `Agent`: ambos se agregaron por revisión de seguridad
  (#93), no estaban planificados, y se documentan aquí en su lugar.
- La propia Matriz de Amenazas de `design.md` ya listaba las brechas de
  contención de carpetas de quien importa (escape por symlink, TOCTOU,
  revalidación de command tools) como riesgos aplicables con tests
  planificados; #97 es el PR que efectivamente los cerró, dentro de esta
  misma cadena, no un seguimiento diferido.
- La sección D8 de `design.md` describe el marcador de Guard 1 como
  exigido una vez fusionado #103; el propio cuerpo del PR #103 ya
  señalaba que todavía no era un check de estado obligatorio, y el #104
  — un cambio exclusivo de configuración del repositorio, no un PR — es
  lo que efectivamente lo volvió obligatorio. La sección D8 de arriba
  documenta ambos pasos.
- La tabla de Riesgos de `proposal.md` marcaba la descripción de
  `exploration.md` de un auto-grant previo a ADR-003 como desactualizada en
  `main.py`; el código entregado ya usaba el modelo actual basado en
  `DEPLOY_GRANTS` durante todo este cambio, confirmado por lectura directa
  en cada PR en lugar de asumido a partir de esa nota desactualizada.
- Todos los PR de esta cadena superaron su estimación en la Estrategia de
  Testing de `design.md` y el presupuesto por defecto de ~400 líneas (PR2:
  1830 líneas; PR4a-ii: 1090; PR4b: 1336) — abrumadoramente cobertura
  tests-first por escenario nombrado. Cada uno lleva su propia
  recomendación `size:exception` en `state.yaml` en lugar de una suite de
  tests recortada.
