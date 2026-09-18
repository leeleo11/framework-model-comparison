"""standard_dict：OSIS 标准数据字典（模块化，唯一机器事实源）。

标签命名（v4 起，v7 全量缩短）：`【模块-具体内容】`——模块用英文单词，具体内容用英文缩写/词
（无合适英文时用拼音），**不再使用数字编号**。例：`【image-shear】`（斜截面抗剪
包络图）、`【chk-flex】`（正截面抗弯验算结论）、`【pro-btype】`。

模块（固定）：
- profile   项目画像与项目身份（18 项）
- model     模型基本信息（6 项）
- table     材料/钢束/边界/阶段/参数表（23 项）
- check     验算结果与结论（10 项）
- image     图片（28 项）
- list      荷载组合及动态列表（2 项）
- seismic   抗震验算（13 项，预置）
- miss      标准结构缺项（无编号，由 AI 按内容命名 `miss-<slug>`，compile 强制唯一）

compile 按此字典生成 mapping.slots[<tag>] 的固定 source；AI 只判「模板位置 → tag」，
不得自编 source；不同数据项不得用同一 tag（重复定义报错）；同一数据项多处出现可
多位置引用同一已定义槽位。

v3→v4 变更：去 osis- 前缀与数字编号，全量改为内容化命名（旧 osis-*-N 一律不再合法）。
v6→v7 变更：全量缩短为短前缀+短内容（pro/mdl/tbl/chk/img/lst/seis/miss + 短 slug），
旧 v6 长名标签（如 profile-project-name）一律不再合法，旧模板包须重新首接。
v6→v7 变更：全量缩短为短前缀+短内容（pro/mdl/tbl/chk/img/lst/seis/miss + 短 slug），
旧 v6 长名标签（如 pro-name）一律不再合法，旧模板包须重新首接。
"""
from __future__ import annotations

import hashlib
import json
import re

#: 模块（有序）→ 模块名。
MODULES: dict[str, str] = {
    "pro": "项目画像与项目身份",
    "mdl": "模型基本信息",
    "tbl": "材料/钢束/边界/阶段/参数表",
    "chk": "验算结果与结论",
    "img": "图片",
    "lst": "荷载组合及动态列表",
    "seis": "抗震验算（预置）",
    "miss": "标准结构缺项（无固定条目，由 AI 按内容命名 miss-<slug>）",
}
_MODULE_ORDER = {name: i for i, name in enumerate(MODULES)}

