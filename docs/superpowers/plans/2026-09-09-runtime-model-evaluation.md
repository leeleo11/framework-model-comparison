# 执行后模型参数抽取与并行评分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保持源码 CLI 主分不变的前提下，从 PyOSIS 执行后的模型快照抽取 canonical 参数，复用父仓库桥型评分器生成独立运行态逐项评分，并纳入 Excel 后处理。

**Architecture:** `PyOSISAdapter` 在现有 counts probe 旁生成版本化 `runtime_measurements.json`；`common/runtime_measurements.py` 将快照归一化为 51-key 参数；`common/runtime_scorer.py` 调用父仓库 `SCORERS` 和 `generate_report` 写入 `runtime_score.json/.md`。`ExperimentRunner` 在 PyOSIS 返回后调用运行态链，但只写 sidecar，绝不修改 `model_score.json`、`reference_score.json` 或 `evaluation.json` 的源码分。Excel 读取 sidecar 并额外提供运行态矩阵和逐项长表。

**Tech Stack:** Python 3.13 comparison venv, PyOSIS subprocess probe, parent `src/evaluation/levels/model_conformance` scorers, pytest, openpyxl.

## Global Constraints

- `model_score.json`、`reference_score.json` 和 `evaluation.json.quality_score` 的现有源码评分路径保持不变。
- 运行态缺失参数写入 `None` 和 `missing_params`，不得用隐藏参考或任务目标补齐，不得静默按零分。
- 运行态任何异常只影响 `runtime_score.json` 状态，不阻塞原有运行结果。
- 详细快照和 sidecar 只能落在当前 run 目录，不得写入 candidate 工程或桌面当前项目。
- 桥型规则从父仓库 `SCORERS`/`generate_report` 复用，不复制六类评分公式。

---

### Task 1: 设计文档与快照接口

**Files:**
- Create: `docs/superpowers/specs/2026-09-09-runtime-model-evaluation-design.md`
- Modify: `common/pyosis_adapter.py:20-65,373-431`
- Test: `tests/test_pyosis_adapter.py`

**Interfaces:**
- Produces `_MODEL_STATE_PROBE_CODE` output file `runtime_measurements.json` with schema `osis-runtime-measurements-v1`.
- Preserves existing `model_state.json` fields `status`, `summary`, `validation_passed`.

- [x] **Step 1: Write the failing test**

Add a fake probe response containing a JSON object with `summary` and a serialized section/node/material, then assert that `execute()` writes `runtime_measurements.json`, retains counts in `model_state.json`, and records the schema version. Add a second assertion that a probe with no detail data still writes a valid `status: "unavailable"` sidecar instead of raising.

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_pyosis_adapter.py -q
```

Expected: FAIL because the current probe only prints counts and no `runtime_measurements.json` is written.

- [ ] **Step 3: Write minimal implementation**

Extend the embedded probe with a JSON normalizer that handles `dataclasses`, `Enum`, mappings, sequences and public object attributes. Keep independent count probing, then call `.all()` for `nodes`, `elements`, `sections`, `materials`, `boundaries`, `loadcases`, `tendon.prop`, `tendon.shape`, `stages`, and available element groups. Write the detailed payload to an absolute path supplied through `OSIS_RUNTIME_MEASUREMENTS_PATH`; print only the old summary JSON to stdout so `_parse_summary()` remains compatible. In `execute()`, pass the run-local path via the probe environment and write a sidecar with `status: "unavailable"` and `probe_error` if the file is missing.

- [ ] **Step 4: Run test to verify it passes**

Run the same pytest command; expected: all adapter tests PASS and old fake runners still parse counts.

- [ ] **Step 5: Commit**

```powershell
git add common/pyosis_adapter.py tests/test_pyosis_adapter.py
git commit -m "feat: persist detailed PyOSIS runtime measurements"
```

---

### Task 2: Runtime snapshot normalization and canonical parameters

**Files:**
- Create: `common/runtime_measurements.py`
- Create: `tests/test_runtime_measurements.py`
- Create: `tests/fixtures/runtime_measurements_cantilever.json`

**Interfaces:**
- `load_measurements(path: Path) -> dict[str, Any]`
- `measurements_to_params(measurements: Mapping[str, Any], *, bridge_type: str | None = None, is_continuous: bool | None = None, is_prestressed: bool | None = None) -> tuple[dict[str, Any], list[str], dict[str, Any]]`
- Returned params contain every canonical key in `CANONICAL_PARAM_KEYS`; missing values are `None` or empty lists and are listed once in `missing_params`.

- [ ] **Step 1: Write the failing test**

Create a small fixture with nodes at `x=0,65,185,250`, BEAM3D elements linking sections, two girder sections (`height=7.5` and `height=3.2`), material grade `C60`, and support records. Assert `measurements_to_params()` returns `L=120`, `span_lengths=[65,120,65]`, `H_root=7.5`, `H_mid=3.2`, `concrete_grade=60`, `node_count=4`, `section_count=2`, and the full canonical key set. Assert omitted tendon data appears in `missing_params`, not as a false `True` value.

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_runtime_measurements.py -q
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Write minimal implementation**

Implement normalization helpers for enum-like strings, numeric values, coordinates, section contour and manager records. Build source-compatible rows (`nodes_full`, `beams`, `sections`) and derive height/thickness profiles, total length, supports/spans, section counts, concrete grade, stage names, tendon presence and available material/geometry values. Reuse parent common geometry helpers when importable, but keep all source-score inputs explicit and do not infer hidden targets. Return provenance describing which manager fields were used and which canonical keys could not be derived.

- [ ] **Step 4: Run test to verify it passes**

Run the focused test, then:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_runtime_measurements.py tests/test_pyosis_adapter.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add common/runtime_measurements.py tests/test_runtime_measurements.py tests/fixtures/runtime_measurements_cantilever.json
git commit -m "feat: derive canonical parameters from runtime snapshots"
```

