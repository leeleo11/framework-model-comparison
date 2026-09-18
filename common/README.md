# 统一实验边界

- `skill_adapter.py`：只读索引、读取技能/参考资料并计算固定包哈希；
- `pyosis_adapter.py`：在隔离子进程中执行 `candidate_project/py/prep/main.py`，可选调用 `engine.solve()`，再探针 `engine.model_summary()`，并把 manager 详细记录落盘为 `runtime_measurements.json`；
- `cli_scorer.py`：调用 `osis-skill-enhance-main/.agents/skills/osis-auto-testconformance/scripts/test_conformance.py`，不复制评分规则；
- `runtime_measurements.py`：从 `runtime_measurements.json` 归一化 51 个运行态参数；
- `runtime_scorer.py`：复用父仓库 `SCORERS` 生成独立的 `runtime_score.json/.md`，不覆盖源码评分；
- `runner.py`：固定阶段顺序、执行总/分任务截止时间、写入运行工件和 manifest；
- `evaluator.py`：汇总 CLI 模型分、PyOSIS 执行证据、结构和追溯性。

`osis_adapter.py` 只保留旧导入路径兼容，不再提供 dry-run 假成功实现。
