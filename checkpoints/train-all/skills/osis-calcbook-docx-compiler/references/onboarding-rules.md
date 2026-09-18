# 新模板一次性审阅规则

本文件只指导 OSISAI 在首次接入模板时决定“哪里要变、对应哪个固定 Tag”。模板包生成后，日常渲染不再使用 AI。

## 1. 三个角色

- Prepare 脚本：清除旧图片本体和旧表数据行，输出带 `temp_id` 的模板视图；不判断动态/静态。
- OSISAI：完整阅读模板，逐个位置做一次语义判断，输出 reviewed blocks 和 taggedits；不改 OOXML。
- Compile/Render 脚本：写 Tag、绑定固定 source、读取项目输出、生成 DOCX；不猜语义。

数据完整性是 OSISAI 的前置职责：compile/render 前确认 `Temperary` 有效且 `image/` 已出图；缺数据时用 `python $PLUGIN export --project P` 显式导出一次（内部调 `output_result_for_calc_book()`），导出失败（如 OSIS 闪退）则停止并报告，不带病渲染。数据齐备后，brief/render 代码不再触发任何导出，缺项一律红字如实呈现。

## 2. 先判断动态，再看 source

唯一判断题：换一个工程后，这个内容是否可能变化？

### 必须 dynamic

- 项目名称、桥型、体系、基础/支座、跨径、材料、设计程序、安全等级、重要性系数；
- 节点/单元/边界/施工阶段数量和所有项目参数；
- 施工阶段、沉降组、荷载组合、构件清单、挠度等项目列表；
- 所有项目表格和图片槽；
- 控制值、限值、OK/NG、满足/不满足等验算结论。

即使当前项目没有输出，也必须 dynamic；不得保留模板旧值。

### 可以 static

只允许四种 `static_reason`：

- `标题`（仅固定章节标题；封面项目名称必须 dynamic）
- `规范原文`
- `公式定义`
- `通用说明`

表格、图片槽、阶段行、沉降行、项目参数和验算结果不得 static。混合段落中的规范原文可以保留，但项目值或结论子句必须单独 Tag 化。

## 3. Tag 选择

### 有标准业务项

先用 prepared-view 的 `chapter + caption + 公式/单位` 确认业务对象，再对照 `osis-output-structure.md` 的精确 label/alias 选择固定 `模块-内容` 标签：

- 项目画像/身份：`pro-*`
- 模型数量：`mdl-*`
- 表格：`tbl-*`
- 结论：`chk-*`
- 图片：`img-*`
- 荷载与阶段列表：`lst-*`

AI 只写 Tag，不写 source。source 由 `standard_dict.py` 固定注入。当前样例项目缺少该 source 不会阻止 compile；render 当前项目时才决定填值或红字。

同一业务项多处出现可以复用同一 Tag；不同业务项不能共用同一 Tag。特别是混凝土、预应力钢材、普通钢筋分别使用 `tbl-c / tbl-ps / tbl-rb`。

只允许同一业务项的简称差异，不允许跨验算近似：

- “正截面抗弯承载能力”只能使用抗弯 Tag；
- 承载力章节的“正截面抗压/抗拉”不等于“正截面压应力/施工阶段拉应力”；
- “预应力钢筋材料性能表”使用 `tbl-ps`，“预应力钢筋特性值表/钢束属性表”使用 `tbl-tdp`；
- 字典找不到精确业务项时 unresolved，不得选语义最接近的 Tag。

### 无标准业务项

不要借用相似 Tag，也不要自编编号或 source。写：

```json
{
  "temp_id": "P0123",
  "tag": null,
  "unresolved": true,
  "kind": "text",
  "original_text": "设计单位：旧单位",
  "replace_span": "旧单位"
}
```

并由 AI 按内容命名 `tag = miss-<拼音/英文slug>`（如 `miss-kangla`；每个位置唯一，compile 强制校验，重复/缺失会拒绝），mapping source 为 `null`，render 输出一个红字缺失提示。

## 4. 位置定位

每个 taggedit 必须带 `temp_id` 或 `group_id`。

- 段落：`temp_id + original_text`；text 必须再给 `replace_span`。
- 表格/图片：使用 prepared-view 中对应 block 的 `temp_id + caption`；图片再给 `caption_name`。
- 重复文字：以 `temp_id` 的模板顺序为准，不要求 `original_text` 在全文唯一。
- 同一业务图片在模板出现多次时，每个图槽各写一条 taggedit，复用同一 Tag；每条保留自己的 `temp_id / caption / caption_name`。compile 会按位置分别保存宽高，render 逐槽按原宽等比缩放。
- 一个段落含多个项目值：建立多个 taggedit，使用相同 `temp_id`、不同 `replace_span`；reviewed block 的 `tags` 列出全部 Tag。

## 5. 连续列表必须整组处理

相邻、同构、属于同一业务列表的段落必须放入一个 `reviewed_group`。成员包含全部旧行，不能只标第一行。

### 施工阶段