#: <tag> → {kind, label, source, aliases}。kind ∈ text/table/conclusion/image/format(repeat)
BJ: dict[str, dict] = {
    # ================= profile 项目画像与项目身份（field:profile.基本信息.*）=================
    "pro-name": {"kind": "text", "label": "项目名称",
                             "source": "field:profile.基本信息.项目名称",
                             "aliases": ["项目名称", "工程名称", "桥名"]},
    "pro-btype": {"kind": "text", "label": "桥型",
                            "source": "field:profile.基本信息.桥型",
                            "aliases": ["桥型", "桥梁类型", "结构类型"]},
    "pro-sys": {"kind": "text", "label": "结构体系",
                                  "source": "field:profile.基本信息.结构体系",
                                  "aliases": ["结构体系", "结构形式", "体系"]},
    "pro-fnd": {"kind": "text", "label": "基础/支座体系",
                           "source": "field:profile.基本信息.基础或支座体系",
                           "aliases": ["基础", "支座体系", "桥台"]},
    "pro-span": {"kind": "text", "label": "跨径",
                     "source": "field:profile.基本信息.跨径",
                     "aliases": ["跨径", "计算跨径", "全长"]},
    "pro-width": {"kind": "text", "label": "桥面宽度",
                           "source": "field:profile.基本信息.桥面宽度",
                           "aliases": ["桥面宽度", "桥宽", "单片梁宽"]},
    "pro-code": {"kind": "text", "label": "设计标准/规范",
                            "source": "field:profile.基本信息.设计标准",
                            "aliases": ["设计标准", "设计规范", "主要规范"]},
    "pro-safety": {"kind": "text", "label": "安全等级",
                             "source": "field:profile.基本信息.安全等级",
                             "aliases": ["安全等级", "设计安全等级"]},
    "pro-imp": {"kind": "text", "label": "重要性系数",
                                  "source": "field:profile.基本信息.重要性系数",
                                  "aliases": ["重要性系数"]},
    "pro-mat": {"kind": "text", "label": "主梁材料",
                                "source": "field:profile.基本信息.主梁材料",
                                "aliases": ["主梁材料", "材料"]},
    "pro-lane": {"kind": "text", "label": "车道标准/活载等级",
                           "source": "field:profile.基本信息.车道标准",
                           "aliases": ["车道标准", "公路等级", "JTG"]},
    "pro-lanlen": {"kind": "text", "label": "车道长度",
                            "source": "field:profile.基本信息.车道长度",
                            "aliases": ["车道长度"]},
    "pro-temp": {"kind": "text", "label": "温度/升降温",
                            "source": "field:profile.基本信息.温度",
                            "aliases": ["温度", "升降温", "整体升降温"]},
    "pro-tup": {"kind": "text", "label": "整体升温",
                                  "source": "field:profile.基本信息.整体升温",
                                  "aliases": ["整体升温", "升温"]},
    "pro-tdn": {"kind": "text", "label": "整体降温",
                                  "source": "field:profile.基本信息.整体降温",
                                  "aliases": ["整体降温", "降温"]},
    "pro-creep": {"kind": "text", "label": "收缩徐变参数",
                                "source": "field:profile.基本信息.收缩徐变",
                                "aliases": ["收缩徐变", "收缩龄期", "出生时间"]},
    "pro-sw": {"kind": "text", "label": "自重",
                            "source": "field:profile.基本信息.自重",
                            "aliases": ["自重", "容重"]},
    "pro-soft": {"kind": "text", "label": "设计程序",
                               "source": "field:profile.基本信息.设计程序",
                               "aliases": ["设计程序", "设计软件", "计算程序"]},

    # ================= model 模型基本信息（field:model.*）=================
    "mdl-node": {"kind": "text", "label": "节点数量",
                         "source": "field:model.node_count",
                         "aliases": ["节点数", "节点数量"]},
    "mdl-elem": {"kind": "text", "label": "单元数量",
                            "source": "field:model.element_count",
                            "aliases": ["单元数", "单元数量"]},
    "mdl-bc": {"kind": "text", "label": "边界数量",
                             "source": "field:model.boundary_count",
                             "aliases": ["边界条件数量", "边界数"]},
    "mdl-stage": {"kind": "text", "label": "施工阶段数量",
                          "source": "field:model.construction_stages",
                          "aliases": ["施工阶段数量", "阶段数"]},
    "mdl-ver": {"kind": "text", "label": "软件版本",
                      "source": "field:model.version",
                      "aliases": ["软件版本", "版本号"]},
    "mdl-g": {"kind": "text", "label": "自重系数",
                                  "source": "field:model.gravity_coefficient",
                                  "aliases": ["自重系数"]},

    # ================= table 材料/钢束/边界/阶段/参数表（table:*）=================
    "tbl-mat": {"kind": "table", "label": "材料参数表",
                       "source": "table:材料参数",
                       "aliases": ["材料主要指标", "混凝土材料", "材料表"]},
    "tbl-bc": {"kind": "table", "label": "边界条件表",
                       "source": "table:边界条件",
                       "aliases": ["边界约束", "边界"]},
    "tbl-stage": {"kind": "table", "label": "施工阶段表",
                    "source": "table:施工阶段",
                    "aliases": ["施工步骤", "阶段表"]},
    "tbl-tdp": {"kind": "table", "label": "钢束属性表",
                     "source": "table:钢束属性",
                     "aliases": ["预应力钢筋特性", "钢束属性"]},
    "tbl-tdc": {"kind": "table", "label": "钢束坐标表",
                           "source": "table:钢束坐标",
                           "aliases": ["钢束线型坐标", "钢束坐标表"]},
    "tbl-gt": {"kind": "table", "label": "梯度温度表",
                            "source": "table:梯度温度",
                            "aliases": ["梯度温度"]},
    "tbl-cr": {"kind": "table", "label": "收缩徐变表",
                    "source": "table:收缩徐变",
                    "aliases": ["收缩徐变表"]},
    "tbl-rct": {"kind": "table", "label": "支座反力表",
                       "source": "table:支座反力",
                       "aliases": ["支反力", "支座反力表"]},
    "tbl-c": {"kind": "table", "label": "混凝土材料参数表",
                                "source": "table:材料参数.混凝土",
                                "aliases": ["混凝土材料", "混凝土材料主要指标"]},
    "tbl-ps": {"kind": "table", "label": "预应力钢材参数表",
                              "source": "table:材料参数.预应力钢材",
                              "aliases": ["预应力钢筋材料", "预应力钢材", "钢绞线材料"]},
    "tbl-rb": {"kind": "table", "label": "普通钢筋参数表",
                             "source": "table:材料参数.普通钢筋",
                             "aliases": ["普通钢筋材料", "钢筋材料"]},
    "tbl-lc": {"kind": "table", "label": "荷载工况表",
                       "source": "table:荷载工况",
                       "aliases": ["荷载工况", "工况表"]},
    "tbl-def": {"kind": "table", "label": "挠度验算表",
                         "source": "table:验算表格.挠度验算",
                         "aliases": ["挠度验算", "变形验算"]},
    "tbl-mc": {"kind": "table", "label": "正截面抗弯承载能力验算表",
                              "source": "table:验算表格.正截面抗弯承载能力验算",
                              "aliases": ["抗弯验算表", "正截面抗弯表"]},
    "tbl-sc": {"kind": "table", "label": "斜截面抗剪承载能力验算表",
                             "source": "table:验算表格.斜截面抗剪承载能力验算",
                             "aliases": ["抗剪验算表", "斜截面抗剪表"]},
    "tbl-dcr": {"kind": "table", "label": "斜截面频遇组合抗裂验算表",
                             "source": "table:验算表格.斜截面频遇组合抗裂验算",
                             "aliases": ["斜截面抗裂表", "顶底板腹板抗裂表"]},
    "tbl-dcp": {"kind": "table", "label": "斜截面主压应力验算表",
                                   "source": "table:验算表格.斜截面主压应力验算",
                                   "aliases": ["主压应力验算表", "斜截面抗压表"]},
    "tbl-qcr": {"kind": "table", "label": "正截面准永久组合抗裂验算表",
                          "source": "table:验算表格.正截面准永久组合抗裂验算",
                          "aliases": ["准永久抗裂表"]},
    "tbl-cp": {"kind": "table", "label": "正截面压应力验算表",
                          "source": "table:验算表格.正截面压应力验算",
                          "aliases": ["正截面抗压表", "压应力验算表"]},
    "tbl-scp": {"kind": "table", "label": "施工阶段压应力验算表",
                                "source": "table:验算表格.施工阶段压应力验算",
                                "aliases": ["施工阶段抗压表", "施工阶段压应力表"]},
    "tbl-stn": {"kind": "table", "label": "施工阶段拉应力验算表",
                            "source": "table:验算表格.施工阶段拉应力验算",
                            "aliases": ["施工阶段抗拉表", "施工阶段拉应力表"]},
    "tbl-fcr": {"kind": "table", "label": "正截面频遇组合抗裂验算表",
                             "source": "table:验算表格.正截面频遇组合抗裂验算",
                             "aliases": ["频遇抗裂表", "正截面抗裂表"]},
    # OSIS 5.00 新增：正截面抗拉压验算表
    "tbl-tc": {"kind": "table", "label": "正截面抗拉压验算表",
                                  "source": "table:验算表格.正截面抗拉压验算",
                                  "aliases": ["抗拉压验算表"]},

    # ================= check 验算结果与结论（verdict:*）=================
    "chk-flex": {"kind": "conclusion", "label": "正截面抗弯承载能力验算",
                       "source": "verdict:正截面抗弯承载能力验算",
                       "basis": "依据《桥规》第5.1.2-1条",
                       "aliases": ["抗弯", "抗弯承载"]},
    "chk-shear": {"kind": "conclusion", "label": "斜截面抗剪承载能力验算",
                    "source": "verdict:斜截面抗剪承载能力验算",
                    "basis": "依据《桥规》第5.1.2-1条",
                    "aliases": ["抗剪", "抗剪承载"]},
    "chk-dcr": {"kind": "conclusion", "label": "斜截面频遇组合抗裂验算",
                             "source": "verdict:斜截面频遇组合抗裂验算",
                             "basis": "依据《桥规》第6.3.1条",
                             "aliases": ["斜截面抗裂", "顶底板", "腹板"]},
    "chk-dcp": {"kind": "conclusion", "label": "斜截面主压应力验算",
                                   "source": "verdict:斜截面主压应力验算",
                                   "basis": "依据《桥规》第7.1.6条",
                                   "aliases": ["主压应力", "斜截面抗压"]},
    "chk-qcr": {"kind": "conclusion", "label": "正截面准永久组合抗裂验算",
                          "source": "verdict:正截面准永久组合抗裂验算",
                          "basis": "依据《桥规》第6.3.1条",
                          "aliases": ["准永久抗裂"]},
    "chk-cp": {"kind": "conclusion", "label": "正截面压应力验算",
                          "source": "verdict:正截面压应力验算",
                          "basis": "依据《桥规》第7.1.5-1条",
                          "aliases": ["正截面压应力", "混凝土压应力", "压应力"]},
    "chk-scp": {"kind": "conclusion", "label": "施工阶段压应力验算",
                                "source": "verdict:施工阶段压应力验算",
                                "basis": "依据《桥规》第7.2.8条",
                                "aliases": ["施工阶段压应力", "施工阶段法向应力",
                                            "法向压应力", "法向应力"]},
    "chk-stn": {"kind": "conclusion", "label": "施工阶段拉应力验算",
                            "source": "verdict:施工阶段拉应力验算",
                            "basis": "依据《桥规》第7.2.8条",
                            "aliases": ["施工阶段拉应力", "法向拉应力"]},
    "chk-fcr": {"kind": "conclusion", "label": "正截面频遇组合抗裂验算",
                             "source": "verdict:正截面频遇组合抗裂验算",
                             "basis": "依据《桥规》第6.3.1条",
                             "aliases": ["抗裂", "频遇抗裂"]},
    # OSIS 5.00 新增：正截面抗拉压验算（合并了旧模板"抗压/抗拉"两项承载力验算）
    "chk-tc": {"kind": "conclusion", "label": "正截面抗拉压验算",
                                  "source": "verdict:正截面抗拉压验算",
                                  "basis": "依据《桥规》第5.1.2-1条",
                                  "aliases": ["抗拉压", "正截面抗压", "正截面抗拉"]},

    # ================= image 图片（image:*，本体+图名同一 tag）=================
    # 包络图别名不对称：Min 变体保留通用业务别名（模板图题通常不写 UxMin/UzMax
    # 字样，默认命中 Min 控制方向）；Max 变体只认 UxMax/UzMax 精确字样。
    "img-model": {"kind": "image", "label": "计算模型图",
                    "source": "image:计算模型图",
                    "aliases": ["模型", "计算模型"]},
    "img-tendon": {"kind": "image", "label": "预应力钢束布置图",
                            "source": "image:预应力钢束布置图",
                            "aliases": ["钢束布置"]},
    "img-m-dead": {"kind": "image", "label": "恒载内力图My",
                          "source": "image:恒载内力图My",
                          "aliases": ["恒载内力"]},
    "img-m-tp1": {"kind": "image", "label": "钢束一次内力图My",
                              "source": "image:钢束一次内力图My",
                              "aliases": ["钢束一次"]},
    "img-m-tp2": {"kind": "image", "label": "钢束二次内力图My",
                              "source": "image:钢束二次内力图My",
                              "aliases": ["钢束二次"]},
    "img-m-ss": {"kind": "image", "label": "收缩二次内力图My",
                               "source": "image:收缩二次内力图My",
                               "aliases": ["收缩二次"]},
    "img-m-cs": {"kind": "image", "label": "徐变二次内力图My",
                           "source": "image:徐变二次内力图My",
                           "aliases": ["徐变二次"]},
    "img-m-tdn": {"kind": "image", "label": "整体降温内力图My",
                          "source": "image:整体降温内力图My",
                          "aliases": ["整体降温"]},
    "img-m-tup": {"kind": "image", "label": "整体升温内力图My",
                          "source": "image:整体升温内力图My",
                          "aliases": ["整体升温"]},
    "img-m-gtdn": {"kind": "image", "label": "梯度降温内力图My",
                                   "source": "image:梯度降温内力图My",
                                   "aliases": ["梯度降温"]},
    "img-m-gtup": {"kind": "image", "label": "梯度升温内力图My",
                                   "source": "image:梯度升温内力图My",
                                   "aliases": ["梯度升温"]},
    "img-m-veh": {"kind": "image", "label": "汽车荷载内力图My",
                             "source": "image:汽车荷载内力图My",
                             "aliases": ["汽车荷载"]},
    "img-m-bc": {"kind": "image", "label": "基本组合内力图My",
                                 "source": "image:基本组合内力图My",
                                 "aliases": ["基本组合"]},
    "img-m-cc": {"kind": "image", "label": "标准组合内力图My",
                                    "source": "image:标准组合内力图My",
                                    "aliases": ["标准组合"]},
    "img-m-fc": {"kind": "image", "label": "频遇组合内力图My",
                              "source": "image:频遇组合内力图My",
                              "aliases": ["频遇组合"]},
    "img-m-qc": {"kind": "image", "label": "准永久组合内力图My",
                           "source": "image:准永久组合内力图My",
                           "aliases": ["准永久组合"]},
    "img-uxmin": {"kind": "image", "label": "UxMin下正截面抗弯承载能力包络图",
                             "source": "image:UxMin下正截面抗弯承载能力包络图",
                             "aliases": ["正截面抗弯", "抗弯包络",
                                         "UxMin", "UxMin下正截面抗弯", "UxMin抗弯包络"]},
    "img-uxmax": {"kind": "image", "label": "UxMax下正截面抗弯承载能力包络图",
                             "source": "image:UxMax下正截面抗弯承载能力包络图",
                             "aliases": ["UxMax", "UxMax下正截面抗弯", "UxMax抗弯包络"]},
    "img-uzmin": {"kind": "image", "label": "UzMin下斜截面抗剪承载能力包络图",
                          "source": "image:UzMin下斜截面抗剪承载能力包络图",
                          "aliases": ["斜截面抗剪", "抗剪包络",
                                      "UzMin", "UzMin下斜截面抗剪", "UzMin抗剪包络"]},
    "img-uzmax": {"kind": "image", "label": "UzMax下斜截面抗剪承载能力包络图",
                          "source": "image:UzMax下斜截面抗剪承载能力包络图",
                          "aliases": ["UzMax", "UzMax下斜截面抗剪", "UzMax抗剪包络"]},
    "img-fcr": {"kind": "image", "label": "正截面在作用频遇组合下抗裂验算结果",
                             "source": "image:正截面在作用频遇组合下抗裂验算结果",
                             "aliases": ["正截面抗裂", "频遇抗裂结果"]},
    "img-qcr": {"kind": "image", "label": "正截面在作用准永久组合下抗裂验算结果",
                          "source": "image:正截面在作用准永久组合下抗裂验算结果",
                          "aliases": ["准永久抗裂结果"]},
    "img-dcr": {"kind": "image", "label": "斜截面在作用频遇组合下抗裂验算结果",
                             "source": "image:斜截面在作用频遇组合下抗裂验算结果",
                             "aliases": ["斜截面抗裂", "顶底板斜截面抗裂", "腹板斜截面抗裂"]},
    "img-cp": {"kind": "image", "label": "正截面混凝土压应力验算结果",
                          "source": "image:正截面混凝土压应力验算结果",
                          "aliases": ["正截面压应力", "混凝土压应力"]},
    "img-dcp": {"kind": "image", "label": "斜截面混凝土主压应力验算结果",
                                   "source": "image:斜截面混凝土主压应力验算结果",
                                   "aliases": ["斜截面主压应力", "混凝土主压应力"]},
    "img-scp": {"kind": "image", "label": "施工阶段混凝土压应力验算结果",
                                "source": "image:施工阶段混凝土压应力验算结果",
                                "aliases": ["施工阶段压应力", "施工阶段法向应力", "法向应力"]},
    "img-stn": {"kind": "image", "label": "施工阶段混凝土拉应力验算结果",
                            "source": "image:施工阶段混凝土拉应力验算结果",
                            "aliases": ["施工阶段拉应力结果"]},
    # OSIS 5.00 新增：正截面抗拉压验算包络图
    "img-tc": {"kind": "image", "label": "正截面抗拉压验算包络图",
                                  "source": "image:正截面抗拉压验算",
                                  "aliases": ["正截面抗压", "抗压包络",
                                              "正截面抗拉", "抗拉包络"]},

    # ================= list 荷载组合及动态列表（repeat）=================
    "lst-lc": {"kind": "format", "label": "荷载组合列表",
                      "source": "load_combinations",
                      "aliases": ["荷载组合", "组合列表"],
                      "repeat_source": "load_combinations"},
    "lst-stage": {"kind": "format", "label": "施工阶段步骤列表",
                   "source": "table:施工阶段",
                   "aliases": ["施工步骤", "施工阶段列表"],
                   "repeat_source": "table:施工阶段"},

    # ================= seismic 抗震验算（预置）=================
    "seis-cat": {"kind": "text", "label": "桥梁抗震设防类别",
                         "source": "field:profile.基本信息.桥梁抗震设防类别",
                         "aliases": ["抗震设防类别", "设防类别"]},
    "seis-int": {"kind": "text", "label": "设防烈度",
                          "source": "field:profile.基本信息.设防烈度",
                          "aliases": ["设防烈度", "抗震烈度"]},
    "seis-site": {"kind": "text", "label": "场地类型",
                           "source": "field:profile.基本信息.场地类型",
                           "aliases": ["场地类型"]},
    "seis-t": {"kind": "text", "label": "分区特征周期",
                       "source": "field:profile.基本信息.分区特征周期",
                       "aliases": ["特征周期", "分区特征周期"]},
    "seis-damp": {"kind": "text", "label": "阻尼比",
                        "source": "field:profile.基本信息.阻尼比",
                        "aliases": ["阻尼比"]},
    "seis-vert": {"kind": "text", "label": "是否考虑竖向地震作用",
                         "source": "field:profile.基本信息.是否考虑竖向地震作用",
                         "aliases": ["竖向地震", "竖向地震作用"]},
    "seis-big": {"kind": "text", "label": "是否为大桥/特大桥",
                             "source": "field:profile.基本信息.是否为大桥或特大桥",
                             "aliases": ["大桥", "特大桥"]},
    "seis-soil": {"kind": "text", "label": "基岩或土层",
                          "source": "field:profile.基本信息.基岩或土层",
                          "aliases": ["基岩", "土层"]},
    "seis-spec": {"kind": "image", "label": "反应谱函数",
                         "source": "image:反应谱函数",
                         "aliases": ["反应谱", "反应谱函数"]},
    "seis-pier": {"kind": "conclusion", "label": "桥墩强度验算",
                           "source": "field:check.桥墩强度验算.pass",
                           "aliases": ["桥墩强度", "墩强度"]},
    "seis-ptbl": {"kind": "table", "label": "桥墩强度验算表",
                           "source": "table:桥墩强度验算",
                           "aliases": ["桥墩强度验算表"]},
    "seis-cap": {"kind": "conclusion", "label": "盖梁强度验算",
                              "source": "field:check.盖梁强度验算.pass",
                              "aliases": ["盖梁强度", "盖梁验算"]},
    "seis-ctbl": {"kind": "table", "label": "盖梁强度验算表",
                              "source": "table:盖梁强度验算",
                              "aliases": ["盖梁强度验算表"]},
}

