# Solve Gate 与父仓库评分统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让正式 T1–T6 运行统一执行 PyOSIS solve，保留父仓库源码评分，并将求解失败作为总分为零的门禁。

**Architecture:** 在现有 `ExperimentRunner`/`PyOSISAdapter` 边界上只切换正式默认配置，不重写求解器。运行态参数评分继续生成诊断文件，但从主评价与 Excel 聚合路径剥离；已有候选代码通过补跑执行阶段复用。

**Tech Stack:** Python 3.11+, pytest, PyOSIS, openpyxl 导出脚本。

## Global Constraints

- 主评分唯一来源为父仓库 `src/evaluation` 的 CLI/model score。
- 正式运行必须启用 PyOSIS；`--no-pyosis` 仅限诊断。
- solve 失败必须 `quality_score=0` 且 `complete_success=false`。
- 不修改父仓库测试环境、数据集或评分规则。
- 不删除已有运行目录。

---

### Task 1: 固定 solve gate 的配置契约

**Files:**
- Modify: `scripts/run_dataset.py`
- Modify: `scripts/run_campaign.py`
- Test: `tests/test_run_campaign.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- `run_dataset.py` 继续接受 `--solve-gate`，正式批量调用默认传入它。
- `ExperimentRunner(..., solve_gate=True)` 控制 `PyOSISAdapter.execute(..., solve=True)`。

- [ ] **Step 1: Write the failing tests**

增加测试断言正式命令构造包含 solve gate，并断言 Runner 将 `solve_gate` 传给 PyOSIS 执行器。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_run_campaign.py tests/test_runner.py`

Expected: 新增断言失败，现有代码默认不启用 solve gate。

- [ ] **Step 3: Implement the minimal configuration change**

批量驱动器调用单任务脚本时补充 `--solve-gate`；保留显式参数以便诊断和兼容旧命令。不要改变 `PyOSISAdapter.execute` 的求解实现。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_run_campaign.py tests/test_runner.py`

Expected: PASS。

### Task 2: 保证 solve 失败只影响主门禁，不新增运行态主分

**Files:**
- Modify: `common/runner.py`
- Modify: `common/evaluator.py`
- Test: `tests/test_evaluator.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- `common/runner.py` 继续写入 `runtime_score.json` 作为诊断，但 `evaluation.json` 主分只取父仓库 CLI/static 结果。
- `evaluate_run()` 接收 `solve_required` 与 `solver_converged`，求解失败时返回零分和失败原因。

- [ ] **Step 1: Write the failing tests**

增加两个测试：求解失败时总分为 0；即使 `runtime_score.json` 有高分，主评价仍使用源码评分结果，不被运行态分覆盖。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_evaluator.py tests/test_runner.py`

Expected: 至少一个断言失败，暴露当前运行态/主分耦合或 solve 门禁缺口。

- [ ] **Step 3: Implement the minimal scoring change**

确保 `runtime_score` 只写 trace/诊断文件；主评价输入只使用 `model_score`（父仓库 CLI）及既有结构/编译/求解门禁字段。不得删除运行态文件。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_evaluator.py tests/test_runner.py`

Expected: PASS。

### Task 3: 更新结果导出与旧运行补跑入口

**Files:**
- Modify: `scripts/export_results_xlsx.py`
- Modify: `scripts/rescore_runs.py`
- Modify: `docs/建模任务线接入说明.md`
- Test: `tests/test_export_results_xlsx.py`

**Interfaces:**
- Excel 主分列标注父仓库 CLI 来源；solve 状态单独展示；runtime score 不参与主聚合。
- `rescore_runs.py` 能对已有 `candidate_project` 重新执行 PyOSIS/solve，而不调用框架生成器。

- [ ] **Step 1: Write the failing tests**

测试导出聚合忽略 runtime score，并检查求解状态列；测试补跑入口保留候选代码路径。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_export_results_xlsx.py`

Expected: 新增列/聚合断言失败。

- [ ] **Step 3: Implement the minimal export/rescore changes**

只调整列映射和聚合筛选，保留旧结果文件；文档写明旧实验可补跑 solve，不需重新生成代码。

- [ ] **Step 4: Run full verification**

Run: `pytest -q`

Expected: 全部通过。

