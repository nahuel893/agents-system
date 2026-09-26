# Modelo de Permisos (Permission Model)

## Empleados como usuarios RBAC

La plataforma modela a los empleados corporativos como usuarios con control de acceso basado en roles (RBAC). Cada runtime de agente que actúa en representación de un empleado está estrictamente vinculado a la identidad de dicho empleado y hereda su conjunto de permisos — el agente **no** utiliza una cuenta de servicio genérica de plataforma.

Esto garantiza que:
- Un agente jamás pueda realizar una acción que su empleado asociado no tenga permitida.
- El acceso a las herramientas esté filtrado por los permisos reales que posee el usuario, no únicamente por lo declarado en el `manifest.md` del rol del agente.
- La carga de información contextual esté sujeta a las barreras de privacidad organizacional correspondientes al rol del empleado.
- La delegación a agentes hijos no pueda ser explotada para evadir o eludir los controles de acceso del modelo RBAC.

Para aquellos agentes disparados de forma automática por el sistema (tareas programadas por cron, respuestas a webhooks periféricos), se asocia una identidad de servicio con un conjunto mínimo de permisos explícitos estrictamente acotado en reemplazo de la identidad del empleado.

---

## Evaluación de permisos

Los permisos se evalúan en dos momentos clave del ciclo de vida del runtime:

### 1. Al momento de la inyección de capacidades (Tiempo de inicio)
Cuando el Capability Injector prepara el runtime, contrasta el conjunto de permisos del usuario con las exigencias declaradas en el campo `required_permissions` de cada herramienta registrada. Si el usuario no cuenta con los permisos necesarios, esa herramienta se excluye por completo de la superficie de capacidades inyectada en el runtime.

Si la herramienta excluida estaba marcada como **obligatoria** (el rol no puede operar sin ella), la instanciación de la factoría se aborta y falla. Si la herramienta era opcional, sencillamente se omite del set y el agente inicia con capacidades reducidas.

Esta es la barrera de contención primaria de la plataforma. La mayoría de los descartes y controles ocurren aquí.

