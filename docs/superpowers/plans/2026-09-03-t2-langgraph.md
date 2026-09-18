# T2 LangGraph ReAct Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real LangGraph ReAct generation adapter for T2 that uses the shared OSIS skills and the fixed `glm-5.3-flash` OpenAI-compatible endpoint, then hands the generated project to the existing PyOSIS/CLI evaluation runner.

**Architecture:** T2 owns only the generation layer. A LangGraph `create_agent` graph loops between the model and bounded read/write tools. The adapter writes only `candidate_project`; `ExperimentRunner` remains the sole owner of PyOSIS execution, model-state extraction, and CLI scoring. Model credentials come from environment variables and are never persisted.

**Tech Stack:** Python 3.11+, LangGraph/LangChain, `requests`, existing `SkillAdapter`, `ExperimentRunner`, and PyOSIS/`model_conformance` CLI.

## Global Constraints

- All architectures use the same model: `glm-5.3-flash`.
- T2 uses the installed LangGraph/LangChain versions and records them in generation metadata.
- T2 may read the shared skills/references through `SkillAdapter`, but may write only its isolated workspace.
- T2 must not execute PyOSIS, solve the model, call the scorer, or modify `runs/` results.
- The common runner remains responsible for total/subtask timeouts, PyOSIS execution, and CLI scoring.
- API keys are read from environment variables and must not appear in logs, manifests, or exceptions.

---

### Task 1: Define the T2 generation contract with failing tests

**Files:**
- Create: `baselines/t2_langgraph/adapter.py`
- Test: `tests/test_t2_langgraph.py`

**Interfaces:**
- Consumes: `TaskSpec`, `SkillAdapter`, a writable workspace, and model configuration.
- Produces: `workspace / "candidate_project"` plus `t2_generation.json` metadata.

- [x] **Step 1: Write failing tests** for safe file tools, model configuration, and a fake LangGraph run that produces a canonical candidate project.
- [x] **Step 2: Run `pytest tests/test_t2_langgraph.py -q` and confirm the new imports/functions fail because the adapter does not exist.
- [x] **Step 3: Implement the minimal adapter, OpenAI-compatible LangChain model wrapper, and bounded tools.
- [x] **Step 4: Run the focused tests and confirm they pass.

### Task 2: Add a runnable T2 CLI entry point

**Files:**
- Create: `scripts/run_t2.py`
- Modify: `README.md`
- Test: `tests/test_t2_cli.py`

**Interfaces:**
- Consumes: `--skills-dir`, `--task`, `--parent-repo`, `--runs-dir`, `--seed`, and optional model/endpoint overrides.
- Produces: a generated candidate project and the standard `ExperimentRunner` run artifacts.

- [x] **Step 1: Write a parser test** that verifies the default model is `glm-5.3-flash` and the command exposes the common timeout flags.
- [x] **Step 2: Run the focused test and confirm it fails before the CLI exists.
- [x] **Step 3: Implement generation followed by `ExperimentRunner.run(..., architecture_id="T2")`; never call PyOSIS from the generator.
- [x] **Step 4: Run the focused CLI tests and confirm they pass.

### Task 3: Verify the real endpoint and common-runner handoff

**Files:**
- Modify: `common/adapters.py` (only T2 version metadata if the installed version differs from the design lock)
- Modify: `对比实验框架设计文档-建模任务线.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the live `glm-5.3-flash` OpenAI-compatible endpoint and the existing OSIS service on port 18080.
- Produces: a documented command and a verified T2 run with generation, PyOSIS execution, and CLI scoring artifacts.

- [x] **Step 1: Run the T2 CLI generation path with a deterministic fake transport test.
- [x] **Step 2: Run a live minimal model/tool call using `glm-5.3-flash` without printing the API key.
- [ ] **Step 3: Run one full T2 smoke task through `--execute-pyosis` and inspect `model_state.json`, `model_score.json`, and `evaluation.json` (requires a longer live generation run).
- [x] **Step 4: Run the full comparison test suite; 39 tests passed.
