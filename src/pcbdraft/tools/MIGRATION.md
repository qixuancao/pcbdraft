# Tools-owned native runtime migration

## Change manifest

All paths below are relative to `src/pcbdraft/tools/`, except the test file.

- Execution backends: `environments/base.py`, `daytona.py`, `docker.py`,
  `file_sync.py`, `local.py`, `managed_modal.py`, `modal.py`, `singularity.py`,
  `ssh.py`, `vercel_sandbox.py` (all under `environments/`).
- Runtime, process and file handling: `approval.py`, `checkpoint_manager.py`, `code_execution_tool.py`,
  `credential_files.py`, `env_passthrough.py`, `file_operations.py`, `file_tools.py`,
  `process_registry.py`, `self_repo_guard.py`, `subagent_worktree.py`,
  `terminal_tool.py`, `tirith_security.py`, `tool_result_storage.py`.
- Tool routing and ownership: `dispatch.py`, `registry.py`, `plugin_guard.py`,
  `tool_backend_helpers.py`, `tool_search.py`, `toolsets.py`, `delegate_tool.py`,
  `delegation_live_log.py`, `yuanbao_tools.py`.
- Skills and metadata: `blueprints.py`, `bot_mode_probe.py`, `skill_linter.py`,
  `skill_manager_tool.py`, `skills_guard.py`, `skills_hub.py`, `skills_sync.py`,
  `skills_sync_client.py`, `skills_tool.py`, `threat_patterns.py`.
- Browser and desktop: `browser_camofox.py`, `browser_camofox_state.py`,
  `browser_supervisor.py`, `browser_use_cli.py`, `close_terminal_tool.py`,
  `read_preview_tool.py`, `read_terminal_tool.py`, `read_window_tool.py`;
  `computer_use/browser_route.py`, `cua_backend.py`, `doctor.py`, `permissions.py`,
  `schema.py`, `tool.py` (the latter six under `computer_use/`).
- MCP and integrations: `mcp_oauth.py`, `mcp_oauth_manager.py`, `mcp_tool.py`,
  `setup_mcp_tool.py`, `discord_tool.py`, `kanban_tools.py`, `lazy_deps.py`,
  `microsoft_graph_client.py`, `osv_check.py`, `send_message_tool.py`,
  `session_search_tool.py`, `url_safety.py`.
- Media, voice and provider identity: `flux3_video_tool.py`,
  `image_generation_tool.py`, `transcription_tools.py`, `tts_tool.py`,
  `video_generation_tool.py`, `vision_tools.py`, `voice_mode.py`, `wake_word.py`,
  `x_search_tool.py`, `xai_http.py`, `wakewords/README.md`.
- New helpers/documentation: `acp_edit_approval.py`, `legacy_metadata.py`,
  `media_cache.py`, this file.
- Dedicated offline tests: `tests/tools/test_native_migration.py` and
  `tests/tools/test_review_migration.py` (review fault/concurrency regressions).

## Ownership and path contract

Local state is resolved by `pcbdraft.core.runtime_environment` (including its
context-local profile override and `get_pcbdraft_dir` cache-layout compatibility).
The tools layer does not discover, import, rename, merge, or clean a standalone
Hermes home. Core owns migration of PCBDraft's own previous runtime directory.

