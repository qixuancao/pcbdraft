# PCBDraft roadmap

PCBDraft is currently a `0.1.0` alpha for small, non-safety-critical KiCad
prototypes. This roadmap describes desired outcomes, not shipped features or
delivery dates. A capability counts as released only when the public version,
documentation, and reproducible evidence refer to the same commit.

## Where the project is now

The current codebase provides a terminal and browser interface, a configured
model-driven planning flow, native KiCad project generation, bounded PCB editing
operations, and project-local checks and evidence. Projects can be kept in a
repository and reopened for later work.

Those are engineering capabilities, not proof of broad usefulness. The project
does not yet have enough documented external trials to distinguish product
problems from installation friction, unclear positioning, model variability, or
low visibility. ERC, DRC, and software fixtures also do not prove that a board is
functionally correct, manufacturable, or safe.

## Near-term outcomes

### 1. A dependable first session

A new user should be able to install PCBDraft, configure a provider, run
diagnostics, create a project, and understand the next action when any step fails.
The documented path must cover the supported Python and KiCad versions and avoid
assuming an existing developer checkout.

The same user should be able to restart PCBDraft, find a recent project, resume
it without knowing an internal ID, and see clearly whether work is active,
interrupted, awaiting review, or blocked.

### 2. Three reproducible examples

Publish three small examples: an LED indicator, a passive RC filter, and an I2C
pull-up adapter. Each example must report rather than imply its result and include:

- the exact PCBDraft revision, OS, Python version, and KiCad version;
- the request and non-secret provider/model settings;
- elapsed time plus model usage and estimated cost when the provider exposes it;
- the native KiCad artifacts and the checks that actually ran; and
- every failed, skipped, or unavailable check, including the absence of physical
  fabrication and measurement.

An example is still useful when it fails reproducibly. None of these examples is
a production-readiness claim or a fixed-board branch in the implementation.

### 3. One coherent release surface

Align the public default branch, install scripts, package metadata, changelog,
documentation, CI commands, tag, and GitHub Release to one verified revision.
Installation instructions should identify what was installed, and CI must invoke
commands that exist in that revision. A released build should not depend on
unpublished development-branch behavior.

Release only after the required CI and release checks pass for that exact
revision. A local development checkpoint is not a public release.

### 4. Evidence from external trial users

Invite an initial group of roughly 5–10 external users to attempt the first-board
and resume paths. Record install completion, time to the first native project,
where users stop, whether they can reopen the project, model cost when available,
and the failure classes they encounter. Do not collect private board content or
credentials.

Use that evidence to choose the next product work. The trial is intended to
discover problems; it is not a promised adoption number or a success claim.

## After the onboarding evidence

Only then prioritize broader engineering work according to observed failures:

- clearer review, recovery, semantic diffs, and KiCad handoff;
- better generic electrical checks and component-evidence handling;
- mounting holes, outline intent, current classes, stackup-aware constraints,
  and rule-area support; and
- bounded placement and routing improvements with inspectable failure reasons.

Passing a small corpus must never be restated as arbitrary-board support.

## Non-goals

PCBDraft is not a replacement for KiCad, a source of certified component data,
or an authority for production approval. It must not let a model write unchecked
native files or shell commands, hide an incomplete route, infer production
readiness from ERC/DRC, or claim human review, fabrication, assembly, or physical
test that did not happen.

Safety-critical, medical, aviation, mains-voltage, high-power, and production
certification workflows are outside the current product scope. Future support
would require explicit domain evidence and independent expert review, not a
larger prompt or a passing software fixture.
