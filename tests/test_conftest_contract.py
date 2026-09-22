"""Pins the contract ``tests/conftest.py`` owes the rest of the suite.

D-014 S5 moved the empty-secret check into a ``Settings`` model validator that
raises. ``agentsys.main`` ends with a module-level ``app = create_app()``, so
that validator runs during pytest COLLECTION, before any fixture exists. The
only place that can keep the suite collectable is conftest's import-time env
assignment — and if that assignment is written as ``os.environ.setdefault``, an
inherited ``ALLOW_INSECURE=false`` silently defeats it and every module that
imports ``agentsys.main`` dies as an unattributable collection error.

These tests fail loudly and by name when that regression is reintroduced.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentsys.config import Settings
from agentsys.services.participants import (
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
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path('tests').resolve()))\n"
            "import conftest\n"
            "assert 'agentsys.services.clients' not in sys.modules\n"
            "assert 'agentsys.services.conversation_log' not in sys.modules\n"
            "assert 'agentsys.models.tables' not in sys.modules\n"
            "app = conftest.create_test_app()\n"
            "leaked = [m for m in sys.modules if m in {\n"
            "    'agentsys.services.clients',\n"
            "    'agentsys.services.conversation_log',\n"
            "}]\n"
            "assert not leaked, f'Leaked modules: {leaked}'\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
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
