"""Pins the contract ``tests/conftest.py`` owes the rest of the suite.

D-014 S5 moved the empty-secret check into a ``Settings`` model validator that
raises. ``agents_system.main`` ends with a module-level ``app = create_app()``, so
that validator runs during pytest COLLECTION, before any fixture exists. The
only place that can keep the suite collectable is conftest's import-time env
assignment — and if that assignment is written as ``os.environ.setdefault``, an
inherited ``ALLOW_INSECURE=false`` silently defeats it and every module that
imports ``agents_system.main`` dies as an unattributable collection error.

These tests fail loudly and by name when that regression is reintroduced.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents_system.config import Settings
from agents_system.services.participants import (
    ConversationRecorder,
    Participant,
    ParticipantDirectory,
)


def test_conftest_forces_insecure_mode_over_inherited_env() -> None:
    """conftest must OVERRIDE an inherited ALLOW_INSECURE, not defer to it."""
    assert os.environ["ALLOW_INSECURE"] == "true"


def test_ambient_environment_can_construct_settings() -> None:
    """``Settings()`` with no kwargs — exactly what ``get_settings()`` does at
    import time — must construct under the suite's environment.

    This is the collection-time invariant stated as a test: if it fails, the
    real symptom is not this assertion but four collection errors.
    """
    settings = Settings()
    assert settings.allow_insecure is True


def test_conftest_fixture_assembly_does_not_load_client_services() -> None:
    """Importing conftest and assembling test fixtures must not load client services.

    Pins the decoupling (#70-B6) required so services/clients.py and
    services/conversation_log.py can be deleted in a subsequent slice.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, str(Path('tests').resolve()))\n"
                "import conftest\n"
                "assert 'agents_system.services.clients' not in sys.modules\n"
                "assert 'agents_system.services.conversation_log' not in sys.modules\n"
                "assert 'agents_system.models.tables' not in sys.modules\n"
                "app = conftest.create_test_app()\n"
                "leaked = [m for m in sys.modules if m in {\n"
                "    'agents_system.services.clients',\n"
                "    'agents_system.services.conversation_log',\n"
                "}]\n"
                "assert not leaked, f'Leaked modules: {leaked}'\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test_fakes_structurally_conform_to_platform_protocols() -> None:
    """Test doubles must satisfy Participant, ParticipantDirectory, and ConversationRecorder."""
    from conftest import (
        FakeConversationRecorder,
        FakeParticipant,
        FakeParticipantDirectory,
    )

    # 1. FakeParticipant conforms to runtime_checkable Participant
    participant = FakeParticipant()
    assert isinstance(participant, Participant)
    assert hasattr(participant, "id")
    assert hasattr(participant, "active")

    # 2. FakeParticipantDirectory conforms to ParticipantDirectory
    directory: ParticipantDirectory = FakeParticipantDirectory()
    assert callable(directory.normalize_address)
    assert inspect.iscoroutinefunction(directory.resolve)
    sig_resolve = inspect.signature(directory.resolve)
    assert set(sig_resolve.parameters.keys()) >= {"session", "address"}

    # 3. FakeConversationRecorder conforms to ConversationRecorder
    recorder: ConversationRecorder = FakeConversationRecorder()
    assert inspect.iscoroutinefunction(recorder.record_turn)
    sig_record = inspect.signature(recorder.record_turn)
    assert set(sig_record.parameters.keys()) >= {
        "session",
        "thread_id",
        "participant_id",
        "user_text",
        "assistant_text",
    }


def test_create_test_app_defaults_to_fakes_and_preserves_overrides() -> None:
    """create_test_app stores FakeParticipantDirectory and FakeConversationRecorder by default,
    and honors explicit overrides."""
    from conftest import (
        FakeConversationRecorder,
        FakeParticipantDirectory,
        create_test_app,
    )

    # Defaults
    app = create_test_app()
    assert isinstance(app.state.participant_directory, FakeParticipantDirectory)
    assert isinstance(app.state.conversation_recorder, FakeConversationRecorder)

    # Explicit overrides
    custom_dir = object()
    custom_rec = object()
    app_override = create_test_app(
        participant_directory=custom_dir,
        conversation_recorder=custom_rec,
    )
    assert app_override.state.participant_directory is custom_dir
    assert app_override.state.conversation_recorder is custom_rec

    # Explicit None overrides
    app_none = create_test_app(
        participant_directory=None,
        conversation_recorder=None,
    )
    assert app_none.state.participant_directory is None
    assert app_none.state.conversation_recorder is None


