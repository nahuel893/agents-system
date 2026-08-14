"""Tests for Redactor PII redaction (T-11, T-12, T-13).

The Redactor is the security-critical piece of D-007: it is documented as
default-DENY, so anything not explicitly allowed must come out redacted. A
redactor that leaks on an unanticipated shape is worse than no redactor,
because downstream code trusts it.

Two conventions in this file:

1. Every test asserts at least one thing that is redacted AND (where relevant)
   one thing that is preserved. A test that only asserts preservation is
   satisfied by ``copy.deepcopy`` — i.e. by deleting the feature — and is not
   coverage.
2. Tests marked ``xfail(strict=True)`` assert the DOCUMENTED contract against
   an implementation that does not meet it yet. They are red today for the
   reason in the marker. ``strict=True`` means that the moment the source is
   fixed the test reports XPASS = failure, which is the signal to delete the
   marker rather than let the assertion rot.

Known implementation shape (``redactor.py``): the loop skips every non-``str``
value (``if not isinstance(value, str): continue``) and detects phones with
``phonenumbers.parse(value, None)``, which requires a leading ``+``.
"""

from __future__ import annotations

import pytest

from agentsys.audit.redactor import Redactor

# ---------------------------------------------------------------------------
# Reasons for the pinned-defect markers
# ---------------------------------------------------------------------------
NO_RECURSION = (
    "redactor.py skips every non-str value ('if not isinstance(value, str): continue'), "
    "so dicts, lists and tuples are never walked and their contents leak verbatim"
)
NON_STR_VALUES = (
    "redactor.py skips every non-str value, so the operator-supplied "
    "audit_policy.redact_keys denylist silently no-ops on dict/list/int/bytes values"
)
NO_REGION = (
    "redactor.py parses with phonenumbers.parse(value, None) / PhoneNumberMatcher(value, None), "
    "which require an explicit country code; this system takes AR national-format numbers "
    "(services/clients.py sets DEFAULT_REGION = 'AR')"
)
WHOLE_VALUE_REPLACED = (
    "redactor.py replaces the WHOLE value with the marker instead of stripping the match, "
    "so capture_tool_input=True still destroys any body that contains a phone or email"
)


class TestRedactorPhone:
    """Phone numbers are ALWAYS redacted regardless of policy."""

    def test_phone_international_redacted(self) -> None:
        """Given a phone number in international format, Redactor replaces it."""
        result = Redactor().redact({"phone": "+5491123456789"})
        assert result["phone"] == "[REDACTED:phone]"

    def test_phone_us_format(self) -> None:
        """Given a US-format phone number, it is redacted."""
        result = Redactor().redact({"phone": "+1 555 123 4567"})
        assert result["phone"] == "[REDACTED:phone]"

    def test_phone_embedded_in_message(self) -> None:
        """Given a message containing a phone number, the phone is redacted."""
        result = Redactor().redact({"message": "Hi, call me at +5491123456789"})
        assert result["message"] == "[REDACTED:phone]"

    def test_phone_always_redacted_even_with_capture(self) -> None:
        """Given capture_tool_input=True, phones are STILL redacted."""
        result = Redactor().redact({"phone": "+5491123456789"}, {"capture_tool_input": True})
        assert result["phone"] == "[REDACTED:phone]"

    def test_phone_redacted_under_an_unlisted_key(self) -> None:
        """Default-deny: a phone leaks through no matter what the key is called.

        ``nota`` is in no allow-list and no deny-list; the value is what matters.
        """
        result = Redactor().redact({"nota": "cliente Juan, tel +54 9 11 2345-6789"})
        assert result["nota"] == "[REDACTED:phone]"

    @pytest.mark.parametrize(
        "raw",
        [
            "11 2345-6789",
            "1123456789",
            "011 15 2345-6789",
        ],
    )
    @pytest.mark.xfail(strict=True, reason=NO_REGION)
    def test_national_format_phone_redacted(self, raw: str) -> None:
        """AR national-format numbers must be redacted — that is how they arrive.

        Clients are synced from the medallion with local-format ``telefono_movil``
        values and normalised with ``phonenumbers.parse(raw, DEFAULT_REGION)``.
        A redactor blind to that format is blind to the dominant real case.
        """
        result = Redactor().redact({"phone": raw})
        assert result["phone"] == "[REDACTED:phone]", f"national-format phone leaked: {raw!r}"

    @pytest.mark.xfail(strict=True, reason=NON_STR_VALUES)
    def test_phone_stored_as_int_redacted(self) -> None:
        """A phone that arrives as an int must not survive redaction."""
        result = Redactor().redact({"phone": 5491123456789})
        assert result["phone"] != 5491123456789

    @pytest.mark.xfail(strict=True, reason=NON_STR_VALUES)
    def test_phone_stored_as_bytes_redacted(self) -> None:
        """A phone that arrives as bytes must not survive redaction."""
        result = Redactor().redact({"phone": b"+5491123456789"})
        assert result["phone"] != b"+5491123456789"


