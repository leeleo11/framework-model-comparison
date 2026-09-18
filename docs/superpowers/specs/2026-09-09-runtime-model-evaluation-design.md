# 执行后模型参数抽取与并行评分设计

## 目标

在不改变现有源码 CLI 评分的前提下，增加一条可复现的运行态评价链：从 AI 生成代码实际执行后在 PyOSIS 中落盘的模型数据抽取梁高、跨径、截面、材料、预应力、节点/单元/边界/阶段等参数，复用父仓库已有桥型评分规则逐项评分，并把原始测量、参数、维度和逐项结果保存到每个 run 目录及最终 Excel。

## 不可变约束

1. `model_score.json`、`model_score.md` 仍由现有 `src/evaluation` CLI 生成，是源码主指标；其数值和计算路径不替换。
2. `reference_score.json` 与 `evaluation.json.quality_score` 继续沿用当前源码评价链；运行态分不覆盖、不混入现有综合分。
3. 运行态分是独立副分，缺失参数显示为 `None`/“不可抽取”，不得静默当作 0，也不得用测试目标值填充候选模型。
4. 评分规则只复用父仓库 `src/evaluation/levels/model_conformance` 的 `SCORERS`，不在 comparison 项目复制六类桥型的规则。
5. PyOSIS 执行失败或快照不可用时，源码评分照常落盘；运行态只生成带原因的 `not_available`/`extraction_error`/`score_error` sidecar，不使整条运行链崩溃。
6. 所有运行态文件位于当前 run 目录，不能写入候选工程目录或 OSIS 桌面当前项目。

## 运行态数据流

```text
candidate_project/py/prep/*.py
        │
        ├─ 现有源码链：CLI → model_score.json → reference_score/evaluation.json（不变）
        │
        └─ PyOSIS 执行
             └─ 独立 probe 查询 manager.all()
                  ├─ model_state.json（现有 counts/status，向后兼容）
                  └─ runtime_measurements.json（详细模型快照）
                       └─ runtime_measurements.py
                            └─ canonical 51-key params
                                 └─ 父仓库 SCORERS[bridge_type]
                                      ├─ runtime_score.json
                                      └─ runtime_score.md
```

## 快照格式

`runtime_measurements.json` 使用版本化 JSON，至少包含：

```json
{
  "schema_version": "osis-runtime-measurements-v1",
  "status": "available",
  "summary": {"nodes": 0, "elements": 0, "sections": 0},
  "nodes": [], "elements": [], "sections": [], "materials": [],
  "boundaries": [], "loadcases": [], "tendon_props": [],
  "tendon_shapes": [], "stages": [], "element_groups": [],
  "probe_errors": {}
}
```

Probe 对每个 manager 独立捕获异常；某个可选 manager 不支持时仍保留其它数据和错误。对象、dataclass、Enum、嵌套向量统一转换为 JSON 基本类型，并保留对象字段名，便于以后扩展参数而不重新执行模型。

## 参数抽取

`common/runtime_measurements.py` 负责把快照转换为与现有源码提取器一致的 51 个 canonical key。运行态路径使用节点坐标、梁单元端截面、截面高度/轮廓、材料 grade、边界节点、阶段、钢束属性/形状等真实执行数据；不能由后端可靠得到的值保持缺失并列入 `missing_params`。抽取结果不读取任务目录名，也不从隐藏参考工程补值。必要的桥型和连续性只由任务资源注入，和现有 CLI 入口一致。

## 运行态评分结果

`runtime_score.json` 至少包含：

```json
{
  "scorer": "osis-runtime-model-conformance",
  "status": "evaluated",
  "source": "pyosis_runtime_snapshot",
  "bridge_type": "cantilever_box",
  "candidate_score": 0.0,
  "total_score_5": 0.0,
  "params": {},
  "missing_params": [],
  "dimensions": {},
  "provenance": {
    "runtime_measurements_path": "runtime_measurements.json",
    "model_state_path": "model_state.json"
  }
}
```

`dimensions` 保留父评分器返回的 D1–D6 及其中的 `items`，因此梁高、厚度、跨径、材料等级、预应力配置等可以在 Excel 中逐项审计。`runtime_score.md` 使用同一 `generate_report` 生成，只是输入换成运行态评分结果。

## Excel 输出

现有 `运行明细` 的源码列和 `框架对比` 主矩阵保持不变；新增运行态列（状态、分数、缺失参数、sidecar 路径），并新增：

- `运行态对比`：桥型 × full/gen/edit × 架构的运行态均值、有效 N、可用率，明确标注为副指标。
- `运行态逐项评分`：一行一个 run × 维度 × item，包含任务、架构、桥型、任务形式、参数键、实际值、评分、备注和来源文件。

运行态不可用的记录保留在明细中，但不虚构分数；源码汇总的纳入规则和结果不改变。

## 失败隔离与版本

快照、抽取器、运行态评分器分别记录 `schema_version`/`extractor_version`。任何运行态异常都写入 sidecar 并记录 traceback 摘要，`ExperimentRunner.run()` 仍返回原有 `RunSummary`。现有测试首先验证：快照序列化、canonical 参数键集合、同一父评分器输出、源码评分字段不变、Excel 在 sidecar 缺失时安全留空。
