# 复现环境搭建指南

本文件是 [`README.md`](README.md) 的详细补充。README 给出最短的安装和运行路径；本文件说明
双 Python 环境、T6 的外部依赖、Windows 限制、评分运行时和常见故障。若两者的命令出现差异，
以仓库当前脚本和 README 的 quick start 为准，并优先使用下面的 `uv run` 入口。

本指南面向**拿到源码仓库、要在一台干净机器上重建实验环境的人**。目标是让六个架构
（T1–T6）能在同一台机器上跑出与论文口径一致的结果。

所有命令都在 **Windows + PowerShell** 下验证。本设施是 Windows-only 的（框架解释器路径写死为
`Scripts\python.exe`，T6 的引擎路径用 `os.add_dll_directory` 与 `DETACHED_PROCESS`）。

---

## 0. 先理解：这里有两个环境，不是两个

最容易踩的坑。T1–T5 和 T6 跑在**不同的 Python 环境**里：

| 环境 | Python | 谁在用 | 怎么建 |
|---|---|---|---|
| `<框架目录>\.venvs\{main,t3,t4,t5}` | **3.13.x** | T1、T2（共用 `main`）、T3、T4、T5 | `setup_framework_envs.py` |
| `<父仓库>\.venv` | **3.11.x** | **T6** | `uv sync`（在父仓库里） |

**`setup_framework_envs.py` 不建父仓库那个环境，只探测它。** 复现者必须自己建——否则 T6
起不来，而报错信息不会直说是环境缺失。

为什么两个版本：`crewai` 要求 `<3.14`、`openhands-sdk` 要求 `>=3.12`，3.13 是交集；
T6 侧（pyosis / opencode_client）生态在 3.11。

---

## 1. 前置条件

