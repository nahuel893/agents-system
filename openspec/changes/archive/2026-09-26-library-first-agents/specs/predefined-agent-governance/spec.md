# Predefined Agent Governance Specification

## Purpose

Once importer-defined agents can extend a predefined role, the eight
packaged roles' own tool/permission surface becomes a shared safety
baseline other agents build on. This capability (Guard 1) makes any
change to a predefined role's tools or permissions a deliberate, reviewed,
versioned event: a change that widens or otherwise alters what a predefined
role may do MUST be recorded as a breaking change, carry a MAJOR bump of
that role's own `version:` field, and gain a `CHANGELOG.md` entry — enforced
by an automated contract check, not by review discipline alone.

This capability is independent of and does not re-implement Guard 2
(explicit deploy-time grants, no auto-grant), which is already delivered by
the permission-model change and specified in
`openspec/specs/permission-hierarchy/spec.md`.

Guard 1's "what counts as released" reference mechanism (the exact form of
the versioned snapshot) is intentionally left to the design phase per the
proposal's own note (exploration Q6); the requirements below constrain the
observable behavior the mechanism must produce, not its storage format.

## Requirements

### Requirement: A versioned, independent snapshot of every predefined role's surface exists

The system MUST maintain a versioned, checked-in record of every predefined
role's `tools` and `permissions` surface, written and maintained
independently of the role manifests themselves — extending the existing
`tests/platform_role_contract.py` `EXPECTED_ROLE_TOOLS` "deliberate,
reviewed, independent expectation" pattern rather than deriving the
expectation from whatever the manifest currently resolves to. A change to a
manifest MUST NOT, by itself, update this snapshot.

#### Scenario: The snapshot does not grade a role against itself

- GIVEN a predefined role's manifest is edited to add a new tool, with no corresponding edit to the versioned snapshot
- WHEN the snapshot is compared against the resolved manifest
- THEN the comparison finds a divergence — the snapshot is never re-derived from the manifest it is checking

#### Scenario: An unmodified predefined role matches its snapshot

- GIVEN a predefined role whose manifest has not changed since the snapshot was last updated
- WHEN the snapshot is compared against the resolved manifest
- THEN no divergence is found

### Requirement: A contract check fails on any tools/permissions divergence lacking both a version bump and a CHANGELOG entry

A contract check MUST compare each predefined role's currently resolved
`tools`/`permissions` surface against its versioned snapshot. When a
divergence is found, the check MUST fail UNLESS both of the following hold:
(a) the role's own `version:` frontmatter reflects a MAJOR SemVer bump
relative to the version recorded for that role in the snapshot, and (b)
`CHANGELOG.md` gained an entry documenting the change for the same PR/change.
Satisfying only one of (a) or (b) MUST still fail the check.

#### Scenario: A tools change with a version bump but no CHANGELOG entry still fails

- GIVEN a predefined role's `tools` list changed, and its `version:` field took a MAJOR bump, but `CHANGELOG.md` gained no corresponding entry
- WHEN the contract check runs
- THEN it MUST fail, naming the role and stating that the CHANGELOG entry is missing

#### Scenario: A tools change with a CHANGELOG entry but no version bump still fails

- GIVEN a predefined role's `permissions` list changed and `CHANGELOG.md` gained an entry for it, but the role's `version:` field is unchanged (or bumped by less than MAJOR)
- WHEN the contract check runs
- THEN it MUST fail, naming the role and stating that the required MAJOR version bump is missing

#### Scenario: A tools change with both a version bump and a CHANGELOG entry passes

- GIVEN a predefined role's `tools` list changed, its `version:` field took a MAJOR bump, and `CHANGELOG.md` gained a corresponding entry
- WHEN the contract check runs
- THEN it passes, and the snapshot is updated to record the new surface and version as the new baseline

#### Scenario: A non-tools, non-permissions manifest edit does not trigger the guard

- GIVEN a predefined role's `role.md` prose is edited (no change to `tools` or `permissions`)
- WHEN the contract check runs
- THEN it MUST NOT require a version bump or a CHANGELOG entry on this account — Guard 1 constrains only `tools`/`permissions` surface changes

