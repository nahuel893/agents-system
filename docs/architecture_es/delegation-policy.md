# Política de Delegación

## Por defecto: sin delegación

Un runtime de agente no tiene permitido spawnear agentes hijos a menos que su archivo `policy.md` lo autorice explícitamente (`delegation_policy.allowed: true`) y la plataforma le haya inyectado los módulos de políticas de orquestación adecuados.

La delegación es una capacidad inyectada de forma controlada, no una suposición por defecto. Si a un agente no se le han concedido derechos de delegación explícitos al instanciarse, no podrá iniciarla bajo ninguna circunstancia, independientemente de lo que decida el modelo de lenguaje en su razonamiento.

> **Decisión abierta #2:** ¿Debería permitirse que los agentes decidan de forma autónoma cuándo spawnear agentes hijos si determinan que es beneficioso, o todo nacimiento de agente hijo debe ocurrir únicamente bajo reglas del sistema explícitamente programadas en tiempo de diseño? La política actual es la opción conservadora por defecto (únicamente bajo reglas explícitas). La arquitectura no impide soportar un modo de "delegación autónoma" en el futuro, pero esta decisión afecta directamente las garantías de seguridad, los límites de presupuesto de tokens, la auditabilidad y la predictibilidad del grafo de ejecución.

---

## Cuándo se permite la delegación

La sección `delegation_policy` en el `policy.md` del agente gobierna si un runtime puede delegar. La factoría solo inyectará políticas de orquestación si se declara `allowed: true`.

Incluso cuando está permitida, la delegación únicamente se considera adecuada y útil en los siguientes casos excepcionales:

| Caso de uso | Descripción |
|---|---|
| **Descomposición de tareas** | Una tarea es demasiado grande o heterogénea para ser procesada bajo el alcance de un solo rol. El agente padre la divide en subtareas estructuradas y delega cada una a un rol de agente hijo apropiado. |
| **Investigación en paralelo** | Múltiples tareas independientes de recolección de información pueden correr de forma concurrente. Cada agente hijo opera en un dominio de información o conector distinto, acelerando la respuesta. |
| **Ejecución aislada de herramientas** | Una herramienta requiere permisos específicos o contextos sensibles que deben aislarse del contexto del padre por motivos de privacidad o menor privilegio. Se crea un hijo con los accesos mínimos, ejecuta la herramienta y devuelve únicamente el resultado. |
| **Separación de Productor y Revisor** | Un primer runtime genera una propuesta o salida; un segundo runtime con contexto limpio y fresco la revisa o verifica de forma independiente. Esto evita que el agente revisor sufra del "sesgo de anclaje" respecto a los razonamientos del productor. |
| **Fronteras de contexto protegido** | Dos partes de una tarea exigen accesos a bases de información que jamás deben coexistir en el mismo runtime (ej. datos de multi-inquilinos). Cada hijo recibe únicamente el fragmento de contexto estrictamente autorizado para su subtarea. |

Estos son casos extraordinarios de diseño de flujos, no el comportamiento operativo por defecto.

---

## Módulos de políticas de orquestación

Cuando el Capability Injector equipa a un runtime con la capacidad de delegar, inyecta uno o ambos módulos de orquestación:

### `orchestrator_generic`

Define las reglas de comportamiento universales aplicables a cualquier agente que delegue, independientemente de su especialidad de negocio:
- **Ciclo de vida de spawneo** — cómo solicitar un runtime hijo a la factoría, qué variables pasar como contexto inicial y cómo capturar el resultado.
- **Reglas de delegación** — validación de los nombres de roles hijos candidatos contra la lista de permitidos `permitted_child_roles` definida en el manifiesto.
- **Registro de auditoría obligatorio** — cada evento de delegación debe guardarse en el log: rol del hijo creado, motivo de la delegación, inputs provistos y outputs consolidados.
- **Semántica de handoff** — cómo transferir datos de forma segura al hijo y cómo integrar la respuesta recibida dentro de la ejecución del padre.
- **Límites de seguridad** — control de la profundidad máxima de delegación, concurrencia máxima y comportamiento de mitigación si un hijo falla.

