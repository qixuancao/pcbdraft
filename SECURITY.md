# PCBDraft security policy and threat model

## Supported versions

The current repository version is supported. There is no remote service operated
by this project and no security update SLA yet. Report suspected vulnerabilities
privately to the repository maintainer through GitHub's private vulnerability
reporting feature; do not attach proprietary board data to a public issue.

## Trust boundaries

Treat as untrusted:

- all project files, names, paths, metadata, archives, and external evidence;
- requirements and semantic change sets supplied by callers;
- model prompts and model output;
- KiCad/model API responses and generated reports;
- repository-local configuration or instructions found inside a reviewed project.

The runtime itself, its bundled catalogs, its isolated worker, installed KiCad
libraries, and the invoking user's configured executables form the local trusted
computing base. `doctor` reports executable paths/versions but does not attest to
their provenance.

## Implemented controls

- strict allow-list schemas and independent document versions;
- finite-number, UTF-8 byte, JSON nesting, file/member, and total-size limits;
- canonical create-only output paths;
- symlink, hardlink, special-file, traversal, ambiguity, and tree-size rejection;
- subprocess wall-clock and combined-output limits with process-group termination;
- no login shell and no shell interpolation for runtime subprocesses;
- system `pcbnew` worker launched with Python isolated mode and an internally
  generated bounded job;
- atomic same-directory writes, fsync, manifests, pre/post hashes, and locks;
- staging, backups, rollback, undo, and recovery receipts;
- exact archive inventory, safe-name, hash, encryption, timestamp, and expansion
  verification;
- model requests use a bounded JSON schema and the credential is sent only in
  the HTTPS Authorization header; prompts and API keys are not written to receipts.
- `/connect` writes credentials only to the user-owned PCBDraft config with mode
  `0600`; project files and application records never contain provider secrets.
- the browser binds to loopback only, rejects non-loopback configuration, validates
  Host and Origin, requires per-process CSRF tokens for writes, sends a restrictive
  CSP/security headers, limits request bodies, and serves only allow-listed project
  artifacts;
- private application workspaces, per-project locks, durable jobs/events, explicit
  confirmation, and restart recovery prevent browser/terminal races and implicit
  replay of interrupted side effects;
- provider output uses an exact bounded schema and is normalized and scope-checked
  before deterministic code can act on it.

Tests in `tests/integration/test_security.py`, `tests/core/test_process.py`,
`tests/services/test_transactions.py`, `tests/integration/test_workflows.py`, and
`tests/services/test_application.py` exercise these boundaries.

## Residual risks

The model API boundary is not a security boundary for data sent to a provider. A
configured service receives the bounded project evidence needed for its task. Use
a container or VM for hostile or confidential projects and disclose only authorized
data.

KiCad and its libraries are large native-code dependencies. Crafted files may
exercise vulnerabilities below this Python runtime. Keep KiCad patched and use OS
isolation for adversarial inputs.

Atomic replacement is limited to one filesystem. Whole managed-project publication
uses directory rename and a retained backup, but power loss or filesystem failure
can still require explicit recovery. Use version control and durable backups.

External L4/L6/L7 artifacts are copied, hashed, and attributed, not cryptographically
signed or independently authenticated. The runtime cannot prove supplier/fabricator
claims, reviewer identity, test-lab competence, board serial provenance, or physical
measurement integrity.

No check in this project is a safety certification, regulatory approval, or
substitute for qualified engineering review.

## Secrets and privacy

The TUI masks API-key entry and stores the key only in PCBDraft's private TOML
configuration. PCBDraft does not copy credentials into subprocess arguments or
persist them in projects, conversations, jobs, events, diagnostics, or model
receipts. User text is redacted for common credential forms before provider
invocation and durable storage.

Receipts record redacted argv and bounded tool versions, not the full environment.
The configured model endpoint receives the normalized conversation needed to
interpret the request; use the offline provider for data that must not leave the
machine. Do not commit confidential receipts, model events, or board designs
without reviewing them.

The local web UI is not a multi-user service. Loopback prevents ordinary remote
access, but another process running as the same OS user may still read the private
workspace or connect locally. Use separate accounts or a VM when users do not share
a trust boundary.