def test_fake_participant_directory_normalize_address_behavior() -> None:
    """fake_normalize_address rejects empty/non-phone input and normalizes valid numbers."""
    from conftest import FakeParticipantDirectory

    directory = FakeParticipantDirectory()

    # Valid numbers normalize to +<digits>
    assert directory.normalize_address("+5491123456789") == "+5491123456789"
    assert directory.normalize_address("5491123456789") == "+5491123456789"
    assert directory.normalize_address("15551234567") == "+15551234567"
    assert directory.normalize_address("+1-555-123-4567") == "+15551234567"

    # Non-phone or empty inputs reject with ValueError
    for invalid in ["", "   ", "not-a-phone", "abc", "123", "+"]:
        with pytest.raises(ValueError):
            directory.normalize_address(invalid)


async def test_fake_conversation_recorder_records_turns_and_supports_spy() -> None:
    """FakeConversationRecorder records turns in memory and invokes optional spy."""
    from conftest import FakeConversationRecorder

    spy_calls: list[dict[str, object]] = []

    def my_spy(session: object, **kwargs: object) -> None:
        spy_calls.append({"session": session, **kwargs})

    recorder = FakeConversationRecorder(spy=my_spy)
    assert recorder.turns == []

    mock_session = object()
    await recorder.record_turn(
        mock_session,
        thread_id="+15551234567",
        participant_id="p-1",
        user_text="hello",
        assistant_text="world",
    )

    assert len(recorder.turns) == 1
    assert recorder.turns[0]["thread_id"] == "+15551234567"
    assert recorder.turns[0]["participant_id"] == "p-1"
    assert recorder.turns[0]["user_text"] == "hello"
    assert recorder.turns[0]["assistant_text"] == "world"

    assert len(spy_calls) == 1
    assert spy_calls[0]["thread_id"] == "+15551234567"


# ---------------------------------------------------------------------------
# #158 -- the offline suite must be isolated from the developer's `.env` and
# from ambient WHATSAPP_*/ADAPTER_*/AGENT_REGISTRATIONS/DEPLOY_GRANTS/
# REGISTRY_FACTORY/provider env vars. `Settings` (agents_system.config)
# defaults `model_config["env_file"]` to `.env`, so any `Settings()` call
# that does not pass its own `_env_file` picks up whatever `.env` sits at
# the process cwd -- and #141's fail-closed WhatsApp boot check
# (main.py::lifespan) turns exactly that leak into a hard, machine-dependent
# offline failure: a checkout whose `.env` configures WHATSAPP_TOKEN/
# WHATSAPP_PHONE_NUMBER_ID with no matching WHATSAPP_RUNTIME_ID (the common
# shape for a developer who only wants to poke the adapter locally) fails
# every offline lifespan test that builds a real `Settings()`.
#
# These run the target test as a subprocess rather than in-process: the leak
# happens at `Settings()` construction time inside `main.py`/`server.py`,
# which this same test process cannot observe from the outside without
# reaching into module internals, and a subprocess is what actually
# reproduces "run `pytest -q` on a checkout with a populated `.env`".
# ---------------------------------------------------------------------------


def _run_offline_pytest(
    *, cwd: Path, env: dict[str, str], node_id: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", node_id],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(cwd),
        env=env,
        check=False,
    )


# #165 review follow-up -- the test below writes a real `.env` at the repo
# root and removes it in `finally`. A run killed between the write and that
# `finally` (Ctrl-C, an OOM kill, a CI job timeout) leaves the file behind;
# every later run then hits the `dotenv_path.exists()` branch above and
# silently `pytest.skip()`s forever, indistinguishable from "a real developer
# `.env` is here". This marker, written as the file's first line, lets the
# next run tell its OWN leftover apart from a real one and fail loudly
# instead.
_PLACEHOLDER_DOTENV_MARKER = (
    "# placeholder written by "
    "tests/test_conftest_contract.py::"
    "test_offline_lifespan_test_passes_whether_or_not_dotenv_sets_whatsapp_vars "
    "-- safe to delete if a killed test run left this behind"
)


def _is_own_placeholder_dotenv(path: Path) -> bool:
    """Whether *path* is this test's own leftover placeholder, not a real
    developer `.env` (empty/unreadable counts as "not ours" -- a real `.env`
    is never assumed to be our leftover just because it happens to be empty
    or briefly unreadable mid-write).
    """
    try:
        first_line = path.read_text().splitlines()[:1]
    except (OSError, IndexError):
        return False
    return bool(first_line) and first_line[0] == _PLACEHOLDER_DOTENV_MARKER


