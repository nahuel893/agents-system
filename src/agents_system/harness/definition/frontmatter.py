"""Reading a definition file: the YAML frontmatter, the prose body and the escalation descriptions written in a policy body."""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterable
from typing import Any

import yaml

from agents_system.harness.definition.errors import DefinitionError

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body).

    The frontmatter is the YAML block delimited by leading ``---`` markers.
    If no frontmatter is present, returns an empty dict and the full text.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text

    # Find the closing ---
    lines = stripped.split("\n")
    end_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, text

    yaml_block = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    parsed = yaml.safe_load(yaml_block) or {}
    return parsed, body


def _read_md(
    path: pathlib.Path, *, shown: pathlib.Path | None = None
) -> tuple[dict[str, Any], str]:
    """Read a markdown file and return (frontmatter, body).

    ``shown`` is how an error names the file: relative to its root, never
    the host path (issue #75). Defaults to the file name.
    """
    if not path.exists():
        name = shown.as_posix() if shown is not None else path.name
        raise DefinitionError(f"Required definition file not found: {name}")
    return _split_frontmatter(path.read_text(encoding="utf-8"))


def as_str_list(value: Any) -> list[str]:
    """Coerce a YAML value to a list of strings: a scalar is a one-item list.

    Also used for `Agent(...)`'s list parameters (`agent/spec.py`), which is
    why a tuple counts as a list: YAML never produces one, but Python does.
    """
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return [str(value)]


#: Issue #88 — matches a `- `name` — description` bullet, the shape
#: `data-agent`/`orchestrator`/`sales-agent`/`summary-agent`'s
#: `## escalation_rules` prose sections already used before anything parsed
#: it back out. Tolerates a plain `--` too, since a human hand-typing prose
#: reaches for either dash.
_ESCALATION_DESCRIPTION_RE = re.compile(
    r"^-\s+`([A-Za-z][A-Za-z0-9_]*)`\s+(?:—|--)\s+(.*)$"
)


def _parse_escalation_descriptions(
    body: str,
    *,
    known_conditions: Iterable[str],
    source: pathlib.Path | str | None = None,
) -> dict[str, str]:
    """Parse `- `name` — description` bullets out of a policy.md prose body.

    Issue #88: `escalation_rules.conditions` is structured YAML, but WHY each
    condition exists lived only in `policy.md`'s prose -- `harness/
    factory.py::_render_escalation_block` rendered the bare snake_case name
    and nothing else, which #82 showed was not enough for a model to reliably
    act on (`accountant-agent`'s `figure_requested_outside_report_catalog`
    needed a concrete restatement in `role.md` before its live scenario
    passed reliably). This is the loader half: read the description back out
    of the SAME prose a human already writes to explain the condition to the
    next maintainer, instead of requiring it to be duplicated into `role.md`
    by hand for every condition, on every role.

    Only a bullet whose backtick name matches a condition THIS file's own
    frontmatter declares is captured -- *known_conditions* -- so unrelated
    backtick-dash prose elsewhere in a policy.md (there is plenty, e.g. in
    the autonomy/delegation sections) is never mistaken for an escalation
    description.

    A description that wraps onto following lines -- every predefined role's
    policy.md hand-wraps at ~80 columns, indented under the bullet -- is
    joined back onto one string with single spaces. A blank line, a heading,
    or a new bullet ends the one being continued.

    A fenced code block (a line that, once stripped, starts with ```,
    with or without a language tag) is skipped entirely while it is open
    (#88 follow-up): a documentation example inside one -- e.g. showing a
    future maintainer the bullet convention itself -- is prose about the
    format, not a real description, and must never be captured or compete
    with the real bullet outside the fence.

    An odd number of fence markers -- one opened and never closed anywhere
    in the body -- is an authoring mistake, not "everything after it is
    inside the fence": left unchecked, it would silently discard every real
    (non-fenced) bullet that follows, with no error, warning, or log,
    reintroducing the exact "bare condition name only" failure mode issue
    #88 was opened to fix (PR #99 review follow-up). It raises
    ``DefinitionError`` naming *source* (the ``policy.md`` this body came
    from) instead.

    Two real (non-fenced) bullets for the same condition name in one file
    is an authoring mistake, not a "last one wins": it raises
    ``DefinitionError`` naming the condition, rather than silently keeping
    whichever bullet happened to come last (#88 follow-up).
    """
    known = set(known_conditions)
    if not known:
        return {}

    descriptions: dict[str, str] = {}
    current: str | None = None
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            current = None
            continue
        if in_fence:
            continue

        match = _ESCALATION_DESCRIPTION_RE.match(line)
        if match:
            name, text = match.group(1), match.group(2).strip()
            if name in known:
                if name in descriptions:
                    raise DefinitionError(
                        f"Invariant violation — escalation_rules: condition "
                        f"'{name}' has more than one description bullet in "
                        f"this file's policy.md prose. Keep exactly one — "
                        f"delete the duplicate, or move an intentional "
                        f"example into a fenced code block."
                    )
                current = name
                descriptions[current] = text
            else:
                current = None
            continue

        stripped = line.strip()
        is_continuation = (
            current is not None
            and line[:1] in (" ", "\t")
            and bool(stripped)
            and not stripped.startswith(("-", "#"))
        )
        if is_continuation:
            assert current is not None  # narrows for mypy; guarded above
            descriptions[current] = f"{descriptions[current]} {stripped}"
            continue

        # Blank line, heading, unindented prose, or a new bullet: whatever
        # bullet was being continued is done.
        current = None

    if in_fence:
        where = f" in {source}" if source is not None else ""
        raise DefinitionError(
            f"Invariant violation — escalation_rules: an unterminated fence "
            f"(an opened ``` with no matching close){where} would silently "
            f"discard every description bullet after it. Close the fence, "
            f"or remove the stray ``` marker."
        )

    return descriptions