| Producer / consumer | Native location or namespace |
| --- | --- |
| Root Docker / Modal sandbox runtime | `/root/.pcbdraft/runtime` |
| SSH runtime | `<remote_home>/.pcbdraft/runtime` |
| Daytona / Vercel runtime | `<detected_remote_home>/.pcbdraft/runtime` |
| Local runtime / profiles / managed toolchain | Core runtime helpers |
| Project configuration protection | `.pcbdraft` (also protects another application's `.hermes`) |
| Docker creation, reuse, orphan sweep | `pcbdraft-*`, `pcbdraft-agent=1`, native task/profile/egress labels |
| Modal app | `pcbdraft` |
| Managed Modal logical reuse key | `pcbdraft:<task_id>` |
| Daytona sandbox name / owner | `pcbdraft-*`, `pcbdraft_task_id`, `pcbdraft-owner` |
| Singularity instance / overlays | `pcbdraft_*`, `pcbdraft-overlays` |
| Generated Python RPC module | `pcbdraft_tools.py` |
| Plugin module namespace | `pcbdraft_plugins.<slug>` |
| Platform toolsets | `pcbdraft-*` |

`credential_files` produces credential/skill/cache mappings. Docker mounts these
mappings; SSH, Modal, Daytona and Vercel use `environments.file_sync` for upload
and return synchronization. Their tar archive member roots, image-path mapping,
file-tool mirror protection and exported `PCBDRAFT_RUNTIME_HOME` agree. Managed
Modal uses its existing gateway protocol and does not implement host file sync.
Singularity uses explicit read-only credential, skill and cache binds under
`/root/.pcbdraft/runtime`. Its `--containall` / `--no-home` isolation disables the
implicit host-home mount. Runtime exports, image hints, and forward/reverse media
path mapping all use that explicit mirror. Failed mount preparation aborts startup.

Temporary session artifacts (shell snapshots, RPC sockets, SSH control sockets,
background logs and voice scratch files) use native prefixes. They are distinct
from persistent runtime state.

## Data and protocol compatibility

- New embedded skill metadata uses `metadata.pcbdraft`. The tools helper
  `legacy_metadata.read_pcbdraft_metadata` adapts its metadata-block argument to
  the agent-owned `legacy_compat.read_skill_metadata` frontmatter API. It preserves
  legacy-only fields, lets native fields win (including explicit empty values),
  and does not rewrite source documents. Blueprint, hub, linter and skill-view
  consumers share this reader. Contributor/author attribution is not relabeled.
- Checkpoints copy `refs/hermes/*` into absent `refs/pcbdraft/*` **inside the
  PCBDraft-owned shadow store**. Existing native refs and old commit objects are
  retained. Migration, list/status/diff/rollback, snapshot creation, ref deletion,
  retention/size pruning, GC and clearing share a reentrant cross-process store
  lock. The lock file lives outside the deletable checkpoint base.
  `git update-ref --stdin` publishes all missing native refs and a versioned
  completion ref in one verified `start` / `prepare` / `commit` transaction;
  object/reference fsync is requested. Completion content is validated before
  use. A valid previous `pcbdraft-refs-migrated` file is upgraded without copying
  old refs again. Failed/incomplete state blocks maintenance and is not reported
  as an empty history or successful prune. All existing-store entry points run
  migration first, including the first list/status/rollback after an upgrade.
  New checkpoint attribution is native.
- MCP token/client/server metadata files keep their formats and server keys.
  MCP protocol versions, OAuth registered client IDs, explicit client names and
  provider-specific registration requirements are preserved. New default client
  metadata and real MCP initialize messages identify PCBDraft.
- Internal environment markers, process attributes, generated shell markers,
  RPC module names, toolset keys and plugin module names move together. Old
  ephemeral protocols are not aliases for native ownership: restart running
  sessions/workers during rollout and update integrations to the native names.
- Docker never sweeps or reuses old `hermes-*` ownership labels. No automatic
  transfer of a standalone application's containers is attempted.
- The plugin entry-point loader is owned outside this directory. The registry
  now agrees with its native `pcbdraft_plugins` namespace and `pcbdraft.plugins`
  entry-point group. Public `TOOLSETS['pcbdraft']` stays the same closed nine-tool
  PCB interface; platform bundle renames do not add tools to it.

## Offline content defaults

The default skills router only reads locally packaged optional skills. There is
no default online index, GitHub tap, optional-skill repository fallback or sync
service URL. No PCBDraft content URL has been invented.

Explicit third-party configuration is supported through:

- `PCBDRAFT_RUNTIME_INDEX_URL` for a supplied index URL. Its cache is keyed by URL
  and its content remains `configured-index` / community trust, even if the
  remote JSON claims `official` or `builtin`.
- Existing explicitly stored GitHub taps.
- `PCBDRAFT_RUNTIME_SKILL_SOURCES`, a comma-separated opt-in list of `skills-sh`,
  `well-known`, `url`, `clawhub`, `lobehub`, or `browse-sh`.
- Existing `sync.base_url` / `PCBDRAFT_RUNTIME_SYNC_BASE_URL` plus the separate
  sync enable/eligibility controls. The existing third-party sync wire format
  and authentication contract are preserved.

## Media and approvals

`media_cache` provides bounded local image/audio/document storage without
`services.messaging`. It shares cache roots with sandbox path translation,
sanitizes filename hints and creates unique private files. MCP media markers
and document references keep their existing consumer-visible formats.

`acp_edit_approval` treats a genuinely absent optional ACP integration as inactive.
ACP owners can bind `set_acp_edit_approval_enabled` / reset its token, or launch
with `_PCBDRAFT_ACP=1`. When active, missing modules and guard failures still reach
the dispatcher's fail-closed write/patch handling. Broken transitive imports in
an installed adapter are not treated as an absent integration.

## Tirith installation consent

`tirith_enabled` enables use of an installed scanner, not downloading one.
Both `ensure_installed()` and command checks use PATH / configured / managed
local binaries offline by default. Missing binaries retain the configured
fail-open/fail-closed scan behavior without starting an installer thread or
writing download-failure state.

Download permission is separate: `security.tirith_allow_download: true`,
`TIRITH_ALLOW_DOWNLOAD=1`, or an explicit `ensure_installed(allow_download=True)`
opt-in permits installation. `allow_download=False` forces an offline ensure
even when configuration permits downloading. Existing download verification and
retry controls remain in place. Installed binaries can be used even when the
platform has no upstream downloadable build.

## Preserved exceptions

The executable legacy-string audit permits only protection patterns in
`approval`, `file_tools`, `skills_guard`, `threat_patterns`, the centralized
legacy metadata reader and the checkpoint ref migration. Python identifiers
have no Hermes spelling. Runtime command guidance uses supported PCBDraft
commands (`connect`, `doctor`, `--help`) or describes explicit configuration.

The original `wakewords/hey_hermes.onnx` and `.tflite` files retain their actual
trained labels and provenance. They are archived third-party resources, not
default or relabeled PCBDraft models. OpenWakeWord requires an explicit model;
the default wake phrase is empty. Third-party model IDs, copyright/provenance
and upstream historical commentary are retained where they describe originals.
Historical comments/docstrings are not a current runtime path or CLI contract.

## Offline verification

Run the dedicated module with a 90-second batch budget:

```sh
timeout 90 .venv/bin/python -m unittest discover -s tests/tools -p 'test_*migration.py' -v
git diff --check
.venv/bin/ruff format --check src/pcbdraft/tools tests/tools
```

Tests exercise upload/return mapping, upload-only credential protection, foreign
tree exclusion, SSH tar commands, native Docker owner filters, managed reuse and
execution payloads, shell environment/CWD markers, RPC code generation, metadata
merge behavior, checkpoint ref preservation, plugin ownership, MCP media and
identity, offline catalogs, model selection and ACP absent/broken branches.
Network, container and remote-shell operations are mocked; no live services or
container acceptance runs are needed for this module.

Review regressions also use isolated temporary bare Git stores and spawned test
processes: rejected ref batches, lost commit acknowledgements, process death
before/after the ref transaction, first-read migration, corrupted completion
state, and migration racing delete/prune/clear. Fixtures use Git object/ref
plumbing; they do not create working-branch commits or contact remotes.
