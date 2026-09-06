"""Regression coverage for the _LABEL/_TOKEN ordering defect (scc-0pu.15
side discovery): a recognized label (e.g. "Authorization:") immediately
followed by "Bearer <token>"/"Basic <token>" must redact the full unit, not
just the label-adjacent scheme word, leaving the real secret exposed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "skills/beads/scripts/safe_output.py"


def _load():
    spec = importlib.util.spec_from_file_location("beads_safe_output_ordering", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sanitize(safe, value: str):
    return safe.sanitize_value(
        value, sensitive=safe.SensitiveSet(()), max_utf8_bytes=1_000_000
    )


def test_label_prefixed_bearer_token_is_fully_redacted() -> None:
    safe = _load()
    result = _sanitize(
        safe,
        "request included Authorization: Bearer sk-fixture-FAKEBEARERTOKEN0123456789 in the reply",
    )
    assert result.status == "REDACTED"
    assert result.redaction_count == 1
    assert "sk-fixture-FAKEBEARERTOKEN0123456789" not in result.value
    assert "Bearer" not in result.value
    assert result.value == "request included [REDACTED] in the reply"


def test_label_prefixed_basic_token_inside_command_is_fully_redacted() -> None:
    safe = _load()
    result = _sanitize(
        safe,
        "curl -H 'Authorization: Basic dXNlcjpwYXNz1234567890' https://example.com",  # gitleaks:allow -- fixture token, not a secret
    )
    assert result.status == "REDACTED"
    assert result.redaction_count == 1
    assert "dXNlcjpwYXNz1234567890" not in result.value
    # Surrounding text (quotes, trailing URL) survives untouched.
    assert result.value == "curl -H '[REDACTED]' https://example.com"


def test_proxy_authorization_basic_token_is_fully_redacted() -> None:
    safe = _load()
    result = _sanitize(
        safe, "Proxy-Authorization: Basic dXNlcjpwYXNz1234567890 extra-trailer"
    )
    assert result.status == "REDACTED"
    assert result.redaction_count == 1
    assert "dXNlcjpwYXNz1234567890" not in result.value
    assert result.value == "[REDACTED] extra-trailer"


def test_bare_bearer_token_without_a_label_still_redacts_via_token_pattern() -> None:
    safe = _load()
    result = _sanitize(
        safe,
        "note: captured native bd output included Bearer sk-fixture-FAKEBEARERTOKEN0123456789 in the reply",
    )
    assert result.status == "REDACTED"
    assert result.redaction_count == 1
    assert "sk-fixture-FAKEBEARERTOKEN0123456789" not in result.value


def test_non_secret_text_is_left_untouched() -> None:
    safe = _load()
    result = _sanitize(safe, "no secrets here")
    assert result.status == "OK"
    assert result.redaction_count == 0
    assert result.value == "no secrets here"


def test_password_and_api_key_labels_still_redact_as_a_single_unit() -> None:
    safe = _load()
    password = _sanitize(safe, "password: hunter2secret")
    assert password.status == "REDACTED"
    assert password.redaction_count == 1
    assert password.value == "[REDACTED]"

    api_key = _sanitize(
        safe,
        "api_key=abcd1234efgh5678",  # gitleaks:allow -- fixture token, not a secret
    )
    assert api_key.status == "REDACTED"
    assert api_key.redaction_count == 1
    assert api_key.value == "[REDACTED]"
