"""Tests for the `.claude/hooks/guard-main.sh` PreToolUse hook.

The hook receives Claude Code's PreToolUse JSON payload on stdin
(``{"tool_input": {"command": "..."}, "cwd": "..."}``) and must decide
whether a `git commit`/`push`/... invocation inside `command` targets a
git checkout that is on `main`, regardless of the *hook's own* process
cwd. These tests invoke the script directly via subprocess against real
git repos built under `tmp_path`, so no real repository state is at risk.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard-main.sh"


def run_hook(command: str, cwd: str) -> subprocess.CompletedProcess[str]:
    """Invoke the hook script with a PreToolUse-shaped JSON payload on stdin."""
    payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd})
    return subprocess.run(
        [str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )


def deny_reason(result: subprocess.CompletedProcess[str]) -> str:
    """Assert the hook denied and return the human-readable reason."""
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    return output["permissionDecisionReason"]


def assert_allowed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo checked out on `main` with an initial commit."""
    main_checkout = tmp_path / "main-checkout"
    _init_repo(main_checkout)
    return main_checkout


@pytest.fixture
def feature_worktree(repo: Path, tmp_path: Path) -> Path:
    """A worktree of `repo` checked out on a feature branch."""
    wt = tmp_path / "feature-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature/x", str(wt)],
        check=True,
        capture_output=True,
    )
    return wt


def test_hook_script_exists_and_is_executable() -> None:
    assert HOOK_SCRIPT.is_file()
    assert HOOK_SCRIPT.stat().st_mode & 0o111, "hook script must be executable"


def test_cd_into_feature_worktree_then_commit_is_allowed(
    repo: Path, feature_worktree: Path
) -> None:
    # The reported bug: the session's primary checkout (`repo`) is on
    # main, but the command cd's into a feature-branch worktree first.
    result = run_hook(f"cd {feature_worktree} && git commit -m x", cwd=str(repo))
    assert_allowed(result)


def test_dash_C_into_feature_worktree_push_is_allowed(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f"git -C {feature_worktree} push -u origin HEAD", cwd=str(repo))
    assert_allowed(result)


