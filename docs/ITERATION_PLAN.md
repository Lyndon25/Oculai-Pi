# Oculai-Pi production-readiness iteration plan

This document is the acceptance contract for the current hardening cycle. A phase is not
complete because its code exists; it is complete only when every listed gate has executable
evidence. The final release gate is cumulative.

## Scope and invariants

The target is a Windows desktop application that can be installed on a clean x64 machine,
starts its own deterministic backend, performs a resumable multi-agent sourcing run, and
keeps all candidate ranking and external side effects behind deterministic evidence and
human-review controls.

The following invariants are non-negotiable:

1. PostgreSQL is the authoritative run state. Renderer state and recent-run files are caches.
2. A task is claimable only when every dependency is `done`; claims and terminal transitions
   are ownership checked and concurrency safe.
3. No credential or database connection string is ever included in an LLM prompt or renderer
   payload.
4. Agent, tool, and UI events always identify `run_id`; agent-specific events also identify
   `agent_id`.
5. Candidate scores cannot pass with missing required dimensions or insufficient evidence.
6. A shortlist/report cannot leave the application without a recorded human approval.
7. The packaged schema, development schema, and migration history come from one source.
8. The installer contains (or deterministically prepares) every required runtime and fails the
   build if a required artifact is absent.

## Iteration 1 — database DAG and migrations

Deliverables:

- Dependency-aware, `FOR UPDATE SKIP LOCKED` task claiming.
- Ownership-checked complete/fail transitions and correct retry exhaustion.
- Atomic plan checkpoint with validation for duplicate steps, missing dependencies, and cycles.
- Forward-only migrations for every schema change.
- Generated desktop schema resources and a byte-for-byte drift verifier.

Acceptance:

- A dependent task cannot be claimed before all parents finish and becomes claimable afterward.
- Two concurrent claimers never receive the same task.
- The last allowed failure leaves a task in `error`, never unclaimable `pending`.
- Invalid plans create no Plan, Task, Dependency, or run-status residue.
- `python scripts/sync_db_schema.py --check` exits 0.
- Database integration tests exercise fresh install and upgrade paths.

## Iteration 2 — real agent runtime and run lifecycle

Deliverables:

- A callable subagent tool backed by isolated child sessions sharing the deterministic tool
  bridge, with bounded parallel mode and cancellation.
- Concurrent JSONL request dispatch with serialized output writes and per-request correlation.
- Restartable sidecar supervision, timeouts, cancellation, and bounded in-flight requests.
- Per-run lifecycle state machine: start, pause/abort, resume, review, complete.
- Runtime-effective model, source, budget, and concurrency settings.

Acceptance:

- A parent can launch at least two subagents concurrently and receive isolated results.
- Dashboard events show distinct run and agent identities for spawn/progress/completion.
- Aborting one run does not corrupt another run and persists `aborted` in PostgreSQL.
- Resume starts real work from persisted unfinished tasks rather than only reading state.
- Sidecar crash rejects pending requests and a subsequent restart succeeds.
- `pi-session.ts` is covered by TypeScript checking without `@ts-nocheck`.

## Iteration 3 — evidence, scoring, and governance

Deliverables:

- Canonical evidence-tier policy shared by prompts and deterministic code.
- Evidence ownership validation and enforced score/evidence gates.
- Required-dimension completeness checks without partial-score inflation.
- Auditable approval/denial state transitions.
- Deterministic human-review gates for shortlist finalization and external report export.

Acceptance:

- Missing a must-pass dimension yields `gate_status=failed`.
- Unsupported high scores are rejected or deterministically capped before persistence.
- Evidence from a different candidate/run cannot support a score.
- Social profile evidence is not T1 primary evidence.
- Approval transitions are actor-attributed, one-way from pending, and idempotent.
- Export without an approved matching action fails; approved export succeeds and is audited.

## Iteration 4 — distributable runtime

Deliverables:

- Frozen Python JSONL sidecar built from the locked project dependencies.
- Bundled PostgreSQL runtime including pgvector, `pg_trgm`, and required client tools.
- Secure first-run database initialization with a generated password stored through Electron
  safe storage.
- Build-time runtime manifest and verifier.

