# Harness (El Soporte del Entorno)

El *harness* es la capa de infraestructura que envuelve a un modelo de lenguaje y lo convierte en un agente autónomo y funcional.

El modelo es el cerebro: procesa la entrada y genera la salida. El harness es el sistema operativo: proporciona las manos, los ojos y la memoria que el modelo necesita para interactuar con el mundo real.

| Capa | Qué proporciona el harness |
|---|---|
| **Manos** | Herramientas (*tools*): conectores y ejecutores que permiten al agente tomar acciones como enviar un mensaje, escribir un registro o consultar una base de datos. |
| **Ojos** | Pipeline de contexto: una percepción estructurada del mundo, que incluye el evento disparador, la identidad del usuario, el estado de la sesión y el conocimiento de la organización que el agente tiene permitido ver. |
| **Memoria** | Manejadores de memoria de trabajo y persistente: estado de sesión a corto plazo, memoria de trabajo a nivel de equipo y memoria empresarial a largo plazo. |

Sin el harness, un modelo solo puede leer y escribir texto. Con él, el modelo se convierte en un agente que percibe su entorno, actúa sobre él y recuerda información a través de las interacciones.

Este documento describe el pipeline de ejecución completo del harness, el modelo de inyección de capacidades, su ciclo de vida y sus restricciones de aislamiento.

---

## Pipeline completo

```
Trigger (Disparador)
  → System Router (Enrutador del Sistema)
    → Agent Factory (Factoría de Agentes)
      → Capability Injector (Inyector de Capacidades)
        → Agent Runtime (Entorno de Ejecución del Agente)
          → Tool Execution / Delegation (Ejecución de Herramientas / Delegación)
            → Audit / Memory (Auditoría / Memoria)
```

Cada ejecución de un agente sigue estrictamente este pipeline. No existen atajos.

---

## Descripción de las etapas

### 1. Trigger (Disparador)

Un disparador es cualquier evento que inicia la ejecución de un agente. La plataforma acepta los siguientes tipos de disparadores:

| Tipo | Ejemplos |
|---|---|
| Acción de usuario | Mensaje de WhatsApp, llamada a la API, envío de un formulario web. |
| Mensaje | Mensaje entrante desde un conector de integración. |
| Programación | Procesamiento por lotes programado por cron, recordatorios temporizados. |
| Evento externo | Webhook de un servicio de terceros, mensaje de una cola de mensajería. |
| Callback de herramienta | Llegada del resultado asíncrono de una herramienta desde un sistema externo. |

Los disparadores transportan: la carga útil del evento sin procesar (*raw payload*), un identificador de origen (de qué integración proviene) y una identidad solicitante (usuario/empleado, o el sistema para disparadores programados).

---

### 2. System Router (Enrutador del Sistema)

El System Router recibe el disparador y toma tres decisiones:

1. **Identificación del dominio** — ¿A qué dominio y subsistema pertenece este disparador?
2. **Selección del rol** — ¿Qué definición del agente debe manejar este disparador?
3. **Reutilización del runtime** — ¿Existe un runtime en caché activa (*warm cache*) válido para este rol e identidad, o se debe instanciar uno nuevo?

El System Router no ejecuta ninguna lógica del agente. Produce una decisión de enrutamiento: un nombre de rol, una identidad solicitante y una directiva de caché (`use_cache: true/false`).

---

### 3. Agent Factory (Factoría de Agentes)

La Agent Factory recibe la decisión de enrutamiento y construye el runtime base:

- Resuelve el modelo y el proveedor de IA (por ejemplo, Claude Sonnet 4 para conversaciones complejas, Claude Haiku 4.5 para clasificación rápida).
- Aplica la política de ejecución base (tiempo de espera o *timeout*, política de reintentos, temperatura).
- Lee la carpeta de definición del agente para el rol seleccionado (`role.md`, `manifest.md`, `policy.md`).
- Produce un runtime no equipado: un entorno con identidad y política de ejecución, pero sin capacidades inyectadas aún.

La factoría de agentes no inyecta capacidades. Esa es responsabilidad del Capability Injector.

---

### 4. Capability Injector (Inyector de Capacidades)

El Capability Injector recibe el runtime no equipado junto con la identidad solicitante, e inyecta las capacidades en un orden fijo. Este orden no es arbitrario; cada paso de inyección puede depender de lo que se inyectó en los pasos anteriores.

