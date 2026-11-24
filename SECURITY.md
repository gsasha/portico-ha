# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

* Preferred: use GitHub's private vulnerability reporting on this repository
  (Security tab, "Report a vulnerability").
* Otherwise email gsasha@gmail.com with a description, the Portico and Home Assistant versions, and steps to
  reproduce.

You can expect an acknowledgement within a few days. Portico is maintained by one person in spare time, so a fix
may take longer; please allow a reasonable period before disclosing publicly.

## Scope

The integration (`custom_components/portico`), the transport binary (`transport-rs`) and the packaging scripts.
Secrets (API keys, enrolment codes, push credentials) must never be placed in issues or pull requests; see
[docs/reference/keys-and-secrets.md](docs/reference/keys-and-secrets.md) for where each one lives.
