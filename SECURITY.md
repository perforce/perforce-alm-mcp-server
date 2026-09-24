# Security Policy

The Perforce ALM MCP Server connects to the Perforce ALM REST API and handles credentials (API key or Basic auth) and connection configuration.

Security is important to us. If you discover a security vulnerability, please report it responsibly.

---

## Supported Versions

Only the latest released version is supported with security updates.

---

## Reporting a Vulnerability

Please **do not open a public issue** for security vulnerabilities.

Instead:

1. Use GitHub Security Advisories (if enabled), or
2. Contact a maintainer privately, or
3. Report via our dedicated support email for vulnerabilities

Include the following information:

* A description of the vulnerability
* Steps to reproduce (if applicable)
* Its potential impact
* Suggested mitigation (if known)

Please remove any sensitive credentials before sharing logs.

---

## What Qualifies as a Security Issue?

Examples include:

* Credential exposure or leakage (API keys, Basic auth, Bearer tokens)
* Path traversal or arbitrary file read/write via attachment upload or download
* Unsafe default behavior that allows unintended destructive operations
* Improper permission or authorization handling
* Vulnerable or malicious dependencies

If you are unsure whether something qualifies as a security issue, report it privately.

---

Thank you for helping keep the Perforce ALM MCP Server secure.
