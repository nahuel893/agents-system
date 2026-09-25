# Release process

This project versions itself automatically from [Conventional
Commits](https://www.conventionalcommits.org/) using
[release-please](https://github.com/googleapis/release-please), cut locally
via `scripts/release.sh`. Before this (issue #18), `version = "0.1.0"` in
`pyproject.toml` never moved, there were no tags and no releases, and nobody
could say what was actually deployed. This closes that gap: the version, the
tag, and `CHANGELOG.md` are now derived from commit history instead of
hand-edited.

## Cutting a release

A PR opened with the default `GITHUB_TOKEN` does not trigger other workflows,
so the required `ci`, `secret-scan`, and `dependency-audit` checks never run
and branch protection prevents the release PR from merging. Rather than store
a fine-grained Personal Access Token (PAT) or GitHub App token as a
repository secret to work around that, `.github/workflows/release-please.yml`
is disabled (it is kept, `workflow_dispatch`-only, for optional manual use)
and releases are cut locally by a maintainer instead, using `gh`'s own
authentication so the release PR is opened under their account and CI runs
normally.

**Prerequisites:** `gh auth login` with `repo` scope, and Node.js/`npx`
available locally.

1. **Open or update the release PR:**

   ```sh
   scripts/release.sh pr
   ```

   This runs `npx --yes release-please release-pr` with `--token` from
   `gh auth token`, targeting `main`. It keeps exactly one standing release
   PR open/updated, as described below.

2. **Review and merge the release PR** (squash merge). Because it was opened
   under the maintainer's own account, the required checks run like any
   other PR.

3. **Create the tag and GitHub Release:**

   ```sh
   scripts/release.sh tag
   ```

   This runs `npx --yes release-please github-release` with the same
   arguments, creating the `vX.Y.Z` tag and the GitHub Release from the
   merged `CHANGELOG.md` section.

## What drives a version bump

release-please reads the Conventional Commit prefix of every commit merged
to `main` since the last release:

| Prefix | Effect |
| --- | --- |
| `fix:` | Patch bump (`0.1.0` → `0.1.1`); listed under **Bug Fixes**. |
| `feat:` | Minor bump (`0.1.0` → `0.2.0`); listed under **Features**. |
| `feat!:`, `fix!:`, or any type with a trailing `!`, **or** a commit body containing a `BREAKING CHANGE:` footer | Breaking bump according to `bump-minor-pre-major`. |
| `perf:` | No version bump; listed under **Performance Improvements**. |
| `revert:` | No version bump; listed under **Reverts**. |
| `docs:`, `build:`, `refactor:`, `ci:` | No version bump; each is listed in its own **Documentation**, **Build System**, **Code Refactoring**, or **Continuous Integration** section. |
| `chore:`, `style:`, `test:` | No version bump; hidden from the changelog. |

`release-please-config.json` sets `"bump-minor-pre-major": true`. Under
0.x.y, SemVer treats every change as potentially breaking, so a strict
"breaking → major" rule would jump straight to `1.0.0` on the first `feat!:`
— read by downstream consumers as "this project reached a stable public
API", which is not yet true. With this flag, a breaking change before the
first `1.0.0` release bumps **minor** instead (`0.1.0` → `0.2.0`), and only
a manually released `1.0.0` (or a `release-as` override) opens the door to
real major bumps.

The manifest (`.release-please-manifest.json`) holds the version
release-please currently believes is released; `pyproject.toml`'s
`[project].version` is the file it edits to apply a release. A CI check
(`tests/test_release_manifest.py`) fails the build if the two ever diverge,
so a hand-edit to one without the other is caught immediately instead of
surfacing later as a wrong build.

## What a release PR looks like

Running `scripts/release.sh pr` keeps exactly one standing pull request
open, titled something like `chore(main): release 0.2.0`, that:

- bumps `.release-please-manifest.json`, `pyproject.toml`'s `version`, and
  the package's own entry in `uv.lock` (an `extra-files` rule), so the
  lockfile never drifts from the released version;
- writes the accumulated changes to a new dated `## [0.2.0]` heading in
  `CHANGELOG.md`, grouped into the configured sections (such as **Features**,
  **Bug Fixes**, and **Documentation**) and linked to their originating
  commits/PRs.

Re-running `scripts/release.sh pr` after more commits land amends that same
PR in place — it does not open a second one. Nothing is released until a
human merges it.

## What merging the release PR does

Merging it does not create the tag or GitHub Release by itself. Run
`scripts/release.sh tag` afterward: release-please then creates the Git tag
(`vX.Y.Z`, e.g. `v0.2.0`) and a GitHub Release from the merged
`CHANGELOG.md` section, on `main`, from the version now committed there.

## What this does **not** do yet

There is no publish step: nothing builds or uploads to PyPI. The owner
decided not to publish this package yet (#18) — tagging and changelog
generation come first, publishing is a separate future decision. When that
decision is made, it becomes an additional step, not a change to how
versions are decided.

## Looking ahead: library-first agents

The planned "library-first agents" change (predefined agents that ship
inside the library rather than being wired per client) touches the
package's public surface directly. Any change to a predefined agent's
declared tools or permissions is a breaking change to that contract for
every downstream consumer, even if no Python signature changes — it must
ship as `feat!:`/`fix!:` (or a `BREAKING CHANGE:` footer) with a
`CHANGELOG.md` entry describing exactly what tool or permission moved, not
folded into an unrelated commit.
