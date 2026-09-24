# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are generated from [Conventional Commits](https://www.conventionalcommits.org/)
by [release-please](https://github.com/googleapis/release-please) — see
`docs/operations/release-process.md` for how a release PR is cut and merged.

## [Unreleased]

### Changed

- **BREAKING:** Renamed the installed Python package from `agentsys` to
  `agents_system` to match the repository name (`build!: rename package to
  agents-system`, #189).
- Adopted ruff 0.16 (#39).
- Dropped references to purged files and smoothed generic client wording in
  the docs (#6).

### Fixed

- Bounded the request body size of `POST /v1/chat/completions` before
  processing it (#37, #40).
- Rendered `escalation_rules` into the composed system prompt, which the
  harness previously dropped (#42).
