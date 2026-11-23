# Security policy

Gatewright runs commands, handles secrets and produces artifacts that people rely on, so security reports
are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Instead, use GitHub's private reporting:
*Security → Report a vulnerability* on the repository page (or contact the maintainers listed in the
repository profile).

Include what you found, how to reproduce it, the affected version (`gatewright --version`) and the
impact as you see it. You'll get an acknowledgement within a week. Once a fix is released, you'll be
credited unless you prefer not to be.

## Supported versions

Only the latest release gets security fixes while Gatewright is below 1.0.

## Things to know (by design, not vulnerabilities)

- Jobs run **with your user's permissions** unless they use `image:` (containers). Approving a pull
  request runs its code. For untrusted pull requests (forks, unlisted authors) Gatewright withholds
  secrets and uses your own job definitions, but their tests and build scripts still run. Use
  containers for anything you don't fully trust (`vcs.untrusted: containers` enforces that).
- The signing key created by `gatewright keygen` has no passphrase unless you ask for one (`--passphrase`, then
  ssh-agent signs), so unattended runs can sign. Protect the key file.
- Opening a local build runs its code as you. It gets no secrets, unless you open it as a project of its own.
- Webhook deliveries are only accepted with a valid signature/token. Keep the listener on
  `127.0.0.1` behind a reverse proxy or tunnel.
- Secret masking covers the value and common encodings (base64, hex, URL, JSON). Split, compressed or encrypted
  secrets in tool output are not masked.
- Without `signing:`, artifact checksums only detect accidental changes. With signing, use
  `require_signature` on environments that matter.

What must never regress is listed in [docs/project.md](docs/project.md#security-decisions-that-must-not-regress).
The points above are listed in [docs/known-issues.md](docs/known-issues.md#limitations).
