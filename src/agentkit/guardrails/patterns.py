"""The redaction patterns that `guardrails.redact_patterns` can name."""

import re

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "api_key": re.compile(
        r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[abps])[-_][A-Za-z0-9_\-]{16,}"
        r"|\bAKIA[0-9A-Z]{16}\b"
        r"|\bBearer\s+[A-Za-z0-9._\-]{20,}"
    ),
}