### Requirement: The contract check names the exact divergence

When the contract check fails, its failure output MUST name the affected
role, the specific tools and/or permissions that diverged from the snapshot
(added, removed, or changed), and which of the two required conditions
(version bump, CHANGELOG entry) is unmet.

#### Scenario: Failure output is actionable

- GIVEN a predefined role's `tools` list gained one tool with neither a version bump nor a CHANGELOG entry
- WHEN the contract check fails
- THEN its output names the role, the added tool, and states both the missing version bump and the missing CHANGELOG entry

### Requirement: Guard 1 applies only to roles discovered under platform_root/roles

The contract check's discovery of "predefined roles to check" MUST reuse the
same scoped discovery mechanism the `agent-definition-locator` capability
requires ("Predefined-role discovery stays scoped to the platform-role
tree"). Importer-defined agents — whether folder, inline, or extending a
predefined role — MUST NOT be subject to Guard 1's version-bump/CHANGELOG
requirement, regardless of how similar their tool surface is to a predefined
role's.

#### Scenario: An importer agent extending a predefined role changing its own added tools triggers no Guard 1 obligation

- GIVEN an importer-defined agent extends `sales-agent` and later adds a new tool of its own on top of the inherited surface
- WHEN the contract check runs
- THEN it evaluates only the eight predefined roles under `platform_root/roles`; the importer agent's own change is out of Guard 1's scope entirely

#### Scenario: Discovery used by Guard 1 matches discovery used by boot guards

- GIVEN the predefined-role discovery mechanism defined in `agent-definition-locator`
- WHEN Guard 1's contract check enumerates roles to compare against the snapshot
- THEN it enumerates exactly the same set discovery returns elsewhere in the system — no separate, potentially drifting discovery logic

### Requirement: Guard 1's per-role version is independent of the package's own SemVer

A predefined role's `version:` frontmatter bump (required by Guard 1) is a
separate space from the installable package's own release version. The
package's own breaking changes for this proposal (see Backward
Compatibility & Migration) ship with a conventional-commit `BREAKING
CHANGE:` footer, which — per the already-configured
`release-please-config.json` (`bump-minor-pre-major: true`) — bumps the
package's MINOR version while it remains pre-1.0. This package-level bump
MUST NOT be treated as satisfying, or as a prerequisite for, Guard 1's
independent per-role MAJOR version-bump requirement.

#### Scenario: A package-level breaking-change release does not itself satisfy Guard 1

- GIVEN a PR ships a `BREAKING CHANGE:` conventional-commit footer for this proposal's own registration-scheme change, and no predefined role's `tools`/`permissions` changed in that PR
- WHEN the contract check runs
- THEN it passes trivially (no tools/permissions divergence exists) — the package-level breaking-change marker plays no role in this evaluation either way

#### Scenario: A predefined-role tools change still needs its own role-level MAJOR bump even in a MINOR package release

- GIVEN the package's own release stays a MINOR bump under `bump-minor-pre-major`, and within that same release a predefined role's `tools` changed
- WHEN the contract check runs
- THEN it still requires that role's own `version:` field to show a MAJOR bump, independent of the package's MINOR release number

### Requirement: CHANGELOG.md exists and is the recorded location for Guard 1 entries

The system MUST establish `CHANGELOG.md` at the repository root (none exists
prior to this change) as the location the contract check reads to satisfy
condition (b) above. Its format MUST be compatible with
`release-please-config.json`'s configured `changelog-path`.

#### Scenario: CHANGELOG.md exists after this capability ships

- GIVEN the repository after this change ships
- WHEN the repository root is inspected
- THEN `CHANGELOG.md` exists, and it is the file `release-please-config.json`'s `changelog-path` points at

#### Scenario: An entry the contract check accepts references the specific role and change

- GIVEN a `CHANGELOG.md` entry documenting a predefined role's tools change
- WHEN the contract check looks for condition (b)
- THEN an entry that identifies the affected role and change is treated as satisfying the CHANGELOG requirement; an unrelated or generic entry that does not reference the role/change MUST NOT be treated as satisfying it
