---
name: osis-calcbook-docx-compiler
description: Use when 首次接入新的计算书 DOCX 模板，或检查、修复该模板编译 Skill；已有模板包的日常生成不使用本 Skill。
---

# OSIS 计算书 DOCX 编译

## 边界

这个 Skill 只做一次性模板接入：

```text
脚本清旧图/旧表数据 → OSISAI 审阅模板并标 Tag → 脚本编译模板包
                                                      ↓
后续任意项目：固定 mapping + 项目输出 → 脚本渲染 DOCX（零 AI）
```

- 保留现有五种槽：`text / format / conclusion / table / image`。
- Runtime 只按 `Tag → mapping → source → 当前项目输出` 工作，不分析模板语义。
- 不新增通用同义词引擎、适配层、运行时猜测或 AI 结论生成。
- **数据完整性是 OSISAI 的前置职责**：渲染前确认 `Temperary` 有效且 `image/` 已出图；
  缺数据时执行一次 `python $PLUGIN export --project P` 显式导出（内部调
  `output_result_for_calc_book()`）。导出失败或 OSIS 闪退 → 停止并报告，不带病渲染。
  数据齐备后，brief/render 代码**永不**触发导出——缺项一律红字如实呈现，管线不再考虑数据完整性。

## 完成标准

只有同时满足以下条件，才可报告模板接入完成：

1. `prepared-view` 的每个 block 都有明确的 `static/dynamic` 审阅结果；
2. 所有项目相关内容均已 Tag 化，缺少标准 source 的内容也没有保留旧项目文字；
3. `mapping.json` 与模板中的 `【模块-内容】` 标签一致；
4. 真实项目渲染后无残留 Tag、旧项目图表、旧项目阶段、旧项目结论或旧项目参数；
5. 有输出的数据被填入，无输出的数据以红字提示；DOCX 可正常打开。

`needs_taggedits` 是中间状态。`partial` 表示仍有真实缺项，不能描述成“全部完成”。

## 路由

- **渲染前数据自检**：`project-brief.json` 的 `missing` 出现 `project_dir` 等系统缺失项，或项目 `image/` 缺标准图 → 先 `python $PLUGIN export --project P` 显式导出一次，再渲染；导出失败或 OSIS 闪退则停止并报告。
- 已有与模板 SHA 匹配的模板包：直接走插件 `render`，本 Skill 不再介入。
- **第二次及以后的日常生成不需要模板文件**：不知道 template_id 时先 `python $PLUGIN packages` 查看已装包；只有一个包时直接 `render --project PROJ --output ...`（自动选用该包）；多个包时列出 template_id 让用户选一个。**项目目录里没有模板 .docx 时不要向用户索要模板路径**——模板包在插件目录里，render 不需要源模板。
- 没有模板包或模板 SHA 已变化：执行下面的一次性首接流程。

## 一次性首接流程

### 1. 固定脚本预处理

运行 compile 的第一段，生成：

- `prepared-template.docx`：有图题的项目图清成图槽；有表题的数据表删数据行、保留表头和样式；正文不做语义修改；
- `prepared-view.json`：每个位置的 `temp_id`、类型、文本/题注、章节与图槽尺寸；
- `template-text.md`：供 OSISAI 阅读的模板全文；
- `project-brief.json`：当前项目的 facts、tables、images、checks、荷载组合和项目画像。

当前项目只用于验证数据结构，不能用“本项目没输出”反推模板内容是静态的。

### 2. OSISAI 一次审阅

compile 第一段会自动生成 `draft.json` 草稿（workspace 内）：封面“计算书”自动预置为 `pro-name`，连续荷载组合行自动预置为 `lst-lc` 组；其余块的 static/dynamic 决定机械预填，label/alias 唯一命中的自动预选 tag，多候选列入 `_draft.candidates`，相邻同构未决行自动建组。`_draft.needs_decision` 列出全部待复核块。**AI 在草稿上编辑，不得从零手写 JSON**：给每个待决定块选精确 tag（确认为无标准项的，命名 `tag = miss-<拼音/英文slug>`，每个位置唯一）、收窄 `replace_span` 到动态片段、把误判 dynamic 的固定文字改 static 并删除其条目。改完用 `python draft_taggedits.py check --taggedits <文件> --prepared-view <视图>` 预检（内存校验，不写产物），通过后**另存为 `tg.json`** 交给 compile。

必须完整阅读：

- `references/onboarding-rules.md`
- `references/osis-output-structure.md`
- `template-text.md`
- `project-brief.json`

逐个 `temp_id` 判断：换一个工程后会不会变化？

