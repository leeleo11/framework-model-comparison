# Solve Gate 与父仓库评分统一方案

## 目标

正式 T1–T6 实验必须执行 PyOSIS `engine.solve()`；主评分完全沿用父仓库 `src/evaluation` 规则；求解失败时总分为 0，但不新增运行态参数评分维度。

## 设计

1. `ExperimentRunner` 的正式模式默认启用 `solve_gate=True`。代码生成、编译、PyOSIS 建模、求解和评分仍使用同一任务目录与任务截止时间。
2. `common/pyosis_adapter.py` 继续负责实际建模和 `engine.solve()`，落盘 `solve_stdout.log`、`solve_stderr.log`、`backend_status.json`。
3. `common/evaluator.py` 保持现有父仓库兼容综合分和失败门禁：`solve_required=True` 且 `solver_converged=False` 时记录 `solver_not_converged`，并令 `complete_success=false`；正式输出将总分置 0。
4. `common/runtime_scorer.py` 产生的运行态参数评分只作为可选诊断，不参与 `evaluation.json` 主分、不覆盖父仓库 CLI 分数，也不进入正式汇总平均分。
5. 结果导出保留求解状态和日志路径，明确主分来源为父仓库 CLI 评分；运行态评分列改为诊断列或不纳入聚合。
6. 已有候选工程可复用，补跑 PyOSIS 建模与 solve；无需重新调用模型生成代码。

## 兼容性

- `--no-pyosis` 仍只用于诊断，正式运行禁止使用。
- `--solve-gate` 继续可显式传入；正式批量驱动器默认补上该开关。
- 旧的未求解运行记录不删除，标记为 `solve_not_requested`，不冒充已完成求解实验。

## 验收标准

- 新运行的 `frozen_config.json` 中 `solve_gate=true`。
- 成功运行的 `backend_status.json` 中 `solve_requested=true`、`solve_status=succeeded`。
- 求解失败运行的 `evaluation.json` 中 `quality_score=0`、`complete_success=false`。
- 父仓库 CLI 分数仍写入 `model_score.json`，且运行态评分不会改变该分数。
- 现有测试全部通过，并新增 solve gate 默认值和“运行态评分不影响主分”的测试。