def test_cd_into_main_checkout_then_commit_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    # Session cwd/hook cwd is a feature worktree, but the command cd's
    # into the main checkout before committing.
    result = run_hook(f"cd {repo} && git commit -m x", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert str(repo) in reason
    assert "main" in reason


def test_dash_C_into_main_checkout_commit_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f"git -C {repo} commit -m x", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert str(repo) in reason


@pytest.mark.parametrize(
    "push_cmd",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin refs/heads/main",
    ],
)
def test_push_to_main_ref_from_feature_branch_is_denied(
    feature_worktree: Path, push_cmd: str
) -> None:
    result = run_hook(push_cmd, cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert "main" in reason


def test_push_without_naming_main_from_feature_branch_is_allowed(
    feature_worktree: Path,
) -> None:
    result = run_hook("git push -u origin HEAD", cwd=str(feature_worktree))
    assert_allowed(result)


@pytest.mark.parametrize(
    "cmd",
    [
        "git cherry-pick abc123",
        "git rebase main",
        "git merge some-branch",
        "git revert abc123",
        "git am patch.diff",
    ],
)
def test_history_rewriting_subcommands_denied_on_main(repo: Path, cmd: str) -> None:
    result = run_hook(cmd, cwd=str(repo))
    reason = deny_reason(result)
    assert str(repo) in reason


@pytest.mark.parametrize(
    "cmd",
    ["git cherry-pick abc123", "git rebase main", "git merge some-branch"],
)
def test_history_rewriting_subcommands_allowed_on_feature_branch(
    feature_worktree: Path, cmd: str
) -> None:
    result = run_hook(cmd, cwd=str(feature_worktree))
    assert_allowed(result)


def test_non_git_command_ls_is_allowed(repo: Path) -> None:
    result = run_hook("ls -la", cwd=str(repo))
    assert_allowed(result)


def test_string_mentioning_git_commit_is_conservatively_denied(
    repo: Path,
) -> None:
    # Round-2 (fail-closed) design: an OTHER segment (first word isn't
    # `git`/`cd`/`pushd`) that raw-text-mentions a guarded git pattern
    # cannot be proven safe, so it is denied even though this particular
    # case (an echoed string) is harmless. This is a documented, accepted
    # false positive (see the script's header) — the alternative is the
    # security regression where wrapped/disguised git invocations
    # (env/sudo/bash -c/...) were silently allowed.
    result = run_hook('echo "git commit -m x"', cwd=str(repo))
    reason = deny_reason(result)
    assert "verify" in reason.lower() or "cannot" in reason.lower()


def test_nonexistent_target_directory_is_denied_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = run_hook(f"git -C {missing} commit -m x", cwd=str(tmp_path))
    reason = deny_reason(result)
    assert str(missing) in reason


def test_cd_with_double_quoted_path_is_allowed_into_feature_worktree(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f'cd "{feature_worktree}" && git commit -m x', cwd=str(repo))
    assert_allowed(result)


def test_cd_with_tilde_expands_to_home_and_is_denied_on_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _init_repo(home)
    monkeypatch.setenv("HOME", str(home))
    result = run_hook("cd ~ && git commit -m x", cwd=str(home))
    reason = deny_reason(result)
    assert str(home) in reason


def test_multiple_git_invocations_denied_if_any_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    cmd = f"git -C {feature_worktree} status; git -C {repo} commit -m y"
    result = run_hook(cmd, cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert str(repo) in reason


def test_multiple_git_invocations_all_allowed_produces_no_output(
    feature_worktree: Path,
) -> None:
    cmd = f"git -C {feature_worktree} status && git -C {feature_worktree} commit -m x"
    result = run_hook(cmd, cwd=str(feature_worktree))
    assert_allowed(result)


# ---------------------------------------------------------------------------
# Round-2 (fail-closed) design: bypasses found in parent verification of the
# round-1 parser. Round 1 tried to fully parse every command form and, as a
# consequence, silently ALLOWED several disguised guarded-git invocations
# against `main`. Round 2 flips the default: only a cleanly parsed
# `git`/`cd`/`pushd` segment gets the precise target-dir/branch check;
# anything else that even coarsely mentions a guarded git invocation is
# denied (fail closed), with a reason asking the agent to rewrite as
# `cd <dir> && git ...` or `git -C <dir> ...`.
# ---------------------------------------------------------------------------


def test_env_assignment_prefix_denied_when_target_is_main(repo: Path) -> None:
    # Previously allowed unconditionally: the round-1 parser required the
    # segment to start with the literal word `git`, so `VAR=1 git commit`
    # was never recognized as a git invocation at all and no check ever
    # ran. Now a plain (non-GIT_*) env-var prefix is part of the "clean"
    # git-segment shape, so this goes through the normal branch check.
    result = run_hook("VAR=1 git commit -m x", cwd=str(repo))
    reason = deny_reason(result)
    assert str(repo) in reason


def test_env_assignment_prefix_still_allowed_on_feature_branch(
    feature_worktree: Path,
) -> None:
    # The fix above must not become a blanket deny of any VAR=value
    # prefix — it should go through the ordinary branch check like a
    # plain `git commit` would.
    result = run_hook("VAR=1 git commit -m x", cwd=str(feature_worktree))
    assert_allowed(result)


@pytest.mark.parametrize(
    "wrapped_command",
    [
        "env git commit -m x",
        "sudo git commit",
        "command git commit",
        "nice git commit -m x",
        "sh -c 'git commit -m x'",
        "eval 'git commit -m x'",
    ],
)
def test_wrapper_commands_around_git_are_denied(repo: Path, wrapped_command: str) -> None:
    # None of these wrappers are named explicitly by the hook — they are
    # caught because the segment's first word isn't `git`/`cd`/`pushd`
    # (so it can never reach the precise check) and the segment's raw
    # text still mentions a guarded git invocation, so it is denied as
    # unaccounted-for.
    result = run_hook(wrapped_command, cwd=str(repo))
    reason = deny_reason(result)
    assert reason


def test_bash_c_wrapped_commit_is_denied(repo: Path) -> None:
    result = run_hook("bash -c 'git commit -m x'", cwd=str(repo))
    reason = deny_reason(result)
    assert reason


def test_subshell_cd_into_main_then_commit_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    # The reported bypass: splitting on `&&` without understanding the
    # wrapping `( ... )` meant the `cd` was never recognized (it starts
    # with a stray "(") so the target dir silently fell back to the
    # session's feature-branch cwd, allowing a commit that actually lands
    # on main inside the subshell.
    result = run_hook(f"(cd {repo} && git commit -m x)", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert reason


def test_dollar_paren_command_substitution_is_denied(repo: Path) -> None:
    result = run_hook("echo $(git commit -m x)", cwd=str(repo))
    reason = deny_reason(result)
    assert reason


def test_backtick_command_substitution_is_denied(repo: Path) -> None:
    result = run_hook("echo `git commit -m x`", cwd=str(repo))
    reason = deny_reason(result)
    assert reason


def test_git_dir_flag_from_feature_worktree_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f"git --git-dir={repo}/.git commit", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert reason


def test_work_tree_flag_from_feature_worktree_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f"git --work-tree={repo} commit", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert reason


def test_namespace_flag_from_feature_worktree_is_denied(feature_worktree: Path) -> None:
    result = run_hook("git --namespace=foo commit", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert reason


def test_git_dir_env_prefix_from_feature_worktree_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f"GIT_DIR={repo}/.git git commit", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert reason


def test_git_work_tree_env_prefix_from_feature_worktree_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    result = run_hook(f"GIT_WORK_TREE={repo} git commit", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert reason


def test_pushd_into_main_then_commit_is_denied(repo: Path, feature_worktree: Path) -> None:
    result = run_hook(f"pushd {repo} && git commit", cwd=str(feature_worktree))
    reason = deny_reason(result)
    assert str(repo) in reason


def test_commit_message_mentioning_git_words_is_allowed_on_feature_branch(
    feature_worktree: Path,
) -> None:
    # Rule-4 exception: quoted text that is an ARGUMENT of an already
    # cleanly-parsed git segment must not trigger the fail-closed path.
    result = run_hook('git commit -m "fix git push docs"', cwd=str(feature_worktree))
    assert_allowed(result)


# ---------------------------------------------------------------------------
# Round-3: double quotes do NOT block command/process substitution in bash
# (only single quotes do), so a `$(...)`/backtick/`<(...)`/`>(...)` inside a
# DOUBLE-quoted argument of an otherwise-clean git segment still actually
# executes. The veto must fire there too, while a single-quoted occurrence
# (fully literal in bash) must still be allowed through.
# ---------------------------------------------------------------------------


def test_dollar_paren_inside_double_quoted_commit_message_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    cmd = f'cd {feature_worktree} && git commit -m "$(git -C {repo} commit -m y)"'
    result = run_hook(cmd, cwd=str(repo))
    reason = deny_reason(result)
    assert reason


def test_backtick_inside_double_quoted_commit_message_is_denied(
    repo: Path, feature_worktree: Path
) -> None:
    cmd = f'cd {feature_worktree} && git commit -m "`git -C {repo} commit`"'
    result = run_hook(cmd, cwd=str(repo))
    reason = deny_reason(result)
    assert reason


def test_literal_dollar_paren_inside_single_quoted_message_is_allowed(
    feature_worktree: Path,
) -> None:
    # Single quotes are fully literal in bash: `$(x)` here never executes,
    # it is just text in the commit message.
    result = run_hook(
        "git commit -m 'literal $(x) in single quotes'", cwd=str(feature_worktree)
    )
    assert_allowed(result)
