# Control del tamaño de contexto

Cada llamada al modelo lleva el prompt de sistema, los esquemas de las
herramientas y el historial de la conversación. Antes del issue #28 el
historial se enviaba completo en cada llamada, por largo que se hubiera puesto.
Una conversación suficientemente larga excedería la ventana de contexto del
modelo y el proveedor respondería con un error duro, de modo que el agente
dejaba de funcionar justo cuando la conversación más importaba.

Ahora el runtime acota la solicitud. Es el ítem G.24 del ADR-002
(`docs/architecture_es/adr-002-agent-model-and-capabilities.md`).

## La decisión: primero recorte, la compactación después

El ADR-002 dejó abierto el mecanismo. La decisión, registrada aquí y en el
ADR: **recortar, no resumir**, como primer mecanismo.

| | Recorte | Compactación (resumir turnos antiguos) |
|---|---|---|
| Costo | ninguno | una llamada extra al proveedor por compactación |
| Modo de fallo | se olvidan los turnos antiguos | los turnos antiguos se parafrasean, y un resumen puede afirmar algo que nadie dijo |
| Determinismo | totalmente determinista, se prueba offline | depende de un modelo, así que requiere evaluación en vivo |
| Datos disponibles hoy | alcanzan para elegir un umbral seguro | aún no hay evidencia de que el recorte pierda algo que importe |

La compactación queda como seguimiento. Debe construirse cuando haya
evidencia, en conversaciones reales, de que el recorte descarta contexto que
un agente necesitaba.

## Qué hace

