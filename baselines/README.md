# 框架生成层

`t1_direct`、`t2_langgraph`、`t3_smolagents`、`t4_openhands`、`t5_crewai` 和 `T6` 的接入都属于生成层。每个接入器必须实现同一语义的入口：

```python
generate(task, skill_reader, workspace) -> workspace / "candidate_project"
```

约束：

1. 只能通过 `SkillAdapter` 只读访问同一份 `.agents/skills`、references 和任务文档；
2. 只能在自己的 `workspace` 写文件；
3. 最终提交 `candidate_project/py/prep/` 标准工程，不写评分、求解或 `runs/` 结果；
4. 生成层结束后由 `common.runner.ExperimentRunner` 统一物化、执行和评分。

框架之间只比较编排方式（one-shot、ReAct、CodeAct、角色协作、OSIS 原生路由）。技能内容、PyOSIS 环境、CLI 评分器、任务限时和输出契约保持不变。
