# 复现清单

本副本是可提交到源码仓库的实验设施，不是正式实验运行目录。复现实验时应先导出同级运行工作区：

仓库提交 `pyproject.toml`、`uv.lock` 和 `.python-version`，不提交虚拟环境目录；因此复现者
只需安装 `uv`，即可按下面步骤得到同一组基础依赖。

```powershell
uv python install 3.13
uv sync --python 3.13
uv run python scripts/export_repro_workspace.py
cd ..\osis-framework-comparison-runtime
uv run python scripts/setup_framework_envs.py --base-python (uv python find 3.13)
```

导出脚本只复制实验源码、配置、任务描述、测试和 `checkpoints/train-all` 快照；不会复制 `runs/`、`.venvs/`、缓存、日志、API key 或父仓库的原始 `.agents/skills`。运行目录中的 `configs/parent_repo.local.txt` 是本机配置，不应提交。

## 快照边界

`checkpoints/train-all` 是实验时挂载给各框架的统一知识快照。它包含训练阶段可见的技能与案例，不包含 test 模板和 test 答案。正式运行所需的 test/reference 数据仍从 `--parent-repo` 指向的源码仓库读取，但不会作为模型上下文直接挂载。

## 复现前检查

1. 使用与论文记录一致的父仓库版本。
2. 运行 `uv run python scripts/setup_framework_envs.py` 创建各框架环境。
3. 运行 `scripts/build_train_all_snapshot.py --parent-repo <父仓库>` 重新校验或构建训练快照。
4. 设置模型网关密钥后，再运行 `scripts/run_dataset.py` 或批量驱动脚本。
5. 结果写入导出工作区的 `runs/`，不会污染源码仓库。
