# Plan de ejecución de ADR-002

Cómo se construye el trabajo registrado en
[ADR-002](../architecture_es/adr-002-agent-model-and-capabilities.md): en qué
orden, en qué porciones y qué significa "terminado" para cada una.

Este documento es el *plan*. El *contrato* de cada porción es su issue de
GitHub, y las reglas generales (Definition of Ready, TDD estricto, revisión,
Definition of Done) viven en
[engineering-workflow.md](../delivery/engineering-workflow.md). Si este plan y
ese estándar se contradicen, gana el estándar y este plan está mal.

## Principios

- **La issue es la especificación.** Cada ítem de ADR-002 corresponde a una
  issue con la etiqueta `adr-002`. La issue declara el problema, los criterios
  de aceptación, los tests y las dependencias. No se empieza una issue que no
  cumple la Definition of Ready; primero se corrige la issue.
- **SDD solo donde la ambigüedad es real.** La ola 3 (modelo de agente y
  persistencia) pasa por el ciclo SDD completo, porque identidad, aislamiento
  de memoria e historial durable cambian contratos de los que dependen varios
  componentes y todavía no hay acuerdo sobre su forma. Las demás olas van por
  la ruta directa: la sección del ADR más el cuerpo de la issue ya son la
  especificación.
- **Seguridad antes que evaluación, evaluación antes que el modelo de
  agente.** Evaluar en vivo agentes cuyas herramientas todavía no tienen
  límites mide lo equivocado; un modelo de agente nuevo construido antes de
  que exista la evaluación no se puede demostrar mejor.

## Olas

Cada ola tiene una issue épica que lista sus hijas en orden de ejecución.

| Ola | Objetivo | Ítems | Ruta |
|---|---|---|---|
| 0 — Higiene (#125) | Hacer confiable el CI y quitar lo que confunde | H.31 → H.27 → H.28, F.19–21, B.8–9, H.34 (configuración) | Directa |
| 1 — Seguridad (#126) | Límites deterministas sobre lo que un agente puede hacer | C.11 → C.10 → C.12 → C.14, C.11 → C.13, C.15, D.17 | Directa, revisión independiente obligatoria |
| 2 — Evaluación en vivo (#127) | Probar de punta a punta los agentes de la plataforma | E.18 | Directa |
| 3 — Modelo de agente (#128) | Identidad, memoria y persistencia durable | A.1–A.6, G.22–G.26 | SDD completo |

H.29–30 y H.32–33 no dependen de ninguna ola y se intercalan donde haya un
escritor libre.

### Reglas de orden dentro de cada ola

- **Ola 0.** Primero H.31: reduce a la mitad el tiempo de CI de todo lo que
  sigue. B.8–9 tiene que entrar antes de cualquier evaluación en vivo, porque
  hoy la prosa de diseño del rol se envía al modelo como su system prompt.
- **Ola 1.** C.11 (`untrusted_input` y su invariante contra `exec:*`) es la
  raíz: los niveles (C.10) y el control de canales (C.13) se apoyan en ella,
  los `command_tools` declarativos (C.12) se apoyan en los niveles, y el
  sandbox con bubblewrap (C.14) envuelve a los `command_tools`. C.15 (backends
  de referencia) es requisito de la ola 2.
- **Ola 3.** Memoria de trabajo e historial de chat durables (A.4 + G.22) van
  primero, y tienen que reutilizar la decisión de Postgres como fuente de
  verdad tomada para el outbox del webhook (#43, #44, #46) o apartarse de ella
  de forma explícita en el diseño. La identidad (A.2, #53) precede a la
  memoria propia del agente (G.25); los tipos de memoria (A.3) preceden a la
  aplicación de `memory_policy` (A.5).

## Porciones

- **Una issue, una rama, un worktree, un pull request.** El nombre de la rama
  lleva el número de issue (`feat/NN-nombre-corto`). Los worktrees viven bajo
  el directorio home, nunca bajo `/tmp`.
- **Menos de ~400 líneas cambiadas por pull request.** Una issue que no entra
  se divide antes de empezar, no durante la revisión.
- **Sin pull requests apilados.** Cada rama parte de `origin/main`. Si apilar
  es inevitable, el pull request de arriba se redirige a `main` *antes* de
  mergear el de abajo; si no, el squash merge de la rama de abajo deja
  varada a la de arriba.
- **Trabajo en paralelo solo sobre archivos disjuntos.** Como máximo dos o
  tres escritores a la vez, cada uno en su worktree, y nunca dos escritores
  sobre el mismo módulo.

## Roles

| Rol | Quién | Responsabilidad |
|---|---|---|
| Orquestador | Sesión principal | Elige la siguiente issue, verifica que esté Ready, delega, verifica, mergea |
| Escritor | Subagente Sonnet, uno por issue | Rojo → verde → refactor, documentación, abre el pull request |
| Proponente / diseñador | Subagente Opus | Solo ola 3: propose y design de SDD |
| Revisor | Agente nuevo, distinto del escritor | Revisión adversarial antes del merge en pull requests de alto riesgo |

El escritor nunca revisa su propio trabajo. Un pull request es **de alto
riesgo** cuando toca un límite de permisos, el interceptor de herramientas, la
persistencia, la entrega de mensajes o las compuertas de CI: en la práctica,
todos los de la ola 1 y la mayoría de la ola 3.

## Definition of Done de una porción de ADR-002

Además de la Definition of Done de
[engineering-workflow.md](../delivery/engineering-workflow.md#definition-of-done):

- [ ] Se escribió primero un test que falla y se lo vio fallar (TDD estricto);
      un test de seguridad demuestra que falla si se quita la protección que
      cubre
- [ ] Documentación actualizada en inglés **y** en español en el mismo pull
      request
- [ ] La fila del ítem en la tabla resumen de ADR-002 pasa a ✅ en ambos
      idiomas
- [ ] El cuerpo del pull request contiene `Closes #NN`
- [ ] CI en verde, incluidos los jobs de integración que el cambio toca
- [ ] Alto riesgo: corrió una revisión independiente y sus hallazgos están
      resueltos o tienen su propia issue

## Seguimiento

La tabla resumen de ADR-002 lleva el número de issue de cada ítem en su
columna *Etapa planificada*. Las épicas llevan el orden de ejecución. Si las dos no
coinciden, se actualiza la épica; el ADR registra decisiones, no la agenda.
