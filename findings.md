# 当前发现

## 接入现状（2026-09-08）

- T1 是真正的一次性请求：接收完整 `TaskSpec.to_dict()`，通过文件块解析写入候选工程；无工具循环。
- T2 是真实 LangGraph `create_agent` ReAct 图；8 个受控工具只能读取挂载快照或写候选工程，工具异常统一返回 `TOOL_ERROR`。
- T3 是 smolagents `CodeAgent`；额外导入白名单只有 `json`，不能用 `pathlib` 绕过 `write_file`；`success/max_steps_error` 状态已正确映射。
- T4 是 OpenHands SDK 原生 `load_skills_from_dir` + 原生会话；工具注册使用具体 Observation 子类，避免 SDK 联合类型错误；每次 run 的原生项目独立创建并同步回候选目录。
- T5 是 CrewAI 顺序角色协作（researcher → engineer → reviewer）；三角色共享同一套 8 个受控工具，模型调用和墙钟预算由统一配置按比例分配。
- T6 是独立 OSIS/OpenCode 原生会话；先创建本 run 的 PyOSIS 项目，再以该项目作为 OpenCode cwd，结束后只同步其 `py/` 到候选目录。不会继续使用桌面残留项目。

## 统一流程与评分

- `scripts/run_dataset.py` 是唯一正式入口；`run_t2_dataset.py` 现在只是兼容转发器，不再维护第二套快照、限时或评分协议。
- 所有架构都只负责生成 `workspace/candidate_project`。统一 runner 负责物化、布局检查、编译、PyOSIS 建模门禁、已有 CLI/`src/evaluation` 评分和 manifest。
- 正式知识快照为对比仓库 `checkpoints/train-all/skills`；它由父仓库只读生成，原始 `.agents/skills` 被入口拒绝。`gen/edit` 的 `base_files` 由工具侧预置，答案模板只在生成完成后写入 `scorer_private/`。
- 正式 `evaluation.json` 由 `common/official_evaluation.py` 组装父仓库 `src/evaluation` 的六个系统；源码构造分在编译失败或 `model_created=false` 时置零。运行态三十多项参数抽取不在当前范围。
- 预生成清单现在保存绝对路径和 SHA-256；重试只在没有新/改文件时进行，半成品和其源码分数不会被重试删除。
- 余额不足/HTTP 402 等永久基础设施错误不再做无意义重试，写入 `infra_failure.json` 并标记 `excluded_from_aggregate=true`；模型读循环、写不完整等仍是有效框架观察。

## 计时与观测

- 总任务时限从数据集任务开始计时，生成和后续 PyOSIS/CLI 阶段共用同一个绝对截止时间；分任务时限仍写入 `timer.json`/`frozen_config.json` 并传给执行器。
- 每个生成日志尽可能记录 `model_calls`、`tool_calls`、`framework_steps`、`stop_reason`、`tokens`、`elapsed_s` 和 `framework_version`；T2 另有逐轮 JSONL transcript，T6 对 SSE 更新去重后统计工具调用和 token。
- Excel 导出脚本读取 T1–T6 的生成元数据（含 T6），新增模型调用、无效工具调用、步数、停止原因和基础设施排除列。

## 验证结论

- 主项目单元/集成测试：`114 passed`。
- T3/T4/T5 独立虚拟环境可导入且版本分别为 smolagents 1.26.0、openhands-sdk 1.44.1、crewai 1.15.18；T4 快照原生加载实测 28 个 skill。
- 尚未完成真实 T1–T6 full 复跑：当前 API 账户无余额；启用 PyOSIS/T6 时还需父仓库 OSIS 后端服务监听 `localhost:18080`。因此不能把离线测试当成真实模型结果。
