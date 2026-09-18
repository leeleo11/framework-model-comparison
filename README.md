# OSIS 框架对比实验设施

本目录是随 `osis-skill-enhance-main` 源码仓库保存的实验设施副本，包含 T1–T6
适配器、统一运行器、源码评分、运行态旁路评分、Excel 后处理、测试和无测试答案的
`checkpoints/train-all` 技能快照。

## 不要直接在本目录运行正式实验

本目录位于 OSIS 源码仓库内部，而父仓库本身包含原始 `.agents/skills` 和测试答案。
直接在这里运行可能让 T6 沿祖先目录发现不应挂载的文件。正式运行前，先从父仓库
导出一个同级的干净工作区：

```powershell
uv python install 3.13
uv sync --python 3.13
uv run python scripts/setup_framework_envs.py --base-python (uv python find 3.13)
```

这会用锁文件安装基础依赖，并用 `uv` 在 `.venvs/` 下创建 T1–T5 的隔离环境；不会把
任何虚拟环境或安装包提交到 Git。T6 使用父仓库自己的 `.venv` 和 OSIS 工具链，需另行
按 `--parent-repo` 配置。

如果本仓库是从父仓库内导出的副本，先运行：

```powershell
uv run python scripts/export_repro_workspace.py
cd ..\osis-framework-comparison-runtime
```

导出脚本会把比较实验源码和无答案技能快照复制到同级目录，并写入仅存在于本地的
`configs/parent_repo.local.txt`。它不会复制 `runs/`、`.venvs/`、缓存、日志或 API key。

## 运行实验

在导出的同级工作区中设置模型网关密钥，然后按仓库根目录 README 的命令运行：

```powershell
$env:OSIS_MODEL_API_KEY = '<your-key>'
uv run python scripts/run_dataset.py `
  --architecture T2 `
  --bridge osis-bridge-cantilever-box `
  --form full --index 0 --seed 0 `
  --parent-repo ..\osis-skill-enhance-main
```

正式 test/reference 数据仍由父仓库提供；模型实际只挂载导出的 `train-all` 快照，
不会直接挂载原始 `.agents/skills`。

正式数据运行默认会执行 `engine.solve()` 并要求求解收敛；诊断时可使用
`--no-solve-gate`。首次运行前可用 `uv run pytest -q` 验证环境。实验产生的 `runs/`、`reports/`、缓存和本地
父仓库配置均被 Git 忽略；仓库只保存代码、配置模板、测试、脚本和无答案的训练快照。

## 三阶段自动运行

`scripts/run_campaign.py --auto-forms --resume` 会严格按 `full → gen → edit` 调度。
上一阶段的每个任务必须同时有 `evaluation.json` 和 `manifest.json` 才会放行下一阶段；
中断任务会被重跑，已有终态结果会保留并跳过。
