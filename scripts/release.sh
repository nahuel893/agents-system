#!/usr/bin/env bash
# Cut a release locally with release-please, using the gh CLI's own
# authentication instead of a stored token.
#
# release-please normally runs as a GitHub Actions workflow on every push to
# main, but a PR opened with the default GITHUB_TOKEN does not trigger other
# workflows, so the required CI checks never run and branch protection blocks
# the release PR from merging. The owner decided not to store a
# fine-grained PAT as a repository secret just to work around that (#18), so
# the release-please workflow is disabled and releases are cut locally
# instead: this script runs release-please under the maintainer's own `gh`
# auth, so the release PR is opened under their account and CI runs
# normally.
#
# Usage:
#   scripts/release.sh pr    Open or update the standing release PR.
#   scripts/release.sh tag   After the release PR is merged: create the
#                            vX.Y.Z tag and the GitHub Release.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/release.sh pr|tag

  pr    Open or update the release PR (release-please release-pr).
  tag   After merging the release PR, create the vX.Y.Z tag and the
        GitHub Release (release-please github-release).

Prerequisites: `gh auth login` with repo scope, and Node.js/npx available.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

mode="$1"

case "${mode}" in
  pr | tag) ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage
    exit 2
    ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh CLI not found; run 'gh auth login' first" >&2
  exit 1
fi

if ! command -v npx >/dev/null 2>&1; then
  echo "error: npx not found; install Node.js first" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
repo_root="$(cd -- "${script_dir}/.." >/dev/null 2>&1 && pwd)"
cd -- "${repo_root}"

token="$(gh auth token)"

common_args=(
  --token="${token}"
  --repo-url=nahuel893/agents-system
  --config-file=release-please-config.json
  --manifest-file=.release-please-manifest.json
  --target-branch=main
)

case "${mode}" in
  pr)
    npx --yes release-please release-pr "${common_args[@]}"
    ;;
  tag)
    npx --yes release-please github-release "${common_args[@]}"
    ;;
esac
