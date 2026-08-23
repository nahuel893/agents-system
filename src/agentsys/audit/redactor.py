"""Redactor — PII redaction for audit event payloads (REQ-AUDIT-40..43)."""

from __future__ import annotations

import copy
import re
from typing import Any

import phonenumbers


# Phone-related key names that are always added to pii_keys when redacted
_PHONE_KEYS: frozenset[str] = frozenset({
    "phone", "tel", "telephone", "phone_number",
    "mobile", "cell", "contact_phone",
})


class Redactor:
    """Redacts PII from audit event payloads using a default-deny policy.

    Phones are ALWAYS redacted regardless of policy.
    Emails are ALWAYS redacted regardless of policy.
    Default sensitive keys (``message``, ``body``, ``text``) are redacted unless
    ``audit_policy.capture_tool_input`` is ``True`` (free-text bodies kept;
    phones and emails still stripped).
    Additional keys can be added via ``audit_policy.redact_keys``.
    """

    # Keys whose free-text values are redacted by default
    DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset({"message", "body", "text", "email"})

    # Regex for email detection
    EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    def redact(
        self,
        payload: dict[str, Any],
        audit_policy: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Redact PII from ``payload`` and return it with a list of redacted keys.

        Args:
            payload: The raw event payload to redact.
            audit_policy: Optional policy dict. Supports:
                - ``capture_tool_input`` (bool): If True, keep ``message``/``body``/``text``
                  values (but still redact phones and emails embedded in them).
                - ``redact_keys`` (list[str]): Additional top-level keys to always redact.

        Returns:
            A tuple of (redacted_payload, pii_keys) where pii_keys is a list of
            top-level keys whose values were redacted.
            Values replaced with ``[REDACTED:phone]``, ``[REDACTED:email]``,
            ``[REDACTED:body]``, or ``[REDACTED:custom]``.
        """
        if audit_policy is None:
            audit_policy = {}

        payload = copy.deepcopy(payload)
        pii_keys: list[str] = []
        capture_tool_input = audit_policy.get("capture_tool_input", False)
        extra_keys = set(audit_policy.get("redact_keys", []))

        redact_keys = self.DEFAULT_SENSITIVE_KEYS | extra_keys

        for key, value in list(payload.items()):
            if not isinstance(value, str):
                continue

            # 1. Phone detection (always active)
            if self._contains_phone(value):
                payload[key] = "[REDACTED:phone]"
                pii_keys.append(key)
                if key in _PHONE_KEYS or self._contains_phone(payload.get(key, "")):
                    # Phone-related key always in pii_keys when value was redacted
                    pass
                continue

            # 2. Email regex (always active)
            if self.EMAIL_RE.search(value):
                payload[key] = "[REDACTED:email]"
                pii_keys.append(key)
                continue

            # 3. Default sensitive keys + extra redact_keys
            if key in redact_keys:
                # Only redact free-text body if capture_tool_input is False
                if key in self.DEFAULT_SENSITIVE_KEYS and not capture_tool_input:
                    payload[key] = f"[REDACTED:{key}]"
                    pii_keys.append(key)
                elif key in extra_keys:
                    payload[key] = "[REDACTED:custom]"
                    pii_keys.append(key)

        return payload, pii_keys

    @staticmethod
    def _contains_phone(value: str) -> bool:
        """Return True if ``value`` contains a phone number detected by phonenumbers."""
        try:
            # Try direct parse — works for standalone numbers like "+1 555 123 4567"
            parsed = phonenumbers.parse(value, None)
            # Use possible-number check (format-valid) not valid-number (assigned-valid)
            if phonenumbers.is_possible_number(parsed):
                return True
        except Exception:
            pass
        # Fall back to matcher for embedded phones in free text
        try:
            for match in phonenumbers.PhoneNumberMatcher(value, None):
                if match.raw_string:
                    return True
        except Exception:
            pass
        return False