| 需要 | 版本 / 说明 |
|---|---|
| Windows | 必须。路径与进程 API 都是 Windows 专用 |
| [uv](https://docs.astral.sh/uv/) | 用它装两套 Python |
| Python 3.13 | `uv python install 3.13` |
| Python 3.11 | 父仓库 `requires-python = ">=3.11"`；`uv sync` 会准备 |
| 父仓库 `osis-skill-enhance-main` | 与论文记录**同一个 commit** |
| OpenCode 安装包 | 只有 **T6 用**。默认按 `Rbin64*/opencode` 同级目录找 |
| OSIS 引擎文件 | `PySolver.dll` + `Osis.exe`，在 `Rbin64*` 目录里 |
| 模型网关密钥 | 自备，见第 6 节 |

### 1.1 两个**不在本仓库内**的外部安装件

复现者最容易卡在这里：Python 依赖都能从公开渠道装好，但下面两样**源码仓库不提供、也不随仓库发布**。

**① OSIS 应用（≥ 5.0）—— 必需，且是商业软件**

`osis-python`（导入名 `pyosis`）本身公开在 PyPI，可以 `pip install osis-python`，但它**只是客户端库**：官方说明里写明它 **Requires OSIS >= 5.0**，即机器上必须已安装 OSIS 应用。该应用提供：

- `PySolver.dll`、`Osis.exe`（在 `Rbin64*` 目录里，求解与执行引擎）
- pyosis 真正驱动的后端进程

本设施按同级目录 `Rbin64*`（或 `OSIS_SOLVER_INSTALL`）自动发现它。**没有它，PyOSIS 执行阶段无法运行**——所有架构的"构造正确性"都会是 0，只剩静态分可用（见第 11 节）。

**② 打包版 OpenCode —— 只有 T6 需要**

`opencode.exe` 本身是开源项目，但本设施用的是 `Rbin64*/opencode` 下的**定制包**（含 `.agents/AGENTS.md` 原生工作流、全局插件、全局技能）。用 `OSIS_OPENCODE_DIR` 指向含 `opencode.exe` 的目录即可；T1–T5 完全不需要它。

> **没有这两样时能做到什么**：环境能装好、测试能跑过、`full` 的**静态分**链路能复现；但 PyOSIS 执行与"构造正确性"维度、以及 T6 的全部能力都无法复现。论文口径里必须写清这一条。

> **注意**：本仓库的环境脚本会直接调用 `uv venv` 和 `uv pip` 创建、安装 T1–T5 的隔离环境；
> 项目自身的命令统一通过 `uv run` 启动。`--base-python` 仍必须指向一个真实存在的
> `python.exe`，通常使用 `uv python find 3.13` 得到它。T6 的父仓库环境单独在父仓库中用
> `uv sync` 创建。

---

## 2. 取得两份代码，并确认它们是**同级目录**

框架与父仓库必须是**平级**，不能把框架放进父仓库里面。

```
<某个父目录>\
├── osis-skill-enhance-main\        ← 父仓库（技能、数据集、评分器）
└── osis-framework-comparison-runtime\   ← 导出的干净运行区
```

框架源码随父仓库发布，在本目录。导出成同级运行区：

```powershell
cd <父目录>\osis-skill-enhance-main
python experiments\framework-comparison\scripts\export_repro_workspace.py
# 默认导出到 <父目录>\osis-framework-comparison-runtime
cd ..\osis-framework-comparison-runtime
```

导出脚本会跳过 `.venvs/ .venv/ runs/ tmp/ reports/ node_modules/`，并写一份
`configs\parent_repo.local.txt` 记录父仓库的**本机绝对路径**——这个文件不要提交。

---

## 3. 建框架的 4 个环境

```powershell
cd <父目录>\osis-framework-comparison-runtime
$py = uv python find 3.13
uv run python scripts\setup_framework_envs.py --base-python $py
```

装出来的东西（`common\adapters.py` 是版本权威来源）：

```
main  langgraph==1.2.11  langchain==1.3.18  openpyxl  requests  PyYAML  pytest
t3    smolagents==1.26.0
t4    openhands-sdk==1.44.1
t5    crewai==1.15.18
```

可选参数：`--only {main,t3,t4,t5}`、`--dry-run`。

产出 `.venvs\environments.json`，记录每个环境的 `packages` / `python` / `version` / `status`。
**这是校验自己装得对不对的凭据，别删。**（`version` 字段是尽力探测，探测不到时值为字符串
`"installed"`，属正常。）

---

## 4. 建父仓库的环境（T6 用）

```powershell
cd <父目录>\osis-skill-enhance-main
uv sync
```

产出 `<父仓库>\.venv`，里面有 `opencode-ai`、`httpx`、`PyYAML`、`osis-python>=0.6.2`（pyosis 在这里）。

---

## 5. 建技能快照

模型只能看到"无答案"的技能快照，不是父仓库的原始技能树：

```powershell
cd <父目录>\osis-framework-comparison-runtime
uv run python scripts\build_train_all_snapshot.py
```

默认输出 `<框架目录>\checkpoints\train-all\`。读取父仓库的 `.agents\skills` 与 `configs\datasets.yaml`，
清洗掉 test 模板名后落盘，并写 `manifest.json`（含全量文件哈希）。

跑实验时若快照不存在，会直接退出并提示用这个脚本重建。

---

## 6. 配置模型网关

```powershell
$env:OSIS_MODEL_API_KEY = '<你的密钥>'
```

- 变量名也可用 `OSIS_API_KEY`（别名）。两个都没设 → 运行会以 `api_key_missing` 拒绝启动。
- 密钥只从环境读，**不写入任何运行产物**。

模型与思考强度可在单次运行或整批实验中切换。支持 `low`、`medium`、`high`：

```powershell
uv run python scripts\run_dataset.py `
  --architecture T2 --bridge osis-bridge-cantilever-box --form full `
  --model deepseek-v4.1-flash-expires-on-0910 --reasoning-effort high
```

不传 `--reasoning-effort` 时不会向网关发送该字段，保持历史默认行为。显式选择后，T1–T5
将其作为 OpenAI-compatible 请求参数传递，T6 将其作为 OpenCode model variant 传递；模型名、
温度和强度都会冻结到 `frozen_config.json`。模型网关若不支持所选等级会返回请求失败，运行不会
静默改用另一档。

Windows 的系统代理会劫持到该网关的请求。**驱动脚本会自动为子进程补 `NO_PROXY`**，不需要手工设置；
但如果绕过驱动直接调模型，需要自己处理。

---

## 7. 实验时限（与父仓库对齐）

生成预算对齐**父仓库原生工具链自身的口径**，避免任何架构被截断得比原生更短：

| | 值 | 出处 |
|---|---|---|
| 生成预算 | **6000s（100 分钟）** | 对应父仓库 `opencode_client.DEFAULT_TIMEOUT = 6000.0`（`train/runner.py`、`dashboard/jobs.py` 同值） |
| 生成后预留 | 1800s | 覆盖编译、PyOSIS 执行与源码评分；实测这三步合计 **< 200s** |
| 单任务总时限 | **7800s** | 上两者之和 |

这三个常量在 `common\task_schema.py` 的 `DEFAULT_TOTAL_TIMEOUT_S` 与
`scripts\run_dataset.py` 的 `FROZEN_GENERATION_STEP_TIMEOUT_S` / `FROZEN_POST_GENERATION_RESERVE_S`。

> **注意**：时限是写入每个 run 的 `frozen_config.json` 的**冻结变量**。改了常量之后，
> 之前用旧时限跑出的 run **不能与新数据混用**——它们是不同的实验条件。

`DEFAULT_SUBTASK_TIMEOUT_S`（P0–P6）里只有 **P4（PyOSIS 执行）与 P5（评分）**被当作真实超时；
P0–P3 只是 `timeout_stage` 的阶段标注，不约束生成预算。

---

## 8. 验证环境

### 8.1 确认父仓库解析正确

```powershell
uv run python -c "from common.paths import resolve_parent_repo; print(resolve_parent_repo())"
```

解析顺序（代码实现，**优先于 README 的描述**）：

```
--parent-repo  >  OSIS_PARENT_REPO  >  configs\parent_repo.local.txt
               >  configs\parent_repo.txt  >  同级目录 osis-skill-enhance-main
```

环境变量与配置文件里的候选值会做**三项校验**（`configs/datasets.yaml`、`.agents/skills`、
`datasets/` 三者齐备才算数），不合格的候选被跳过；显式传入的 `--parent-repo` 不做校验，直接采信。

### 8.2 跑测试

```powershell
uv run pytest -q
```

应全部通过。

### 8.3 对比环境清单

把 `.venvs\environments.json` 与论文附带的环境审计件对比。

---

## 9. 跑一次冒烟

用独立目录，避免污染正式结果：

```powershell
cd <父目录>\osis-framework-comparison-runtime
uv run python scripts\run_dataset.py `
  --architecture T1 `
  --bridge osis-bridge-cantilever-box `
  --form full --index 0 --seed 0 `
  --parent-repo ..\osis-skill-enhance-main `
  --runs-dir runs\smoke\preflight
```

首行应输出 `{"leakage_guard": "passed"}`。若为 `"failed"`，运行会被拒绝，失败详情写在
`runs\smoke\preflight\leakage_guard_failures\`。

---

## 10. 跑正式实验

单条：

```powershell
uv run python scripts\run_dataset.py `
  --architecture T2 --bridge osis-bridge-cantilever-box `
  --form full --index 0 --seed 0 `
  --parent-repo ..\osis-skill-enhance-main `
  --runs-dir runs\official\T2
```

成批（推荐，自动处理 T6 独占与阶段闸门）：

```powershell
$env:OSIS_MODEL_API_KEY = '<你的密钥>'
uv run python scripts\run_campaign.py `
  --label formal-YYYYMMDD `
  --auto-forms --resume `
  --parent-repo ..\osis-skill-enhance-main `
  --bridges osis-bridge-cantilever-box osis-bridge-conventional-box osis-bridge-hollow-slab `
            osis-bridge-precast-small-box osis-bridge-precast-t-girder osis-bridge-rigid-frame-box `
  --jobs 3
```

- `--auto-forms`：按 `full → gen → edit` 三个阶段跑，阶段之间**硬闸门**（前一阶段有未完成的 run 就拒绝进入下一阶段）
- `--resume`：跳过已有 `evaluation.json` + `manifest.json` 的 run
- 结果落在 `runs\<label>\`，汇总写成 `campaign_summary.json` 与 `reports\<label>.xlsx`

**T6 是串行的，这是硬约束。** OSIS 引擎是进程级单例，`project.create` 会切换所有客户端的当前项目；
T6 的原生会话在自己的沙箱里跑 pyosis，任何锁都盖不住。一个 T6 会话和另一个 run 的 P4 重叠会让两边
**双双损坏**。调度器因此把每个阶段拆成"非 T6 并行 + T6 严格串行"两段。

---

## 11. 两个分数

每个 run 的 `evaluation.json` 同时给出两个**框架综合分**，两者用同一套权重，**只差那个 0.40 权重的
"构造正确性"维度的取值来源**：

| 字段 | 构造维度的输入 | 含义 |
|---|---|---|
| `quality_score_static` | 候选的**源码文本**（AST 抽取） | 与父仓库评分器的总分一致（构造维度被门禁归零时不等于） |
| `quality_score_runtime` | **实际建出的模型**抽取的 51 项测量值 | 判定对象是真实模型 |
| `quality_score` | 同 static | 向后兼容保留 |

**合格判定不看总分**，看 `complete_success`：

```
布局完整 ∧ 编译通过 ∧ 模型真的建出来 ∧ 验证通过 ∧ 参考评分可用 ∧ 构造维度 ≥ 0.8
```

`Excel` 导出（`reports\<label>.xlsx`）同时给出两列，另有 `运行态评分状态` / `运行态缺失参数` 便于审计。

---

## 12. 已知坑

| 症状 | 原因 | 处理 |
|---|---|---|
| `unrecognized arguments: --parent-repo` | 把父仓库参数传给了只负责建 T1–T5 环境的脚本 | 从 `setup_framework_envs.py` 命令中移除该参数；父仓库通过 `OSIS_PARENT_REPO`、配置文件或运行命令的 `--parent-repo` 指定 |
| T6 报 `framework_env_missing` | 父仓库的 `.venv` 没建 | 回第 4 节 `uv sync` |
| 运行立刻退出，`{"leakage_guard": "failed"}` | 泄漏门禁拦下 | 看 `runs\<...>\leakage_guard_failures\<task>.json` 的 `checks` 字段定位是哪一项 |
| `FileExistsError: [WinError 183]` | 直接调 `run_dataset.py` 时目标 run 目录已存在（`run_campaign` 会自动归档，单条命令不会） | 先把旧目录移走 |
| T6 卡在 `opencode server did not become healthy` | OpenCode 没找到或没起来 | 设 `OSIS_OPENCODE_DIR` 指向含 `opencode.exe` 的目录 |
| 模型请求挂住直到超时 | Windows 系统代理劫持（只影响 httpx 系：T3/T4/T5/T6） | 走驱动脚本即自动处理；直连需自设 `NO_PROXY` |
| `reasoning_content ... must be passed back to the API` | 网关以 thinking 模式运行模型，要求回传上一轮推理内容；litellm 系架构（T3/T4/T5）不回传 | 已被识别为**基础设施故障**，会自动重试并排除出统计 |
| 并发一高就 429 / 网关超时 | 网关压力 | 降 `--jobs`（3 是验证过的值） |
| `skills snapshot does not exist` | 跳过第 5 节 | 跑 `build_train_all_snapshot.py` |
| 磁盘写满 | 每个 run 会产大量中间件 | 驱动带剩余空间保护；预留充足空间 |

---

## 13. 复现不了的部分

**环境可以重建到一致，模型不能。** 论文的口径必须写清楚：

- 冻结的模型位于一个特定网关，其可用性不由本设施保证；`frozen_config.json` 记录了模型名与 `base_url`
- API 密钥不随仓库发布，复现者需自备可达该网关的密钥
- 因此**逐位复现历史分数是不可能的**；可复现的是**方法与环境**，以及在同一模型下的**相对比较**

---

## 附录：环境变量清单

| 变量 | 必需 | 默认 | 作用 |
|---|---|---|---|
| `OSIS_MODEL_API_KEY` | **是** | 无 | 模型网关密钥；未设则拒绝启动 |
| `OSIS_API_KEY` | 否 | 无 | 上者的别名 |
| `OSIS_PARENT_REPO` | 否 | 见第 8.1 节 | 父仓库位置 |
| `OSIS_RUN_ROOT` | 否 | `<框架>\runs` | 结果根目录（仅在未传 `--runs-dir` 时生效） |
| `OSIS_SKILLS_DIR` | 否 | `<框架>\checkpoints\train-all\skills` | 技能快照位置 |
| `OSIS_OPENCODE_DIR` | 否 | PATH → 同级 `Rbin64*/opencode` | OpenCode 安装目录（仅 T6） |
| `T6_AI_PORT` | 否 | `4097` | T6 的 OpenCode 服务端口 |
| `OSIS_HTTP_PORT` | 否 | `18080` | OSIS 引擎 HTTP 端口 |
| `OSIS_SOLVER_PORT` | 否 | `18081` | T6 求解器侧端口 |
| `OSIS_SOLVER_INSTALL` / `_INSTALL2` | 否 | 同级 `Rbin64*` | 含 `PySolver.dll` 的目录 |
