#!/usr/bin/env bash
# Run the complete live test suite against a real model, grouped by layer
# (ADR-010 part 3):
#   mechanism    offline guards and the no-model load layer; no model is called
#   behavior     the capability scenarios, the guardrail behavior scenario, the
#                SQL tool, the folder target and the Agent API / extends /
#                skills / importer
#   composition  the reference agents through the harness: smoke, composition,
#                Phase 1 and the real-model load layer
#
# Usage:  scripts/run-live-suite.sh [--quick]
#   --quick   EVAL_RUNS=1 instead of 3 (a cheap smoke pass)
#
# Needs: Postgres on 127.0.0.1:5432 and Redis on 127.0.0.1:6379 (the
# docker-compose services), bwrap, and provider keys in .env. EVAL_PROVIDER
# defaults to openai_compatible (OpenRouter); set EVAL_PROVIDER_OVERRIDE to use another.
# MODEL_PRICES defaults to the reference model's prices when unset, so the
# reported cost is never null. The model itself always comes from
# OPENAI_COMPATIBLE_MODEL (force openai/gpt-6-sol for a gate).
# Every suite runs even if an earlier one fails; logs and a summary (per suite,
# per group, with cost) land in evals/results/live-suite-<timestamp>/
# (gitignored).
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo" || exit 1

runs=3
[[ "${1:-}" == "--quick" ]] && runs=1

stamp="$(date +%Y%m%d-%H%M%S)"
out="$repo/evals/results/live-suite-$stamp"
mkdir -p "$out"

# Provider keys and settings. .env is loaded, never printed.
set -a
# shellcheck disable=SC1091
[[ -f "$repo/.env" ]] && . "$repo/.env"
set +a
export EVAL_PROVIDER="${EVAL_PROVIDER_OVERRIDE:-openai_compatible}"
export EVAL_RUNS="$runs"
default_prices='{"openai/gpt-6-sol": {"input_per_million": 2.0, "output_per_million": 10.0}}'
MODEL_PRICES="${MODEL_PRICES:-$default_prices}"
export MODEL_PRICES
pytest_bin="$repo/.venv/bin/pytest"

# --- Preflight -------------------------------------------------------------
pg_container="$(docker ps --filter publish=5432 --format '{{.Names}}' | head -1)"
redis_container="$(docker ps --filter publish=6379 --format '{{.Names}}' | head -1)"
if [[ -z "$pg_container" || -z "$redis_container" ]]; then
  echo "Postgres or Redis is not running. Start them with: docker compose up -d postgres redis" >&2
  exit 1
fi
command -v bwrap >/dev/null || echo "warning: bwrap missing, sandbox scenarios will be skipped" >&2

psql_c() { docker exec -i "$pg_container" psql -U postgres -v ON_ERROR_STOP=1 "$@"; }

# The guardrail suite reads demo data from agents_system_demo.
if ! psql_c -Atc "select 1 from pg_database where datname='agents_system_demo'" | grep -q 1; then
  echo "agents_system_demo is missing: load it with demo/load_demo_company.py first (see docs/platform/reference-backends.md)" >&2
  exit 1
fi

# The SQL tool suite needs its fixture views and the sql_readonly role in a
# throwaway database, like CI's sql integration job.
sql_db="live_sql_$stamp"
sql_db="${sql_db//-/_}"
sql_password="live-only-$RANDOM$RANDOM"
psql_c -c "CREATE DATABASE $sql_db" >/dev/null
psql_c -d "$sql_db" < tests/fixtures/sql_query_fixture.sql >/dev/null
psql_c -d "$sql_db" -v sql_password="$sql_password" -v sql_views=sql_tool_fixture.sales_v \
  < scripts/provision_sql_readonly.sql >/dev/null
sql_admin_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/$sql_db"
sql_role_url="postgresql+asyncpg://sql_readonly:$sql_password@127.0.0.1:5432/$sql_db"

