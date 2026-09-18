# OSIS 标准数据结构清单（模块化标准数据字典 · 唯一事实来源）

> 跨项目通用、**固定有序**。按**模块**组织，标签 = `【模块-具体内容】`：
> 模块用英文单词，具体内容用英文缩写/词（无合适英文时用拼音），**不用数字编号**。
> 例：`【image-shear】`、`【chk-flex】`、`【pro-btype】`。
>
> - 命名规则：`模块-内容`，内容为 kebab 小写词（可多段，如 `img-m-tp1`）；
>   新增数据项追加到对应模块末尾，起一个未占用的内容名。
> - 使用：首次接入——AI 只判断「模板位置对应哪个 tag」→ 写 `【模块-内容】`；
>   Render——`【tag】` → mapping.slots[tag] → 固定 source → 当前项目数据。
> - 当前项目 brief 仅样例验证，不决定"有没有字段"：标准项有、当前项目没输出 → 照打 Tag（render 红字）。
> - 表格保留模板表头、列数、顺序和样式；项目输出按语义列名映射并提供数据行，已知单位差异做固定换算。
> - 表内各列不单独编号；图片本体与图名共用同一 tag；荷载组合列表整体一个 tag（repeat 展开）。

---

## 模块 profile · 项目画像与项目身份（`field:profile.基本信息.<键>`）

画像保留章节层级：`profile.基本信息.桥型`。

| tag | kind | label | source |
|---|---|---|---|
| pro-name | text | 项目名称 | field:profile.基本信息.项目名称 |
| pro-btype | text | 桥型 | field:profile.基本信息.桥型 |
| pro-sys | text | 结构体系 | field:profile.基本信息.结构体系 |
| pro-fnd | text | 基础/支座体系 | field:profile.基本信息.基础或支座体系 |
| pro-span | text | 跨径 | field:profile.基本信息.跨径 |
| pro-width | text | 桥面宽度 | field:profile.基本信息.桥面宽度 |
| pro-code | text | 设计标准/规范 | field:profile.基本信息.设计标准 |
| pro-safety | text | 安全等级 | field:profile.基本信息.安全等级 |
| pro-imp | text | 重要性系数 | field:profile.基本信息.重要性系数 |
| pro-mat | text | 主梁材料 | field:profile.基本信息.主梁材料 |
| pro-lane | text | 车道标准/活载等级 | field:profile.基本信息.车道标准 |
| pro-lanlen | text | 车道长度 | field:profile.基本信息.车道长度 |
| pro-temp | text | 温度/升降温 | field:profile.基本信息.温度 |
| pro-tup | text | 整体升温 | field:profile.基本信息.整体升温 |
| pro-tdn | text | 整体降温 | field:profile.基本信息.整体降温 |
| pro-creep | text | 收缩徐变参数 | field:profile.基本信息.收缩徐变 |
| pro-sw | text | 自重 | field:profile.基本信息.自重 |
| pro-soft | text | 设计程序 | field:profile.基本信息.设计程序 |

> 画像没有的项（如项目名称/安全等级/重要性系数）→ 保持缺失，render 红字；**绝不从桥型推测基础类型**。

## 模块 model · 模型基本信息（`field:model.*`）

| tag | kind | label | source |
|---|---|---|---|
| mdl-node | text | 节点数量 | field:model.node_count |
| mdl-elem | text | 单元数量 | field:model.element_count |
| mdl-bc | text | 边界数量 | field:model.boundary_count |
| mdl-stage | text | 施工阶段数量 | field:model.construction_stages |
| mdl-ver | text | 软件版本 | field:model.version |
| mdl-g | text | 自重系数 | field:model.gravity_coefficient |

## 模块 table · 材料/钢束/边界/阶段/参数表（`table:*`）