Acceptance:

- `python scripts/build_python_sidecar.py` produces the expected executable.
- `powershell scripts/prepare_windows_runtime.ps1` produces a validated runtime tree.
- `python scripts/verify_runtime_bundle.py --root oculai-desktop/resources/runtime` exits 0.
- The installer build refuses to run when any runtime artifact or extension is missing.
- An unpacked build starts without relying on `python`, `psql`, or `pg_ctl` from `PATH`.

## Iteration 5 — engineering quality and release gates

Deliverables:

- Unit tests for protocol, lifecycle, scoring, approval, adapters, and migration validation.
- Integration tests for PostgreSQL concurrency and the Electron-to-sidecar boundary.
- Installer-content smoke test.
- Hard-failing lint, type, schema, generated-code, test, and package jobs in CI.

Final acceptance commands:

```powershell
python scripts/check_schema_drift.py
python scripts/sync_db_schema.py --check
python -m compileall -q oculai-mcp/src/oculai_mcp
cd oculai-mcp
python -m ruff check src tests
python -m mypy src/oculai_mcp
python -m pytest tests -v --tb=short
python tests/integration_test.py
cd ../oculai-desktop
npm ci
npm audit --omit=dev --audit-level=moderate
npm audit --audit-level=moderate
npm run typecheck
npm test
npm run build -- --publish never
cd ..
python scripts/verify_runtime_bundle.py --root oculai-desktop/resources/runtime
python scripts/verify_installer_contents.py --app oculai-desktop/dist-electron/win-unpacked
powershell -File scripts/smoke_packaged_app.ps1 -App oculai-desktop/dist-electron/win-unpacked
powershell -File scripts/smoke_packaged_app.ps1 -App oculai-desktop/dist-electron/win-unpacked -CloseDuringStartup
git diff --exit-code -- oculai-desktop/src/main/generated-tools.ts oculai-desktop/resources/schema
```

Deterministic protocol, scheduler, abort/resume, scoring, and approval/export tests prove the
workflow semantics without requiring a developer or release secret. The packaged-application
smoke separately proves the clean-machine boundary: renderer load, first-run database
initialization, baseline plus migration application, a live sidecar tool call, graceful
shutdown, a second launch against the migrated database, and closure during backend startup.
A live provider exercise remains an operator check for configured credentials, not a release
gate that can silently depend on an external account.

## Acceptance evidence — 2026-07-17

- Schema drift, generated schema mirror, Python compile, Ruff, and mypy: passed.
- PostgreSQL-backed Python suite: **43/43 passed**, including concurrent DAG claiming,
  incremental upgrade, governance, JSONL concurrency, and end-to-end iteration persistence.
- Standalone integration workflow: passed through evidence-backed scoring, human approval,
  audited HTML export, and final state inspection.
- Desktop suite: **12 files / 30 tests passed**; TypeScript and production renderer/main builds
  passed.
- Dependency security: production and full npm audits both report **0 vulnerabilities** after
  upgrading to Electron 43.1.1, Pi 0.80.9, electron-builder 26.15.3, and Vite 8.1.5.
- Frozen runtime: Python 3.12 / PyInstaller 6.15.0, **43 tools**, successful live
  `oculai_list_source_capabilities` probe with **17 sources**, PostgreSQL 18.4 with **1813**
  files, `vector`, and `pg_trgm`.
- Installer verification: app.asar main/preload/renderer entries, 9 baseline SQL files, 2
  checksum-tracked migrations, both required extensions, and disposable shutdown passed.
- Packaged app: clean first launch, migrated second launch, and close-during-startup all passed
  with zero residual Oculai, sidecar, or PostgreSQL processes.
- NSIS artifact: `Oculai-Setup-0.0.3.exe`, 438,017,971 bytes,
  SHA-256 `03707BA0155304C655198109F6AA5BBE3D05B1E3434BC078BF190D5A3FE97D7D`.

## Completion policy

Failures are fixed at their owning layer; tests are not weakened to preserve broken behavior.
Advisory CI checks are promoted to blocking only after the repository is clean. Any deferred
item must remain explicitly open and prevents declaring this plan complete.
