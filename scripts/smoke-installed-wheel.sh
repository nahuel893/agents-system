#!/usr/bin/env bash
# Smoke-test agents_system as an INSTALLED LIBRARY, the way an importer uses it.
#
# Builds the wheel, installs it into a throwaway virtualenv outside the
# repository -- a plain install, so the core alone, with no extra (issue
# #188) -- and runs two documented examples with an EMPTY environment (no
# `.env`, no server variables, not even PATH or HOME), each in its own
# process: the README's library example (the block marked
# `<!-- runnable-example: library -->`) and the Quickstart of
# docs/platform/library-usage.md (`<!-- runnable-example: quickstart -->`,
# issue #168). A scripted chat model is passed to each example's own
# `main(model)`, so nothing reaches a real provider.
#
# It fails if the plain install brought any package an extra provides
# (agents_system.extras.EXTRA_MODULES), if a turn does not complete (one tool
# call, then the answer), if the Quickstart registers any tool besides its
# own, if any traceback is
# printed, or if the missing AuditSink is reported anything other than
# exactly once per process (issue #166). Everything it creates lives in one
# temporary directory that is removed on exit, success or failure.
#
# Usage:
#   scripts/smoke-installed-wheel.sh
#
# Prerequisites: uv. Third-party dependencies are pinned to uv.lock, so the
# install is what the test suite runs against (and is served from uv's cache
# when it is warm).
set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

log() {
  printf 'smoke-installed-wheel: %s\n' "$*" >&2
}

fail() {
  log "FAILED: $*"
  exit 1
}

command -v uv >/dev/null 2>&1 || fail "uv not found; install it first"
[[ -f "${repo_root}/README.md" ]] || fail "README.md not found in ${repo_root}"
quickstart_doc="${repo_root}/docs/platform/library-usage.md"
[[ -f "${quickstart_doc}" ]] || fail "docs/platform/library-usage.md not found in ${repo_root}"

python_version="3.12"
if [[ -f "${repo_root}/.python-version" ]]; then
  read -r python_version <"${repo_root}/.python-version"
fi

workdir="$(mktemp -d "${TMPDIR:-/tmp}/agents-system-wheel-smoke.XXXXXX")" ||
  fail "could not create a temporary directory"
cleanup() {
  rm -rf -- "${workdir}"
}
trap cleanup EXIT