class TestRedactorEmail:
    """Email addresses are ALWAYS redacted regardless of policy."""

    def test_email_redacted(self) -> None:
        """Given an email address, it is replaced with [REDACTED:email]."""
        result = Redactor().redact({"email": "user@example.com"})
        assert result["email"] == "[REDACTED:email]"

    def test_email_embedded_in_body(self) -> None:
        """Given a message body containing an email, the address does not survive."""
        result = Redactor().redact({"message": "Contact me at user@example.com"})
        assert "user@example.com" not in result["message"]
        assert result["message"] == "[REDACTED:email]"


class TestRedactorDefaultKeys:
    """Default sensitive keys (message, body, text) are redacted by default."""

    def test_message_key_redacted(self) -> None:
        """Given a payload with 'message' key, value is redacted."""
        result = Redactor().redact({"message": "Hello world"})
        assert result["message"] == "[REDACTED:message]"

    def test_body_key_redacted(self) -> None:
        """Given a payload with 'body' key, value is redacted."""
        result = Redactor().redact({"body": "This is the body text"})
        assert result["body"] == "[REDACTED:body]"

    def test_text_key_redacted(self) -> None:
        """Given a payload with 'text' key, value is redacted."""
        result = Redactor().redact({"text": "Plain text content"})
        assert result["text"] == "[REDACTED:text]"

    def test_non_sensitive_keys_preserved_while_sensitive_ones_are_not(self) -> None:
        """Operational metadata survives in the same payload where PII does not.

        The negative half alone would pass with redaction deleted, so it is
        asserted together with the positive half.
        """
        payload = {"tool_name": "order_writer", "count": 5, "message": "Hola Juan"}
        result = Redactor().redact(payload)
        assert result["tool_name"] == "order_writer"
        assert result["count"] == 5
        assert result["message"] == "[REDACTED:message]"

    def test_message_preserved_when_capture_tool_input(self) -> None:
        """capture_tool_input=True keeps free text but never disables phone redaction."""
        payload = {"message": "Hello world", "body": "Body text", "phone": "+5491123456789"}
        result = Redactor().redact(payload, {"capture_tool_input": True})
        assert result["message"] == "Hello world"
        assert result["body"] == "Body text"
        assert result["phone"] == "[REDACTED:phone]"


class TestRedactorCaptureToolInput:
    """capture_tool_input keeps the body and strips only the PII inside it.

    ``redactor.py`` documents: "free-text bodies kept; phones and emails still
    stripped". These tests pin exactly the distinction the previous
    ``assert "[REDACTED:phone]" in result["message"]`` assertions could not
    make — that marker is present both when the phone is surgically removed
    and when the entire body is thrown away.
    """

    @pytest.mark.xfail(strict=True, reason=WHOLE_VALUE_REPLACED)
    def test_body_survives_phone_stripping(self) -> None:
        """The phone goes, the surrounding order text stays."""
        payload = {"body": "Pedido de Juan. Tel +5491123456789. Gracias."}
        result = Redactor().redact(payload, {"capture_tool_input": True})
        assert "+5491123456789" not in result["body"]
        assert "Pedido de Juan" in result["body"]
        assert "Gracias" in result["body"]

    @pytest.mark.xfail(strict=True, reason=WHOLE_VALUE_REPLACED)
    def test_body_survives_email_stripping(self) -> None:
        """The address goes, the surrounding message stays."""
        payload = {"message": "Escribime a juan@badie.com por favor"}
        result = Redactor().redact(payload, {"capture_tool_input": True})
        assert "juan@badie.com" not in result["message"]
        assert "Escribime a" in result["message"]
        assert "por favor" in result["message"]


class TestRedactorAuditPolicy:
    """Extra redact_keys from audit_policy are always redacted."""

    def test_extra_redact_keys(self) -> None:
        """Given audit_policy.redact_keys, those values are redacted."""
        payload = {"customer_name": "John Doe", "ssn": "123-45-6789"}
        result = Redactor().redact(payload, {"redact_keys": ["customer_name", "ssn"]})
        assert result["customer_name"] == "[REDACTED:custom]"
        assert result["ssn"] == "[REDACTED:custom]"

    def test_extra_redact_keys_preserves_other(self) -> None:
        """Given extra redact_keys, other keys are not affected."""
        payload = {"customer_name": "John Doe", "tool_name": "order_writer"}
        result = Redactor().redact(payload, {"redact_keys": ["customer_name"]})
        assert result["customer_name"] == "[REDACTED:custom]"
        assert result["tool_name"] == "order_writer"

    @pytest.mark.xfail(strict=True, reason=NON_STR_VALUES)
    def test_extra_redact_keys_applies_to_dict_values(self) -> None:
        """redact_keys is the one control an operator has — it must not care about type."""
        payload = {"credentials": {"password": "hunter2"}}
        result = Redactor().redact(payload, {"redact_keys": ["credentials"]})
        assert "hunter2" not in repr(result)

    @pytest.mark.xfail(strict=True, reason=NON_STR_VALUES)
    def test_extra_redact_keys_applies_to_int_values(self) -> None:
        """An SSN stored as an int is still an SSN."""
        payload = {"ssn": 123456789}
        result = Redactor().redact(payload, {"redact_keys": ["ssn"]})
        assert result["ssn"] != 123456789

    @pytest.mark.xfail(strict=True, reason=NON_STR_VALUES)
    def test_extra_redact_keys_applies_to_list_values(self) -> None:
        """A denylisted key holding a list must not leak the list."""
        payload = {"phones": ["+5491123456789", "+5491198765432"]}
        result = Redactor().redact(payload, {"redact_keys": ["phones"]})
        assert "+5491123456789" not in repr(result)


