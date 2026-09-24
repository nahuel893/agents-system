# Proceso de release

Este proyecto versiona automáticamente a partir de [Conventional
Commits](https://www.conventionalcommits.org/) usando
[release-please](https://github.com/googleapis/release-please)
(`.github/workflows/release-please.yml`). Antes de esto (issue #18),
`version = "0.1.0"` en `pyproject.toml` nunca se movía, no había tags ni
releases, y nadie podía decir con certeza qué estaba desplegado. Esto cierra
esa brecha: la versión, el tag y `CHANGELOG.md` ahora se derivan del
historial de commits en lugar de editarse a mano.

## Configuración inicial

Una PR abierta con el `GITHUB_TOKEN` predeterminado no dispara otros
workflows, por lo que los checks requeridos `ci`, `secret-scan` y
`dependency-audit` nunca corren y la protección de rama impide mergear la PR
de release. Creá un Personal Access Token (PAT) fine-grained, o un token de
GitHub App, limitado a este repositorio y con permisos `Contents: read/write`
y `Pull requests: read/write`; después guardalo como el secret de repositorio
`RELEASE_PLEASE_TOKEN`.

Sin ese secret, el fallback `github.token` también requiere habilitar "Allow
GitHub Actions to create and approve pull requests" en la configuración del
repositorio. Los checks requeridos deben dispararse a mano; por ejemplo,
cerrando y reabriendo la PR de release.

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

Cada push a `main` que cambia el estado relevante para el release (`feat`,
`fix`, etc.) vuelve a correr `release-please-action`. Mantiene abierta
exactamente una pull request permanente, titulada algo como
`chore(main): release 0.2.0`, que:

- bumpea `.release-please-manifest.json` y el `version` de `pyproject.toml`;
- escribe los cambios acumulados bajo un nuevo encabezado con fecha
  `## [0.2.0]` en `CHANGELOG.md`, agrupados en las secciones configuradas
  (por ejemplo, **Features**, **Bug Fixes** y **Documentation**) y enlazados
  a sus commits/PRs de origen.

Cada push posterior que califica actualiza esa misma PR en el lugar — no
abre una segunda. Nada se libera hasta que una persona la mergea.

## Qué pasa al mergear la PR de release

Mergearla es el único disparador de un release: release-please crea
entonces el tag de Git (`vX.Y.Z`, p. ej. `v0.2.0`) y un GitHub Release a
partir de la sección de `CHANGELOG.md` mergeada, sobre `main`, desde la
versión ya commiteada ahí.

## Qué todavía NO hace

El workflow no tiene paso de publicación: no construye ni sube nada a PyPI.
El dueño del proyecto decidió no publicar este paquete todavía (#18) — tag y
generación de changelog van primero, publicar es una decisión futura
separada. Cuando esa decisión se tome, se agrega como un paso adicional
condicionado al output `release_created` del job de release-please, no como
un cambio en cómo se deciden las versiones.

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
