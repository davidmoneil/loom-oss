"""Default DLP scanner rules.

Embedded rules used when no external rules file is configured. Each rule
defines a sensitive data type with regex patterns and a default action.
"""

DEFAULT_RULES = [
    {
        "name": "ssn",
        "description": "US Social Security Numbers",
        "enabled": True,
        "action": "redact",
        "patterns": [r"\b\d{3}-\d{2}-\d{4}\b"],
        "mask_format": "***-**-{last4}",
    },
    {
        "name": "credit_card",
        "description": "Credit/debit card numbers (Visa, MC, Amex, Discover)",
        "enabled": True,
        "action": "mask",
        "patterns": [
            r"\b4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
            r"\b5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
            r"\b3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}\b",
            r"\b6(?:011|5\d{2})[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
        ],
        "mask_format": "****-****-****-{last4}",
    },
    {
        "name": "email",
        "description": "Email addresses",
        "enabled": False,
        "action": "redact",
        "patterns": [r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"],
    },
    {
        "name": "phone_us",
        "description": "US phone numbers",
        "enabled": True,
        "action": "mask",
        "patterns": [r"\b(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"],
        "mask_format": "(***) ***-{last4}",
    },
    {
        "name": "aws_key",
        "description": "AWS Access Key IDs",
        "enabled": True,
        "action": "redact",
        "patterns": [r"\bAKIA[0-9A-Z]{16}\b"],
    },
    {
        "name": "api_key_generic",
        "description": "Generic API keys and tokens",
        "enabled": True,
        "action": "redact",
        "patterns": [
            r"\bsk-[a-zA-Z0-9]{20,}\b",
            r"\bghp_[a-zA-Z0-9]{36}\b",
            r"\bglpat-[a-zA-Z0-9\-]{20,}\b",
            r"\bxoxb-[a-zA-Z0-9\-]{20,}\b",
        ],
    },
    {
        "name": "bearer_token",
        "description": "Bearer authentication tokens",
        "enabled": True,
        "action": "redact",
        "patterns": [r"(?<=Bearer )[A-Za-z0-9_/+=\-]{20,}"],
    },
    {
        "name": "ip_address",
        "description": "Public IPv4 addresses (private ranges excluded)",
        "enabled": False,
        "action": "redact",
        "patterns": [
            r"\b(?!10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
        ],
    },
    {
        "name": "private_key",
        "description": "PEM private keys",
        "enabled": True,
        "action": "redact",
        "patterns": [r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"],
    },
    {
        "name": "jwt",
        "description": "JSON Web Tokens",
        "enabled": True,
        "action": "mask",
        "patterns": [
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ],
        "mask_format": "eyJ...{last8}",
        "streaming_mode": "passthrough",
    },
]