**Segunda barrera (ADR-002 C.10, issue #109):** independientemente del control de permisos anterior, un rol cuyo `policy.md` declara `untrusted_input: true` nunca recibe una herramienta con tier `T3` (ejecución en el host), aunque `required_permissions` se cumpliría de otro modo. Esto cierra el caso que un control por nombre de permiso no puede cubrir: una herramienta T3 registrada bajo un permiso sin prefijo `exec:`.

### 2. En tiempo de ejecución (Revalidación en caliente)
Para acciones clasificadas como **sensibles** —tales como escrituras en bases de datos, envíos de mensajes salientes a clientes o modificaciones de estado en ERPs externos— los permisos se vuelven a validar inmediatamente antes de que el conector de la herramienta se ejecute. Esto nos protege contra:
- Cambios de permisos realizados en el backend administrativo mientras el agente corre en una sesión de chat larga.
- Runtimes hijos delegados cuyas credenciales cacheadas por el padre puedan haber quedado desactualizadas.
- Runtimes recuperados de la caché activa (*warm cache*) cuyo snapshot de permisos se haya vuelto obsoleto.

La revalidación consulta la misma fuente RBAC autoritativa que el inyector. Si la revalidación falla, el conector se bloquea, la llamada se aborta de inmediato y el harness dispara una alerta y regla de escalamiento.

> **Resuelto:** el umbral que clasifica qué constituye una "acción sensible" que amerita revalidación es el **tier** de capacidad de cada herramienta (ADR-002 C.10, issue #109): T2 (escritura/envío acotado) y T3 (ejecución en el host) siempre se revalidan en tiempo de llamada; T0 (inherente) y T1 (lectura acotada) no, salvo que una herramienta puntual opte por `always_revalidate: true`. El tier es un campo explícito y revisable en cada `ToolSpec` — no se infiere de cómo está nombrado `required_permissions` —, de modo que una herramienta cuyo permiso peligroso no lleva prefijo `write:`/`send:`/`exec:` igual queda atrapada. Ver las cuatro capas deterministas de aplicación de ADR-002 (introducción de la sección C) y C.10 para la tabla completa de tiers y su fundamento.

---

## Origen del grant al arrancar (issue #38)

El campo `permissions` del `manifest.md` de un rol es lo que ese rol **declara que puede necesitar** — no es, por sí solo, un grant. El arranque de `agents_system.main` otorga permisos de forma explícita mediante la variable de entorno `DEPLOY_GRANTS`: un objeto JSON que mapea cada id de runtime registrado (los ids opacos que registra `AGENT_REGISTRATIONS` y que nombran `ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID`) a la lista de nombres de permiso (wire names) realmente otorgados a ese runtime, por ejemplo:

```bash
DEPLOY_GRANTS='{"acme-sales": ["read:catalog", "write:orders"]}'
```

El arranque falla de forma explícita, nombrando el id del runtime, cuando un runtime registrado no tiene una entrada correspondiente — no existe un grant automático, y los permisos declarados por un rol nunca se convierten silenciosamente en su grant. El grant resultante se persiste en el runtime (`EquippedRuntime.deploy_grant_ceiling`) de forma independiente del conjunto declarado por el rol, y es lo que acota la revalidación en tiempo de ejecución descrita arriba: la Capa 2 verifica los permisos requeridos por una herramienta sensible contra este límite (ceiling), nunca contra el conjunto completo declarado por el rol. Los consumidores de la librería (que no arrancan a través de `agents_system.main`) otorgan permisos de la misma forma, explícitamente, mediante `build_runtime(..., granted_permissions=[...])`.

Una aplicación que registra sus agentes con `create_app(agents=..., grants=...)` (ADR-004) otorga por cada id de runtime que eligió, por ejemplo `grants={"acme-sales": ["read:catalog", "write:orders"]}`; sin `grants=`, la fuente es `DEPLOY_GRANTS`, con esos mismos ids registrados como claves. Las reglas son las mismas: un id sin entrada hace fallar el arranque, nombrándolo junto con la fuente; un valor tiene que ser una lista (un string suelto hace fallar el arranque en lugar de otorgarse carácter por carácter); y `build_runtime` sigue rechazando cualquier grant T3 a un agente `untrusted_input` (R4), incluido un `Agent` propio. Ver `docs/platform_es/deployment.md`.

---

## Qué gobierna el filtrado de permisos

| Capacidad / Recurso | Cómo se aplican los permisos |
|---|---|
| **Acceso a Herramientas** | Cada herramienta declara su campo `required_permissions`. Las herramientas cuyas exigencias superen los permisos del usuario solicitante se excluyen en tiempo de inyección. |
| **Carga de Contexto** | El acceso a fuentes de información está delimitado por fronteras de alcance organizacionales. El manifiesto del agente solicita el contexto; el inyector solo carga los datos si el RBAC del usuario lo permite. |
| **Delegación** | Declarar `allowed: true` en la política del agente es condición necesaria pero no suficiente para delegar. El usuario disparador también debe poseer privilegios de delegación explícitos en su set RBAC. |

---

## Fronteras de privacidad del contexto

La plataforma divide el acceso a la información en tres alcances de contexto:

| Alcance (Scope) | Descripción | Ejemplos |
|---|---|---|
| **local-only** | Visible únicamente para esa instancia de agente y el usuario asociado. | Historial de chat del cliente actual, borradores de pedidos locales, estado de sesión actual. |
| **shared-team** | Compartido entre agentes que actúan para miembros de un mismo equipo o zona. | Catálogos de clientes asignados a una ruta de preventa específica, listas de precios modificadas de la zona. |
| **org-wide** | Visible por cualquier runtime de la organización. | Catálogo general de productos de la distribuidora, listas de precios base, políticas generales. |

El `manifest.md` (campo `context`) y el `policy.md` (campo `memory_policy.read_scope`) del agente definen a qué alcances solicita conectarse el rol. El juego de permisos del usuario debe validar y permitir el acceso a dichos alcances.

> **Decisión abierta #4:** Las delimitaciones precisas para cada tipo de dato en los rangos `local-only`, `shared-team` y `org-wide` no se han especificado formalmente para toda la plataforma. Específicamente: ¿los logs de conversación son estrictamente locales o accesibles para supervisores?, ¿los registros de clientes son org-wide o limitados por preventista?, y ¿el historial de pedidos de un comercio es local del cliente o visible para cualquier preventista que atienda la zona? Estos alcances tienen fuertes implicaciones de privacidad y cumplimiento y deben resolverse antes de que la plataforma maneje datos reales y sensibles en producción.

---

## Cómo fluyen los permisos hacia los agentes hijos

Cuando un runtime delega una subtarea en un agente hijo, el hijo recibe una inyección fresca con su propio conjunto de permisos evaluado de forma independiente. Este conjunto es la intersección de:
1. El campo `permissions` declarado en el `manifest.md` del rol del hijo (lo que el hijo necesita).
2. Los permisos activos del usuario en el sistema RBAC (lo que el usuario puede hacer).

**Regla de oro de delegación:** La delegación jamás puede resultar en una elevación de privilegios. El agente hijo operará únicamente dentro de la intersección del manifiesto del hijo y los permisos del usuario — lo que sea **más restrictivo**. El padre no puede otorgarle al hijo privilegios que él mismo no posea.

---

## Reglas de inyección específicas de conectores

La siguiente tabla detalla qué roles de agente tienen permitido declarar e inyectar conectores de herramientas específicos de la plataforma. Para que la inyección proceda, el manifiesto del agente debe declarar la herramienta y la identidad del usuario debe poseer el permiso correspondiente:

| Conector / Herramienta | Nombre del Tool | Roles permitidos | Permiso RBAC exigido |
|---|---|---|---|
| **WhatsApp Business API** | `whatsapp_sender` | `preventa_agent` | `send:whatsapp` |
| **Fuente de catálogo provista por el despliegue** | `catalog_search` | Roles cuyo manifiesto declara la herramienta | `read:catalog` |
| **PostgreSQL** | `postgres_order_writer` | `preventa_agent` | `write:orders`, `write:order_items` |
| **PostgreSQL** | `client_lookup` | `preventa_agent`, `orchestrator_agent` | `read:client_registry` |
| **Redis** | `redis_session_state` | `preventa_agent`, `orchestrator_agent`, `employee_agent` | `read:session_state`, `write:session_state` |
| **Slack API** | `slack_notifier` | `orchestrator_agent`, `summary_agent` | `send:slack` |
| **DeW / App Preventas** | `catalog_sync_reader`, `order_writer_dew` | `preventa_agent`, `data_agent` | `read:dew_catalog`, `write:dew_orders` |
| **App Sergio** | `sergio_data_reader` | `data_agent` | `read:sergio` |
| **Outline Wiki** | `wiki_reader` | `data_agent`, `employee_agent` | `read:outline` |
| **Element Chat** | `meeting_transcript_reader` | `summary_agent` | `read:element` |

*Esta tabla ilustra los principios del modelo y no es un registro exhaustivo de todas las herramientas futuras.*

---

## Referencias cruzadas

- Campo `permissions` del manifiesto del agente: `docs/platform_es/role.md`
- Campo `required_permissions` de la herramienta: `docs/platform_es/tool.md`
- Pipeline de inyección de capacidades en el harness: `docs/platform_es/harness.md`
- Restricciones en delegaciones jerárquicas: `docs/architecture_es/delegation-policy.md`
- Jerarquía de permisos entregada: `docs/architecture_es/adr-003-permission-hierarchy.md`
