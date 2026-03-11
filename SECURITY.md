# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Device Connect, please report it responsibly. **Do not open a public GitHub issue.**

Instead, please report vulnerabilities via [GitHub's private vulnerability reporting](https://github.com/arm/device-connect/security/advisories/new).

### What to include

- Description of the vulnerability
- Steps to reproduce
- Affected package(s) (`device-connect-sdk`, `device-connect-server`, `device-connect-agent-tools`)
- Impact assessment (what an attacker could do)
- Suggested fix (if you have one)

### What to expect

- **Acknowledgment** within 3 business days
- **Assessment** within 10 business days
- **Fix or mitigation** timeline communicated after assessment
- Credit in the release notes (unless you prefer to remain anonymous)

## Scope

This policy covers all packages in the Device Connect monorepo:

| Package | Scope |
|---------|-------|
| `device-connect-sdk` | Messaging clients, device runtime, credential handling |
| `device-connect-server` | Registry service, JWT/TLS security, state store, CLIs |
| `device-connect-agent-tools` | Agent connection, MCP bridge, tool invocation |

## Supported Versions

Security fixes are applied to the latest release on `main`. We do not backport fixes to older versions.
