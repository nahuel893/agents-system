# Política (Policy)

Una política define cómo se comporta el runtime de un agente —no lo que tiene permitido hacer (eso le corresponde al modelo de permisos), sino cómo actúa dentro de esos límites establecidos.

Las políticas se inyectan en último lugar dentro de la secuencia de inyección de capacidades, después de las herramientas, habilidades, contexto, permisos y memoria. Gobiernan el comportamiento del runtime al momento de utilizar todas las demás capacidades inyectadas.

---

## Política frente al modelo de permisos (*Policy vs. permission model*)

| Aspecto | Respondido por |
|---|---|
| ¿Puede este rol llamar a esta herramienta? | `permission-model.md` |
| ¿Cuándo debe escalar el agente en lugar de actuar? | `policy.md` |
| ¿Qué ocurre si se bloquea la llamada a una herramienta? | `policy.md` |
| ¿Cuánto tiempo debe esperar el agente antes de expirar (*timeout*)? | `policy.md` |
| ¿Qué acciones requieren confirmación humana? | `policy.md` |

---

## Niveles de autonomía

El runtime de un agente opera en uno de tres niveles de autonomía. El nivel de autonomía se declara en el archivo `policy.md` del agente. Este nivel puede ser restringido (pero nunca elevado) en tiempo de ejecución por el límite mínimo global de la plataforma (*global policy floor*).

| Nivel | Comportamiento |
|---|---|
| `full` | El agente actúa sobre todas las herramientas permitidas sin solicitar confirmación. Adecuado para operaciones de bajo riesgo y reversibles. |
| `supervised` | El agente puede actuar de forma autónoma dentro de un alcance definido. Las acciones fuera de ese alcance requieren escalamiento o confirmación humana. |
| `confirm` | El agente propone acciones y espera la aprobación humana explícita antes de ejecutar cualquier llamada a herramientas. Adecuado para operaciones de alto riesgo o irreversibles. |

El nivel de autonomía es un techo, no un piso. Un agente `supervised` que opere sobre un conector sensible aún puede requerir confirmación para llamadas a herramientas específicas, tal como se define en el flag `sensitive: true` de la herramienta y en las reglas de revalidación del modelo de permisos.

---

## Reglas de escalamiento (*Escalation rules*)

Un agente debe escalar cuando se cumpla cualquiera de las siguientes condiciones:

| Condición | Acción requerida |
|---|---|
| Una herramienta requerida no está en la superficie inyectada | Escalar — no intentar proceder sin ella |
| Una llamada a herramienta es bloqueada por el interceptor | Escalar — no fallar silenciosamente ni reintentar con otra herramienta |
| La confianza en la acción correcta es inferior al umbral definido | Escalar — no adivinar |
| La intención del usuario es ambigua tras N intentos de aclaración | Escalar — no seguir preguntando |
| Una acción sensible superaría el nivel de autonomía inyectado | Escalar — no degradar la acción para hacerla encajar |
| Falla la delegación a un agente hijo | Escalar al padre o al humano según la profundidad y política |

El escalamiento siempre produce un registro en la auditoría. No se permiten fallos silenciosos.

---

## Umbrales para la intervención humana (*Human-in-the-loop thresholds*)

> **Decisión abierta #3:** Los umbrales exactos para la aprobación humana frente a la ejecución autónoma aún no se han finalizado. Las categorías presentadas a continuación son de carácter orientativo.

### Siempre autónomo (no requiere confirmación)
- Operaciones de lectura: consultas de catálogo, consultas de clientes, lecturas de sesión.
- Composición de mensajes que aún no han sido enviados.
- Transiciones de estado internas.

### Requiere confirmación
- Persistencia de pedidos (escribir en la base de datos en nombre de un cliente).
- Cualquier acción que dispare una escritura en un sistema externo (ERP, sistema de entregas).
- Escalamiento a un operador humano (notificación en Slack, transferencia de chat).

### Requiere aprobación humana (el agente se bloquea hasta recibir respuesta)
- Acciones no cubiertas por la definición actual del agente.
- Acciones marcadas por el interceptor de herramientas como fuera de alcance.
- Cualquier acción que el propio agente clasifique como incierta bajo el nivel de autonomía `confirm`.

---

## Aplicación de límites a herramientas (*Tool call enforcement*)

La plataforma aplica los límites de las herramientas a través de dos capas independientes. Ambas deben estar presentes en el sistema.

### Capa 1 — Capability Injector (tiempo de compilación/inicio)

Durante la fase de inyección, el Capability Injector construye la superficie de herramientas permitidas a partir del manifiesto del agente (`manifest.md`) y el conjunto de permisos de la identidad solicitante. Solo se incluyen las herramientas que pasan ambos controles.

La superficie inyectada se transfiere al LLM mediante `bind_tools()`. El modelo solo puede ver e invocar las herramientas que figuran en esta lista. Las herramientas fuera de la lista sencillamente no existen desde la perspectiva del modelo.

Esta es la barrera principal de control.

### Capa 2 — Tool Call Interceptor (tiempo de ejecución)

Antes de que se ejecute cualquier conector de herramienta, el Tool Call Interceptor valida la invocación contra la superficie inyectada en el runtime actual.