#: 有序 tag 列表：按模块顺序、模块内保持声明顺序。
BJ_ORDER: list[str] = [tag for m in MODULES for tag in BJ if tag.startswith(m + "-")]

#: 标签命名规则：`模块-内容`，内容为 kebab 小写词（英文/拼音），无数字编号要求。
_TAG_MODULES = "|".join(MODULES)
_TAG_BODY = rf"(?:{_TAG_MODULES})-[a-z0-9]+(?:-[a-z0-9]+)*"
TAG_NAME_RE = re.compile(rf"^{_TAG_BODY}$")
#: 文本中出现的完整标签：`【模块-内容】`（无捕获组，findall 返回完整匹配）。
TAG_RE = re.compile("【" + _TAG_BODY + "】")
#: miss（标准结构缺项）：由 AI 按内容命名 `miss-<slug>`，compile 强制唯一。
MISS_NAME_RE = re.compile(r"^miss-[a-z0-9]+(?:-[a-z0-9]+)*$")

#: 标准结构版本（追加/新增模块时 +1；不重排已有）。
#: v6：追加整体升/降温并固定结论依据；v5：追加 OSIS 5.00 正截面抗拉压验算三件套；
#: v4：全量内容化命名；v3：img 别名不对称。
STANDARD_STRUCTURE_VERSION = 7


def sha256() -> str:
    """标准字典 sha：按固定顺序序列化 BJ（append 变更时更新，用于 mapping 版本绑定）。"""
    payload = json.dumps(
        {k: BJ[k] for k in BJ_ORDER}, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
