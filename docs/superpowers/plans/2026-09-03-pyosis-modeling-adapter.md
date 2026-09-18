# Implementation Plan: PyOSIS-backed modeling comparison

## Goal

把建模任务线从“候选代码 + dry-run 状态”改成统一的真实执行链：T1–T6 在相同的 SKILL/文档/工具输入下生成候选工程；比较项目通过 PyOSIS 运行该工程并导出实际模型状态；随后调用 `osis-skill-enhance-main` 中已有的模型一致性 CLI/评估器评分。OSIS 图形界面、OpenCode 界面和框架自带评分器不进入正式实验变量。

## Design constraints

- T1–T6 只负责规划、调用工具和写出 `candidate_project`，不得直接写评分或运行结果。
- 统一候选工程契约仍为 `candidate_project/py/prep/` 下的 12 个标准文件。
- PyOSIS 是唯一建模执行边界；执行失败、服务不可用或超时必须显式记录，不能伪造通过。
- `model_conformance` CLI/评估器作为模型代码与模型实体的一致性评分来源；PyOSIS 运行状态作为独立执行证据。
- 求解/收敛不是建模框架主分数；若启用，仅作为可选的 execution gate 单独记录。
- 保留总任务时限和 P0–P6 分任务时限，超时样本保留已生成工件并判失败。

## Planned changes

1. **Contract tests first**
   - 覆盖 PyOSIS 适配器的命令构造、成功/失败/超时状态和 `model_state.json` 工件。
   - 覆盖 runner 在候选工程存在时的统一顺序：materialize → compile → PyOSIS execute → model-conformance score。
   - 覆盖 task JSON 的总时限与分任务时限字段。

2. **Implement the runtime boundary**
   - 新增 `common/pyosis_adapter.py`，封装候选工程入口执行、可选求解、`OSISEngine.model_summary()` 状态探针和运行日志。
   - 适配器使用可注入的子进程执行器，测试无需启动 OSIS 服务；真实运行默认使用当前 Python 环境中的 `pyosis`。
   - 将旧 `common/osis_adapter.py` 改为兼容导出，避免现有调用失效。

3. **Unify scoring and artifacts**
   - 新增 CLI 评分包装层，调用父仓库 `.agents/skills/osis-auto-testconformance/scripts/test_conformance.py`，保存原始报告及机器可读结果。
   - 修改 `common/runner.py`：把 PyOSIS 和 CLI 评分接在候选工程之后，生成 `build_status.json`、`model_state.json`、`model_score.json/md`、`execution_trace.json`。
   - 调整综合评价：模型一致性分数为主；求解收敛不再阻断建模质量分，只在 execution gate/失败原因中单独体现。
   - 增加真实总时限/分阶段限时的截止时间字段和超时状态。

4. **Update command and project layout**
   - 更新 `scripts/run_modeling.py` 参数，显式选择 PyOSIS 执行、求解 gate、总时限和运行目录。
   - 将 `baselines/` 约定为只产出候选工程的框架接入层，将 `common/` 约定为共享技能挂载、PyOSIS 执行和评分层。
   - 更新 README、建模任务线接入说明和建模线设计文档，给出“一次实验输入/工具/输出/检查点”的最短操作说明。

5. **Verification**
   - 运行比较项目全量 pytest。
   - 运行一次 mock PyOSIS 集成测试，确认成功、服务不可用和超时均不会被报告为成功。
   - 在 OSIS 服务可用时保留一条真实命令示例；当前服务不可用时，在文档和输出中明确说明原因。

## Files expected to change

- `common/pyosis_adapter.py` (new)
- `common/cli_scorer.py` (new)
- `common/osis_adapter.py`
- `common/adapters.py`
- `common/task_schema.py`
- `common/runner.py`
- `common/evaluator.py`
- `scripts/run_modeling.py`
- `tests/test_pyosis_adapter.py` (new)
- `tests/test_runner_integration.py` (new or extended)
- `docs/建模任务线接入说明.md`
- `对比实验框架设计文档-建模任务线.md`
- `README.md`
