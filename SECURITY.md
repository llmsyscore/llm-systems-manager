# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public issue, pull request, or discussion for them.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and click **Report a vulnerability**, or go directly to
<https://github.com/llmsyscore/llm-systems-manager/security/advisories/new>.
That opens a private advisory visible only to the maintainers.

Please include:

- a description of the issue and its impact,
- steps to reproduce, with a proof-of-concept if you have one,
- the affected component (manager, alarm engine, or agent) and its version,
- any suggested remediation.

## What to expect

This is a small, maintainer-driven project, so handling is best-effort:

- acknowledgement of your report within a few days,
- an initial assessment and severity triage,
- coordinated disclosure once a fix is available.

Please give us a reasonable opportunity to address the issue before disclosing it publicly.

## Scope

In scope:

- the manager, alarm engine, and agent code in this repository,
- the installer and update scripts,
- authentication and role enforcement, the internal CA / TLS handling, and the agent ingest and control channels.

Out of scope:

- vulnerabilities in third-party dependencies (report those upstream; we update once a fix is released),
- issues that require pre-existing root/administrator access or physical access to a host,
- denial of service produced by unrealistic load against a single-operator deployment.

## Supported versions

The project ships as a rolling set of versioned components (manager, alarm engine, agent). Security fixes land on the latest version on the `main` branch. Please update to the latest version before reporting.

## Exposed secrets

If you find exposed credentials (InfluxDB tokens, SMTP passwords, agent bearer or ingest tokens) committed to the repository or present in a released artifact, report them through the private channel above so they can be rotated.