- 不会变化：`decision: static`，且 `static_reason` 只能是 `标题 / 规范原文 / 公式定义 / 通用说明`。这里的“标题”仅指固定章节标题；封面项目名称必须 dynamic。
- 会变化：`decision: dynamic`，必须产生 Tag；表格和图片槽一律是 dynamic。

动态内容的绑定规则：

1. 先用 `chapter + caption + 公式/单位` 确认业务对象，再在标准字典中找精确 label/alias：选固定 `模块-内容` 标签（如 `【image-shear】`）。AI 不写 source，compile 从 `standard_dict.py` 注入。
2. 标准字典没有该业务输出：写 `tag: null, unresolved: true` 并命名 `tag = miss-<拼音/英文slug>`（每个位置唯一，compile 强制校验），source 固定为 `null`。
3. 相邻的阶段、沉降组、构件清单等列表必须建一个 `reviewed_group`，覆盖全部成员；不能只标第一行。
4. 每个 taggedit 必须带 `temp_id` 或 `group_id`。编译优先按 `temp_id` 定位，因此重复结论文字不会再依赖全局文本唯一性。
5. 同一数据在模板多处出现可复用同一标准 Tag；不同数据不得共用同一 Tag。

禁止跨验算借用相似词：承载力章节的“正截面抗压/抗拉”不等于正常使用阶段的“正截面压应力”或“施工阶段拉应力”；没有精确标准项时必须 unresolved。

### 3. 编译与验收

把 `reviewed_blocks + reviewed_groups + taggedits` 交给 compile：

- 段落内值替换为 Tag；
- 连续列表整体替换为一个 Tag；
- 原表替换为表格 Tag，并记录样式骨架；
- 原图槽替换为图片 Tag，并记录模板宽度与题注图名；
- 输出 `template.docx + mapping.json + package.json`。

编译后模板中的所有字面 Tag 均为红色，便于人工检查；Render 填值后沿用正常模板样式，真实缺项仍以红色提示。标准结构版本已提升，旧模板包不迁移，必须重新首接。

再用真实项目执行一次 render，并检查业务内容。首接样例缺少某标准输出不会阻止模板编译；是否缺值由每次 render 决定。

## 五种业务对象

### 文字

单个项目值只替换值本身，保留标签、标点与单位。例如同一段中的设计程序、安全等级、重要性系数分别使用 `pro-soft / pro-safety / pro-imp`。

项目名称、桥型、体系、基础/支座、跨径、材料等优先使用 `pro-*`；模型数量使用 `mdl-*`。

设计程序固定填 `OSIS`。整体升温/降温从 OSIS 命令中的 `UTEMP` 提取，分别使用 `pro-tup / pro-tdn`；不要把两处都标成笼统的 `pro-temp`。

### 连续列表

- 荷载组合段落：`lst-lc`。先解析 `_logfile.log`；该文件存在但没有组合时继续读取 `Error/Command.log`。`item_sources` 只列 `item_template` 实际使用的占位符，并直接使用 `id / type / type_name / operation / formula / chinese`，不加 `column:`。
- 施工阶段段落：`lst-stage`，数据来自 `table:施工阶段`。
- 没有标准输出的旧阶段、沉降、挠度或其他列表：一个 unresolved group，最终只出现一次红字，不保留任一旧行。

### 验算结论

选择对应的 `chk-*` 结论标签（九项验算各一个）。结论由结构化验算表确定：任一行 NG 则整体 NG，并选控制行生成控制值、限值、单位和满足/不满足片段。

混合段落应通过 `replace_span` 只替换“本项目数值 + 比较关系 + 最终结论”，保留模板中的规范条文和固定表述；不得把整段规范说明删掉，也不得让 AI 在 Runtime 改写结论。

### 表格

表头、列数、顺序和全部数据行**以 OSIS 输出为准**（建模软件是 OSIS，midas 模板表头只是参考格式；与 OSIS 官方计算书 JTG 宏输出的表头一致）；模板表只提供边框、字体等样式骨架。OSIS 没有输出的表：保留模板表头并补一行红“空”占位（整行每格），不得保留旧数据。

材料表必须区分：

- `tbl-c`：混凝土；
- `tbl-ps`：预应力钢材；
- `tbl-rb`：普通钢筋。

混凝土、普通钢筋和预应力钢材的等级从项目材料命令读取，`fck/ftk/fcd/ftd/fsk/fsd/f'sd/fpk/fpd/f'pd` 按固定 JTG 3362 参数表补入，不由 AI 猜值。

