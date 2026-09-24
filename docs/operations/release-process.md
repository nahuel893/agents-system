# Release process

This project versions itself automatically from [Conventional
Commits](https://www.conventionalcommits.org/) using
[release-please](https://github.com/googleapis/release-please)
(`.github/workflows/release-please.yml`). Before this (issue #18), `version
= "0.1.0"` in `pyproject.toml` never moved, there were no tags and no
releases, and nobody could say what was actually deployed. This closes that
gap: the version, the tag, and `CHANGELOG.md` are now derived from commit
history instead of hand-edited.

## What drives a version bump

release-please reads the Conventional Commit prefix of every commit merged
to `main` since the last release:

| Prefix | Effect |
| --- | --- |
| `fix:` | Patch bump (`0.1.0` → `0.1.1`). |
| `feat:` | Minor bump (`0.1.0` → `0.2.0`). |
| `feat!:`, `fix!:`, or any type with a trailing `!`, **or** a commit body containing a `BREAKING CHANGE:` footer | Breaking change. |
| `build:`, `chore:`, `docs:`, `refactor:`, `style:`, `test:`, `ci:` | No version bump; still listed in the changelog. |

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

Every push to `main` that changes the release-relevant version, `feat`, or
`fix` state re-runs `release-please-action`. It keeps exactly one standing
pull request open, titled something like `chore(main): release 0.2.0`, that:

- bumps `.release-please-manifest.json` and `pyproject.toml`'s `version`;
- moves the accumulated `## [Unreleased]` entries in `CHANGELOG.md` under a
  new dated `## [0.2.0]` heading, grouped the same way (`Changed`, `Fixed`,
  `Added`, …), each entry linking back to its originating commit/PR.

Each subsequent qualifying push amends that same PR in place — it does not
open a second one. Nothing is released until a human merges it.

## What merging the release PR does

Merging it is the only trigger for a release: release-please then creates
the Git tag (`vX.Y.Z`, e.g. `v0.2.0`) and a GitHub Release from the merged
`CHANGELOG.md` section, on `main`, from the version now committed there.

## What this does **not** do yet

The workflow has no publish step: it does not build or upload anything to
PyPI. The owner decided not to publish this package yet (#18) — tagging and
changelog generation come first, publishing is a separate future decision.
When that decision is made, it becomes an additional step gated on the
release-please job's `release_created` output, not a change to how versions
are decided.

## Looking ahead: library-first agents

The planned "library-first agents" change (predefined agents that ship
inside the library rather than being wired per client) touches the
package's public surface directly. Any change to a predefined agent's
declared tools or permissions is a breaking change to that contract for
every downstream consumer, even if no Python signature changes — it must
ship as `feat!:`/`fix!:` (or a `BREAKING CHANGE:` footer) with a
`CHANGELOG.md` entry describing exactly what tool or permission moved, not
folded into an unrelated commit.