`_call_model` (`agent/graph.py`) arma la entrada del modelo en este orden:
repara las llamadas a herramientas sin respuesta (#181), recorta al
presupuesto y antepone el prompt de sistema. `agent/context.py:trim_history`
decide qué se descarta. Las reglas, por prioridad:

1. El prompt de sistema y los esquemas de las herramientas se envían siempre.
2. El último mensaje del usuario se conserva siempre.
3. Un mensaje del asistente con llamadas a herramientas y sus respuestas son
   una sola unidad, que se conserva entera o se descarta entera. Una llamada
   sin respuesta, o una respuesta sin su llamada, la rechazan los proveedores
   (#181).
4. Del resto, ganan los mensajes más nuevos. El historial que queda es un
   sufijo contiguo y empieza en un mensaje del usuario, porque algunos
   proveedores rechazan una conversación que abre con un turno del asistente.

El recorte sólo da forma a la **proyección de la entrada del modelo**.
`state["messages"]` nunca se reescribe, así que un hilo con checkpointer
conserva su historial completo y los mensajes que devuelve un turno no cambian.

Un historial que cabe en el presupuesto se envía sin modificar: no se descarta
ningún mensaje y no se registra nada.

Si las reglas 1 a 3 solas ya exceden el presupuesto (por ejemplo, un único
mensaje de usuario enorme), la entrada se envía por encima del presupuesto y
se marca `over_budget`. Descartar la pregunta del usuario no respondería nada,
y el error del propio proveedor es entonces el fallo honesto.

Durante un bucle largo de herramientas se aplican las mismas reglas dentro del
turno actual: se conservan las rondas de herramientas más nuevas, se descartan
las más antiguas y el mensaje del usuario permanece.

## Configuración

El presupuesto es `execution_limits.max_context_tokens`, por agente, en el
`policy.md` del rol, un manifiesto de carpeta, un parámetro de `Agent(...)` o
una sobreescritura de despliegue:

```yaml
execution_limits:
  max_context_tokens: 24000   # un modelo local pequeño
```

Sigue las reglas de los demás límites de ejecución:

- Un valor ausente o `null` significa el valor por defecto de la plataforma.
- Una definición puede bajarlo y nunca subirlo por encima del valor de su
  padre, ni por encima del valor por defecto de la plataforma cuando el padre
  no fija ninguno (`docs/platform_es/role.md`).
- Tiene que ser un número finito y no negativo. `0` está permitido y conserva
  sólo las partes fijas y el último mensaje del usuario.

El presupuesto cuenta la solicitud completa: prompt de sistema, esquemas de
herramientas e historial. No cuenta la respuesta del modelo.

### El valor por defecto

`DEFAULT_MAX_CONTEXT_TOKENS` (`agent/context.py`) es **64.000**, derivado de la
ventana de contexto del modelo:

```
ASSUMED_CONTEXT_WINDOW_TOKENS (128.000) x CONTEXT_BUDGET_FRACTION (0,5)
```

- **128.000** es la menor ventana entre los modelos alojados con los que corre
  esta plataforma, no la del modelo de referencia. Un rol con un modelo de
  ventana mayor igual recibe un presupuesto seguro.
- **0,5** es margen, no un número afinado. El conteo de tokens es una
  estimación (unos cuatro caracteres por token,
  `count_tokens_approximately` de `langchain_core`) y ningún tokenizador cubre
  a todos los proveedores configurados. El texto en español y la salida JSON de
  las herramientas son más densos que la prosa en inglés. El resto de la
  ventana además hace falta para la respuesta.

Un agente con un modelo más chico (uno local de 8k o 32k) debe bajar
`max_context_tokens` por su cuenta: la plataforma no puede ver qué ventana
tiene un modelo configurado.

## Observabilidad

Una llamada recortada nunca es silenciosa:

- Una advertencia `runtime.context_trimmed` con `dropped_messages`,
  `kept_messages`, `tokens_before`, `tokens_after`, `max_context_tokens` y
  `over_budget`. Sin texto de mensajes.
- Un evento de auditoría `context_trimmed` con los mismos conteos
  (`docs/platform_es/audit.md`).
- Una muestra de `agent_limit_trips_total{limit="max_context_tokens"}`
  (`docs/platform_es/observability.md`).

Una llamada al modelo que se repite dentro de un turno (bucle de herramientas)
informa cada vez que recorta.

## Línea base: qué dicen los datos medidos

**Todavía no hay tráfico de producción**, así que esto no son datos de
producción. Se midió con lo que existe: los resultados de la evaluación en
vivo en `evals/results/` (297 corridas evaluadas en 33 escenarios) y las
líneas de log `runtime.turn_usage` guardadas en `.pi/ops/logs/` (238 turnos
distintos, casi todos con el modelo de referencia y otro modelo alojado).

| Medida | Mediana | p90 | Máx. |
|---|---|---|---|
| Tokens que consumió un turno completo, entrada más salida (`evals/results/`, 297 corridas) | 4.434 | 8.486 | 14.804 |
| Tokens de entrada sumados sobre las llamadas de un turno (logs, 238 turnos) | 3.982 | 8.184 | 23.097 |
| Tokens de entrada de una llamada, promediados sobre su turno (logs) | 1.552 | 2.052 | 2.647 |
| Llamadas al modelo en un turno (logs) | 3 | n/d | 9 |

Todos los escenarios son de un solo turno de usuario (mediana de 62
caracteres, máximo 211), así que nada de esto mide una conversación de varios
turnos. Lo que los datos sí dicen:

- Una llamada al modelo en estas corridas lleva entre 1,5k y 2,6k tokens, y la
  mayor parte es la parte fija (prompt de sistema y esquemas de herramientas),
  no conversación.
- El presupuesto por defecto, 64.000, es unas 24 veces la mayor solicitud
  individual medida. No puede recortar nada del tráfico medido, así que no
  cambia nada en los escenarios de la suite.

Lo que los datos **no** dicen es cuán rápido crece una conversación real ni
qué presupuesto necesitan las conversaciones reales. Por eso el valor por
defecto se deriva de la ventana del modelo (arriba) y no de esta línea base, y
esta línea base sólo verifica que el valor por defecto está lejos del uso
ordinario. Cuando un canal tenga tráfico real, hay que volver a medir con la
advertencia `runtime.context_trimmed` y el contador `agent_limit_trips_total`:
un presupuesto que recorta seguido es chico para las conversaciones, uno que
nunca recorta es correcto o demasiado holgado.

## Fuera de alcance (seguimientos)

- **Compactación / resumen** de los turnos recortados (ver la decisión arriba).
- **Crecimiento persistido.** El recorte acota lo que ve el modelo, no lo que
  guarda el checkpointer. Un hilo muy activo sigue creciendo en Redis hasta que
  vence su ventana de retención. Acotar el almacenamiento es otro tema (#24).
- **Una consulta de ventana por modelo** para derivar el presupuesto del modelo
  configurado automáticamente, en lugar de la ventana asumida fija.
- **Conteo exacto de tokens** por proveedor.
