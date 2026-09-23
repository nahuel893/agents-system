#!/usr/bin/env bash
#
# guard-main.sh — PreToolUse(Bash) hook: block direct history-writing git
# commands against a checkout that is on `main`.
#
# This replaces an inline one-liner in .claude/settings.json that had two
# bugs:
#
#   1. It ran `git rev-parse --abbrev-ref HEAD` in the HOOK's own process
#      cwd (the session's primary checkout), not in the directory the
#      *command itself* targets. With the primary checkout on main, that
#      blocked `cd /some/worktree && git commit` even when the worktree
#      was on a feature branch (false positive), and conversely would have
#      allowed a commit on main run from another worktree while the
#      primary checkout happened to be on a feature branch (false
#      negative).
#
#   2. It only matched the literal substrings "git commit" and "git push",
#      so it missed every other history-writing subcommand (cherry-pick,
#      merge, rebase, revert, am) and any `git -C <dir> commit|push` or
#      `git -c k=v commit|push` form, where the substring "git commit"
#      never appears contiguously. It also never caught `git push` when
#      the destination ref names `main` from a feature branch.
#
# --- Fail-closed redesign (round 2) -------------------------------------
#
# A static text scanner can never fully parse an arbitrary shell command
# (subshells, wrappers, command substitution, env-var redirection, ...).
# Round 1 tried to parse everything it recognized and allow everything
# else through unchecked. In review that turned out to silently ALLOW
# several disguised forms of a guarded git command against `main`:
# `VAR=1 git commit`, `env git commit`, `sudo git commit`,
# `(cd main-dir && git commit)`, `bash -c 'git commit'`,
# `echo $(git commit)` / backticks, `git --git-dir=... commit`,
# `GIT_DIR=... git commit`, `pushd main-dir && git commit`.
#
# Round 2 flips the failure mode: a git invocation only reaches the
# precise target-dir/branch check when it is part of a *cleanly parsed
# simple segment* (see `try_parse_git`/`try_parse_cd` below). Anything
# else that even loosely looks like it might invoke a guarded git
# subcommand is DENIED, with a reason asking the agent to rewrite it as
# `cd <dir> && git ...` or `git -C <dir> ...`.
#
# Four fail-closed layers:
#
#   1. Coarse whole-command trigger (`coarse_guarded`): does the raw
#      command text contain the standalone word `git` AND a standalone
#      guarded-subcommand word (commit|push|cherry-pick|merge|rebase|
#      revert|am) ANYWHERE (order/adjacency not required)? If not, exit
#      immediately — there is nothing to guard.
#
#   2. Whole-command subshell/substitution veto: deny outright if the
#      command contains, outside of SINGLE quotes, a bare '(' or ')' (not
#      inside any quotes at all), a backtick, `$(`, `<(`, `>(`, or `$'`.
#      Single quotes are fully literal in bash, so a quoted occurrence
#      there (e.g. `git commit -m 'literal $(x) in single quotes'`) is
#      exempt — but double quotes do NOT block `$(...)`/backtick/`<(...)`/
#      `>(...)` expansion in bash, so those four are vetoed inside double
#      quotes too (e.g. `git commit -m "$(git -C main commit)"` is denied
#      even though it's "just" a quoted argument). A bare unquoted paren
#      with no `$`/`<`/`>` in front of it, e.g. inside a commit message
#      `git commit -m "call foo() here"`, stays exempt when double-quoted
#      (bash treats it as inert text there) — this is what matches rule 4
#      of the design brief. `$'...'` (ANSI-C quoting) is vetoed outright
#      rather than modeled precisely, since it allows backslash-escaped
#      quotes a naive scanner would mis-track. This layer does not need to
#      understand what is inside any of these constructs to block them.
#
#   3. Per top-level segment (split on && || ; | and newlines, quote
#      aware — see `split_segments`), each segment is one of:
#        - a clean `cd`/`pushd` — first word `cd` or `pushd`; the rest
#          of the segment is not inspected further. `pushd` is modeled
#          exactly like `cd` (no directory *stack*; a later `popd` is
#          not understood and does not undo it — a pragmatic, documented
#          limitation).
#        - a clean `git` invocation — first word `git`, OPTIONALLY
#          preceded by plain `NAME=value` environment assignments whose
#          NAME does not start with `GIT_` (so `VAR=1 git commit` is
#          clean and goes through the normal branch check, but
#          `GIT_DIR=...`/`GIT_WORK_TREE=...` do not), then only `-C
#          <dir>` / `-c <key>=<value>` as global options before the
#          subcommand (so `--git-dir=`, `--work-tree=`, `--namespace=`,
#          or any other flag, disqualifies the segment). This is the
#          ONLY path that reaches the target-dir + branch logic.
#        - anything else ("OTHER": wrapper commands like `env`, `sudo`,
#          `command`, `nice`, `xargs`, `time`; `bash -c`/`sh -c`/`eval`;
#          a git segment disqualified above) is raw-text-scanned (the
#          same coarse word check as layer 1, applied to just that
#          segment) and DENIED if it contains the guarded pattern. This
#          is what catches `env git commit`, `sudo git commit`,
#          `bash -c 'git commit -m x'`, `git --git-dir=... commit`,
#          `GIT_DIR=... git commit` — none of these need to be named
#          explicitly; they are caught because they are not a clean
#          segment yet still mention a guarded git invocation.
#
#   4. Residual whole-command safety net: if the command was
#      `coarse_guarded` (layer 1) but, after processing every segment,
#      no *clean* git segment with a *guarded* subcommand was ever found
#      anywhere in the command, deny. This closes the gap where the
#      coarse pattern's two halves (the word `git`, the word naming a
#      guarded subcommand) are split across two otherwise-innocuous
#      OTHER segments that individually don't contain the full pattern
#      (e.g. `echo git && echo commit`).
#
# Known, accepted limitations (documented rather than "fixed" — fixing
# them would require a real shell parser):
#
#   1. `echo "git commit"` is DENIED (an accepted false positive): layer
#      3 sees an OTHER segment ("echo ...") whose raw text contains both
#      "git" and "commit" and cannot tell it is inert echoed text. Any
#      quoted mention of a guarded git command inside a NON-git segment
#      is treated the same way. The one exception is quoted text that is
#      itself an ARGUMENT of an already-clean git segment (e.g.
#      `git commit -m "fix git push docs"` is allowed — the segment as a
#      whole is clean and is never raw-scanned).
#   2. Ordering is not enforced in layers 1/4: `git` and a guarded word
#      only need to both appear somewhere in the text.
#   3. `am` (git's patch-apply subcommand) is a common English word; any
#      command that mentions "git" and, elsewhere, the standalone word
#      "am" (word-boundary matched, so "team"/"same" don't count, but
#      "I am done" does) can trip layers 1/3/4.
#   4. No runtime variable-substitution chasing: `S=commit; git $S -m x`
#      does not contain the literal word "commit" in the command text
#      and is invisible to a static scanner — out of scope.
#   5. Does not recurse into `bash -c "..."` / `sh -c "..."` / `eval ...`
#      contents to look for a *safe* invocation inside; any such segment
#      that also mentions a guarded pattern is denied outright, never
#      inspected further.
#
# Input: PreToolUse hook JSON on stdin, e.g.
#   {"tool_input": {"command": "git commit -m x"}, "cwd": "/path"}
# Output: on deny, one JSON object on stdout (permissionDecision: deny);
# otherwise no output. Always exits 0 — decisions are communicated via
# stdout JSON, not the exit code.