### `orchestrator_role`

Define las particularidades y reglas específicas para un rol de padre determinado:
- **Roles hijos autorizados** — qué roles específicos de la plataforma puede spawnear este padre (puede restringir aún más la lista general de la plataforma).
- **Herramientas delegables** — qué herramientas del set de herramientas del padre pueden ser expuestas o transferidas al agente hijo.
- **Reglas de escalamiento de negocio** — condiciones específicas bajo las cuales el padre debe detenerse y escalar al operador humano en lugar de delegar.
- **Restricciones del rol** — cualquier limitación adicional del dominio de negocio sobre cómo el rol actual debe coordinar a sus hijos.

---

## El Agente de Empleado como orquestador local temporal

Cuando la política de un Agente de Empleado (`employee_agent`) permite delegar, este actúa temporalmente como un **orquestador local limitado estrictamente a su propio dominio operativo**. No adquiere privilegios de orquestación globales. Solo puede instanciar hijos que estén autorizados en su política `orchestrator_role` local.

Esto es diferente de un **Orchestrator Agent (Agente Orquestador)** de primer nivel, el cual es un rol centralizado de la plataforma con responsabilidades globales de ciclo de vida y ruteo de disparadores.

---

## Límites de seguridad en delegaciones

Para prevenir bucles infinitos, fugas de información y elevaciones de privilegios, se aplican los siguientes límites estrictos:

| Límite | Regla de control |
|---|---|
| **Profundidad máxima (`max_depth`)** | Se declara en el `policy.md` de cada agente (`delegation_policy.max_depth`). El harness lo valida en caliente. Un padre en profundidad $N$ solo puede spawnear un hijo en profundidad $N+1$ si $N+1 \le \text{max\_depth}$. |
| **Aislamiento de permisos** | Un agente hijo **nunca** hereda automáticamente la superficie de permisos completa de su padre. El hijo recibe exclusivamente los permisos declarados en su propio manifiesto de rol, los cuales se validan independientemente contra el RBAC del usuario solicitante. |
| **Prevención de fugas de contexto** | El padre solo puede transferir variables de contexto explícitas y acotadas al hijo. No puede compartir su superficie de contexto completa. Toda transferencia de información debe ser declarada en la llamada y registrada en la auditoría. |
| **Prohibición de escalamiento ascendente** | Un agente hijo jamás puede recibir o ejecutar herramientas que exijan permisos que su propio `manifest.md` no declare, sin importar que su padre posea permisos más elevados. |

---

## Escalamiento frente a delegación

Estos dos mecanismos de control son conceptualmente distintos y no deben confundirse:

| Concepto | Qué significa | Quién ejecuta a continuación |
|---|---|---|
| **Delegación** | El agente actual crea un hijo para resolver una subtarea y se bloquea esperando su resultado. | Un entorno de ejecución de agente hijo (`AgentRuntime`). |
| **Escalamiento** | El agente actual aborta su ejecución de inmediato y transfiere la situación a una autoridad superior o humana. | Un operador humano o un rol de supervisión diferente. |

La delegación mantiene el control dentro de los límites autónomos del sistema de agentes. El escalamiento sale del flujo de ejecución autónomo.

Las condiciones de escalamiento se definen explícitamente en el campo `escalation_rules` del `policy.md` del agente y son aplicadas de forma directa por el harness en tiempo de ejecución, sin quedar a discreción del modelo de lenguaje.

---

## Referencias cruzadas

- Configuración del campo de delegación del agente: `docs/platform_es/policy.md`
- Ciclo de vida y tubería de inyección en el harness: `docs/platform_es/harness.md`
- Restricciones de seguridad y permisos en delegaciones: `docs/architecture_es/permission-model.md`