**Orden de inyección:**

1. **Tools (Herramientas)** — Inyecta los conectores y las capacidades de ejecución declarados en el campo `tools` de `manifest.md` del agente. La validación de permisos ocurre aquí. Las herramientas se inyectan primero porque las habilidades pueden declarar `context_requirements.requires_tool_output`, lo que requiere saber qué herramientas están disponibles de antemano.
2. **Skills (Habilidades)** — Inyecta los paquetes de comportamiento y módulos de prompts declarados en el campo `skills` de `manifest.md` del agente. Las habilidades que requieren salidas de herramientas pueden verificar aquí que su dependencia fue resuelta en el paso 1.
3. **Context (Contexto)** — Inyecta el contexto de la tarea, el de la sesión, la identidad del usuario y el contexto organizacional según el campo `context` de `manifest.md` del agente. El contexto se inyecta después de las habilidades porque los módulos de prompts de estas últimas se integran directamente sobre la superficie del contexto.
4. **Permissions (Permisos)** — Finaliza el conjunto de permisos vinculados a este runtime. Aunque la evaluación inicial de permisos ocurrió durante la inyección de herramientas, aquí se resuelve la superficie completa de permisos (incluyendo derechos de delegación y acceso a contextos protegidos).
5. **Memory (Memoria)** — Asocia los manejadores de memoria (local, de equipo u organizacional) según la política `memory_policy` del `policy.md` del agente. La memoria se inyecta después de los permisos porque su alcance de lectura/escritura depende enteramente de ellos.
6. **Policies (Políticas de ejecución)** — Inyecta las políticas que gobiernan el comportamiento del runtime: nivel de autonomía, reglas de escalamiento y política de delegación (incluyendo los módulos de políticas de orquestación `orchestrator_generic` y/o `orchestrator_role` si la delegación está permitida), provenientes de `policy.md` del agente. Las políticas son lo último en inyectarse porque gobiernan cómo interactúa el runtime con el resto de las capacidades.

Al finalizar la inyección, el runtime está completamente equipado y listo para la ejecución.

---

### 5. Agent Runtime (Entorno de Ejecución del Agente)

El Agent Runtime ejecuta el rol asignado utilizando las capacidades inyectadas. Sus tareas son:

- Procesar el evento disparador.
- Invocar herramientas cuando sea necesario.
- Aplicar las habilidades inyectadas para razonar sobre la tarea.
- Observar las reglas de escalamiento a partir de la política inyectada.
- Delegar en agentes hijos si la política de delegación lo permite (ver `docs/architecture/delegation-policy.md`).
- Generar salidas (mensajes, registros escritos, tareas delegadas, escalamientos).

El runtime opera dentro de un límite estricto: solo puede utilizar lo que fue inyectado explícitamente. No puede adquirir nuevas capacidades durante la ejecución.

---

### 6. Tool Execution / Delegation (Ejecución de Herramientas / Delegación)

Las llamadas a herramientas y los eventos de delegación son sub-pasos de ejecución distintos dentro de un runtime en funcionamiento:

**Ejecución de herramientas:**
- El runtime selecciona una herramienta de su superficie inyectada.
- Para herramientas sensibles (marcadas para revalidación al momento de la inyección), los permisos se vuelven a validar contra el estado actual justo antes de proceder con la llamada.
- El conector de la herramienta ejecuta la operación externa.
- El resultado se devuelve al contexto del runtime.

**Delegación:**
- El runtime invoca la política de delegación para crear un runtime hijo.
- El runtime hijo sigue el pipeline completo desde cero: Factory → Injector → Runtime → Audit.
- El hijo recibe únicamente las capacidades a las que tiene derecho según su propio rol; no hereda el conjunto de capacidades del padre.
- Los resultados del hijo se devuelven al contexto del runtime padre.

---

### 7. Audit / Memory (Auditoría / Memoria)

Después de la ejecución (ya sea exitosa, escalada o fallida), el harness persiste:

- **El registro de auditoría (*audit trail*)**: quién disparó la ejecución, qué rol se usó, qué herramientas se inyectaron, qué llamadas a herramientas se realizaron, qué acciones se tomaron y cuáles fueron sus resultados.
- **Escrituras en memoria**: logs de conversación, actualizaciones de memoria de trabajo y memoria de negocio persistente, de acuerdo con la política `memory_policy` del `policy.md` del runtime.