使用 `lst-stage`，从 `table:施工阶段` 展开：

```json
{
  "reviewed_groups": [
    {
      "group_id": "G-STAGE",
      "member_temp_ids": ["P0201", "P0202", "P0203", "P0204"],
      "decision": "dynamic",
      "tag": "lst-stage"
    }
  ],
  "taggedits": [
    {
      "group_id": "G-STAGE",
      "tag": "lst-stage",
      "kind": "format",
      "item_template": "施工阶段{seq}:{name}；{days}天；",
      "item_sources": {
        "seq": "column:序号",
        "name": "column:名称",
        "days": "column:持续天数(d)"
      }
    }
  ]
}
```

### 荷载组合

使用 `lst-lc`，从 `_logfile.log` 提取；根日志没有组合时固定回退 `Error/Command.log`。`item_template` 保持模板原有句式，`item_sources` 只列句式实际使用的占位符，直接引用 Project Brief 原始字段 `id / type / type_name / operation / formula / chinese`；不加 `column:`，也不能写模板表头“序号/工况名称/描述”。例如：

```json
{
  "group_id": "G-LOAD-COMBINATION",
  "tag": "lst-lc",
  "kind": "format",
  "item_template": "{id}:{type_name}；{chinese}",
  "item_sources": {
    "id": "id",
    "type_name": "type_name",
    "chinese": "chinese"
  }
}
```

模板中的“荷载工况”三列表使用 `tbl-lc`，来自日志 `LoadCase` 行；先读根目录 `_logfile.log`，没有有效 `LoadCase` 行时回退到 `Error/Command.log`，并按“工况名称 + 工况类型码”去重。第三列沿用模板标题“描述”，内容 = 工况名称的英文码（与 midas 模板一致：徐变二次→`CS`、钢束二次→`TS`、温度梯度→`TPG`；名称无命中时回退 OSIS 类型码），不写 `factor`。它不是荷载组合表，荷载组合仅使用上面的 `lst-lc` 列表——其展示串 `chinese` 为模板同款 `系数(码)` 格式（如 `1.200(DL)+1.400(M)`），不再是中文翻译。

### 没有标准输出的列表

沉降组、旧挠度行或其他无对应输出的整组列表：

```json
{
  "reviewed_groups": [
    {
      "group_id": "G-SETTLEMENT",
      "member_temp_ids": ["P0301", "P0302", "P0303", "P0304", "P0305"],
      "decision": "dynamic",
      "tag": null
    }
  ],
  "taggedits": [
    {
      "group_id": "G-SETTLEMENT",
      "tag": null,
      "unresolved": true,
      "kind": "format"
    }
  ]
}
```

最终只显示一次缺失提示，五条旧沉降值全部删除。

## 6. 五种 taggedit

### text：只替换项目值

```json
{
  "temp_id": "P0042",
  "tag": "pro-safety",
  "kind": "text",
  "original_text": "设计安全等级：一级",
  "replace_span": "一级"
}
```

类似“设计程序 / 安全等级 / 重要性系数”挤在同一段时，分别用 `pro-soft / pro-safety / pro-imp`，不要把整团文字保留为静态。

“1.6 计算原则、内容及控制标准”一节的设计程序介绍句（形如“计算书采用 Osis x.xx.xx ……进行验算”）**有数据源，不得整句标 miss**：只圈版本号数值，绑 `mdl-ver`（text）——

```json
{"temp_id":"P0101","tag":"mdl-ver","kind":"text",
 "original_text":"计算书采用Osis5.00.00对桥梁进行分析计算，……验算。",
 "replace_span":"5.00.00"}
```

句中其他项目参数（如“按A类预应力混凝土结构进行验算”的 A 类判定）来自画像/设计决策，有对应 pro-* 就绑定；规范条文与公式（σst-0.85σpc≤0 等）保持静态（规范原文/公式定义）；条文中的具体限值数值若模板出现，可从验算表限值列或 `schemas/mat-strength.json` 取值绑定，取不到就如实红字。注意：OSIS 材料表是合并的（tMatChar），三张材料子表由脚本按材料名称派生，某类无行时该表红“空”是如实缺失。

## 附：验算 txt 缺失时的恢复序列（OSIS 会话内执行）

`CombinationAndCheck` / 单独 `echk` 若只生成部分验算 txt，原因是 **Check 上下文未初始化**——OSIS 需要先选择验算类型，再求解，才能 echk 输出。从成功会话的 `_logfile.log` 提取的权威序列：

```text
CdCSCRatio, 0.8
CdPC, APC, pre, Post
CdEleSel, All
Check,UltM,基本组合包络
Check,UltN,基本组合包络
Check,Shear,基本组合包络
Check,SSNC,标准组合包络
Check,SSPC,标准组合包络
Check,CrackS,频遇组合包络
Check,CrackWeb,频遇组合包络
Check,CrackWidth,频遇组合包络
Check,CrackL,准永久组合包络
Check,CSNC,MinMax
Check,CSNT,MinMax
CheckSolve
PlLCR, 基本组合1, BFw,Fxyz
/output,<输出路径>,echk,<验算标识>
```

