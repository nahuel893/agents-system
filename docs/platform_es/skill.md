# Habilidad (Skill)

## Qué es una habilidad

Una habilidad es un paquete de comportamiento o módulo de prompts que define cómo razona, responde o toma decisiones el runtime de un agente. Las habilidades se inyectan en el prompt del sistema y en la superficie de contexto del runtime durante la fase de inyección de capacidades. No tienen efectos secundarios (*side effects*): no ejecutan llamadas externas, no escriben en bases de datos ni envían mensajes.

Las habilidades son el mecanismo para inyectar conocimiento del dominio, estrategias de razonamiento y convenciones de comportamiento en un agente sin necesidad de escribirlas directamente en la definición del agente ni en el código del runtime.

---

## Habilidad frente a Herramienta (*Skill vs. Tool*)

| Concepto | Qué es | Tiene efectos secundarios | Ejemplos |
|---|---|---|---|
| **Habilidad (Skill)** | Un paquete de comportamiento que define el razonamiento. | No | `colloquial_product_matching`, `escalation_decision` |
| **Herramienta (Tool)** | Un conector ejecutable con un sistema externo. | Sí | `whatsapp_sender`, `rag_catalog_search` |

Una habilidad guía el razonamiento interno del agente antes de que este decida qué acción tomar. Una herramienta es la acción en sí misma. Una habilidad puede indicarle al agente cómo interpretar una solicitud de producto ambigua; la herramienta `rag_catalog_search` es lo que realmente consulta y trae los productos candidatos una vez que el agente ha estructurado una consulta clara.

---

## Esquema de definición de habilidades (*Skill definition schema*)

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `name` | `string` | obligatorio | Identificador único de la habilidad. En snake_case (ej. `order_extraction`). Referenciado por el `manifest.md` del agente en su campo `skills`. |
| `description` | `string` | obligatorio | Describe qué capacidad de comportamiento proporciona esta habilidad al agente. Se utiliza durante la inyección para explicarle al runtime qué le permite hacer esta habilidad. |
| `prompt_modules` | `list[object]` | obligatorio | Lista ordenada de fragmentos de prompts inyectados en el contexto del sistema del agente. Cada entrada contiene: `id` (string), `role` (uno de `system`, `context`, `instruction`, `example`), `content` (string). |
| `context_requirements` | `object` | opcional | Especifica el contexto que la habilidad requiere tener ya inyectado para funcionar correctamente. Subcampos: `requires_tool_output` (lista de nombres de herramientas cuyos resultados deben estar disponibles), `requires_session_context` (booleano). |
| `applicable_roles` | `list[string]` | opcional | Lista de nombres de roles para los que fue diseñada esta habilidad. Si está presente, el inyector emite una advertencia si la habilidad se aplica a un rol fuera de la lista, pero no bloquea la inyección. Una lista vacía significa que la habilidad es de propósito general. |

---

## Cómo se componen e inyectan las habilidades

Las habilidades se inyectan después de las herramientas y antes del contexto dentro del pipeline de inyección (ver `docs/platform/harness.md` para la explicación completa del orden).

**Secuencia de inyección:**

1. Para cada nombre de habilidad declarado en el campo `skills` del `manifest.md` del agente, se recupera su definición correspondiente desde el registro de habilidades.
2. Se verifica el campo `context_requirements.requires_tool_output`: se confirma que las herramientas requeridas ya hayan sido inyectadas (las herramientas preceden a las habilidades en el pipeline). Si alguna herramienta requerida no está presente, se cancela la inyección de la habilidad y se registra la brecha de dependencia en el log.
3. Se integran los fragmentos de `prompt_modules` en la superficie del prompt del sistema del runtime, respetando el orden en que aparecen descritos en la habilidad. Las habilidades declaradas primero en el `manifest.md` del agente se inyectan primero.
4. Si la habilidad declara `context_requirements.requires_session_context: true`, se confirma que la inyección del contexto de sesión esté programada (esta sigue a continuación en el pipeline). Si el contexto de sesión no está disponible, se registra una advertencia en el log pero no se cancela el proceso: la habilidad operará pero con efectividad reducida.

**Regla de composición:** Múltiples habilidades pueden coexistir dentro de un mismo runtime. Sus módulos de prompts se concatenan en el orden de inyección. Los autores de las habilidades son responsables de asegurar que sus módulos no se contradigan entre sí. La plataforma no detecta ni resuelve conflictos semánticos entre habilidades de manera automática.