cleanup() {
  psql_c -c "DROP DATABASE IF EXISTS $sql_db WITH (FORCE)" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- Suites ----------------------------------------------------------------
declare -a names=() results=() suite_groups=() suite_costs=() group_suite_costs=()
declare -a group_names=() group_results=() group_costs=()
group="" group_failed=0 group_start=0
python_bin="$repo/.venv/bin/python"
results_dir="$repo/evals/results"

close_group() {
  [[ -z "$group" ]] && return
  local secs=$((SECONDS - group_start))
  local cost
  cost="$(printf '%s\n' "${group_suite_costs[@]:-n/a}" \
    | awk '$1 != "n/a" {s += $1; n++} END {if (n) printf "%.4f", s; else print "n/a"}')"
  group_names+=("$group")
  group_costs+=("$cost")
  if [[ $group_failed -eq 0 ]]; then
    group_results+=("PASS (${secs}s)")
  else
    group_results+=("FAIL (${secs}s)")
  fi
  group=""
}

run_group() {
  close_group
  group="$1"
  group_failed=0
  group_suite_costs=()
  group_start=$SECONDS
  echo
  echo "##### group: $group #####"
}

run_suite() {
  local name="$1"; shift
  echo
  echo "=== $group / $name ==="
  local start=$SECONDS marker="$out/.marker-$name"
  touch "$marker"
  "$@" 2>&1 | tee "$out/$name.log"
  local rc=${PIPESTATUS[0]}
  local secs=$((SECONDS - start))
  local cost
  cost="$("$python_bin" "$repo/scripts/live_suite_cost.py" "$results_dir" "$marker" "$out/$name.log")"
  suite_costs+=("$cost")
  group_suite_costs+=("$cost")
  names+=("$name")
  suite_groups+=("$group")
  if [[ $rc -eq 0 ]]; then
    results+=("PASS (${secs}s)")
  else
    results+=("FAIL rc=$rc (${secs}s)")
    group_failed=1
  fi
}

run_group mechanism
run_suite 01-mechanism      "$pytest_bin" -m "not live and not load" tests/test_mechanism_suite.py -q -rA
run_suite 02-load-fake      "$pytest_bin" -m "load and not live" tests/test_live_load_phase3.py -q -rA -s

run_group behavior
run_suite 03-capabilities   "$pytest_bin" -m live tests/test_live_eval_capabilities.py -q -rA -s
run_suite 04-guardrails     "$pytest_bin" -m live tests/test_live_eval_guardrails.py -q -rA -s
run_suite 05-sql-tool       env DATABASE_URL="$sql_admin_url" SQL_DATABASE_URL="$sql_role_url" \
                            "$pytest_bin" -m live tests/test_live_eval_sql_query.py -q -rA
run_suite 06-phase4         "$pytest_bin" -m live tests/test_live_eval_phase4.py -q -rA -s

run_suite 07-folder-target  "$pytest_bin" -m live tests/test_live_eval_folder_target.py -q -rA -s

run_group composition
run_suite 08-smoke          "$pytest_bin" -m live tests/test_live_smoke.py -q -rA
run_suite 09-composition    "$pytest_bin" -m live tests/test_live_eval_composition.py -q -rA -s
run_suite 10-phase1         "$pytest_bin" -m live tests/test_live_eval_phase1.py -q -rA -s
run_suite 11-load-real      env EVAL_RUNS=1 "$pytest_bin" -m "load and live" tests/test_live_load_phase3.py -q -rA -s
close_group

# --- Summary ---------------------------------------------------------------
{
  echo "Live suite run $stamp  (EVAL_PROVIDER=$EVAL_PROVIDER, model=${OPENAI_COMPATIBLE_MODEL:-unset}, EVAL_RUNS=$runs)"
  for g in "${!group_names[@]}"; do
    printf '%-12s %s  cost_usd=%s\n' "${group_names[$g]}" "${group_results[$g]}" "${group_costs[$g]}"
    for i in "${!names[@]}"; do
      [[ "${suite_groups[$i]}" == "${group_names[$g]}" ]] || continue
      printf '  %-16s %-20s cost_usd=%s\n' "${names[$i]}" "${results[$i]}" "${suite_costs[$i]}"
    done
  done
  echo "Logs: $out"
  echo "Per-scenario reports (rates, tokens, cost): evals/results/"
} | tee "$out/SUMMARY.txt"