| tag | kind | label | source |
|---|---|---|---|
| tbl-mat | table | 材料参数表 | table:材料参数 |
| tbl-bc | table | 边界条件表 | table:边界条件 |
| tbl-stage | table | 施工阶段表 | table:施工阶段 |
| tbl-tdp | table | 钢束属性表 | table:钢束属性 |
| tbl-tdc | table | 钢束坐标表 | table:钢束坐标 |
| tbl-gt | table | 梯度温度表 | table:梯度温度 |
| tbl-cr | table | 收缩徐变表 | table:收缩徐变 |
| tbl-rct | table | 支座反力表 | table:支座反力 |
| tbl-c | table | 混凝土材料参数表 | table:材料参数.混凝土 |
| tbl-ps | table | 预应力钢材参数表 | table:材料参数.预应力钢材 |
| tbl-rb | table | 普通钢筋参数表 | table:材料参数.普通钢筋 |
| tbl-lc | table | 荷载工况表 | table:荷载工况 |
| tbl-def | table | 挠度验算表 | table:验算表格.挠度验算 |
| tbl-mc | table | 正截面抗弯承载能力验算表 | table:验算表格.正截面抗弯承载能力验算 |
| tbl-sc | table | 斜截面抗剪承载能力验算表 | table:验算表格.斜截面抗剪承载能力验算 |
| tbl-dcr | table | 斜截面频遇组合抗裂验算表 | table:验算表格.斜截面频遇组合抗裂验算 |
| tbl-dcp | table | 斜截面主压应力验算表 | table:验算表格.斜截面主压应力验算 |
| tbl-qcr | table | 正截面准永久组合抗裂验算表 | table:验算表格.正截面准永久组合抗裂验算 |
| tbl-cp | table | 正截面压应力验算表 | table:验算表格.正截面压应力验算 |
| tbl-scp | table | 施工阶段压应力验算表 | table:验算表格.施工阶段压应力验算 |
| tbl-stn | table | 施工阶段拉应力验算表 | table:验算表格.施工阶段拉应力验算 |
| tbl-fcr | table | 正截面频遇组合抗裂验算表 | table:验算表格.正截面频遇组合抗裂验算 |

材料三张表必须分别选择 `tbl-c / tbl-ps / tbl-rb`。材料等级取自项目命令，强度参数按固定 JTG 3362 参数表补入。Render 保留模板表头并按语义列名映射，因此“密度/容重”等措辞或单位不同不会阻断填表。

## 模块 check · 验算结果与结论（`verdict:`）

| tag | kind | label | source |
|---|---|---|---|
| chk-flex | conclusion | 正截面抗弯承载能力验算 | verdict:正截面抗弯承载能力验算 |
| chk-shear | conclusion | 斜截面抗剪承载能力验算 | verdict:斜截面抗剪承载能力验算 |
| chk-dcr | conclusion | 斜截面频遇组合抗裂验算 | verdict:斜截面频遇组合抗裂验算 |
| chk-dcp | conclusion | 斜截面主压应力验算 | verdict:斜截面主压应力验算 |
| chk-qcr | conclusion | 正截面准永久组合抗裂验算 | verdict:正截面准永久组合抗裂验算 |
| chk-cp | conclusion | 正截面压应力验算 | verdict:正截面压应力验算 |
| chk-scp | conclusion | 施工阶段压应力验算 | verdict:施工阶段压应力验算 |
| chk-stn | conclusion | 施工阶段拉应力验算 | verdict:施工阶段拉应力验算 |
| chk-fcr | conclusion | 正截面频遇组合抗裂验算 | verdict:正截面频遇组合抗裂验算 |

只允许同一验算的简称或题名差异。承载力章节的"正截面抗压/抗拉"与"正截面压应力/施工阶段拉应力"不是同一业务，禁止互绑；标准结构无精确输出时使用 `tag:null, unresolved:true`。顶底板/腹板斜截面频遇抗裂仍属于同一标准验算，可绑定 `chk-dcr`。

## 模块 image · 图片（`image:*`，本体+图名同一 tag）

| tag | label |
|---|---|
| img-model | 计算模型图 |
| img-tendon | 预应力钢束布置图 |
| img-m-dead | 恒载内力图My |
| img-m-tp1 | 钢束一次内力图My |
| img-m-tp2 | 钢束二次内力图My |
| img-m-ss | 收缩二次内力图My |
| img-m-cs | 徐变二次内力图My |
| img-m-tdn | 整体降温内力图My |
| img-m-tup | 整体升温内力图My |
| img-m-gtdn | 梯度降温内力图My |
| img-m-gtup | 梯度升温内力图My |
| img-m-veh | 汽车荷载内力图My |
| img-m-bc | 基本组合内力图My |
| img-m-cc | 标准组合内力图My |
| img-m-fc | 频遇组合内力图My |
| img-m-qc | 准永久组合内力图My |
| img-uxmin | UxMin下正截面抗弯承载能力包络图 |
| img-uxmax | UxMax下正截面抗弯承载能力包络图 |
| img-uzmin | UzMin下斜截面抗剪承载能力包络图 |
| img-uzmax | UzMax下斜截面抗剪承载能力包络图 |
| img-fcr | 正截面在作用频遇组合下抗裂验算结果 |
| img-qcr | 正截面在作用准永久组合下抗裂验算结果 |
| img-dcr | 斜截面在作用频遇组合下抗裂验算结果 |
| img-cp | 正截面混凝土压应力验算结果 |
| img-dcp | 斜截面混凝土主压应力验算结果 |
| img-scp | 施工阶段混凝土压应力验算结果 |
| img-stn | 施工阶段混凝土拉应力验算结果 |