---

### Task 3: Independent runtime scorer sidecar

**Files:**
- Create: `common/runtime_scorer.py`
- Create: `tests/test_runtime_scorer.py`

**Interfaces:**
- `evaluate_runtime_snapshot(snapshot_path: Path, run_dir: Path, *, bridge_type: str, expected_L: float | None = None, is_continuous: bool | None = None, is_prestressed: bool | None = None) -> dict[str, Any]`
- Writes `runtime_score.json` and, when scored, `runtime_score.md`; returns a result with `status`, `candidate_score`, `total_score_5`, `params`, `missing_params`, `dimensions`, and `provenance`.

- [ ] **Step 1: Write the failing test**

Use the fixture from Task 2 and a known cantilever bridge type. Assert the result is `evaluated`, dimensions contain the parent scorer’s item maps, and `runtime_score.json` exists. Add a test for a missing snapshot that asserts `status == "not_available"` and no exception.

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_runtime_scorer.py -q
```

Expected: FAIL because no runtime scorer exists.

- [ ] **Step 3: Write minimal implementation**

Load the snapshot, call `measurements_to_params()`, inject only supplied bridge metadata, import the parent `SCORERS` and `generate_report` using the same parent source path used by `static_conformance.py`, call the selected scorer directly, and persist the sidecars. Convert the parent score from its 0–1 scale to `total_score_5 = score * 5`; preserve all nested `dimensions`/`items`. Catch import, extraction, and scoring errors separately and write an explicit status sidecar.

- [ ] **Step 4: Run test to verify it passes**

Run the focused scorer tests and the complete current suite; expected: PASS, with no changes to existing model score fixtures.

- [ ] **Step 5: Commit**

```powershell
git add common/runtime_scorer.py tests/test_runtime_scorer.py
git commit -m "feat: add independent runtime model scorer"
```

---

### Task 4: Runner integration without changing source score

**Files:**
- Modify: `common/runner.py` after PyOSIS execution and before final artifact aggregation
- Create: `tests/test_runtime_runner_integration.py`

**Interfaces:**
- Internal helper `_evaluate_runtime_sidecar(run_dir: Path, task: TaskSpec) -> dict[str, Any]` (or equivalent) must be failure-isolated and return the sidecar result.

- [ ] **Step 1: Write the failing test**

Use a fake PyOSIS adapter that writes a valid `runtime_measurements.json` and a fake CLI scorer that returns a sentinel source score. Assert `model_score.json` and `evaluation.json.quality_score` retain the sentinel, while `runtime_score.json` is produced separately. Add a missing-snapshot case that still returns the normal `RunSummary`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_runtime_runner_integration.py -q
```

