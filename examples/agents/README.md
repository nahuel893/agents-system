# Reference agents

Eight example agents, one folder each: `accountant-agent`, `data-agent`,
`developer-agent`, `operator-agent`, `orchestrator`, `sales-agent`,
`summary-agent` and `support-agent`. They are **examples to copy, not
supported API** (ADR-010): they carry no compatibility promise, and they are
not in the wheel.

Each folder has the three files every agent folder has (`role.md`,
`manifest.md`, `policy.md`) and extends the generic `agent` role.
`developer-agent` extends its sibling, `../operator-agent`.

## Use one

Copy the folder into your project and load it like any agent folder of your
own:

```python
from agents_system import Agent, build_runtime

sales = Agent.from_folder("agents/sales-agent")
equipped = build_runtime(sales, registry, granted_permissions=["read:catalog"])
```

To build on one, extend your copy instead of the role name: a sibling
`extends: ../sales-agent` inside the same folder of agents, or
`Agent.from_folder(path, extends=Agent.from_folder("agents/sales-agent"))` in
Python. Copy `developer-agent` together with `operator-agent`, its parent.

These folders resolve through the same path as an adopter's folder, held to
the importer ceiling: an agent folder may match or tighten its parent's
autonomy and limits, never loosen them, and describes only the escalation
conditions it adds.

## How they differ from the predefined roles

The same eight agents still ship as predefined roles in `platform/roles/`,
and naming one (`extends: sales-agent`, `resolve("sales-agent")`,
`AGENT_REGISTRATIONS`) emits a `DeprecationWarning` that names the folder
here. The next breaking release removes them from `platform/roles/`.

Five predefined roles used a privilege only platform roles have, so their
copies here were fitted to the importer ceiling:

| Agent | Predefined role | Reference agent |
|---|---|---|
| `data-agent` | `autonomy: full` | `autonomy: supervised`, its parent's level |
| `summary-agent` | `autonomy: full` | `autonomy: supervised`, its parent's level |
| `developer-agent` | `total_execution_timeout_s: 120` | Inherits `operator-agent`'s 30 s |
| `sales-agent` | Rewords `confidence_below_threshold` as a catalog-match threshold, and rewords `required_tool_missing` | `catalog_match_below_threshold`, a new condition with that wording; both inherited conditions keep their parent's wording |
| `orchestrator`, `data-agent`, `summary-agent` | Reword `required_tool_missing` | The inherited condition keeps its parent's wording |

The other three, `accountant-agent`, `operator-agent` and `support-agent`,
are unchanged copies. See "Reference agents" in `docs/platform/role.md`.