模板图题必须与上表属于同一验算。承载力章节的正截面抗压/抗拉包络图不得借用压应力图、施工阶段应力图或抗弯包络图；OSIS 确无该验算图时使用 unresolved，报告标准结构缺项。
包络图别名不对称：通用图题（不含 UxMin/UxMax 字样）默认命中 Min 变体 `img-uxmin / img-uzmin`；Max 变体只认精确字样。

## 模块 list · 荷载组合及动态列表

| tag | kind | label | source |
|---|---|---|---|
| lst-lc | format | 荷载组合列表 | load_combinations |
| lst-stage | format | 施工阶段步骤列表 | table:施工阶段 |

`tbl-lc` 只写 `LoadCase` 工况（序号／工况名称／描述）；优先读取根目录 `_logfile.log`，其中没有 `LoadCase` 时回退到 `Error/Command.log`，重复项按"工况名称 + 工况类型码"去重。第三列"描述" = 工况名称的英文码（与 midas 模板一致：徐变二次→`CS`、钢束二次→`TS`、温度梯度→`TPG`；名称无命中时回退 OSIS 类型码），第四字段 `factor` 不进入该三列表。`lst-lc` 才写 `Combine` 荷载组合，展示串 `chinese` 为模板同款 `系数(码)` 格式（如 `1.200(DL)+1.400(M)`），两者不得互相借用。

repeat 机制一次展开全部行；每一行不单独分配 Tag。
`lst-lc` 的可引用列固定为 `id / type / type_name / operation / formula / chinese`；这些是 Project Brief 字段，不是模板表头。

## 模块 seismic · 抗震验算（预置，非抗震项目缺失）

| tag | kind | label | source |
|---|---|---|---|
| seis-cat | text | 桥梁抗震设防类别 | field:profile.基本信息.桥梁抗震设防类别 |
| seis-int | text | 设防烈度 | field:profile.基本信息.设防烈度 |
| seis-site | text | 场地类型 | field:profile.基本信息.场地类型 |
| seis-t | text | 分区特征周期 | field:profile.基本信息.分区特征周期 |
| seis-damp | text | 阻尼比 | field:profile.基本信息.阻尼比 |
| seis-vert | text | 是否考虑竖向地震作用 | field:profile.基本信息.是否考虑竖向地震作用 |
| seis-big | text | 是否为大桥/特大桥 | field:profile.基本信息.是否为大桥或特大桥 |
| seis-soil | text | 基岩或土层 | field:profile.基本信息.基岩或土层 |
| seis-spec | image | 反应谱函数 | image:反应谱函数 |
| seis-pier | conclusion | 桥墩强度验算 | field:check.桥墩强度验算.pass |
| seis-ptbl | table | 桥墩强度验算表 | table:桥墩强度验算 |
| seis-cap | conclusion | 盖梁强度验算 | field:check.盖梁强度验算.pass |
| seis-ctbl | table | 盖梁强度验算表 | table:盖梁强度验算 |

> 预置来源：midas-calcbook 抗震模板草稿。非抗震项目这些项 `present=false` → render 红字（正常缺失）。
> 后续新增抗震/其它模块项 → 在对应模块追加新的内容名（不与既有 tag 重复）。

---

## 标准结构缺项（miss-*）

模板动态内容在本字典中无对应 tag 时：AI 写 `tag:null, unresolved:true` 并**按内容命名** `tag = miss-<拼音/英文slug>`（如 `miss-kangla`、`miss-tension-envelope`）；compile 强制每个未决位置的名字唯一，重复/缺失/非法都会拒绝（Render 红字）。**不得拿另一个 tag 顶替、不得自编新的业务键**。

## 使用（首次接入 → 渲染）

```
AI：模板动态位置 ↔ 字典中哪个 tag（按章节、题名、公式/单位作精确业务判断；仅允许字典明确列出的同义别名）
   → 写 【模块-内容】（compile 按字典生成 mapping.slots[tag] = 固定 source）
Render：【tag】 → mapping.slots[tag].source → 当前项目数据（有填无红）
```
