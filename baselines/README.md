# 框架生成层

`t1_direct`、`t2_langgraph`、`t3_smolagents`、`t4_openhands`、`t5_crewai` 和 `T6` 的接入都属于生成层。每个接入器必须实现同一语义的入口：

```python
generate(task, skill_reader, workspace) -> workspace / "candidate_project"
```

约束：

1. 读取同一份技能快照。T2 经绑定的读工具，T3 经 `pathlib`，T4 经 `invoke_skill`，T5 经 CrewAI `load_skill`，T6 经 OpenCode 自己的技能目录。生成阶段不把 PyOSIS 做成共用工具。T3 可在解释器中导入 `pyosis`，只链接父仓库里的纯 Python 包；
2. 只能在自己的 `workspace` 写文件；
3. 最终提交 `candidate_project/py/prep/` 标准工程，不写评分、求解或 `runs/` 结果；
4. 生成层结束后由 `common.runner.ExperimentRunner` 统一物化、执行和评分。

框架之间比较各自的编排和该框架官方的动作接口。技能内容、任务限时和输出契约保持不变。交卷后不再把报错送回，正式评分只做一次。T2 的工具由调用方交给 `create_agent`，只含读技能和写候选工程，不接知识库，也不含运行工程。T3 只使用 `CodeAgent` 的 Python 解释器和 `final_answer`，解释器放行 `pyosis`。T4 通过 OpenHands 原生 `invoke_skill` 使用技能，并用官方终端和文件编辑器写候选工程。T5 通过 `Crew(skills=...)`、委托工具和 `FileWriterTool` 在同一次 `kickoff` 里写候选工程；代码执行关闭。T6 会话空闲后由运行器对当前 OSIS 工程求解，求解使用父仓库的 Python。
