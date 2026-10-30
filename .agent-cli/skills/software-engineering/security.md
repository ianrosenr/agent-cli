---
name: security
description: Rules for trust boundaries, input validation, authorization, secrets, and secure execution.
version: 1.0.0
---

# Security

Treat external data as untrusted unless its trust boundary is established.

MUST:

- validate untrusted input
- enforce authorization at the authoritative boundary
- use least privilege
- use parameterized database operations
- keep secrets outside source code
- avoid command construction from untrusted strings
- use vetted cryptographic and security implementations
- fail closed for access-control decisions
- minimize exposed capability

Secrets, credentials, tokens, or prohibited sensitive data MUST NOT enter logs, traces, metrics, errors, prompts, fixtures, or source control.

Security controls MUST NOT be bypassed to simplify implementation or testing.

Relevant OWASP verification guidance SHOULD be used for web-facing systems.
