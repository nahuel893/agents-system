# Proceso de release

Este proyecto versiona automáticamente a partir de [Conventional
Commits](https://www.conventionalcommits.org/) usando
[release-please](https://github.com/googleapis/release-please), que se corta
localmente con `scripts/release.sh`. Antes de esto (issue #18),
`version = "0.1.0"` en `pyproject.toml` nunca se movía, no había tags ni
releases, y nadie podía decir con certeza qué estaba desplegado. Esto cierra
esa brecha: la versión, el tag y `CHANGELOG.md` ahora se derivan del
historial de commits en lugar de editarse a mano.

## Cómo cortar un release

Una PR abierta con el `GITHUB_TOKEN` predeterminado no dispara otros
workflows, por lo que los checks requeridos `ci`, `secret-scan` y
`dependency-audit` nunca corren y la protección de rama impide mergear la PR
de release. En vez de guardar un Personal Access Token (PAT) fine-grained o
un token de GitHub App como secret del repositorio para evitar eso,
`.github/workflows/release-please.yml` está deshabilitado (se mantiene, solo
con `workflow_dispatch`, para un uso manual opcional) y los releases se
cortan localmente, a cargo de alguien del equipo, usando la autenticación
propia de `gh` para que la PR de release quede abierta bajo su cuenta y los
checks corran con normalidad.

**Prerrequisitos:** `gh auth login` con scope `repo`, y Node.js/`npx`
disponibles localmente.

1. **Abrí o actualizá la PR de release:**

   ```sh
   scripts/release.sh pr
   ```

   Esto corre `npx --yes release-please release-pr` con `--token` sacado de
   `gh auth token`, apuntando a `main`. Mantiene abierta o actualizada
   exactamente una PR de release permanente, como se describe más abajo.

2. **Pasá la compuerta de release sobre la rama del release y después mergeá
   la PR de release** (squash merge). Como quedó abierta bajo tu propia
   cuenta, los checks requeridos corren como en cualquier otra PR. Antes de
   mergear se tienen que cumplir las tres condiciones, y el cuerpo de la PR de
   release registra sus resultados:

   1. La suite completa en vivo pasa con el modelo de referencia
      (`openai/gpt-6-sol` a través de `EVAL_PROVIDER=openai_compatible`;
      el entorno exacto está en la Definición de terminado de `AGENTS.md`):

      ```sh
      scripts/run-live-suite.sh
      ```

      El script agrupa sus corridas en `mechanism` (offline, sin modelo),
      `behavior` y `composition`, y su resumen da el resultado y el costo de
      cada grupo. Poné ese resumen en el cuerpo de la PR de release. Fija
      `MODEL_PRICES` para el modelo de referencia si no lo hiciste vos, así
      que el costo nunca es nulo.

   2. El smoke test del wheel instalado pasa (ver [Smoke test del wheel como
      biblioteca](#smoke-test-del-wheel-como-biblioteca)):

      ```sh
      scripts/smoke-installed-wheel.sh
      ```

   3. `docs/platform/features.md` y su gemelo en español están al día: las
      filas marcadas "llega en el próximo release" pasan a esta versión, y la
      línea del último release la nombra.

   El cuerpo de la PR de release también lleva la **matriz de capacidades**:
   una tabla para el modelo de referencia (`openai/gpt-6-sol`) y los dos
   modelos de segunda opinión (`google/gemini-3.8-flash` y
   `deepseek/deepseek-v4-flash`), generada con
   `scripts/run-capability-matrix.py`, `EVAL_RUNS=3` y `MODEL_PRICES` definido
   para los tres, de modo que el costo nunca quede en `n/a`. Es información
   para quien lee, no una cuarta compuerta: cada celda es una tasa de un modelo
   con el prompt genérico del kit y un fixture mínimo, no un veredicto sobre
   ningún agente. Ver [Matriz de capacidades por
   modelo](../platform_es/live-eval.md#matriz-de-capacidades-por-modelo-issue-228).

3. **Creá el tag y el GitHub Release:**

   ```sh
   scripts/release.sh tag
   ```

   Esto corre `npx --yes release-please github-release` con los mismos
   argumentos, creando el tag `vX.Y.Z` y el GitHub Release a partir de la
   sección mergeada de `CHANGELOG.md`.

4. **Instalá la versión desde su tag** en un proyecto limpio, fuera del
   repositorio, con el núcleo más cada extra que tocó la versión, y armá un
   agente con ella:

   ```sh
   uv add "agents-system[<extras>] @ git+https://github.com/nahuel893/agents-system@vX.Y.Z"
   ```

## Smoke test del wheel como biblioteca

La suite offline importa `agents_system` desde el árbol de código fuente, con
el manejo de `.env` del repositorio y los fixtures de test alrededor. Nada de
eso es lo que recibe quien lo importa. `scripts/smoke-installed-wheel.sh`
verifica lo que sí recibe (issue #166):

1. Construye el wheel (`uv build --wheel`) en un directorio descartable fuera
   del repositorio.
2. Lo instala ahí en un virtualenv nuevo, con las dependencias de terceros
   fijadas a los pins de `uv.lock`. Es una instalación simple, sin ningún
   extra: solo el núcleo (issue #188). El script falla si se puede importar
   algo que provee un extra (`agents_system.extras.EXTRA_MODULES`), y registra
   el tamaño del virtualenv.
3. Corre el ejemplo de biblioteca del README (el bloque marcado con
   `<!-- runnable-example: library -->`) y el Inicio rápido de
   `docs/platform_es/library-usage.md` (`<!-- runnable-example: quickstart -->`),
   cada uno en su propio proceso con un entorno **vacío** (`env -i`: sin
   `.env`, sin variables del servidor, ni siquiera `PATH` o `HOME`), pasándole
   un modelo de chat guionado al `main(model)` del ejemplo.
4. Falla salvo que cada turno se complete (una llamada a una tool y después
   la respuesta), el paquete se haya importado desde el virtualenv
   descartable, no se haya impreso ningún traceback, y la falta de
   `AuditSink` se haya reportado exactamente una vez por proceso
   (`audit.sink_not_registered`).
5. Borra el directorio descartable al salir, con éxito o con error.

Solo necesita `uv` y no hace ninguna llamada a un proveedor. Corrélo antes de
mergear una PR de release, y después de cualquier cambio en la API pública, en
el empaquetado (los targets de wheel de `pyproject.toml`) o en el ejemplo del
README. `tests/test_readme_library_example.py` corre el mismo ejemplo offline
en cada corrida de `pytest`; este script es la verificación de que el paquete
*construido e instalado* se comporta igual.

## Qué determina un bump de versión

release-please lee el prefijo Conventional Commit de cada commit
mergeado a `main` desde el último release:

| Prefijo | Efecto |
| --- | --- |
| `fix:` | Bump de patch (`0.1.0` → `0.1.1`); queda bajo **Bug Fixes**. |
| `feat:` | Bump de minor (`0.1.0` → `0.2.0`); queda bajo **Features**. |
| `feat!:`, `fix!:`, o cualquier tipo con `!` al final, **o** un cuerpo de commit con un footer `BREAKING CHANGE:` | Bump disruptivo según `bump-minor-pre-major`. |
| `perf:` | Sin bump de versión; queda bajo **Performance Improvements**. |
| `revert:` | Sin bump de versión; queda bajo **Reverts**. |
| `docs:`, `build:`, `refactor:`, `ci:` | Sin bump de versión; cada uno queda en su propia sección: **Documentation**, **Build System**, **Code Refactoring** o **Continuous Integration**. |
| `chore:`, `style:`, `test:` | Sin bump de versión; no queda listado en el changelog. |

`release-please-config.json` define `"bump-minor-pre-major": true`. Bajo
0.x.y, SemVer trata todo cambio como potencialmente disruptivo, así que una
regla estricta de "breaking → major" saltaría directamente a `1.0.0` en el
primer `feat!:` — algo que un consumidor downstream leería como "este
proyecto alcanzó una API pública estable", lo cual todavía no es cierto. Con
este flag, un cambio disruptivo antes del primer release `1.0.0` bumpea
**minor** en su lugar (`0.1.0` → `0.2.0`), y solo un `1.0.0` liberado
manualmente (o un override `release-as`) habilita los bumps de major reales.

El manifest (`.release-please-manifest.json`) guarda la versión que
release-please cree actualmente liberada; `[project].version` en
`pyproject.toml` es el archivo que edita para aplicar un release. Un check
de CI (`tests/test_release_manifest.py`) hace fallar el build si ambos
divergen, así que una edición manual de uno sin el otro se detecta de
inmediato en vez de aparecer más tarde como un build incorrecto. El mismo
test también compara la entrada propia de `agents-system` en `uv.lock`
contra `pyproject.toml` (ver más abajo).

## Cómo es una PR de release

Correr `scripts/release.sh pr` mantiene abierta exactamente una pull
request permanente, titulada algo como `chore(main): release 0.2.0`, que:

- bumpea `.release-please-manifest.json`, el `version` de `pyproject.toml` y
  la entrada del propio paquete en `uv.lock` (una regla `extra-files` con un
  `jsonpath` TOML), así el lockfile nunca queda desfasado de la versión
  publicada. Ese `jsonpath` tiene que ser
  `$.package[?(@.name.value == 'agents-system')].version`, no
  `$.package[?(@.name == 'agents-system')].version` — el updater TOML de
  release-please parsea con un parser que etiqueta cada valor escalar
  (strings, números) envolviéndolo en un objeto `{start, end, value}` para
  poder reemplazarlo sin tocar el resto del archivo, así que un filtro que
  compara `@.name` directamente contra un string nunca matchea; comparar
  `@.name.value` sí llega al string real (issue #150).
  `tests/test_release_manifest.py` es un test de `pytest` puramente offline
  (sin red, sin depender de CI) que falla si la entrada de `uv.lock` y
  `pyproject.toml` alguna vez divergen, así que una regresión de este mismo
  jsonpath se detecta apenas corre `pytest -q`, en local o en CI;
- escribe los cambios acumulados bajo un nuevo encabezado con fecha
  `## [0.2.0]` en `CHANGELOG.md`, agrupados en las secciones configuradas
  (por ejemplo, **Features**, **Bug Fixes** y **Documentation**) y enlazados
  a sus commits/PRs de origen.

Volver a correr `scripts/release.sh pr` cuando entran más commits actualiza
esa misma PR en el lugar — no abre una segunda. Nada se libera hasta que una
persona la mergea.

## Qué pasa al mergear la PR de release

Mergearla no crea el tag ni el GitHub Release por sí sola. Después hay que
correr `scripts/release.sh tag`: recién ahí release-please crea el tag de
Git (`vX.Y.Z`, p. ej. `v0.2.0`) y un GitHub Release a partir de la sección
de `CHANGELOG.md` mergeada, sobre `main`, desde la versión ya commiteada
ahí.

## Qué todavía NO hace

No hay paso de publicación: no se construye ni se sube nada a PyPI. El
dueño del proyecto decidió no publicar este paquete todavía (#18) — tag y
generación de changelog van primero, publicar es una decisión futura
separada. Cuando esa decisión se tome, se agrega como un paso adicional, no
como un cambio en cómo se deciden las versiones.

## Mirando adelante: library-first agents

El cambio planeado "library-first agents" (agentes predefinidos que se
envían dentro de la librería en lugar de cablearse por cliente) toca
directamente la superficie pública del paquete. Cualquier cambio a las
herramientas o permisos declarados de un agente predefinido es un cambio
disruptivo a ese contrato para cada consumidor downstream, incluso si
ninguna firma de Python cambia — debe salir como `feat!:`/`fix!:` (o con un
footer `BREAKING CHANGE:`) con una entrada en `CHANGELOG.md` que describa
exactamente qué herramienta o permiso se movió, no mezclado dentro de un
commit sin relación.