set -uo pipefail

# ---------------------------------------------------------------------------
# Small quote-aware string helpers (no eval / no command substitution risk:
# expanding a variable's *stored* text only word-splits it, it never
# re-parses that text for `$(...)`).
# ---------------------------------------------------------------------------

# Split "$1" into top-level segments on &&, ||, ; and | (and newlines),
# skipping separators that appear inside single or double quotes.
split_segments() {
    local input="$1"
    local -a result=()
    local buf="" ch nextch
    local in_squote=0 in_dquote=0
    local i=0 len=${#input}
    while ((i < len)); do
        ch="${input:i:1}"
        if ((in_squote)); then
            buf+="$ch"
            [[ "$ch" == "'" ]] && in_squote=0
            ((i++))
            continue
        fi
        if ((in_dquote)); then
            buf+="$ch"
            [[ "$ch" == '"' ]] && in_dquote=0
            ((i++))
            continue
        fi
        case "$ch" in
        "'")
            in_squote=1
            buf+="$ch"
            ((i++))
            continue
            ;;
        '"')
            in_dquote=1
            buf+="$ch"
            ((i++))
            continue
            ;;
        $'\n' | ';')
            result+=("$buf")
            buf=""
            ((i++))
            continue
            ;;
        '&')
            nextch="${input:i+1:1}"
            if [[ "$nextch" == "&" ]]; then
                result+=("$buf")
                buf=""
                ((i += 2))
            else
                buf+="$ch"
                ((i++))
            fi
            continue
            ;;
        '|')
            nextch="${input:i+1:1}"
            if [[ "$nextch" == "|" ]]; then
                result+=("$buf")
                buf=""
                ((i += 2))
            else
                result+=("$buf")
                buf=""
                ((i++))
            fi
            continue
            ;;
        *)
            buf+="$ch"
            ((i++))
            continue
            ;;
        esac
    done
    result+=("$buf")
    printf '%s\n' "${result[@]}"
}

