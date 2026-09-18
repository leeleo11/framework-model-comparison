"""osis-calcbook-docx-compiler scripts package.

模块结构（与 .agents/skills/osis-calcbook-docx-compiler/scripts 一致）：
- ooxml_utils.py: OOXML 底层操作（DocxPackage/rels/段落与 run 文本替换/红字原语）
- standard_dict.py: 标准数据字典（Tag→source 唯一机器事实源）
- prepare_template.py: 模板首接预处理（清旧图/清表数据行，产 prepared-view）
- osis_extract.py: OSIS 输出解析器（Temperary/Check/image/日志/画像 → 结构化数据，只读）
- project_brief.py: Project Brief 组装（facts/tables/images/缺失报告；re-export 解析 API）
- compile_mapping.py: Tag 写入 + mapping.json 校验 + 三件套打包/安装/检索
- render.py: 固定 Runtime 主流程（Pass1 块级 / Pass2 内联）、输出自检与 CLI
- docx_tables.py: 表格整表重建（语义列映射 + 单位换算 + 缺表红"空"占位（整行））
- docx_images.py: 插图（尺寸解析/页面 EMU/DrawingML 构造/media/rels 嵌入）
- render_checks.py: 验算结论解析（verdict 列族/控制行/单位换算）
"""
from __future__ import annotations

__version__ = "2.3.0"
