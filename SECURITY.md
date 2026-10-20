# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| < 0.3   | :x:                |

Great Sage is under active development. Only the latest `main` is supported for security fixes.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Use one of the following:

1. **GitHub Private Vulnerability Reporting** (preferred once the repository is public):
   GitHub → *Security* → *Report a vulnerability*.
2. **Email**: open an issue with label `security` and request a private channel — maintainers will provide a contact.

Please include:

- Affected component (`greatsage/tools`, `greatsage/agent`, `greatsage/delegation`, etc.)
- Steps to reproduce
- Impact assessment (what an attacker could do, what is protected)
- Whether the issue is in a default configuration or requires custom `config/sage.yaml`

## What to Expect

- Acknowledgement within 72 hours.
- Fix or mitigation plan within 14 days for critical issues.
- Coordinated disclosure — we will publish a `SECURITY` advisory and credit the reporter unless you prefer anonymity.

## Security Model

The authoritative in-repo policy is `docs/SECURITY_MODEL.md`:

- Every tool execution goes through `Great Sage SecurityPolicy` (allow/ask/deny + risk levels `safe/low/medium/high/critical`).
- No unrestricted filesystem, no blanket approvals, no `shell=True`, no automatic `git` operations.
- Delegation (`greatsage/delegation`) is bounded: depth ≤1, wall-clock/output/permission/session limits, event-poll reconnect limit = 3 (2s poll interval), prompts never in events, protected paths denied.
- Secrets are env-only (`OPENCODE_API_KEY` via `api_key_env`), scrubbed from tool environments and logs.
- Phase 9 hardening (`docs/SECURITY_MODEL.md` §10–§11): secret-format redaction of audit strings, sensitive-value denial (`env`/argv), `.env.*`/`.envrc`/sqlite-sidecar protection on all path args, tighter shell classification (installers, LOLBins, encoded PowerShell), no prompt content in agent events, voice/vision threat-model contract.

See also `docs/TOOLS.md`, `docs/AGENTS.md`, `docs/CONFIGURATION.md`.

## Scope Clarifications

- Do **not** report missing `LICENSE`/`SECURITY.md` (already addressed).
- Do **not** use `C:\GREATSAGE\` defaults as a finding — they are intentional documented defaults (`config/sage.example.yaml`, `greatsage/configuration/defaults.py`).
- Test fakes (`tests/`) using `sk-test` or placeholder tokens are not real credentials.

## No Secrets in the Repository

If you believe a real credential, private key, or personal database was accidentally committed, report it as **critical** — Git history may need purging before the push is mirrored.