case "${workdir}" in
  "${repo_root}"/*) fail "the throwaway directory must be outside the repository" ;;
esac

log "building the wheel"
uv build --quiet --wheel --out-dir "${workdir}/dist" "${repo_root}"
wheels=("${workdir}"/dist/*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]] ||
  fail "expected exactly one wheel in ${workdir}/dist"
log "built $(basename -- "${wheels[0]}")"

log "installing it into a throwaway virtualenv (Python ${python_version})"
# uv.lock's pins, as constraints: the wheel's own dependency metadata is what
# gets resolved, held to the versions the test suite runs against.
uv export --quiet --project "${repo_root}" --frozen --no-dev --no-emit-project \
  --no-hashes --format requirements-txt --output-file "${workdir}/constraints.txt"
venv_python="${workdir}/venv/bin/python"
(cd -- "${workdir}" && uv venv --quiet --python "${python_version}" "${workdir}/venv")

# pyproject.toml pins some packages (the CPU-only torch build) to an EXPLICIT
# index: only those packages may come from it, everything else from PyPI.
# `uv pip` has no per-package index, so those are installed first, alone,
# from their own index; the wheel's resolution below then finds them present.
"${venv_python}" -I - "${repo_root}/pyproject.toml" >"${workdir}/explicit-sources.txt" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    uv_config = tomllib.load(handle).get("tool", {}).get("uv", {})
index_urls = {index["name"]: index["url"] for index in uv_config.get("index", [])}
for package, sources in uv_config.get("sources", {}).items():
    for source in sources if isinstance(sources, list) else [sources]:
        if "index" in source:
            print(package, index_urls[source["index"]])
PY
while read -r package index_url; do
  mapfile -t pins < <(grep -E "^${package}==" "${workdir}/constraints.txt" || true)
  [[ ${#pins[@]} -gt 0 ]] || continue
  uv pip install --quiet --python "${venv_python}" --no-deps \
    --index-url "${index_url}" "${pins[@]}"
done <"${workdir}/explicit-sources.txt"

uv pip install --quiet --python "${venv_python}" \
  --constraints "${workdir}/constraints.txt" "${wheels[0]}"

# Issue #188: a plain install is the core. Nothing an extra provides may be
# importable, so the examples below prove the core alone runs them.
log "checking that the plain install is the core alone"
if ! (cd -- "${workdir}" && env -i "${venv_python}" -I - <<'PY'
import importlib.util
import sys

from agents_system.extras import EXTRA_MODULES

present = sorted(
    {
        module
        for modules in EXTRA_MODULES.values()
        for module in modules
        if importlib.util.find_spec(module) is not None
    }
)
if present:
    sys.exit(f"a plain install must not bring what an extra provides: {present}")
PY
); then
  fail "the plain install is not the core alone"
fi
read -r core_size _ < <(du -sh -- "${workdir}/venv")
log "core-only virtualenv: ${core_size}"

cp -- "${repo_root}/README.md" "${workdir}/README.md"
cp -- "${quickstart_doc}" "${workdir}/library-usage.md"
cat >"${workdir}/run_example.py" <<'PY'
"""Run one documented example against the installed wheel.

Usage: run_example.py <document> <library|quickstart>
"""

import asyncio
import pathlib
import sys

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

import agents_system

# What each example's scripted turn calls, and what must come back.
CASES = {
    "library": {
        "tool": "catalog_search",
        "args": {"query": "Item Alpha"},
        "tool_result": "Item Alpha",
        "answer": "Yes, Item Alpha is in stock.",
        "registry": None,
    },
    "quickstart": {
        "tool": "word_count",
        "args": {"text": "the quick brown fox"},
        "tool_result": '"words": 4',
        "answer": "There are 4 words.",
        # Issue #168: the reader's one tool, and nothing else registered.
        "registry": ("word_count",),
    },
}

document_path = pathlib.Path(sys.argv[1])
kind = sys.argv[2]
case = CASES[kind]
marker = f"<!-- runnable-example: {kind} -->"

document = document_path.read_text(encoding="utf-8")
if document.count(marker) != 1:
    sys.exit(f"{document_path.name} must mark exactly one runnable {kind} example")
after = document.split(marker, 1)[1].lstrip("\n")
if not after.startswith("```python\n"):
    sys.exit("the runnable-example marker must sit right above a ```python fence")
code = after[len("```python\n") :].split("\n```", 1)[0] + "\n"

package_dir = pathlib.Path(agents_system.__file__).resolve().parent
venv_dir = pathlib.Path(sys.prefix).resolve()
if venv_dir not in package_dir.parents:
    sys.exit(f"agents_system was imported from {package_dir}, not the throwaway venv")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


model = ScriptedModel(
    responses=[
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": case["tool"],
                    "args": case["args"],
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=case["answer"]),
    ]
)

namespace = {"__name__": f"{kind}_example"}
exec(compile(code, document_path.name, "exec"), namespace)
if case["registry"] is not None and namespace["registry"].names() != case["registry"]:
    sys.exit(f"expected only {case['registry']} registered, got {namespace['registry'].names()}")
messages = asyncio.run(namespace["main"](model))

tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
if len(tool_messages) != 1 or case["tool_result"] not in str(tool_messages[0].content):
    sys.exit(f"expected one {case['tool']} result with {case['tool_result']!r}, got {messages!r}")
if messages[-1].content != case["answer"]:
    sys.exit(f"unexpected final answer: {messages[-1].content!r}")

print(
    f"SMOKE-RESULT {kind}: agents_system {agents_system.__version__} imported from "
    f"<throwaway venv>/{package_dir.relative_to(venv_dir)}: "
    f"tool result {tool_messages[0].content!r}, answer {messages[-1].content!r}"
)
PY

# Runs one example in its own process with an empty environment, and checks
# that its turn completed, printed no traceback, and reported the missing
# AuditSink exactly once.
run_example() {
  local kind="$1" document="$2" description="$3"
  local run_log="${workdir}/run-${kind}.log"

  log "running the ${description} with an empty environment"
  if ! (cd -- "${workdir}" && env -i "${workdir}/venv/bin/python" -I \
    "${workdir}/run_example.py" "${document}" "${kind}") >"${run_log}" 2>&1; then
    cat -- "${run_log}" >&2
    fail "the ${description} did not run"
  fi

  if grep -q "Traceback" "${run_log}"; then
    cat -- "${run_log}" >&2
    fail "the ${description} printed a traceback"
  fi
  local sink_warnings
  sink_warnings="$(grep -c "audit.sink_not_registered" "${run_log}" || true)"
  if [[ "${sink_warnings}" != "1" ]]; then
    cat -- "${run_log}" >&2
    fail "the ${description}: expected exactly one audit.sink_not_registered warning, got ${sink_warnings}"
  fi

  grep "SMOKE-RESULT" "${run_log}" >&2
  log "${description}: audit.sink_not_registered logged once, no traceback"
}

run_example library "${workdir}/README.md" "README library example"
run_example quickstart "${workdir}/library-usage.md" "library-usage Quickstart"
log "OK"
