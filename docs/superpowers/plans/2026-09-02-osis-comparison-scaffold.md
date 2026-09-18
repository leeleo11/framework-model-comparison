# OSIS Comparison Scaffold Implementation Plan

> **For agentic workers:** Execute this plan in the current session. The design has been approved by the user.

**Goal:** Build a runnable, framework-neutral scaffold for comparing T1–T6 on OSIS bridge-modeling tasks, with safe SKILL mounting, canonical task/output contracts, manifests, and an automatic evaluator.

**Architecture:** Keep the experiment project independent from \`osis-skill-enhance-main\`. The \`common/\` package owns task schemas, read-only skill access, OSIS execution boundaries, manifests, and scoring. \`baselines/\` contains thin architecture adapters whose external LLM/framework execution is optional; until credentials and framework packages are configured, adapters produce explicit \`not_configured\` results rather than fake model scores.

**Tech Stack:** Python 3.11+ standard library, pytest, JSON/JSONL artifacts. The existing Node/docx toolchain remains only for the design document.

## Global Constraints

- Never modify the local OSIS parent repository; its worktree may contain unrelated changes.
- Read the parent repository's \.agents/skills through a read-only SkillAdapter.
- Reject path traversal for skills, references, scripts, task files, and run directories.
- A run is not successful unless the real model backend, solver, and validator explicitly report success.
- T1 is one-shot and cannot use interactive skill reads; T2–T6 may use progressive skill tools.
- Every run writes a manifest with architecture, versions, skill hash, seed, budgets, artifact hashes, failure code, and human-intervention flag.
- No framework dependency is imported by the common package; integrations are optional adapters.

## File Map

- Create: \`common/__init__.py\` — package export surface.
- Create: \`common/task_schema.py\` — validated task dataclasses and JSON loader.
- Create: \`common/skill_adapter.py\` — read-only \`.agents/skills\` index, safe reads, case search, deterministic hashing.
- Create: \`common/osis_adapter.py\` — backend protocol and explicit dry-run filesystem adapter.
- Create: \`common/manifest.py\` — run manifest and artifact hashing.
- Create: \`common/evaluator.py\` — canonical success gate and weighted quality score.
- Create: \`common/adapters.py\` — T1–T6 adapter metadata and registry.
- Create: \`common/runner.py\` — canonical run-directory orchestration.
- Create: \`baselines/__init__.py\` and \`baselines/<t1_direct|t2_langgraph|t3_smolagents|t4_openhands|t5_crewai>/__init__.py\` — adapter package entry points.
- Create: \`tasks/dev/bridge_whole_l1.json\` — a safe, non-solver smoke task.
- Create: \`scripts/run_conformance.py\` — list mounted skills and run adapter conformance in dry-run mode.
- Create: \`tests/test_task_schema.py\`, \`tests/test_skill_adapter.py\`, \`tests/test_evaluator.py\`, \`tests/test_runner.py\`, \`tests/test_adapters.py\` — behavior-first coverage.
- Create: \`pyproject.toml\` — pytest configuration and package metadata.
- Create: \`README.md\` — how to run conformance and where real integrations plug in.
- Modify: none of the existing design documents or build scripts.

## Task 1: Establish the contracts with failing tests

**Files:**
- Create: \`tests/test_task_schema.py\`
- Create: \`tests/test_skill_adapter.py\`
- Create: \`tests/test_evaluator.py\`
- Create: \`tests/test_runner.py\`
- Create: \`tests/test_adapters.py\`

- [ ] Write tests for task validation, read-only skill indexing, path traversal rejection, complete-success gating, weighted scoring, manifest creation, and the six adapter IDs.
- [ ] Run \`pytest -q\`; confirm collection/import failures because the \`common\` package does not exist yet.

## Task 2: Implement task and skill contracts

**Files:**
- Create: \`common/__init__.py\`
- Create: \`common/task_schema.py\`
- Create: \`common/skill_adapter.py\`

**Interfaces:**
- \`TaskSpec.from_dict(data: dict) -> TaskSpec\`
- \`load_task(path: Path) -> TaskSpec\`
- \`SkillAdapter.list_skills() -> list[SkillMeta]\`
- \`SkillAdapter.read_skill(skill_id: str) -> str\`
- \`SkillAdapter.read_reference(skill_id: str, relative_path: str) -> str\`
- \`SkillAdapter.search_cases(query: str) -> list[CaseHit]\`
- \`SkillAdapter.skill_bundle_hash() -> str\`

- [ ] Implement validation for six bridge types, \`whole/module/modify\` task forms, \`L1/L2/L3\` difficulty, and the required natural-language requirement.
- [ ] Implement safe resolved-path checks and deterministic SHA-256 hashing over sorted skill files.
- [ ] Run \`pytest tests/test_task_schema.py tests/test_skill_adapter.py -q\`; confirm green.

## Task 3: Implement backend boundary, manifests, and evaluator

**Files:**
- Create: \`common/osis_adapter.py\`
- Create: \`common/manifest.py\`
- Create: \`common/evaluator.py\`

**Interfaces:**
- \`FilesystemOSISAdapter.create_model(run_dir: Path) -> dict\`
- \`FilesystemOSISAdapter.snapshot_artifacts(run_dir: Path) -> dict\`
- \`Manifest.write(path: Path) -> None\`
- \`evaluate_run(artifacts: dict) -> EvaluationResult\`

- [ ] Implement an explicit dry-run adapter that writes \`backend_status.json\` with \`execution_enabled=false\`; it must never report solver success.
- [ ] Implement artifact hashing and JSON manifest serialization.
- [ ] Implement the complete-success gate and the six-component 40/20/15/10/10/5 quality score.
- [ ] Run \`pytest tests/test_evaluator.py -q\`; confirm green.

## Task 4: Implement architecture registry and canonical runner

**Files:**
- Create: \`common/adapters.py\`
- Create: \`common/runner.py\`
- Create: \`baselines/__init__.py\`
- Create: \`baselines/t1_direct/__init__.py\`
- Create: \`baselines/t2_langgraph/__init__.py\`
- Create: \`baselines/t3_smolagents/__init__.py\`
- Create: \`baselines/t4_openhands/__init__.py\`
- Create: \`baselines/t5_crewai/__init__.py\`

**Interfaces:**
- \`get_adapter(architecture_id: str) -> ArchitectureAdapter\`
- \`ExperimentRunner.run(task: TaskSpec, architecture_id: str, seed: int) -> RunSummary\`

- [ ] Define T1–T6 metadata: mounting mode, interaction mode, max steps, and optional framework import name.
- [ ] Make all adapters emit an explicit \`adapter_request.json\` and \`not_configured\` result until a real executor is injected.
- [ ] Write manifests with version, skill hash, seed, budgets, artifact hashes, and failure code.
- [ ] Run \`pytest tests/test_runner.py tests/test_adapters.py -q\`; confirm green.

## Task 5: Add smoke task, conformance CLI, and documentation

**Files:**
- Create: \`tasks/dev/bridge_whole_l1.json\`
- Create: \`scripts/run_conformance.py\`
- Create: \`pyproject.toml\`
- Create: \`README.md\`

- [ ] Add a non-solver smoke task for a cantilever box whole-model L1 request.
- [ ] Make \`python scripts/run_conformance.py --skills-dir ... --runs-dir ...\` list the mounted skills and execute all six adapters in dry-run mode.
- [ ] Document the exact point where real framework packages and model/API credentials are added.
- [ ] Run the full test suite and the conformance CLI; verify six \`not_configured\` summaries and no writes under the source skill tree.

## Task 6: Verification

- [ ] Run \`pytest -q\`.
- [ ] Run the conformance CLI against the configured parent repository's \.agents/skills.
- [ ] Verify generated run manifests and artifact hashes.
- [ ] Run the DOCX validator on the preserved design document.
- [ ] Report that the scaffold is ready for real executor integration, not that modeling experiments have already completed.

## Execution record (2026-09-02)

The implementation tasks above were completed in this session. Verification results:

- `pytest -q`: 16 passed.
- Conformance CLI: 28 mounted skills discovered and six adapter runs generated.
- All six dry-run adapters return `not_configured` until a real model/framework executor is injected.
- Skill source hash is unchanged after the run; no files were written under the source skill tree.
- The preserved DOCX design document passed XML validation.