另有 `tbl-lc` 荷载工况表（日志 `LoadCase` 行）、`tbl-def` 挠度验算表。先读根目录 `_logfile.log`，没有 `LoadCase` 时再读 `Error/Command.log`；重复命令按“工况名称 + 工况类型码”去重并保持首次出现顺序。三列表的“描述”列 = 工况名称的英文码（与 midas 模板一致：徐变二次→`CS`、钢束二次→`TS`、温度梯度→`TPG`；名称无命中时回退 OSIS 类型码），不写 `factor`。荷载组合仍由 `lst-lc` 展开，展示格式为模板同款 `系数(码)`（如 `1.200(DL)+1.400(M)`），不得写入荷载工况表。项目确实没有对应表时保留表头并补一行红“空”占位（整行每格），不得保留旧数据。

Render 最后根据结构化验算结果追加一段红色“OSIS验算建议”；仅汇总 NG 项或说明全部通过，不再次调用 AI。

### 图片

用对应 `img-*`。最终宽度沿用模板图槽宽度，高度按当前图片原始宽高比计算；不沿用旧高度。缺图时图槽和题注中的旧图名都替换为红字提示，图号与单位保留。
模板原有 `SEQ 图表` 编号域保持不动，Render 写入“打开时更新域”标志，避免所有缓存编号都显示为“图表1”。

## 输出协议

协议与示例见 `references/onboarding-rules.md`。最小要求：

```json
{
  "reviewed_blocks": [
    {"temp_id":"P0001","decision":"static","static_reason":"标题"},
    {"temp_id":"P0002","decision":"dynamic","tags":["pro-btype"]}
  ],
  "reviewed_groups": [],
  "taggedits": [
    {"temp_id":"P0002","tag":"pro-btype","kind":"text",
     "original_text":"桥型：旧桥型","replace_span":"旧桥型"}
  ]
}
```

标准结构缺项：

```json
{"temp_id":"P0003","tag":null,"unresolved":true,"kind":"text",
 "original_text":"设计单位：旧单位","replace_span":"旧单位"}
```

## 禁止事项

- 不因当前 brief 缺键而保留模板旧项目内容；
- 不自编 source、标准 Tag 或业务字段；
- 不把项目参数、阶段行、表格、图片、验算结论判成 static；
- 不把材料、普通钢筋、预应力钢材三张表打成同一个 Tag；
- 不用模板列名去阻止 OSIS 整表写入；
- 不在 render 阶段调用 AI；已有有效数据时不得再次调用 `output_result_for_calc_book()`；
- 不直接手改 OOXML 绕过 compile。

## 命令

```bash
PLUGIN=~/.osisai/plugins/calcbook-docx/main.py

# 渲染前数据自检不通过时：显式导出一次（Temperary/Check/image 补齐）
python $PLUGIN export --project PROJ

# 首接第一段：生成审阅材料
python $PLUGIN compile --template T.docx --project PROJ --id ID

# 首接第二段：写 Tag、生成并安装模板包（tg.json = AI 编辑后的草稿）
python $PLUGIN compile --template T.docx --project PROJ --id ID --taggedits tg.json

# 日常生成：固定脚本，零 AI
python $PLUGIN render --template-id ID --project PROJ --output PROJ/计算书.docx
```

## 产物纪律

- 正式保留：模板包、用户指定的最终 DOCX、最终报告；
- prepared、brief、taggedits 和试验 DOCX 只放 `.calcbook/<id>/` 或系统临时目录，验收后清理；
- render 默认清理自己产生的派生缓存：`json/材料参数.*.json` 与 `<输出名>.report.json`（结果 JSON 里已含校验结论，`--keep-cache` 可保留）；`json/tXxx`、`json/<验算名>`、`项目数据结构.json` 是数据源，**绝不删**；
- 不删除 `_logfile.log`、`Check`、`image`、`json`、`Temperary` 或用户已有 DOCX；
- 源模板只读，所有修改发生在副本。

## 参考

- `references/onboarding-rules.md`：一次性 OSISAI 审阅规则与 JSON 协议；
- `references/osis-output-structure.md`：固定 Tag 字典；
- `schemas/mapping.schema.json`：五种 mapping slot；
- `schemas/mat-strength.json`：JTG 3362-2018 材料强度基准表（材料表 fck/ftk/fcd/ftd 等列的数据源，改值不改代码）；
- `scripts/standard_dict.py`：机器唯一事实源；
- `scripts/osis_extract.py`：OSIS 输出解析器（日志命令/Temperary 表/验算 txt/图片命名 → 结构化数据）。