# Split "$1" into whitespace-separated words, quote-aware (strips a single
# layer of surrounding ' or " quoting), without command substitution.
tokenize_words() {
    local input="$1"
    local -a result=()
    local buf="" ch
    local in_squote=0 in_dquote=0 started=0
    local i=0 len=${#input}
    while ((i < len)); do
        ch="${input:i:1}"
        if ((in_squote)); then
            if [[ "$ch" == "'" ]]; then
                in_squote=0
            else
                buf+="$ch"
            fi
            ((i++))
            continue
        fi
        if ((in_dquote)); then
            if [[ "$ch" == '"' ]]; then
                in_dquote=0
            else
                buf+="$ch"
            fi
            ((i++))
            continue
        fi
        case "$ch" in
        "'")
            in_squote=1
            started=1
            ((i++))
            continue
            ;;
        '"')
            in_dquote=1
            started=1
            ((i++))
            continue
            ;;
        ' ' | $'\t')
            if ((started)); then
                result+=("$buf")
                buf=""
                started=0
            fi
            ((i++))
            continue
            ;;
        *)
            buf+="$ch"
            started=1
            ((i++))
            continue
            ;;
        esac
    done
    if ((started)); then
        result+=("$buf")
    fi
    if ((${#result[@]} > 0)); then
        printf '%s\n' "${result[@]}"
    fi
}

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# Resolve "$1" (a cd/-C target, possibly ~-relative or relative) against
# base directory "$2".
# shellcheck disable=SC2088 # "~" / "~/" below are literal prefix checks, not path expansion
resolve_dir() {
    local path="$1" base="$2"
    if [[ -z "$path" ]]; then
        path="$HOME"
    elif [[ "$path" == "~" ]]; then
        path="$HOME"
    elif [[ "${path:0:2}" == '~/' ]]; then
        path="${HOME}/${path:2}"
    fi
    if [[ "$path" == /* ]]; then
        printf '%s' "$path"
    else
        printf '%s/%s' "$base" "$path"
    fi
}

emit_deny() {
    local reason="$1"
    local escaped
    escaped=$(printf '%s' "$reason" | jq -Rs '.')
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}' "$escaped"
}

# ---------------------------------------------------------------------------
# Coarse raw-text word matching (layers 1, 3-OTHER, 4). Word-boundary aware
# but quote/paren-agnostic on purpose — these are the paranoid checks.
# ---------------------------------------------------------------------------

GIT_WORD_RE='(^|[^A-Za-z0-9_])git([^A-Za-z0-9_]|$)'
SUBCMD_WORD_RE='(^|[^A-Za-z0-9_])(commit|push|cherry-pick|merge|rebase|revert|am)([^A-Za-z0-9_]|$)'

has_git_word() { [[ "$1" =~ $GIT_WORD_RE ]]; }
has_guarded_subcommand_word() { [[ "$1" =~ $SUBCMD_WORD_RE ]]; }

# True if "$1" contains a construct bash would treat as a subshell or a
# command/process-substitution trigger OUTSIDE of single quotes (layer 2).
#
# Single quotes are fully literal in bash — nothing inside them is ever
# special, so they are skipped entirely. Double quotes are NOT literal for
# this purpose: `$(...)`, backticks and (conservatively) `<(...)`/`>(...)`
# are still expanded/executed inside "..." — only a bare '(' / ')' with no
# '$'/'<'/'>' in front of it is inert there. So outside any quotes a bare
# '(' or ')' is enough to veto (it could open/close a subshell group); once
# inside double quotes only the specific triggers `$(`, backtick, `<(` and
# `>(` are checked. `$'...'` (ANSI-C quoting) is vetoed outright rather than
# modeled precisely — bash allows backslash escapes inside it (e.g. `\'`)
# that a naive quote-state scanner would mis-track.
has_unquoted_danger_char() {
    local input="$1" ch nextch
    local in_squote=0 in_dquote=0
    local i=0 len=${#input}
    while ((i < len)); do
        ch="${input:i:1}"
        if ((in_squote)); then
            [[ "$ch" == "'" ]] && in_squote=0
            ((i++))
            continue
        fi
        if ((in_dquote)); then
            case "$ch" in
            '"') in_dquote=0 ;;
            '`') return 0 ;;
            '$' | '<' | '>')
                nextch="${input:i+1:1}"
                [[ "$nextch" == "(" ]] && return 0
                ;;
            esac
            ((i++))
            continue
        fi
        # Fully unquoted state.
        case "$ch" in
        '$')
            nextch="${input:i+1:1}"
            [[ "$nextch" == "'" ]] && return 0 # $'...' — veto, don't model it
            ;;
        "'") in_squote=1 ;;
        '"') in_dquote=1 ;;
        '(' | ')' | '`') return 0 ;;
        esac
        ((i++))
    done
    return 1
}

guarded_subcommand() {
    case "$1" in
    commit | push | cherry-pick | merge | rebase | revert | am) return 0 ;;
    *) return 1 ;;
    esac
}

push_targets_main() {
    # "$@" are the words following the `push` subcommand.
    local w
    for w in "$@"; do
        case "$w" in
        main | HEAD:main | refs/heads/main | *:main)
            return 0
            ;;
        esac
    done
    return 1
}

# ---------------------------------------------------------------------------
# Segment classifiers (layer 3). Each sets output globals and returns 0 on a
# clean match, 1 otherwise (segment falls through to the OTHER raw-scan).
# ---------------------------------------------------------------------------

CD_TARGET=""
GIT_SUBCOMMAND=""
GIT_DASHC=""
GIT_REST=()

try_parse_cd() {
    local trimmed="$1"
    if [[ "$trimmed" =~ ^(cd|pushd)($|[[:space:]]) ]]; then
        local -a w=()
        local tok
        while IFS= read -r tok; do
            w+=("$tok")
        done < <(tokenize_words "$trimmed")
        CD_TARGET="${w[1]:-}"
        return 0
    fi
    return 1
}

# Clean shape: [NAME=value]* git [-C <dir> | -c <k>=<v>]* <subcommand> [args...]
# Any NAME starting with GIT_, or any global option other than -C/-c,
# disqualifies the segment (returns 1).
try_parse_git() {
    local trimmed="$1"
    local -a w=()
    local tok
    while IFS= read -r tok; do
        w+=("$tok")
    done < <(tokenize_words "$trimmed")

    local n=${#w[@]}
    local i=0

    while ((i < n)); do
        tok="${w[i]}"
        if [[ "$tok" =~ ^([A-Za-z_][A-Za-z0-9_]*)=.*$ ]]; then
            local var_name="${BASH_REMATCH[1]}"
            if [[ "$var_name" == GIT_* ]]; then
                return 1
            fi
            ((i++))
            continue
        fi
        break
    done

    if ((i >= n)) || [[ "${w[i]}" != "git" ]]; then
        return 1
    fi
    ((i++))

    local dash_c_dir=""
    while ((i < n)); do
        tok="${w[i]}"
        case "$tok" in
        -C)
            dash_c_dir="${w[i + 1]:-}"
            i=$((i + 2))
            ;;
        -c)
            i=$((i + 2)) # skip -c and its key=value argument
            ;;
        -*)
            return 1 # any other global option (--git-dir, --work-tree, ...) -> unclean
            ;;
        *)
            break
            ;;
        esac
    done

    if ((i >= n)); then
        return 1 # no subcommand token found
    fi

    GIT_SUBCOMMAND="${w[i]}"
    GIT_DASHC="$dash_c_dir"
    i=$((i + 1))
    GIT_REST=("${w[@]:i}")
    return 0
}

# Precise target-dir + branch check for a cleanly-parsed, guarded git
# segment. Reads current_dir/base_cwd, sets denied_reason on the way out.
evaluate_clean_git() {
    local base_for_dashC="${current_dir:-$base_cwd}"
    local target_dir
    if [[ -n "$GIT_DASHC" ]]; then
        target_dir=$(resolve_dir "$GIT_DASHC" "$base_for_dashC")
    else
        target_dir="$base_for_dashC"
    fi

    local branch
    if ! branch=$(git -C "$target_dir" rev-parse --abbrev-ref HEAD 2>/dev/null) || [[ -z "$branch" ]]; then
        denied_reason="Blocked: could not resolve '$target_dir' as a git repository, failing closed. Per AGENTS.md, work on a feature branch and let the Lead integrate."
        return
    fi

    if [[ "$branch" == "main" ]]; then
        denied_reason="Blocked: '$target_dir' is on branch 'main'. Per AGENTS.md, work on a feature branch and let the Lead integrate."
        return
    fi

    if [[ "$GIT_SUBCOMMAND" == "push" ]] && push_targets_main "${GIT_REST[@]}"; then
        denied_reason="Blocked: push from '$target_dir' (branch '$branch') targets 'main'. Per AGENTS.md, work on a feature branch and let the Lead integrate."
        return
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

input="$(cat)"

command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
json_cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)

if [[ -z "$command" ]]; then
    exit 0
fi

# Layer 1: coarse whole-command trigger.
if ! has_git_word "$command" || ! has_guarded_subcommand_word "$command"; then
    exit 0
fi

# Layer 2: whole-command subshell/backtick/command-substitution veto.
if has_unquoted_danger_char "$command"; then
    emit_deny "Blocked: command contains subshell/backtick/command-substitution syntax ('(', ')', \`, \$(...), <(...), >(...) or \$'...') outside of single quotes, which this guard cannot safely verify. Rewrite as 'cd <dir> && git ...' or 'git -C <dir> ...'. Per AGENTS.md, work on a feature branch and let the Lead integrate."
    exit 0
fi

process_cwd=$(pwd -P)
base_cwd="${json_cwd:-$process_cwd}"

current_dir=""
denied_reason=""
found_clean_guarded=0

while IFS= read -r segment; do
    [[ -n "$denied_reason" ]] && break

    trimmed=$(trim "$segment")
    [[ -z "$trimmed" ]] && continue

    if try_parse_cd "$trimmed"; then
        current_dir=$(resolve_dir "$CD_TARGET" "${current_dir:-$base_cwd}")
        continue
    fi

    if try_parse_git "$trimmed"; then
        if guarded_subcommand "$GIT_SUBCOMMAND"; then
            found_clean_guarded=1
            evaluate_clean_git
        fi
        continue
    fi

    # OTHER: this segment could not be cleanly parsed as cd/pushd/git.
    # Raw-scan it alone for the guarded pattern (layer 3 fallback).
    if has_git_word "$trimmed" && has_guarded_subcommand_word "$trimmed"; then
        denied_reason="Blocked: '$trimmed' invokes git in a form this guard cannot safely verify (wrapper command, redirected git dir/work-tree, or an unrecognized global option). Rewrite as 'cd <dir> && git ...' or 'git -C <dir> ...'. Per AGENTS.md, work on a feature branch and let the Lead integrate."
    fi
done < <(split_segments "$command")

# Layer 4: residual safety net — the whole command was coarse_guarded but no
# clean+guarded git segment ever accounted for it.
if [[ -z "$denied_reason" ]] && ((!found_clean_guarded)); then
    denied_reason="Blocked: command appears to invoke a guarded git subcommand (commit/push/cherry-pick/merge/rebase/revert/am) in a form this guard could not fully verify. Rewrite as 'cd <dir> && git ...' or 'git -C <dir> ...'. Per AGENTS.md, work on a feature branch and let the Lead integrate."
fi

if [[ -n "$denied_reason" ]]; then
    emit_deny "$denied_reason"
fi

exit 0
