"""Secret handling helpers: redaction of anything that looks like a credential."""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                 # Google API keys
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)authorization:\s*(?:basic|bearer)\s+[A-Za-z0-9+/=._\-]+"),
    re.compile(r"(?i)(x-goog-api-key|x-api-key|x-github-token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)([?&]key=)[A-Za-z0-9_\-]{20,}"),
]


def redact(text: str, *extra_secrets: str | None) -> str:
    """Remove known secrets and anything shaped like an API key from `text`."""
    if not text:
        return text
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[redacted]")
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + "[redacted]") if m.lastindex else "[redacted]", text)
    return text
