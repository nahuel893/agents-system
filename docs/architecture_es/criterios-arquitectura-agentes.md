# Criterios de arquitectura de agentes

Cada criterio resuelve un problema conocido de los agentes de IA cuando pasan de chatbot a agente empresarial. El objetivo es que la librería ofrezca cada solución ya implementada y lista para usar. Este documento registra qué cubre hoy y qué falta, para ir resolviéndolo criterio por criterio. Los identificadores (`C1`…`C7`) son estables, para citarlos en issues y PRs.

**Alcance y método.** Evaluación contra `main` en `829a399`. El estado sale de `docs/platform/features.md` y de búsquedas en `src/`. La ausencia de caché (C6) y de MCP (C3) se verificó solo con búsqueda de texto. No se ejecutó nada.

## Estado

| Criterio | Problema que resuelve | Solución | Estado |
|---|---|---|---|
| C1 | Falta de identidad | System prompt | Cubierto |
| C2 | Alucinaciones y falta de contexto | RAG | Parcial |
| C3 | Pasividad: solo responde texto | Tool calling y MCP | Parcial |
| C4 | Fugas de información interna | Control de acceso (RBAC y OAuth) | Parcial |
| C5 | Ataques de manipulación | Guardrails | Parcial |
| C6 | Costos operativos elevados | Caché y model routing | Falta |
| C7 | Fallos silenciosos y regresiones | Observabilidad y evals | Evals cubierto; trazas parcial |

## C1 — Identidad: system prompt

**Problema.** El modelo base recomienda a la competencia porque no sabe a quién representa.

**Solución.** Instrucciones base ocultas que viajan en cada interacción y definen rol, identidad y reglas de negocio.

**Hoy.** Cubierto. El prompt se define en `role.md` o con `Agent(system_prompt=...)`; se hereda con `extends`, se amplía con skills y un deployment de cliente puede sobrescribirlo sin tocar el núcleo.

**Falta.** Nada relevante.

## C2 — Contexto: RAG

**Problema.** El agente inventa políticas o montos porque desconoce los datos internos.

**Solución.** Fragmentar documentos internos, convertirlos en embeddings, guardarlos en una base vectorial y recuperar el contexto relevante antes de responder.

**Hoy.** Parcial. Hay búsqueda sobre un catálogo de productos (`build_catalog_rag_connector`, con vectores si hay embedder y keywords si no) y una base de conocimiento en memoria de referencia (`InMemoryKnowledgeBase`).

**Falta.**

- [ ] Ingesta genérica de documentos: carga, fragmentado y embeddings.
- [ ] Un store vectorial detrás de un puerto, con una implementación en Postgres.
- [ ] Una herramienta de recuperación lista para equipar, que entregue los fragmentos como bloques de datos no confiables.
- [ ] Citas de la fuente en la respuesta.

## C3 — Acción: tool calling y MCP

**Problema.** Los usuarios necesitan que el agente ejecute tareas reales: abrir tickets, enviar avisos, procesar pagos.

**Solución.** Conectar el modelo a sistemas externos mediante herramientas y servidores MCP.

**Hoy.** Parcial. Las herramientas propias están resueltas: `ToolSpec` con permisos y tier, comandos declarativos dentro de un sandbox, y una herramienta de SQL de solo lectura.

**Falta.**

- [ ] Cliente MCP. No hay nada en el código; está abierto como `#169 — MCP client, phase 1 (ADR-008): stdio servers as T2 tools`.
- [ ] Un decorador `@tool` para declarar herramientas sin escribir el `ToolSpec` a mano (parte de `#325 — Library ergonomics`).

## C4 — Control de acceso: RBAC y OAuth

**Problema.** Cualquier usuario puede pedirle al agente que consulte datos confidenciales.

**Solución.** El agente ejecuta las herramientas heredando estrictamente los permisos y el rol de quien hace la consulta.

