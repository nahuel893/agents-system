# Changelog

## [0.2.0](https://github.com/nahuel893/agents-system/compare/v0.1.0...v0.2.0) (2026-09-25)


### ⚠ BREAKING CHANGES

* **permissions:** booting the app now requires DEPLOY_GRANTS for every configured runtime; see docs/architecture/permission-model.md.
* rename package to agents-system ([#189](https://github.com/nahuel893/agents-system/issues/189))

### Features

* **demo:** documented entrypoint to serve the OpenAI-compatible API over the demo database ([#53](https://github.com/nahuel893/agents-system/issues/53)) ([4843959](https://github.com/nahuel893/agents-system/commit/4843959e15c3831c9ac4fc4eccfa96e8fdb32d2a)), refs [#46](https://github.com/nahuel893/agents-system/issues/46)
* **permissions:** add Permission class hierarchy and registry (permission-model 1/4) ([#43](https://github.com/nahuel893/agents-system/issues/43)) ([7a209e8](https://github.com/nahuel893/agents-system/commit/7a209e879304d5b960844dada06f0463e93e1e67))
* **permissions:** enforce tier rules R2-R4 in ToolSpec, injector and loader (permission-model 2/4) ([#50](https://github.com/nahuel893/agents-system/issues/50)) ([817410c](https://github.com/nahuel893/agents-system/commit/817410cb3b2834b9b960c001fb0ac134f6832435))


### Bug Fixes

* **adapter:** bound POST /v1/chat/completions body size ([#37](https://github.com/nahuel893/agents-system/issues/37)) ([#40](https://github.com/nahuel893/agents-system/issues/40)) ([fe7f53c](https://github.com/nahuel893/agents-system/commit/fe7f53c1ab3416babca8a64414ddf51d63b6fc46))
* **evals:** explicit grants instead of implicit full-role grant ([#58](https://github.com/nahuel893/agents-system/issues/58)) ([91f2daf](https://github.com/nahuel893/agents-system/commit/91f2daf9c6a24bc774212e4f4518664ab707cc28)), refs [#47](https://github.com/nahuel893/agents-system/issues/47)
* **harness:** render escalation_rules into the composed system prompt ([#42](https://github.com/nahuel893/agents-system/issues/42)) ([f02ea0a](https://github.com/nahuel893/agents-system/commit/f02ea0a892bd9679e94dd79cbf3e169289c178b3))
* **permissions:** explicit deploy grants and Layer-2 grant ceiling (permission-model 3/4) ([#55](https://github.com/nahuel893/agents-system/issues/55)) ([a13dacc](https://github.com/nahuel893/agents-system/commit/a13daccdc70889f6ed252a6292aa85306db8bbd4))
* **reports:** expose per-report parameters in the run_report tool schema ([#54](https://github.com/nahuel893/agents-system/issues/54)) ([4b062db](https://github.com/nahuel893/agents-system/commit/4b062db6988a78b5b939d791c962a85174233979)), refs [#52](https://github.com/nahuel893/agents-system/issues/52)
* **reports:** rank top products by units before truncating ([#51](https://github.com/nahuel893/agents-system/issues/51)) ([cdf5d07](https://github.com/nahuel893/agents-system/commit/cdf5d074d6e594bf48f3d21873c5f319fd7810ff)), refs [#44](https://github.com/nahuel893/agents-system/issues/44)
* resolve ruff 0.16 lint findings (2437fcb) switched the drain-failure ([464753c](https://github.com/nahuel893/agents-system/commit/464753ced26d2d0aa8145fa529d01ed34c1e0545))


### Performance Improvements

* **reports:** describe shared run_report parameters once ([#59](https://github.com/nahuel893/agents-system/issues/59)) ([ffba3e0](https://github.com/nahuel893/agents-system/commit/ffba3e0370f5605f835c4b4a2130c6d868da514e)), refs [#56](https://github.com/nahuel893/agents-system/issues/56)


### Documentation

* **architecture:** ADR-005 operational safety and governance roadmap (proposed) ([#60](https://github.com/nahuel893/agents-system/issues/60)) ([f0e4b2b](https://github.com/nahuel893/agents-system/commit/f0e4b2bc13dfb30bbf9d91a4cfa978363c6c5a20))
* **permissions:** ADR-003 permission hierarchy (permission-model 4/4) ([#57](https://github.com/nahuel893/agents-system/issues/57)) ([68852d4](https://github.com/nahuel893/agents-system/commit/68852d450206518574097320035c3af141c77747))
* **reports:** migration for the agents_system_* reporting view rename ([#49](https://github.com/nahuel893/agents-system/issues/49)) ([28360ed](https://github.com/nahuel893/agents-system/commit/28360ed31f649b6574f2a0c21b5ef5f4e3b728de)), refs [#45](https://github.com/nahuel893/agents-system/issues/45)


### Build System

* adopt ruff 0.16 ([#39](https://github.com/nahuel893/agents-system/issues/39)) ([464753c](https://github.com/nahuel893/agents-system/commit/464753ced26d2d0aa8145fa529d01ed34c1e0545))
* rename package to agents-system ([#189](https://github.com/nahuel893/agents-system/issues/189)) ([6402f58](https://github.com/nahuel893/agents-system/commit/6402f581b695900b7abf9fe2dc4167f29ff300b8))


### Continuous Integration

* automate SemVer releases and CHANGELOG with release-please ([#48](https://github.com/nahuel893/agents-system/issues/48)) ([6778fea](https://github.com/nahuel893/agents-system/commit/6778fea7b608e2ab790b81ec450293edb8a1224e))
* cut releases locally with release-please and plain vX.Y.Z tags ([#62](https://github.com/nahuel893/agents-system/issues/62)) ([487ec68](https://github.com/nahuel893/agents-system/commit/487ec687f28db7b8dc59a41bc5a1aaeaa80d1393)), refs [#18](https://github.com/nahuel893/agents-system/issues/18)

## Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are generated from [Conventional Commits](https://www.conventionalcommits.org/)
by [release-please](https://github.com/googleapis/release-please) — see
`docs/operations/release-process.md` for how a release PR is cut and merged.