要点：`echk` 输出的是**当前求解上下文**的验算——每种验算类型需要“Check 选择 → CheckSolve → echk”一轮；单独 `echk`（无 Check 选择）会静默返回 ok 但不生成文件。注意 `_logfile.log` 是 GBK 编码。

### conclusion：只替换动态结论片段

```json
{
  "temp_id": "P0430",
  "tag": "chk-flex",
  "kind": "conclusion",
  "original_text": "按照《桥规》……控制值≤承载力，满足规范要求；",
  "replace_span": "控制值≤承载力，满足规范要求；"
}
```

保留模板公式和规范条文，只替换本项目控制值、限值、比较关系与最终结论。若 `replace_span` 覆盖公式、条文或整段，compile 必须拒绝。`chk-*` 结论已固定绑定对应验算表与《桥规》依据；整表任一行 NG 则结论 NG，并确定性选择控制行。通常不需要 AI 写 `ok_template/ng_template`；模板有特殊固定句式时才提供。

### table：整表替换

```json
{
  "temp_id": "TBL0003",
  "tag": "tbl-c",
  "kind": "table",
  "caption": "表1.3 混凝土材料主要指标"
}
```

Render 保留模板表头、列序与样式，把项目输出列按语义映射后填入全部数据行。模板写“容重(kN/m³)”、项目写“密度(N/m³)”时会匹配并换算；部分列没有输出只留空，零列匹配才整表标红。材料强度列由项目材料等级与固定 JTG 3362 参数表补齐，不由 AI 推测。

### image：图片本体与图名

```json
{
  "temp_id": "IMG0004",
  "tag": "img-dcr",
  "kind": "image",
  "caption": "图4.7 斜截面在作用频遇组合下抗裂验算结果（单位：MPa）",
  "caption_name": "斜截面在作用频遇组合下抗裂验算结果"
}
```

宽度沿用模板槽宽，高度按当前图片宽高比生成。缺图时，图槽与 `caption_name` 都替换为红字；图号、单位保留。
图号的 Word `SEQ` 域不得纳入 `caption_name`，Render 会请求 Word/WPS 打开时刷新编号。

### format

当前固定 format 主要用于 `lst-lc` / `lst-stage` 连续列表。普通项目值优先使用 text，多值段落用多个 text Tag，避免再造句式映射。

## 7. 完整输出骨架

> **不要从零手写整份文件。** compile 第一段已生成 `draft.json` 草稿：块的决定、条目形态（kind/original_text/replace_span/caption）均已机械预填且结构合法。AI 只编辑草稿：给 `_draft.needs_decision` 里的每个 temp_id 选精确 tag（字典无对应项则保留 unresolved）、收窄 `replace_span`、纠正误判。提交前先自检：
> `python draft_taggedits.py check --taggedits draft.json --prepared-view prepared-view.json`
> 注意非法写法：kind 只能是 `text/format/conclusion/table/image`；unresolved 用 `tag:null + unresolved:true`（不是字符串 "unresolved"）；unresolved 块的 `tags` 写 `[]`，compile 会自动回填其命名的 `miss-<slug>`（未命名的未决条目会被拒绝）。

```json
{
  "reviewed_blocks": [
    {"temp_id":"P0001","decision":"static","static_reason":"标题"},
    {"temp_id":"P0002","decision":"dynamic","tags":["pro-btype"]},
    {"temp_id":"TBL0001","decision":"dynamic","tags":["tbl-c"]}
  ],
  "reviewed_groups": [],
  "taggedits": [
    {
      "temp_id":"P0002",
      "tag":"pro-btype",
      "kind":"text",
      "original_text":"桥型：旧桥型",
      "replace_span":"旧桥型"
    },
    {
      "temp_id":"TBL0001",
      "tag":"tbl-c",
      "kind":"table",
      "caption":"表1.1 混凝土材料表"
    }
  ]
}
```

`reviewed_blocks` 必须覆盖 prepared-view 的全部 block；dynamic block 的 `tags` 必须与 taggedits/group 最终 Tag 一致。对于 unresolved，先写 `tags: []` 并命名 `tag = miss-<slug>`，compile 会自动回填各处引用。

## 8. 提交前自检

- 每个 block 都审阅了吗？static 都有合法理由吗？
- 项目参数、表、图、阶段和结论是否仍被误判 static？
- 相邻列表是否完整成组？
- 标准项是否选择了固定且彼此不同的 Tag？
- 所选 Tag 的 label/alias 是否与该 block 的 chapter/caption/公式属于同一业务验算？
- 无标准项是否使用 `unresolved: true`，而不是借 Tag 或留旧值？
- 结论是否只替换动态片段，保留规范文字？
- 材料三类表是否分别使用 `tbl-c / tbl-ps / tbl-rb`？
- 图片是否给了正确 `caption_name`？