**Hoy.** Parcial. Hay permisos por familia, grants como barrera al armar el runtime, un interceptor que revalida cada llamada, y scope por usuario final con seguridad a nivel de fila en Postgres: un usuario no puede leer los datos de otro.

**Falta.** Es el hueco más importante de la lista.

- [ ] Grants por usuario. Hoy los grants son por runtime: todos los usuarios de un runtime tienen las mismas herramientas. Heredar el rol de quien consulta toca el modelo de permisos.
- [ ] Mapeo de identidad a rol: de la identidad que resuelve el canal o la API a un conjunto de permisos.
- [ ] OAuth: delegar la identidad del usuario hacia los sistemas externos, para que la herramienta actúe con las credenciales de quien consulta y no con una credencial de servicio.

## C5 — Manipulación: guardrails

**Problema.** Un usuario malicioso oculta instrucciones en un texto (prompt injection) para forzar acciones no autorizadas.

**Solución propuesta.** Filtros de entrada que detecten instrucciones maliciosas y filtros de salida que censuren datos sensibles.

**Hoy.** Parcial, con una defensa distinta a la propuesta. La librería no detecta la inyección: limita el daño. Los datos externos entran solo como bloques de datos no confiables, que no cambian herramientas, grants ni límites; un rol marcado `untrusted_input` nunca recibe `exec` ni herramientas T3; y la auditoría pasa por un redactor.

**Criterio de diseño.** Los clasificadores de entrada se evaden. La defensa principal sigue siendo estructural: aunque la inyección pase, el agente no tiene la herramienta ni el permiso para hacer daño. El detector de entrada se suma como capa opcional, no como la defensa.

**Falta.**

- [ ] Filtro de salida para datos sensibles en la respuesta al usuario.
- [ ] Punto de extensión para un detector de entrada opcional.

## C6 — Costos: caché y model routing

**Problema.** Miles de consultas repetitivas disparan el consumo de tokens de modelos grandes.

**Solución.** Guardar en caché las respuestas frecuentes y derivar las tareas simples a modelos chicos, dejando los grandes para los casos complejos.

**Hoy.** Falta. Lo que existe controla el gasto pero no lo reduce: modelo por agente, costo por turno y presupuestos por usuario.

**Falta.**

- [ ] Caché de prompt del proveedor, para no pagar el prompt del sistema en cada turno.
- [ ] Caché de respuestas frecuentes, con cuidado de no cruzar respuestas entre usuarios con distinto scope.
- [ ] Routing por complejidad. Abierto como `#185 — AgentRouter: route each task to a low-cost or a high-cost agent by its complexity`.

## C7 — Calidad: observabilidad y evals

**Problema.** El agente responde mal y no se sabe si falló el prompt, el RAG o una herramienta; o un cambio rompe lo que ya funcionaba.

**Solución.** Registrar la traza completa de cada ejecución y exigir evals antes de desplegar cualquier cambio.

**Hoy.** Evals cubierto: escenarios en YAML, gates por tasa de éxito, suite de capacidades, matriz por modelo y suite de mecanismos offline. Observabilidad parcial: auditoría de grants, denegaciones y llamadas a herramientas, métricas Prometheus y logs con id de request.

**Falta.**

- [ ] Traza completa por turno: prompt enviado, documentos recuperados, llamadas y resultados, enlazados por un id.
- [ ] OpenTelemetry.
- [ ] Métricas por agente. Abierto como `#173 — Per-agent metrics built into every agent (AgentMetrics)`.

## Relación con la revisión de arquitectura

`revision-arquitectura-2026-11.md` trata la salud interna del código; este documento trata lo que la librería le ofrece a quien construye un agente. Se cruzan en dos lugares: C4 y C7 dependen de la mejora M5 (políticas tipadas, `AuditEvent` tipado, puntos de extensión), y el punto de extensión de C5 es el `ToolMiddleware` / `ModelHook` de esa misma mejora.
