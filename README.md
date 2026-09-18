# OSIS Framework Comparison

这是 OSIS 建模框架对比实验仓库。仓库保存实验基础设施、T1–T6 适配器、统一运行器、父仓库评分入口、后处理脚本、测试和无测试答案的 `train-all` 技能快照。

仓库不保存正式实验结果、模型输出、API key、虚拟环境或父仓库的原始 `.agents/skills`。复现者只需要准备 `uv`、父仓库和模型网关，即可重新创建环境并运行实验。

## 实验原则

每个架构都接收同一份只读技能快照，并生成同一格式的候选工程。生成完成后，由统一的 `ExperimentRunner` 依次完成：

1. 候选工程物化和目录结构检查；
2. Python 编译检查；
3. PyOSIS 建模执行；
4. `engine.solve()` 求解；
5. 调用父仓库 `src/evaluation` 进行正式评分；
6. 写入运行清单、计时、评分和审计文件。

T1–T5 的区别只在编排框架，T6 使用 OSIS 原生编排。任务、技能快照、PyOSIS、评分器和输出契约保持一致。

## 环境要求

- Windows（正式 T1–T6 运行依赖 Windows 的 OSIS/PyOSIS/OpenCode 进程接口）；
- Python 3.12 或更高版本，推荐 Python 3.13；
- [`uv`](https://docs.astral.sh/uv/)；
- OSIS 父仓库 `osis-skill-enhance-main`；
- T6 所需的父仓库 `.venv`、PyOSIS 和 OSIS 求解器；
- 可访问模型网关的 API key。

如果需要了解双 Python 环境、T6 的外部安装件、Windows 进程限制和故障排查，请继续阅读
[`ENVIRONMENT.md`](ENVIRONMENT.md)；它是本 README 的详细环境补充。

本仓库使用 `pyproject.toml`、`uv.lock` 和 `.python-version` 管理 Python 依赖。虚拟环境只在本机创建，不提交到 Git。

## 从零安装

在本仓库根目录执行：

```powershell
uv python install 3.13
uv sync --python 3.13
uv run pytest -q
```

`uv sync` 会创建本仓库的 `.venv` 并安装基础运行器和测试依赖。测试通过后，再创建 T1–T5 的隔离框架环境：

```powershell
uv run python scripts/setup_framework_envs.py `
  --base-python (uv python find 3.13)
```

该脚本使用 `uv venv` 和 `uv pip` 创建：

```text
.venvs/main  T1、T2、公共工具
.venvs/t3    T3 smolagents
.venvs/t4    T4 OpenHands
.venvs/t5    T5 CrewAI
```

T6 不安装在这些环境中，而是使用父仓库自己的 `.venv` 和 OSIS 工具链。

只创建某一个环境时，例如：

```powershell
uv run python scripts/setup_framework_envs.py --only t3 --base-python (uv python find 3.13)
```

## 配置父仓库

父仓库提供正式 test/reference 数据、原始技能、标准答案工程和 `src/evaluation` 评分代码。它不应直接挂载给模型。

推荐使用环境变量：

```powershell
$env:OSIS_PARENT_REPO = '<path-to-osis-skill-enhance-main>'
```

也可以在本机创建未提交的配置文件：

```powershell
Copy-Item configs/parent_repo.txt configs/parent_repo.local.txt
# 编辑 configs/parent_repo.local.txt，写入父仓库绝对路径
```

`configs/parent_repo.local.txt` 已被 `.gitignore` 忽略。不要把个人路径、API key 或父仓库原始 `.agents/skills` 提交到本仓库。

正式运行前，确认父仓库评分权重合计为 1：

```powershell
$parentConfig = Join-Path $env:OSIS_PARENT_REPO 'configs/evaluation.yaml'
$env:PARENT_CONFIG = $parentConfig
python -c "import yaml, os; from pathlib import Path; w=yaml.safe_load(Path(os.environ['PARENT_CONFIG']).read_text(encoding='utf-8'))['weights']; print(sum(w.values()))"
```

输出应为 `1.0`。六项评分由父仓库统一执行：文本、工程结构、Python 语法、模型符合度、效率和 Token 成本。

## 技能快照

仓库内的 `checkpoints/train-all/skills` 是实验运行使用的无测试答案快照。它属于基础设施，可以直接使用。若要根据指定父仓库重新生成或校验快照：

```powershell
uv run python scripts/build_train_all_snapshot.py `
  --parent-repo $env:OSIS_PARENT_REPO
```

正式实验不要把父仓库的原始 `.agents/skills` 作为 `--skills-dir` 直接挂载。这样会把测试模板或答案泄漏给模型，破坏实验有效性。

如果本仓库是父仓库内部的嵌入副本，应先导出为同级干净工作区：

```powershell
uv run python scripts/export_repro_workspace.py
cd ..\osis-framework-comparison-runtime
```

## 运行单个任务

正式入口是 `scripts/run_dataset.py`。基本命令如下：

```powershell
$env:OSIS_MODEL_API_KEY = '<your-key>'

uv run python scripts/run_dataset.py `
  --architecture T2 `
  --bridge osis-bridge-cantilever-box `
  --form full `
  --index 0 `
  --seed 0 `
  --parent-repo $env:OSIS_PARENT_REPO
```

可用架构：

| 架构 | 编排方式 | 环境 |
|---|---|---|
| T1 | direct one-shot | `.venvs/main` |
| T2 | LangGraph / LangChain | `.venvs/main` |
| T3 | smolagents CodeAct | `.venvs/t3` |
| T4 | OpenHands CodeAct | `.venvs/t4` |
| T5 | CrewAI 角色协作 | `.venvs/t5` |
| T6 | OSIS 原生路由 | 父仓库 `.venv` 和 OSIS 工具链 |

任务形式有三种：

- `full`：从自然语言需求生成完整工程；
- `gen`：生成任务指定的新模块或新内容；
- `edit`：基于已有初始工程进行修改。

支持的桥型名称以 `common/task_schema.py` 为准，例如 `osis-bridge-cantilever-box`、`osis-bridge-rigid-frame-box`、`osis-bridge-precast-t-girder`、`osis-bridge-precast-small-box`、`osis-bridge-conventional-box` 和 `osis-bridge-hollow-slab`。

模型设置可以按运行切换：

```powershell
uv run python scripts/run_dataset.py `
  --architecture T2 --bridge osis-bridge-cantilever-box --form full `
  --model deepseek-v4.1-flash-expires-on-0910 --reasoning-effort high
```

`--reasoning-effort` 支持 `low`、`medium`、`high`；省略时不发送该字段，保持旧实验口径。
模型 ID、温度和思考强度都会写入每个 run 的 `frozen_config.json`。

正式运行默认开启求解并要求收敛。诊断时可以使用：

```powershell
--no-solve-gate
```

关闭求解 gate 不代表模型通过正式实验，只表示不把求解收敛作为该次诊断的完整成功条件。若只想检查生成和编译，也可以使用 `--no-pyosis`，但这种运行不能产生正式模型评分。

## 运行产物

每次运行写入 `runs/` 下的独立目录。常见文件包括：

| 文件 | 内容 |
|---|---|
| `input.json` | 原始任务规格 |
| `candidate_project/` | 候选工程 |
| `compile.json` | Python 编译结果 |
| `backend_status.json` | PyOSIS 建模、验证和求解状态 |
| `model_score.json` | 父仓库 CLI 模型符合度结果 |
| `reference_score.json` | 父仓库 `src/evaluation` 的六项综合结果 |
| `evaluation.json` | 当前运行的正式综合评价 |
| `runtime_measurements.json` | PyOSIS 运行态测量 |
| `runtime_score.json` | 运行态参数评分旁路结果 |
| `manifest.json` | 模型、seed、技能哈希、版本和产物哈希 |
| `frozen_config.json` | 本次运行的不可变配置记录 |

`evaluation.json` 的正式分数来自父仓库评价器。`runtime_score.json` 是可审计的运行态旁路结果，不替换正式评分。

## 批量阶段运行

三阶段调度器会按 `full → gen → edit` 执行，并支持断点恢复：

```powershell
uv run python scripts/run_campaign.py --label formal-run --auto-forms --resume
```

批量运行也可统一指定模型和强度：

```powershell
uv run python scripts/run_campaign.py `
  --label high-effort-run --auto-forms --resume `
  --model deepseek-v4.1-flash-expires-on-0910 `
  --reasoning-effort high
```

只有上一阶段同时生成 `evaluation.json` 和 `manifest.json` 的任务，才会进入下一阶段。已有终态结果会跳过，中断任务可以重跑。

## 后处理和表格

实验结果不提交到仓库，但后处理程序会随仓库一起发布。对一个运行目录生成 Excel：

```powershell
uv run python scripts/export_results_xlsx.py `
  --runs-dir runs\formal-run `
  --output runs\formal-run\experiment_results.xlsx
```

输出工作簿包含原始运行明细、总表、按任务形式分表、按桥型分表、架构汇总、运行态评分明细和评分口径说明。

汇总表中的均值是对符合统计条件的独立运行求平均，默认按架构、桥型、任务形式分组。生成失败、缺少正式评分或被标记为基础设施失败的运行不会作为有效分数纳入均值；完整成功率单独统计，不会把失败运行从分母中隐去。

其他后处理工具：

```powershell
# 重新处理已有运行
uv run python scripts/rescore_runs.py --runs-dir runs\formal-run

# 查看运行进度
uv run python scripts/progress_board.py --label formal-run

# 检查外部运行目录和数据泄漏
uv run python scripts/audit_external_dirs.py
uv run python scripts/audit_no_cheating.py
```

## 开发任务和测试

`tasks/dev/bridge_whole_l1.json` 是适配器开发用的冒烟任务，不用于论文主结果。正式任务来自父仓库数据集，并在每个运行目录中保存一份 `input.json` 作为审计证据。

运行全部测试：

```powershell
uv run pytest -q
```

只运行后处理测试：

```powershell
uv run pytest -q tests/test_export_results_xlsx.py
```

T6 相关测试还需要 OpenCode agent instructions 和父仓库工具链；没有这些外部组件时，生成、评分和 T6 集成测试不能视为完整复现。

## 仓库边界和提交规则

应该提交：代码、适配器、统一运行器、评分调用代码、`pyproject.toml`、`uv.lock`、`.python-version`、配置模板、任务 schema、测试、文档、无答案技能快照、后处理脚本和表格测试。

不应该提交：`.venv/`、`.venvs/`、`node_modules/`、`runs/`、`reports/`、`tmp/`、缓存、日志、模型会话、API key、父仓库原始 `.agents/skills`、test 答案、本机绝对路径和正式实验结果。

## 常见问题

**`uv lock` 解析失败**：确认使用 Python 3.12+，然后重新执行 `uv python install 3.13` 和 `uv lock`。

**找不到父仓库**：设置 `OSIS_PARENT_REPO`，或给运行命令显式添加 `--parent-repo`。

**T6 无法启动**：确认父仓库 `.venv` 中安装了 PyOSIS，且 OSIS 求解器和 OpenCode 路径已按父仓库要求配置。T6 不会因为 `uv sync` 自动获得这些外部工具。

**只有 `model_score.json` 没有 `reference_score.json`**：通常表示没有找到 `scorer_private/reference_project`，或父仓库评分环境不可用；该运行不能作为正式六维综合分纳入表格。

**想关闭求解**：仅用于诊断，使用 `--no-solve-gate`，不要把这类结果与正式开启求解的结果混合比较。