class TestRedactorNestedStructures:
    """Real tool payloads are nested; the redactor must walk them.

    ``tests/test_audit_event_model.py`` declares the canonical production
    payload as ``{"tool_input": {"phone": "+5491112345678"}}`` — one level of
    nesting, which the current top-level-strings-only loop cannot see.
    """

    @pytest.mark.xfail(strict=True, reason=NO_RECURSION)
    def test_nested_dict_phone_redacted(self) -> None:
        """The canonical payload shape must not leak."""
        payload = {"tool_input": {"phone": "+5491112345678"}}
        result = Redactor().redact(payload)
        assert "+5491112345678" not in repr(result)

    @pytest.mark.xfail(strict=True, reason=NO_RECURSION)
    def test_nested_sensitive_key_redacted(self) -> None:
        """A default sensitive key nested one level down is still sensitive."""
        payload = {"args": {"message": "Hola Juan, tu pedido esta listo"}}
        result = Redactor().redact(payload)
        assert result["args"]["message"] != "Hola Juan, tu pedido esta listo"

    @pytest.mark.xfail(strict=True, reason=NO_RECURSION)
    def test_list_values_redacted(self) -> None:
        """A list of contact strings must not pass through untouched."""
        payload = {"recipients": ["+5491123456789", "juan@badie.com"]}
        result = Redactor().redact(payload)
        assert "+5491123456789" not in repr(result)
        assert "juan@badie.com" not in repr(result)

    @pytest.mark.xfail(strict=True, reason=NO_RECURSION)
    def test_tuple_values_redacted(self) -> None:
        """Tuples are containers too — an unanticipated shape must not be a bypass."""
        payload = {"contacts": ("+5491123456789",)}
        result = Redactor().redact(payload)
        assert "+5491123456789" not in repr(result)

    @pytest.mark.xfail(strict=True, reason=NO_RECURSION)
    def test_deeply_nested_list_of_dicts_redacted(self) -> None:
        """Depth is not a bypass either."""
        payload = {
            "order": {
                "items": [
                    {"sku": "A-1", "note": "entregar a +5491123456789"},
                ],
            },
        }
        result = Redactor().redact(payload)
        assert "+5491123456789" not in repr(result)
        assert "A-1" in repr(result), "SKUs are not PII and must survive"


class TestRedactorDeepCopy:
    """Redactor returns a deep-copied payload; the caller's dict is untouched."""

    def test_returns_new_dict_without_mutating_the_original(self) -> None:
        """The input keeps its raw values; the output is redacted."""
        payload = {"message": "call +5491123456789", "args": {"sku": "A-1"}}
        result = Redactor().redact(payload)
        assert result is not payload
        assert result["args"] is not payload["args"], "nested containers must be copied too"
        assert payload["message"] == "call +5491123456789", "input must not be mutated"
        assert result["message"] == "[REDACTED:phone]"


class TestRedactorEdgeCases:
    """Edge cases for the Redactor."""

    def test_empty_payload(self) -> None:
        """Given an empty payload, no errors occur."""
        assert Redactor().redact({}) == {}

    def test_none_value_ignored(self) -> None:
        """A None value is left alone rather than stringified into a marker."""
        result = Redactor().redact({"message": None, "phone": "+5491123456789"})
        assert result["message"] is None
        assert result["phone"] == "[REDACTED:phone]"

    def test_numeric_metadata_preserved(self) -> None:
        """Counters and sequence numbers are not PII and must stay queryable."""
        result = Redactor().redact({"sequence": 1, "count": 42, "message": "Hola"})
        assert result["sequence"] == 1
        assert result["count"] == 42
        assert result["message"] == "[REDACTED:message]"

    def test_non_pii_list_preserved(self) -> None:
        """Redaction must not become a blanket 'nuke every container' either."""
        result = Redactor().redact({"items": ["a", "b", "c"], "message": "Hola"})
        assert result["items"] == ["a", "b", "c"]
        assert result["message"] == "[REDACTED:message]"

    def test_email_key_explicit(self) -> None:
        """The 'email' key is detected by email regex (always active)."""
        result = Redactor().redact({"email": "test@example.com"})
        assert result["email"] == "[REDACTED:email]"