Expected: FAIL because runner never calls runtime scoring.

- [ ] **Step 3: Write minimal implementation**

After `PyOSISAdapter.execute()` returns, call the runtime scorer only when `runtime_measurements.json` exists (otherwise write `not_available`), pass task bridge metadata, and record the returned sidecar status in a local trace field without modifying source scorer outputs. Ensure disabled PyOSIS and compile/generation failures produce an explicit sidecar but do not add new failure reasons to the source evaluation.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_runtime_runner_integration.py tests/test_runner.py -q
```

Expected: PASS and existing runner assertions remain valid.

- [ ] **Step 5: Commit**

```powershell
git add common/runner.py tests/test_runtime_runner_integration.py
git commit -m "feat: attach runtime scoring as isolated runner sidecar"
```

---

### Task 5: Excel runtime summaries and item-level sheet

**Files:**
- Modify: `scripts/export_results_xlsx.py` (`RUN_COLUMNS`, `collect_run_records`, workbook creation, new sheet helpers)
- Modify: `tests/test_export_results_xlsx.py`

**Interfaces:**
- Existing `框架对比` formulas continue to reference `model_score`.
- New `运行态对比` and `运行态逐项评分` sheets are emitted when at least one runtime sidecar exists; runs without sidecars remain backward-compatible.

- [ ] **Step 1: Write the failing test**

Extend `_make_run()` with a runtime sidecar containing two dimensions and item scores. Assert `collect_run_records()` exposes `runtime_score`, `runtime_score_status`, `runtime_missing_params`, and `runtime_score_path`. Assert the workbook has the two new sheets, source matrix formulas still contain `model_score`, and the item sheet has one row per dimension/item. Keep the no-sidecar fixture assertion for the original sheet list.

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_export_results_xlsx.py -q
```

Expected: FAIL because records and workbook have no runtime fields/sheets.

- [ ] **Step 3: Write minimal implementation**

Read `runtime_score.json` and `runtime_measurements.json` in `collect_run_records()`. Add clearly named runtime columns to `运行明细`; do not change existing source columns or formulas. Implement long-form flattening of `dimensions[*].items[*]` with parameter key, actual value (from `params`), score, skipped flag, note and sidecar path. Implement a runtime comparison matrix using the same bridge/form/architecture grouping but `runtime_score_status == "evaluated"`. Update `说明` to state source CLI remains the primary metric and runtime score is an independent secondary metric.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest tests/test_export_results_xlsx.py -q
```

Expected: PASS for both old no-sidecar and new runtime-sidecar cases.

- [ ] **Step 5: Commit**

```powershell
git add scripts/export_results_xlsx.py tests/test_export_results_xlsx.py
git commit -m "feat: export runtime scores and item-level measurements"
```

---

### Task 6: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/建模任务线接入说明.md`
- Modify: `对比实验框架设计文档-建模任务线.md`

- [ ] **Step 1: Document commands and artifacts**

Document that a run now contains `model_state.json` (counts), `runtime_measurements.json` (raw executed model), `runtime_score.json/.md` (secondary runtime score), while `model_score.json` remains the source CLI primary score. Add an offline export example:

```powershell
.\.venvs\main\Scripts\python.exe scripts/export_results_xlsx.py --runs-dir runs\official --output runs\official\experiment_results.xlsx
```

- [ ] **Step 2: Run full verification**

Run:

```powershell
.\.venvs\main\Scripts\python.exe -m pytest -q
.\.venvs\main\Scripts\python.exe -m compileall common scripts
```

Expected: all tests PASS and no compile errors. Do not run live model generation because the API account currently has no balance.

- [ ] **Step 3: Commit**

```powershell
git add README.md docs/建模任务线接入说明.md 对比实验框架设计文档-建模任务线.md
git commit -m "docs: describe runtime model evaluation artifacts"
```

## Self-review checklist

- Source CLI path remains the only writer of `model_score.json` and remains the formula source for `框架对比`.
- Runtime snapshots contain enough raw records for later re-scoring without rerunning PyOSIS.
- Missing runtime fields are visible and never silently treated as target values.
- All six bridge types route through the existing parent `SCORERS` map.
- Old runs without sidecars still export successfully.
