# Security Policy

The M3Undle Provider Simulator is a test fixture: it serves only synthetic
media and scenario-defined fault behavior, never real provider content,
credentials, or production data. Its threat model is "safe to run against
your own test client on your own machine," not a hardened, internet-facing
service — do not expose it to untrusted networks.

## Reporting a vulnerability

If you find a security issue (e.g. a way for a malicious scenario file or
crafted client request to cause something worse than the simulator's own
documented fault behavior), please report it privately via [GitHub Security
Advisories](../../security/advisories/new) for this repository rather than
opening a public issue, so a fix can land before public disclosure.

Please include:
- The scenario file or request that triggers the issue.
- What you expected vs. what happened.
- Whether it's reproducible with only the public `scenarios/core/` examples.

## Supported versions

Only the latest tagged release is supported. There is no long-term-support
branch.
