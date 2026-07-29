"""Tests for Redactor PII redaction (T-11, T-12, T-13)."""

from __future__ import annotations



class TestRedactorPhone:
    """Phone numbers are ALWAYS redacted regardless of policy."""

    def test_phone_international_redacted(self) -> None:
        """Given a phone number in international format, Redactor replaces it."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"phone": "+5491123456789"}
        result = r.redact(payload)
        assert result["phone"] == "[REDACTED:phone]"

    def test_phone_us_format(self) -> None:
        """Given a US-format phone number, it is redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        # Standard E.164 format with spaces (known to parse reliably)
        payload = {"phone": "+1 555 123 4567"}
        result = r.redact(payload)
        assert result["phone"] == "[REDACTED:phone]"

    def test_phone_embedded_in_message(self) -> None:
        """Given a message containing a phone number, the phone is redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"message": "Hi, call me at +5491123456789"}
        result = r.redact(payload)
        assert result["message"] == "[REDACTED:phone]"
        assert "[REDACTED:phone]" in result["message"]

    def test_phone_always_redacted_even_with_capture(self) -> None:
        """Given capture_tool_input=True, phones are STILL redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"phone": "+5491123456789"}
        result = r.redact(payload, {"capture_tool_input": True})
        assert result["phone"] == "[REDACTED:phone]"


class TestRedactorEmail:
    """Email addresses are ALWAYS redacted regardless of policy."""

    def test_email_redacted(self) -> None:
        """Given an email address, it is replaced with [REDACTED:email]."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"email": "user@example.com"}
        result = r.redact(payload)
        assert result["email"] == "[REDACTED:email]"

    def test_email_embedded_in_body(self) -> None:
        """Given a message body containing an email, it is redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"message": "Contact me at user@example.com"}
        result = r.redact(payload)
        # Email in body is detected and redacted
        assert "[REDACTED:email]" in result["message"]


class TestRedactorDefaultKeys:
    """Default sensitive keys (message, body, text) are redacted by default."""

    def test_message_key_redacted(self) -> None:
        """Given a payload with 'message' key, value is redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"message": "Hello world"}
        result = r.redact(payload)
        assert result["message"] == "[REDACTED:message]"

    def test_body_key_redacted(self) -> None:
        """Given a payload with 'body' key, value is redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"body": "This is the body text"}
        result = r.redact(payload)
        assert result["body"] == "[REDACTED:body]"

    def test_text_key_redacted(self) -> None:
        """Given a payload with 'text' key, value is redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"text": "Plain text content"}
        result = r.redact(payload)
        assert result["text"] == "[REDACTED:text]"

    def test_non_sensitive_key_preserved(self) -> None:
        """Given a key not in sensitive list, value is preserved."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"tool_name": "order_writer", "count": 5}
        result = r.redact(payload)
        assert result["tool_name"] == "order_writer"
        assert result["count"] == 5

    def test_message_preserved_when_capture_tool_input(self) -> None:
        """Given capture_tool_input=True, message/body/text are NOT redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"message": "Hello world", "body": "Body text"}
        result = r.redact(payload, {"capture_tool_input": True})
        # message/body are preserved when capture=True
        assert result["message"] == "Hello world"
        assert result["body"] == "Body text"

    def test_phone_still_redacted_with_capture_tool_input(self) -> None:
        """Given capture_tool_input=True, phones embedded in message are STILL redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        # Phone in message value
        payload = {"message": "Hi, call +5491123456789"}
        result = r.redact(payload, {"capture_tool_input": True})
        # Phone is still caught even though message body is kept
        assert "[REDACTED:phone]" in result["message"]


class TestRedactorAuditPolicy:
    """Extra redact_keys from audit_policy are always redacted."""

    def test_extra_redact_keys(self) -> None:
        """Given audit_policy.redact_keys, those values are redacted."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"customer_name": "John Doe", "ssn": "123-45-6789"}
        result = r.redact(payload, {"redact_keys": ["customer_name", "ssn"]})
        assert result["customer_name"] == "[REDACTED:custom]"
        assert result["ssn"] == "[REDACTED:custom]"

    def test_extra_redact_keys_preserves_other(self) -> None:
        """Given extra redact_keys, other keys are not affected."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"customer_name": "John Doe", "tool_name": "order_writer"}
        result = r.redact(payload, {"redact_keys": ["customer_name"]})
        assert result["customer_name"] == "[REDACTED:custom]"
        assert result["tool_name"] == "order_writer"


class TestRedactorDeepCopy:
    """Redactor returns a deep-copied payload, original is not modified."""

    def test_returns_new_dict(self) -> None:
        """The redact method returns a new dict, not the input."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"message": "Hello"}
        result = r.redact(payload)
        assert result is not payload
        # Original not modified
        assert payload["message"] == "Hello"


class TestRedactorEdgeCases:
    """Edge cases for the Redactor."""

    def test_empty_payload(self) -> None:
        """Given an empty payload, no errors occur."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        result = r.redact({})
        assert result == {}

    def test_none_value_ignored(self) -> None:
        """Given a None value in payload, it is handled gracefully."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"message": None, "tool_name": "order_writer"}
        result = r.redact(payload)
        # None is not a string, should not be redacted
        assert result["message"] is None

    def test_integer_value_ignored(self) -> None:
        """Given an integer value, it is preserved (not treated as string)."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"sequence": 1, "count": 42}
        result = r.redact(payload)
        assert result["sequence"] == 1
        assert result["count"] == 42

    def test_list_value_ignored(self) -> None:
        """Given a list value in payload, it is preserved."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"items": ["a", "b", "c"]}
        result = r.redact(payload)
        assert result["items"] == ["a", "b", "c"]

    def test_nested_dict_ignored(self) -> None:
        """Given a nested dict, it is preserved as-is (no recursive redaction in v1)."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"metadata": {"key": "value"}}
        result = r.redact(payload)
        assert result["metadata"] == {"key": "value"}

    def test_phone_in_body_with_capture(self) -> None:
        """Phone embedded in message is redacted even when capture_tool_input=True."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"body": "Call me at +5491123456789"}
        result = r.redact(payload, {"capture_tool_input": True})
        # phone is redacted
        assert "[REDACTED:phone]" in result["body"]

    def test_email_key_explicit(self) -> None:
        """The 'email' key is detected by email regex (always active)."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        payload = {"email": "test@example.com"}
        result = r.redact(payload)
        # email regex fires before key check
        assert result["email"] == "[REDACTED:email]"

    def test_local_phone_numbers(self) -> None:
        """Local-format phone numbers (without +) are not detected (region=None)."""
        from agentsys.audit.redactor import Redactor

        r = Redactor()
        # Without a country code, these look like regular integers
        payload = {"phone": "555 123 4567"}
        result = r.redact(payload)
        # No + prefix, no country code → not detected as phone
        assert result["phone"] == "555 123 4567"