Los registros de auditoría son inmutables. Las escrituras de memoria pueden estar limitadas a la sesión, al equipo o a la organización según la política del `policy.md` del agente y los permisos del runtime.

---

## Etapas del ciclo de vida

| Etapa | Descripción |
|---|---|
| **Instanciar** | La factoría crea el runtime base con su identidad y política de ejecución base. |
| **Inyectar** | El Capability Injector equipa el runtime con herramientas, habilidades, contexto, permisos, memoria y políticas. |
| **Ejecutar** | El runtime procesa el disparador, invoca herramientas y genera salidas. |
| **Destruir / Cachear** | El runtime se destruye o se devuelve a la caché activa; los artefactos se persisten de forma duradera. |

---

## Política de caché activa (*Warm cache*)

La plataforma puede mantener una caché activa de runtimes pre-instanciados y pre-inyectados para aquellos roles de alta frecuencia donde la latencia de un inicio en frío (*cold start*) sea inaceptable.

**Condiciones para cachear un runtime:**
- El rol es de alta frecuencia (se dispara múltiples veces por minuto).
- La latencia del inicio en frío afecta de forma medible la experiencia del usuario.
- El contexto inyectado es seguro para ser reutilizado entre disparadores (no hay datos privados del usuario en la superficie de capacidades inyectada).

**Qué no debe ser cacheado ni compartido jamás entre usuarios:**
- La identidad del usuario solicitante y su conjunto de permisos.
- Cualquier contexto cargado en nombre de un usuario específico (contexto de sesión, acceso a wikis privados, memoria del empleado).
- Cualquier resultado de herramienta que contenga datos privados de un usuario.

Una entrada de la caché activa es válida únicamente para el mismo rol y la misma identidad de usuario. Un runtime en caché para el usuario A jamás debe servir al usuario B.

**Disparadores de invalidación de caché:**
- Cambio de permisos del usuario asociado al runtime en caché.
- Actualización de la carpeta de definición del agente.
- Señal explícita de invalidación de caché enviada por el System Router.

---

## Aislamiento de contexto

Cada runtime ve exactamente lo que fue inyectado para él. No tiene visibilidad sobre:

- El contexto de sesión de otros usuarios.
- Las capacidades inyectadas en runtimes hermanos o padres.
- El contexto de toda la organización a menos que el `manifest.md` del agente declare `context.org_context: true` y los permisos del usuario lo permitan.
- Memoria fuera del alcance definido por la política `memory_policy` del `policy.md` del agente.

Cuando un runtime padre delega en un hijo, el hijo recibe una inyección fresca y limpia, acotada exclusivamente al rol del hijo y a la identidad del usuario que delega. El hijo no hereda el contexto completo del padre, sino únicamente lo que la política de delegación le transfiere de forma explícita.

---

## Registro de auditoría (*Audit trail*)

Para cada ejecución, debe ser posible reconstruir los siguientes elementos a partir del registro de auditoría:

| Elemento | Descripción |
|---|---|
| Identidad del trigger | Quién o qué inició la ejecución. |
| Rol | Qué definición del agente estaba activa. |
| Herramientas inyectadas | Lista completa de herramientas asociadas en el momento de la inyección. |
| Llamadas a herramientas | Cada invocación realizada: nombre de la herramienta, parámetros de entrada (sanitizados de secretos), estructura de salida y marca de tiempo. |
| Delegaciones | Si se crearon agentes hijos: rol del hijo, motivo de la delegación y resultado obtenido. |
| Escalamientos | Si se disparó un escalamiento: destino del escalamiento, condición que lo disparó y resultado. |
| Salidas | Qué produjo el runtime: mensajes enviados, registros escritos, delegaciones iniciadas. |
| Duración | Tiempo total transcurrido desde el disparador hasta la destrucción del entorno. |

Los registros de auditoría se retienen según los días declarados en `audit_policy.retention_days` del `policy.md` del agente, o el valor por defecto de la plataforma si es `null`.

---

## Referencias cruzadas

- Esquema de la definición del agente: `docs/platform_es/role.md`
- Definición de herramientas (*tools*): `docs/platform_es/tool.md`
- Definición de habilidades (*skills*): `docs/platform_es/skill.md`
- Reglas de delegación y agentes hijos: `docs/architecture/delegation-policy.md`
- Modelo de permisos y RBAC: `docs/architecture/permission-model.md`