def _write_placeholder_dotenv(path: Path, body: str) -> None:
    """Atomically create *path* with the placeholder marker plus *body*.

    Writing to a sibling temp file and ``os.replace``-ing it into place means
    a run killed mid-write never leaves a partially-written `.env` at *path*
    for `_is_own_placeholder_dotenv`/the next run to reason about -- either
    the marked placeholder lands whole, or nothing lands at all.
    """
    tmp_path = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    tmp_path.write_text(f"{_PLACEHOLDER_DOTENV_MARKER}\n{body}")
    os.replace(tmp_path, path)


def test_is_own_placeholder_dotenv_tells_the_leftover_apart_from_a_real_dotenv(
    tmp_path: Path,
) -> None:
    own = tmp_path / "own.env"
    _write_placeholder_dotenv(own, "WHATSAPP_TOKEN=leaked-token\n")

    real = tmp_path / "real.env"
    real.write_text("WHATSAPP_TOKEN=a-real-developer-token\n")

    missing = tmp_path / "missing.env"

    assert _is_own_placeholder_dotenv(own) is True
    assert _is_own_placeholder_dotenv(real) is False
    assert _is_own_placeholder_dotenv(missing) is False


def test_offline_lifespan_test_passes_whether_or_not_dotenv_sets_whatsapp_vars() -> (
    None
):
    """Regression test for #158 acceptance criterion 1: `pytest -q` must
    behave identically whether or not the checkout's `.env` configures
    WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID with no matching
    WHATSAPP_RUNTIME_ID.
    """
    repo_root = Path(__file__).resolve().parents[1]
    dotenv_path = repo_root / ".env"
    if dotenv_path.exists():
        if _is_own_placeholder_dotenv(dotenv_path):
            # A killed previous run of this same test left its own
            # placeholder behind -- fail loudly and say so, rather than
            # silently skipping forever as if a real developer `.env` were
            # here (#165 review follow-up).
            pytest.fail(
                f"{dotenv_path} is this test's own leftover placeholder "
                "from a killed previous run (marker: "
                f"{_PLACEHOLDER_DOTENV_MARKER!r}). Delete it and re-run."
            )
        # A real developer `.env` is never read, backed up, or overwritten
        # by this test -- the write below would otherwise clobber it, and
        # its content is out of scope to inspect. The fixture already
        # covers this exact shape without a real `.env` (see the sibling
        # ambient-env-var test), and CI/a fresh checkout has none.
        pytest.skip(
            "a real .env exists at the repo root; skipping to avoid touching it"
        )

    target = "tests/test_server.py::test_server_lifespan_boots_with_no_agents"
    # The ambient environment must not itself carry the leak under test --
    # only the `.env` file written below may.
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WHATSAPP_", "ADAPTER_"))
    }

    without_dotenv = _run_offline_pytest(cwd=repo_root, env=clean_env, node_id=target)
    _write_placeholder_dotenv(
        dotenv_path,
        "WHATSAPP_TOKEN=leaked-token\nWHATSAPP_PHONE_NUMBER_ID=leaked-phone-id\n",
    )
    try:
        with_dotenv = _run_offline_pytest(cwd=repo_root, env=clean_env, node_id=target)
    finally:
        dotenv_path.unlink()

    assert without_dotenv.returncode == 0, without_dotenv.stdout + without_dotenv.stderr
    assert with_dotenv.returncode == 0, with_dotenv.stdout + with_dotenv.stderr


def test_offline_lifespan_test_ignores_ambient_whatsapp_env_vars() -> None:
    """Scope point 1 (#158): an exported WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID
    in the developer's shell -- not only a `.env` file -- must not leak into
    an offline `Settings()` build either.
    """
    repo_root = Path(__file__).resolve().parents[1]
    target = "tests/test_server.py::test_server_lifespan_boots_with_no_agents"
    env = dict(os.environ)
    env["WHATSAPP_TOKEN"] = "leaked-ambient-token"
    env["WHATSAPP_PHONE_NUMBER_ID"] = "leaked-ambient-phone-id"

    result = _run_offline_pytest(cwd=repo_root, env=env, node_id=target)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.live
def test_isolate_offline_settings_fixture_is_a_noop_for_live_tests() -> None:
    """Acceptance criterion 2 (#158): a test marked `live` (or `load`) keeps
    `Settings`' default `.env` reading untouched -- the isolation fixture
    added for #158 only applies to the offline suite.
    """
    from agents_system.config import Settings

    assert Settings.model_config.get("env_file") == ".env"