---

## Relación entre habilidades y definiciones de agentes

El `manifest.md` del agente declara qué habilidades están activas a través del campo `skills`. Esto actúa como una lista de permitidos (*allowlist*): el runtime solo recibirá las habilidades explícitamente nombradas allí, aunque existan otras habilidades en el registro.

Esto mantiene la superficie de capacidades de cualquier agente en ejecución perfectamente predecible a partir de su definición. Ninguna habilidad se inyecta de forma silenciosa ni por defecto.

Una habilidad puede declarar `applicable_roles` para señalar dónde fue diseñada para trabajar. Esto es solo una recomendación: la plataforma emitirá una advertencia pero permitirá la inyección. La responsabilidad de asignar las habilidades adecuadas recae en el autor de la definición del agente.

---

## Ejemplos

### `order_extraction` (Extracción de Pedidos)

| Campo | Valor |
|---|---|
| Descripción | Enseña al agente a extraer datos estructurados de pedidos a partir de entradas de conversación no estructuradas. Maneja múltiples ítems en un solo mensaje, expresiones de cantidad (unidades, cajones, packs) y especificaciones incompletas que requieren aclaración. |
| Roles aplicables | `preventa_agent` |
| Requerimientos de contexto | `requires_session_context: true` (se necesitan mensajes previos para resolver referencias como "lo mismo que la última vez"). |

Módulos de prompts inyectados:
1. **`system/order-extraction-instructions`** — cómo identificar menciones de productos, cantidades y unidades en texto conversacional.
2. **`instruction/ambiguity-handling`** — cuándo pedir aclaraciones frente a cuándo proceder con la mejor coincidencia estimada.
3. **`example/extraction-examples`** — ejemplos prácticos (*few-shot*) en español coloquial argentino.

---

### `colloquial_product_matching` (Coincidencia Coloquial de Productos)

| Campo | Valor |
|---|---|
| Descripción | Enseña al agente a mapear referencias de productos informales, coloquiales o abreviadas a SKUs específicos en el catálogo. Maneja apodos de marcas, referencias genéricas a categorías, modismos regionales y descripciones parciales comunes en el sector minorista de bebidas argentino. |
| Roles aplicables | `preventa_agent` |
| Requerimientos de contexto | `requires_tool_output: [rag_catalog_search]` (los resultados de búsqueda RAG deben estar disponibles antes de que comience el razonamiento de coincidencia). |

Módulos de prompts inyectados:
1. **`system/colloquial-vocabulary`** — mapeos conocidos y heurísticas para referencias informales comunes de productos argentinos (ej. "la rubia" → Quilmes clásica, "cajón" → cajón retornable de botellas).
2. **`instruction/rag-result-interpretation`** — cómo razonar sobre resultados de similitud de coseno y seleccionar el mejor producto candidato.
3. **`instruction/low-confidence-handling`** — qué hacer cuando ninguna coincidencia supera el umbral de confianza mínimo.

---

### `escalation_decision` (Decisión de Escalamiento)

| Campo | Valor |
|---|---|
| Descripción | Proporciona al agente un marco de decisión estructurado para determinar cuándo debe escalar una situación a un operador humano frente a cuándo puede proceder de forma autónoma. Cubre identidades dudosas, violaciones de políticas, umbrales de aprobación y solicitudes explícitas de clientes. |
| Roles aplicables | `preventa_agent`, `orchestrator_agent` |
| Requerimientos de contexto | ninguno |

Módulos de prompts inyectados:
1. **`system/escalation-principles`** — principios fundamentales: ante la duda, escalar; nunca suponer en nombre del cliente; los humanos resuelven las excepciones.
2. **`instruction/escalation-triggers`** — enumeración explícita de condiciones que requieren escalamiento (refleja directamente las `escalation_rules` del `policy.md` del agente).
3. **`instruction/escalation-communication`** — cómo comunicar un escalamiento al cliente de manera clara, amable y sin tecnicismos.

---

## Referencias cruzadas

- Definiciones de herramientas (*tools* — contraparte ejecutable de las habilidades): `docs/platform/tool.md`
- Pipeline de inyección y orden de precedencia: `docs/platform/harness.md`
- Campo `skills` en el `manifest.md` del agente: `docs/platform/role.md`
