# deployments/

This directory is the mount point for a consumer's own deployment overrides.
It ships empty, and that is the point.

A deployment override narrows a generic role from `platform/roles/` for one
client: it may subtract tools and permissions, never add them. The layout a
consumer creates here is:

```
deployments/
└── <client>/
    └── <role>/            # a role name from platform/roles/
        ├── manifest.md    # tools / skills / permissions (subtractive)
        ├── policy.md      # autonomy, escalation, execution limits
        ├── role.md        # the deployment's prompt additions
        └── skills/        # skill bodies named by manifest.md
```

## Why the directory is tracked while empty

`harness/loader.py::_require_deployments_root` raises loudly when a client
override is requested against a missing root, rather than silently falling
back to the generic role. That strictness is deliberate — a silent fallback
*widens* the tool surface, so a typo in a client name would grant more, not
less.

The consequence is that the root has to exist for any consumer that resolves
a `client=` override at all. Deleting it along with the last deployment would
turn a configuration mistake into an import-time crash for every consumer.

Tests do not use this directory. They resolve against
`tests/fixtures/agents/overrides/deployments/`, which holds generic fixtures
(`client-a`, `bad-tools`, `strict-limits`, …) that exercise the override
contract without any real client's domain.
