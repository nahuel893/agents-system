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

2. **Revisá y mergeá la PR de release** (squash merge). Como quedó abierta
   bajo tu propia cuenta, los checks requeridos corren como en cualquier
   otra PR.

3. **Creá el tag y el GitHub Release:**

   ```sh
   scripts/release.sh tag
   ```

   Esto corre `npx --yes release-please github-release` con los mismos
   argumentos, creando el tag `vX.Y.Z` y el GitHub Release a partir de la
   sección mergeada de `CHANGELOG.md`.

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
inmediato en vez de aparecer más tarde como un build incorrecto.

## Cómo es una PR de release

Correr `scripts/release.sh pr` mantiene abierta exactamente una pull
request permanente, titulada algo como `chore(main): release 0.2.0`, que:

- bumpea `.release-please-manifest.json` y el `version` de `pyproject.toml`;
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
