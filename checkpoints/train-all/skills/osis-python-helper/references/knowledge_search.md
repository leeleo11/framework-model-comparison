# 知识库检索（Weknora）

查 pyosis API 用法/参数语义/错误修法时，除 SKILL 文档外还可检索公司知识库。

## 用法（bash，环境变量已注入时）

```bash
python <skill_dir>/scripts/weknora_search.py "create_rect 参数 签名"
python <skill_dir>/scripts/weknora_search.py "钢束形状 控制点 超出范围 修复"
```

- 输出为 top-5 检索片段；无命中时提示改用 `pyosis_doc.py` / `read_skill`。
- 配置读取环境变量 `WEKNORA_BASE_URL` / `WEKNORA_API_KEY` / `WEKNORA_KB_IDS`；未配置时脚本返回 `TOOL_ERROR`，此时退回 SKILL/`pyosis_doc` 路径。
- 与铁律 10 的关系：本脚本即「查知识库」的可用实现；知识库无命中或未配置时按守则降级。