Si la herramienta no se encuentra en la superficie inyectada —sin importar cómo se haya generado la llamada—, el interceptor realiza las siguientes acciones:

1. Bloquea la ejecución de la herramienta.
2. Registra una violación de política en el log de auditoría (nombre de la herramienta, identidad del runtime, rol, marca de tiempo).
3. Dispara el escalamiento según las reglas descritas en el archivo `policy.md` del agente.

El interceptor no intenta buscar una herramienta alternativa, sustituirla por una similar ni ignorar la violación en silencio. El agente escala de inmediato.

Esta capa existe para atrapar casos que la Capa 1 no puede: alucinaciones del modelo que invocan herramientas inexistentes, intentos de prompt injection para ejecutar conectores fuera de alcance, y errores en el pipeline de inyección que dejen una superficie incompleta.

### Respuesta ante violaciones de las reglas de control:

| Escenario | Respuesta |
|---|---|
| Herramienta no presente en la superficie inyectada | Bloquear + log de auditoría + escalar |
| Herramienta en la superficie, pero la revalidación de permisos falla en ejecución | Bloquear + log de auditoría + escalar |
| Delegación a un rol no permitido por la política de delegación | Bloquear + log de auditoría + escalar |
| Herramienta sensible invocada en autonomía `full` sin revalidar | Bloquear + log de auditoría + escalar |

En todos los casos: **sin fallos silenciosos, sin reintentos con herramientas alternativas y sin atajos.**

---

## Límites de ejecución (*Execution limits*)

Estos son los valores predeterminados de la plataforma. La política `policy.md` de un agente puede definir valores más estrictos, pero nunca más permisivos.

| Límite | Por defecto | Notas |
|---|---|---|
| Timeout de llamada a herramienta | 10s | Por cada llamada individual |
| Timeout total de ejecución | 60s | Desde la recepción del trigger hasta la salida final |
| Máximo de llamadas a herramientas | 20 | Evita bucles infinitos de ejecución |
| Máxima profundidad de delegación | 2 | Padre → hijo → nieto; no más profundo |
| Máximos intentos de aclaración | 3 | Antes de forzar el escalamiento ante entradas ambiguas |

---

## Política de auditoría (*Audit policy*)

El subsistema de auditoría lee `audit_policy` en el momento de emisión. Todas las
claves son opcionales; los valores por defecto son los seguros.

| Clave | Tipo | Por defecto | Efecto |
|---|---|---|---|
| `retention_days` | `int \| null` | valor de plataforma | Cuánto tiempo se conservan los registros antes de descartar la partición |
| `capture_tool_input` | `bool` | `false` | En `true`, los valores de texto libre (`message`, `body`, `text`) se almacenan en lugar de redactarse |
| `redact_keys` | `list[str]` | `[]` | Claves adicionales de primer nivel del payload que siempre se redactan |

`capture_tool_input` es una habilitación acotada, no global. Los números de
teléfono y las direcciones de correo se eliminan incluso cuando está activada:
habilitar la captura de texto libre no habilita el almacenamiento de
identificadores de clientes.

Implementación y tabla completa de redacción: `docs/platform_es/audit.md`.

---

## Modos de fallo (*Failure modes*)

| Fallo | Comportamiento |
|---|---|
| Conector de herramienta inalcanzable | Reintentar una vez tras 2s; si sigue fallando, escalar |
| Proveedor de LLM inalcanzable | Fallar rápido, devolver error al cliente, log de auditoría |
| Error en pipeline de inyección (falta herramienta) | Bloquear la ejecución por completo, alertar al operador de plataforma |
| Fallo en persistencia de auditoría | **No bloquea.** El evento se descarta, se registra `audit.event_dropped` y se incrementa `dropped_count` |

### Decisión abierta: auditabilidad frente a disponibilidad

Esta especificación decía originalmente *"Bloquear la ejecución — no hay
ejecución sin auditabilidad"*, y el `AuditSink` implementado hace lo contrario:
es *fire-and-forget* por construcción, así que una falla de escritura de
auditoría nunca alcanza el camino de la petición.

Ambas posturas son defendibles y son genuinamente incompatibles:

- **Bloquear ante la falla** es la postura correcta si el registro de auditoría
  es un artefacto de cumplimiento: una acción no auditable no debe ocurrir.
- **No bloquear nunca** es la postura correcta si la prioridad es la
  disponibilidad: el subsistema de auditoría no debe volverse una dependencia
  de latencia ni de falla del tráfico de clientes.

El código implementa hoy la segunda. Esta fila se corrigió para coincidir con el
código, y no al revés, porque invertirlo es una decisión de plataforma con
consecuencias reales sobre el camino de WhatsApp, no una corrección de bug.
Queda registrada aquí para que la elección sea explícita y no accidental.

---

## Referencias cruzadas

- Modelo de permisos y RBAC: `docs/architecture/permission-model.md`
- Definición de herramientas y flag `sensitive`: `docs/platform_es/tool.md`
- Reglas de delegación: `docs/architecture/delegation-policy.md`
- Implementación de control en el harness: `docs/platform_es/harness.md`
- Subsistema de auditoría: `docs/platform_es/audit.md`
