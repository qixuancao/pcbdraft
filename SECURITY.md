# Security policy and threat model

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
- KiCad/Codex stdout, stderr, and generated reports;
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
- Codex arguments pin model/reasoning/tier and disable network, hooks,
  multi-agent, project config, approval escalation, and privileged tools.

Tests in `tests/test_security.py`, `tests/test_process.py`,
`tests/test_transactions.py`, and `tests/test_integration.py` exercise these
boundaries.

## Residual risks

Codex `read-only` is a tool policy, not an OS security boundary. A process running
as your account can generally read what that account can read, and reviewed data
may be sent to the configured service. Use a container or VM for hostile or
confidential projects and disclose only authorized data.

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

The runtime never reads or prints authentication tokens. It reuses Codex's existing
authenticated CLI state. Receipts record redacted argv and bounded tool versions,
not the full environment. Do not commit confidential receipts, model events, or
board designs without reviewing them.
