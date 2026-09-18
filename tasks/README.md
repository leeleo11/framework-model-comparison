# 建模任务输入说明

建模线不是“只有一个 JSON”。一次实验输入由四部分组成：

1. **任务规格 JSON**：描述本次要建什么模型、任务形式、难度、预期字段和时限。
2. **共享知识包**：`--skills-dir` 指向的无泄漏快照（正式运行默认为
   `comparison-repo/checkpoints/train-all/skills`），由所有架构通过
   `SkillAdapter` 只读访问；原始 `.agents/skills` 仅供评分器侧读取，不能挂给模型。
3. **统一运行环境**：OSIS/PyOSIS、CLI 评分器、Python 和模型接口；这些通过命令行参数或环境变量提供。
4. **可选初始状态**：只有 `task_form=modify` 时使用 `initial_project_snapshot`，表示待修改的模型快照或其路径。

## 任务规格 JSON

`tasks/dev/bridge_whole_l1.json` 是当前唯一的开发冒烟任务。它包含：

```json
{
  "task_id": "cantilever_whole_l1_smoke",
  "bridge_type": "cantilever_box",
  "task_form": "whole",
  "difficulty": "L1",
  "natural_language_requirement": "建立一座悬臂现浇箱梁桥的基础 PYOSIS 模型。",
  "total_timeout_s": 5400,
  "subtask_timeout_s": {
    "P0": 120,
    "P1": 180,
    "P2": 600,
    "P3": 180,
    "P4": 480,
    "P5": 180,
    "P6": 60
  },
  "expected_fields": {
    "units": true,
    "geometry": true,
    "material": true,
    "section": true,
    "node": true,
    "element": true,
    "boundary": true,
    "loadcase": true
  },
  "metadata": {
    "split": "dev",
    "solver_required": false
  }
}
```

字段由 `common/task_schema.py` 校验：

- `bridge_type`：桥型；
- `task_form`：`whole`（整桥生成）、`module`（单模块生成）或 `modify`（已有模型修改）；
- `difficulty`：`L1`、`L2`、`L3`；
- `natural_language_requirement`：给智能体的自然语言需求；
- `total_timeout_s`、`subtask_timeout_s`：总任务和 P0–P6 分任务时限；正式基线总时限为
  5400 秒（90 分钟），其中生成阶段最多 3600 秒（1 小时），预留 1800 秒给编译、
  PyOSIS 和源码评分；
- `expected_fields`：用于任务约束和结果解释；
- `metadata`：数据集划分、求解 gate 等实验标记。

## 任务集如何扩展

正式实验不应只跑这个开发样例。建议把任务 JSON 分为：

```text
tasks/
├─ dev/       # 冒烟和适配器调试，不用于论文主结果
├─ test/      # 可公开测试集
└─ hidden/    # 参数扰动、跨模块耦合和未见组合的隐藏集
```

每个任务都使用同一 schema；框架、模型、技能包和 PyOSIS 执行器不随任务改变。第一轮可以从
6 种桥型 × 3 种任务形式 × L1/L2 两档难度中抽取小规模集合，再为每个任务运行多个 seed。

运行时，系统会把任务 JSON 原样写入每次运行的 `input.json`，并额外生成：

- `adapter_request.json`：架构看到的统一请求和输出契约；
- `skill_index.json`、`skill_bundle.md`、`skill_mount.json`：本次运行实际挂载的共享技能证据；
- `timer.json`：总时限、分任务时限和耗时；
- `manifest.json`：模型、框架版本、技能包哈希、seed 和产物哈希。
