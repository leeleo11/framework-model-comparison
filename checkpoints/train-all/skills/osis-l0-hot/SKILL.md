---
name: osis-l0-hot
description: L0 热操作工具箱。把常见单标量局部修改(阶段 duration、沉降 setl、自重 GRAVITY 可批量、节点 NFORCE、湿度、预应力 PST、车道长度、LINE 线荷载 Fz)收成可调用 CLI 函数:写回 OSIS + 内置校验 + 可选同步 prep/.py。局部修改命中表中操作时优先用本 SKILL,禁止手写 python -c assert、禁止查 pyosis 读回字段。
---

# osis-l0-hot

> **局部标量改动的最快路径**:命中下表 → 填参数调 CLI → 看 `OK:` / `FAIL:`。  
> **AI 禁止**再写 `python -c "... assert ..."`,**禁止**猜 `lc.get` / `.fy` / `p[2]`——校验已在脚本内。

## 何时用

同时满足:

1. 只改**一个标量**(或单节点力 / 单束应力)
2. 不改组名、节点列表、拓扑、跨模块引用
3. 下表有对应 `op`

否则退出本 SKILL,回 `osis-engine` 普通 L0/L1。

## 工作流(仅 2 步)

```
1. 对照 §操作表 选 op,从 prep/_N 或用户指令抄齐参数
2. 跑一条 CLI(带 --prep 同步 .py)。成功打印 OK:,失败非 0 退出
```

**不要**:读 pyosis 源码、dir(LoadCase)、拆写回/验收、重跑 main.py。

## 调用方式

脚本路径(相对本 SKILL 根目录):

```text
scripts/l0_hot.py
```

用 `python`(已装 pyosis)。先取项目目录:

```bash
python -c "from pyosis import OSISEngine; print(OSISEngine().project.get_directory())"
```

`<prep>` = `<project_dir>/py/prep/_N_xxx.py`。

列出能力:

```bash
python <skill_dir>/scripts/l0_hot.py list
```

## 操作表(命中即调用)

| 用户意图 | op | 必填参数 | --prep 指向 |
|---|---|---|---|
| 改某施工阶段工期 | `stage` | `--no` `--name` `--duration` | `_10_stage.py` |
| 改沉降组沉降量 | `settlement` | `--name` `--setl` `--nodes ...` | `_9_analysis.py` |
| 改自重 z 系数 | `gravity` | `--z` + (`--case` 可多个 \| `--all`) | `_8_loadcase.py` |
| 改单节点竖向力 | `nforce` | `--case` `--node` `--fz` | `_8_loadcase.py` |
| 改年平均湿度 | `humidity` | `--no` `--humidity`(其余有默认) | `_3_material.py` |
| 改单束张拉应力 | `pst` | `--case` `--shape` `--stress` | `_8_loadcase.py` |
| 改车道长度 | `lane` | `--name` `--length`(其余默认同现网) | `_9_analysis.py` |
| 改 LINE 线荷载 Fz | `line` | `--case` `--fz`(单元默认同现网 LINE) | `_8_loadcase.py` |

### 例句(直接可跑)

```bash
# 端横梁节点 2 竖向力 → -21000,并改 _8
python <skill_dir>/scripts/l0_hot.py nforce --case 端横梁荷载工况 --node 2 --fz -21000 --prep "<project>/py/prep/_8_loadcase.py"

# CS4 体系转换 10→15
python <skill_dir>/scripts/l0_hot.py stage --no 4 --name CS4_体系转换 --duration 15 --prep "<project>/py/prep/_10_stage.py"

# 预制单元自重 -1.0→-1.05(单工况)
python <skill_dir>/scripts/l0_hot.py gravity --case 预制单元自重 --z -1.05 --prep "<project>/py/prep/_8_loadcase.py"

# 全部自重工况 z→-1.02(批量,--all 取现网含 GRAVITY 的工况)
python <skill_dir>/scripts/l0_hot.py gravity --all --z -1.02 --prep "<project>/py/prep/_8_loadcase.py"

# 沉降组1 -0.05→-0.06
python <skill_dir>/scripts/l0_hot.py settlement --name 沉降组1 --setl -0.06 --nodes 2 --prep "<project>/py/prep/_9_analysis.py"

# 湿度 75→70(其它参数用默认/与模板一致时显式传)
python <skill_dir>/scripts/l0_hot.py humidity --no 1 --name 收缩徐变 --humidity 70 --birth 7 --type-coeff 5 --shrink-birth 3 --prep "<project>/py/prep/_3_material.py"

# 顶板束 T1-1 应力
python <skill_dir>/scripts/l0_hot.py pst --case 预应力顶板束 --shape T1-1 --stress 1400000000 --prep "<project>/py/prep/_8_loadcase.py"

# 车道长度 15→16(其它参数沿用现网 VE/偏心/组)
python <skill_dir>/scripts/l0_hot.py lane --name 车道 --length 16.0 --prep "<project>/py/prep/_9_analysis.py"

# 铺装工况线荷载 Fz -7250→-7500(I/J 同值;delete+重建)
python <skill_dir>/scripts/l0_hot.py line --case 铺装工况 --fz -7500 --prep "<project>/py/prep/_8_loadcase.py"
```

## 参数从哪抄

| op | 抄自 |
|---|---|
| 全部 | 用户指令里的目标值 + `prep/_N` 里**同形态**那一行的名字/编号/其它未改参数 |
| `humidity` | `_3` 的 `creep_shrink.create(...)` 整行,只换湿度 |
| `stage` | `_10` 的 `stage.create(no, name, duration)`,`name` 必须与现网一致 |
| `lane` | `_9` 的 `live.lane.create(...)`,只换长度;未传的 wheel/ori/组等从现网读回 |
| `line` | `_8` 的 `load.get(...).create("LINE", ...)`,只换 Fz;单元列表默认同现网该工况全部 LINE |
| `gravity` | `_8` 自重工况名;`--all` 时不必手抄名单,CLI 从现网枚举含 GRAVITY 的工况 |

`--prep` **推荐始终带上**,保证脚本与内存一致。若用户只要改内存、不改文件,可省略 `--prep`。

## 成功/失败

| 输出 | 含义 | AI 动作 |
|---|---|---|
| `OK: ...` + exit 0 | 写回(+可选 edit)成功,内置读回已通过 | 简短汇报完成,可顺带改 `项目画像.md` 对应一句 |
| `FAIL: ...` + exit ≠0 | 写回或读回或文件替换失败 | **退出热路径**,按 `osis-engine` 失败修复;不要反复猜 assert |

## 明确不做

- 不替代完整建模 / 改梁高 / 多截面联动(改跨中梁高走 `osis-edit-hmid`)
- 不替代 `osis-check` / 构造评测
- 不发明新 op:表外需求走普通 L0 或提需求加函数

## 与其它 SKILL 关系

| 层级 | 角色 |
|---|---|
| `osis-engine` | 判局部修改 → **先查本表是否命中** → 命中则只加载本 SKILL |
| `osis-module-*` | 未命中本表、或要写新建模代码时再用 |
| 本 SKILL | 热操作执行器;读回知识固化在 `scripts/l0_hot.py` |
