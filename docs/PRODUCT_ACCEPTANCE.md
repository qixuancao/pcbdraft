# CopperWright 1.0 product acceptance

The historical R01–R44 report proves the deterministic engineering runtime that
underlies CopperWright. It does **not** prove an end-user product. This is the
separate application-level completion record for the bounded 1.0 release. The
historical report and artifacts remain unchanged.

Status meanings:

- `implemented`: local production code and an automated verification path exist;
- `verified`: the path was also exercised through a clean installed wheel with the
  real tool named in the evidence column;
- `external gate`: software can record and enforce the state, but only a supplier,
  fabricator, qualified reviewer, lab, or physical board can provide the fact.

## Requirement traceability

| ID | Product requirement | Implementation | Automated verification / persisted evidence | Status |
|---|---|---|---|---|
| PA01 | One authoritative service for CLI, chat, and browser | `pcb_agent.application.ApplicationService`; `pcb_agent.chat`; `pcb_agent.webapp` | `tests/test_application.py`; both product E2E scripts use the same persisted records | verified |
| PA02 | Guided natural-language create/open lifecycle | `ApplicationService.create_project`, `send_message`, `open_project`; `copperwright chat`; project list in browser | `test_supported_request_stops_at_reviewable_confirmation`; `scripts/chat-e2e.sh`; `scripts/browser-e2e.py` | verified |
| PA03 | Focused clarification and assumptions | strict `IntentInterpretation`; required layer clarification; normalized brief builder | application unit tests and both E2E journeys | verified |
| PA04 | Brief, scope decision, BOM, interfaces, and constraints before generation | application review projection and browser review cards | browser E2E asserts these fields while no design directory exists | verified |
| PA05 | Explicit confirmation before create/change side effects | `awaiting_confirmation` state; separate confirm methods; UI/terminal confirmation actions | unit assertion that design is absent pre-confirmation; browser/chat E2E | verified |
| PA06 | Real native KiCad project generation | existing deterministic compiler, placement/router, schematic/PCB/project backends called by application jobs | all three `examples/product_profiles`; real KiCad managed-pipeline tests and E2E | verified |
| PA07 | Real previews and direct artifact paths | `pcb_agent.previews`; artifact allow-list; schematic PDF/SVG, PCB SVG/PNG, 3D render; open-in-KiCad action | browser E2E loads a real image and asserts artifact actions; example preview receipts | verified |
| PA08 | Honest L0–L7 summary and actionable findings | existing validation ladder projected by application service and browser UI | application/UI tests; browser E2E sees all eight levels and external gates | verified |
| PA09 | Conversational semantic preview/apply/discard/undo | typed change set, isolated staged regeneration/validation, hash preconditions, atomic publish, backup | application tests; both E2Es change 2→4 layers and undo the exact content hash | verified |
| PA10 | Candidate export and offline verification in UI and CLI | application release job wraps deterministic release and `release.verify` | both E2Es assert candidate true, production false, and offline verification true | verified |
| PA11 | Restart/reopen without lost authoritative state or duplicate side effects | private project store, append-only messages/events, durable job/transaction records, startup recovery | restart recovery and migration tests; browser E2E restarts the server and reopens the released project | verified |
| PA12 | Structured progress, cancellation, retry, and useful job states | `pcb_agent.jobs.JobRunner`; typed event stream; SSE browser endpoint; cooperative safe-boundary cancellation | queued cancel/retry unit test; browser E2E waits for terminal jobs; retry/cancel actions are state-gated | verified |
| PA13 | Concurrent project protection and crash-safe publication | per-project `ResourceLock`, job exclusivity, transaction locks, atomic records, existing runtime recovery | application contention/restart tests plus transaction/integration suites | verified |
| PA14 | First-run diagnostics and provider setup without browser secrets | diagnostics/setup dialog exposes instructions and provider status only; no credential field | browser security/shell tests and real setup-dialog browser step | verified |
| PA15 | Authenticated Codex as a first-class provider | `CodexIntentProvider` with pinned bounded read-only invocation and strict output schema | real authenticated provider probe; schema regression unit test | verified |
| PA16 | Model-independent OpenAI-compatible provider | `OpenAICompatibleIntentProvider` using runtime environment configuration and Chat Completions schema | local HTTP contract test checks request shape and runtime-only Authorization header | verified |
| PA17 | Offline/no-account operation | `BuiltinIntentProvider` for the exact supported language and clarification contract | provider profile tests; both complete E2Es run with `--provider builtin` | verified |
| PA18 | Model output is untrusted and cannot own engineering decisions | exact JSON schema, response/prompt limits, normalization, scope validation; deterministic compile thereafter | malformed-shape, schema, secret-redaction, unsupported-profile, and security tests | verified |
| PA19 | Secrets stay outside browser/project/transcript/logs | environment-only API key lookup, input sanitization/redaction, safe diagnostics, no secret web endpoint | provider secret test scans the project tree; browser security tests | verified |
| PA20 | Stable project format and 0.2.0 migration | application schema `copperwright-project` v1 plus read-only import of intact managed 0.2 projects | real managed-project import/restart/hash-preservation test | verified |
| PA21 | Local-only browser and web security | default `127.0.0.1`; non-loopback refusal; Host/Origin/CSRF checks; limits; artifact allow-list; CSP | `BrowserSecurityTests`; browser E2E runs on loopback with clean HOME | verified |
| PA22 | Accessible, responsive, coherent CopperWright UI | semantic HTML, labels, focus styles, keyboard controls, responsive layout, brand assets, explicit empty/loading/success/failure/unsupported states | browser shell assertions and real 1440×1000 screenshots | verified |
| PA23 | I2C practical profile | `low_voltage_i2c_controller_v1`: ATtiny402/TMP102/Qwiic/UPDI/LED, 2/4 layers | committed project/validation/previews; real three-profile native test | verified |
| PA24 | SPI practical profile | `low_voltage_spi_environment_v1`: ATtiny402/BME280 four-wire mode 0 at 1 MHz/UPDI, 2/4 layers | committed project/validation/previews; browser E2E; real three-profile native test | verified |
| PA25 | UART plus common LDO profile | `low_voltage_uart_ldo_controller_v1`: regulated 5 V input/AP2112K/3.3 V CMOS UART/UPDI/LED, 2/4 layers | committed project/validation/previews; real three-profile native test | verified |
| PA26 | Parts→blocks→requirements→rules→KiCad→ERC/DRC chain for every advertised profile | trusted part graph, verified block catalog, three compilers, profile-specific L3 contracts | part/block/requirements/semantic/managed-pipeline tests and example acceptance summaries | verified |
| PA27 | USB, buck, RS-232, unverified board dimensions, and high-risk requests fail explicitly | provider and scope/profile policy with reasoned unsupported states | provider/scope/API/envelope tests and E2E unsupported request assertion | verified |
| PA28 | Scriptable existing commands and `pcb-agent` alias remain compatible | existing CLI/API retained; new `chat`/`app` are additive; unchanged `pcb_agent` module/schema identifiers | CLI/unit/compatibility tests; clean wheel invokes both executables | verified |
| PA29 | Clean install, locked dependency, packaging, CI-ready commands | `pyproject.toml`, `uv.lock`, deploy/test/compatibility/release scripts, GitHub Actions KiCad setup | `scripts/release-check.sh` builds and installs wheel/sdist before product/runtime E2E | verified |
| PA30 | Multilingual onboarding and Chinese chat/app quick start | canonical English README plus Simplified Chinese, Japanese, and Korean aligned translations | branding/readme structure-link-command consistency test | verified |
| PA31 | License-clear reuse and independent implementation | Apache-2.0/CC0 notices and `docs/OPEN_SOURCE_REUSE.md` | package-member/license tests and documented upstream license links | verified |
| PA32 | Dogfood primary user journey and persist concise evidence | clean-HOME Firefox and terminal journeys; three committed native examples | `artifacts/product-e2e/`; `docs/PRODUCT_REPORT_ZH.md` | verified |
| PA33 | Live sourcing and selected-fabricator capability | attributed L4 evidence importer/gate only | absent in shipped examples; candidate may pass but production remains false | external gate |
| PA34 | Qualified engineering review | attributed L6 evidence importer/gate only | no v1 application run fabricates or self-signs review | external gate |
| PA35 | Fabrication, assembly, bring-up, EMC, and measured results | attributed L7 evidence importer/gate only | no physical board or lab result is claimed | external gate |

## Persisted product evidence

- Browser product journey and screenshots: `artifacts/product-e2e/browser-e2e.json`
  and `artifacts/product-e2e/copperwright-app-*.png`
- Scriptable terminal journey: `artifacts/product-e2e/chat-e2e.json`
- Authenticated Codex provider contract: `artifacts/product-e2e/codex-provider.json`
- Three native projects, real validation, and previews: `examples/product_profiles/`
- Final exact results and limitations: `docs/PRODUCT_REPORT_ZH.md`

## Reproducible acceptance entry points

```bash
scripts/test.sh
scripts/compatibility.sh
scripts/chat-e2e.sh /tmp/copperwright-chat-e2e
uv run python scripts/browser-e2e.py --output /tmp/copperwright-browser-e2e
uv run python scripts/generate-product-examples.py --output-root /tmp/copperwright-profiles
scripts/release-check.sh
```

`candidate_ready=true` means all locally required deterministic and real-KiCad
checks passed for the bounded design. It never means the PCB is production-ready.
PA33–PA35 can be completed only with genuine external or physical evidence.
