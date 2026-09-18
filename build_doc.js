const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, BorderStyle, WidthType, ShadingType,
  LevelFormat, PageOrientation, ExternalHyperlink, PageNumber, Header, Footer,
  PageBreak
} = require("docx");

const outDir = path.resolve(__dirname);
const outFile = path.join(outDir, "对比实验框架设计文档.docx");
const font = "Microsoft YaHei";
const contentWidth = 9638;
const border = { style: BorderStyle.SINGLE, size: 1, color: "B7C9D6" };
const borders = { top: border, bottom: border, left: border, right: border };

function runs(text, opts = {}) {
  return [new TextRun({
    text,
    font,
    size: opts.size || 21,
    bold: opts.bold || false,
    color: opts.color,
    italics: opts.italics || false
  })];
}

function para(text, opts = {}) {
  return new Paragraph({
    alignment: opts.alignment,
    spacing: { before: opts.before || 0, after: opts.after === undefined ? 100 : opts.after, line: 320 },
    children: runs(text, opts)
  });
}

function heading(text, level = 1) {
  return new Paragraph({
    heading: level === 1 ? HeadingLevel.HEADING_1 : HeadingLevel.HEADING_2,
    spacing: { before: level === 1 ? 300 : 220, after: 140 },
    children: runs(text, { size: level === 1 ? 30 : 25, bold: true, color: level === 1 ? "1F4E79" : "2F5597" })
  });
}

function bullet(text) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 70, line: 300 },
    children: runs(text)
  });
}

function numbered(text) {
  return new Paragraph({
    numbering: { reference: "numbers", level: 0 },
    spacing: { after: 70, line: 300 },
    children: runs(text)
  });
}

function codeBlock(text) {
  return new Paragraph({
    shading: { fill: "F3F6F8", type: ShadingType.CLEAR },
    spacing: { before: 70, after: 120, line: 260 },
    indent: { left: 220, right: 220 },
    children: [new TextRun({ text, font: "Consolas", size: 18, color: "263238" })]
  });
}

function linkParagraph(label, url) {
  return new Paragraph({
    spacing: { after: 70 },
    children: [
      new ExternalHyperlink({
        children: [new TextRun({ text: label, font, size: 20, color: "0563C1", underline: {} })],
        link: url
      })
    ]
  });
}

function cell(text, width, header = false) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    borders,
    margins: { top: 90, bottom: 90, left: 120, right: 120 },
    shading: { fill: header ? "DCE6F1" : "FFFFFF", type: ShadingType.CLEAR },
    children: [new Paragraph({
      spacing: { after: 0, line: 260 },
      children: [new TextRun({ text: String(text), font, size: header ? 18 : 17, bold: header, color: header ? "1F1F1F" : "222222" })]
    })]
  });
}

function table(headers, rows, widths) {
  const allRows = [
    new TableRow({ children: headers.map((h, i) => cell(h, widths[i], true)) }),
    ...rows.map(row => new TableRow({ children: row.map((v, i) => cell(v, widths[i])) }))
  ];
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    columnWidths: widths,
    rows: allRows,
    borders
  });
}

const children = [];

children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { before: 1900, after: 260 },
  children: [new TextRun({ text: "OSIS-AI 与主流智能体框架", font, size: 38, bold: true, color: "1F4E79" })]
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 160 },
  children: [new TextRun({ text: "建模流程对比实验框架设计文档", font, size: 32, bold: true, color: "1F4E79" })]
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 100 },
  children: [new TextRun({ text: "文档版本：v0.1（设计稿）", font, size: 22, color: "666666" })]
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 100 },
  children: [new TextRun({ text: "日期：2026-09-02　|　适用任务线：桥梁数值建模（第一阶段）", font, size: 21, color: "666666" })]
}));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { before: 420, after: 0 },
  children: [new TextRun({ text: "OSIS Skill Enhance 项目", font, size: 23, italics: true, color: "7F8C8D" })]
}));
children.push(new Paragraph({ children: [new PageBreak()] }));

children.push(heading("1. 文档目的与范围"));
children.push(para("本设计用于比较 OSIS-AI（T6）与表格中五类代表性系统（T1–T5）在桥梁建模任务上的端到端能力。第一阶段只评测建模任务线：自然语言需求 → PYOSIS/Python 建模代码 → 软件中的真实模型 → 求解与自动验证 → 可追溯产物。"));
children.push(para("附件《工作一实验计划总纲》只作为实验规格参考。其核心约束包括统一模型、工具、案例和资源预算，覆盖整桥建模、单模块生成、模块修改三类任务，并以软件执行后的真实模型构造正确性为最高优先级；附件没有指定必须使用某个外部框架，也不替代本设计中的版本锁定和执行协议。"));
children.push(para("本设计的目标不是证明任何框架在所有场景下绝对优越，而是在可复现条件下回答："));
children.push(numbered("不同智能体编排方式对桥梁模型构造成功率和质量的影响是什么？"));
children.push(numbered("优势主要出现在需求解析、模块编排、求解验证、错误修复还是增量重跑阶段？"));
children.push(numbered("OSIS 的技能分层、状态化模型、验证器和增量重跑是否带来可测量收益？"));

children.push(heading("2. 参照系统与版本锁定"));
children.push(table(
  ["系统", "推荐实现与版本", "技能/文档挂载", "建模流程定位"],
  [
    ["T1 直接执行", "Liang 2504.09754；参考 FEABench", "运行前生成固定技能索引和文档包；不允许交互式读取", "一次生成脚本并执行"],
    ["T2 ReAct", "LangGraph 1.2.11 + LangChain 1.3.18", "system_prompt 注入索引；工具读取技能和参考资料", "思考 → 工具 → 观察 → 行动循环"],
    ["T3 CodeAct", "smolagents v1.26.0", "instructions、prompt template 和自定义工具", "直接生成 Python 动作代码"],
    ["T4 完整 CodeAct", "OpenHands SDK v1.44.1", "原生支持 SKILL.md、load_skills_from_dir()、AgentContext(skills=...) 和 .agents/skills/", "Agent、Conversation、Workspace 和工具协同"],
    ["T5 角色多智能体", "CrewAI 1.15.18", "主轨采用统一技能工具；副轨使用 knowledge_sources、Agent、Task、Flow", "规划者 → 建模者 → 验证者 → 报告者"],
    ["T6 OSIS-AI", "正式实验前建立干净 OSIS 快照", "挂载由 --skills-dir 指定的训练快照", "OSIS 原生路由、状态、模板和增量重跑"]
  ],
  [1400, 2600, 3500, 2138]
));
children.push(para("T4 是最适合作为“直接挂载现有技能树”主基线的外部框架。T2、T3、T5 通过统一适配器也可以挂载同一套技能和文档。T1 不视为完整框架，而作为一次性生成基线。"));

children.push(heading("3. 总体架构"));
children.push(para("实验系统由四层组成："));
children.push(codeBlock("任务输入层\n    ↓\n框架适配层（T1–T6）\n    ↓\n统一 SkillAdapter + OSISAdapter\n    ↓\n真实模型执行、验证与评估"));
children.push(para("建议在独立目录中实现："));
children.push(codeBlock("<comparison-repo>\\\n├─ common\\\n│  ├─ skill_adapter\\\n│  ├─ osis_adapter\\\n│  ├─ evaluator\\\n│  └─ task_schema\\\n├─ baselines\\\n│  ├─ t1_direct\\\n│  ├─ t2_langgraph\\\n│  ├─ t3_smolagents\\\n│  ├─ t4_openhands\\\n│  └─ t5_crewai\\\n├─ osis_t6\\\n├─ tasks\\\n│  ├─ dev\\\n│  └─ hidden\\\n├─ runs\\\n└─ manifests\\"));

children.push(heading("3.1 SkillAdapter", 2));
children.push(para("所有系统统一提供以下只读能力："));
children.push(codeBlock("list_skills()\nread_skill(skill_id)\nread_reference(skill_id, relative_path)\nsearch_cases(query)\nrun_skill_script(skill_id, script_id, args)\nskill_bundle_hash()"));
children.push(para("适配规则："));
[
  ".agents/skills、参考文档和模板源文件只读。",
  "智能体只能在独立 candidate workspace 写入脚本和模型。",
  "run_skill_script 只能执行预先登记的脚本。",
  "默认只提供技能索引和摘要，需要时再读取完整 SKILL.md。",
  "每次运行记录技能包 SHA-256，避免不同系统读取不同版本。"
].forEach(bullet);

children.push(heading("3.2 OSISAdapter", 2));
children.push(codeBlock("create_model()\napply_model_patch()\nrun_model_script()\nsolve_model()\ninspect_model_state()\nvalidate_model()\nsnapshot_artifacts()\ndiff_model_state()"));
children.push(para("T1–T5 在主轨中通过同一 OSISAdapter 执行真实模型；T6 额外保留 OSIS 原生路由、状态管理和增量重跑机制。所有适配器最终输出相同语义的模型快照和验证结果。"));

children.push(heading("4. 两条实验轨道"));
children.push(heading("4.1 architecture-only 主轨", 2));
children.push(para("主轨只允许编排机制不同，以下内容必须一致："));
[
  "模型快照和模型参数。",
  "技能与案例内容（字节级一致）。",
  "工具语义、权限和可见文件范围。",
  "Python、PYOSIS、求解器和容器。",
  "timeout、token、工具调用和重试预算。",
  "自动评估器和隐藏测试集。"
].forEach(bullet);
children.push(para("该轨道用于分析路由、状态、验证、修复和增量重跑等架构机制。"));
children.push(heading("4.2 native-best-practice 副轨", 2));
children.push(para("允许各框架使用自身最佳实践，例如 OpenHands 原生技能加载、CrewAI 原生知识源、LangGraph middleware 和 smolagents prompt template。该轨道反映实际系统效果，但结果只能解释为“系统级效果”，不能单独作为纯架构因果结论。"));

children.push(heading("5. 统一任务和 I/O 协议"));
children.push(heading("5.1 任务形态", 2));
children.push(para("每种桥型包含三类任务："));
children.push(numbered("整桥生成：从自然语言需求生成完整模型。"));
children.push(numbered("单模块生成：只生成指定建模模块。"));
children.push(numbered("模块修改：基于已有模型快照修改一个或多个字段。"));
children.push(para("难度分为 L1、L2、L3。首轮试验覆盖 L1/L2，完整实验再加入 L3。"));
children.push(heading("5.2 输入", 2));
children.push(codeBlock("input.json\n  ├─ natural_language_requirement\n  ├─ bridge_type\n  ├─ task_form\n  ├─ difficulty\n  └─ initial_project_snapshot   # 仅模块修改任务使用"));
children.push(heading("5.3 输出", 2));
children.push(codeBlock("artifacts/\n├─ plan.json\n├─ model_script.py\n├─ model_state.json\n├─ solver.log\n├─ validation.json\n└─ trace.json"));
children.push(para("model_state.json 必须从软件实际构造后的模型导出，不能仅根据生成代码或文本相似度评分。"));

children.push(heading("6. 统一建模流程和检查点"));
children.push(table(
  ["检查点", "统一含义", "OSIS 内部阶段映射"],
  [
    ["P0", "需求解析、桥型识别和任务边界", "_1 控制"],
    ["P1", "建模假设、坐标、单位和材料假设", "_2 几何/属性、_3 材料"],
    ["P2", "截面、节点、单元、边界、荷载和施工阶段", "_4–_8"],
    ["P3", "生成并执行 PYOSIS/模型脚本", "_9 分析准备"],
    ["P4", "编译、求解、收敛和基础数值检查", "_9"],
    ["P5", "自动验证、诊断和有限轮次修复", "osis-auto-testconformance"],
    ["P6", "结果整理、模型快照和可追溯报告", "_10 施工阶段/产物"]
  ],
  [1300, 5500, 2838]
));
children.push(para("每个框架都要记录 P0–P6 的阶段状态、输入输出、耗时和失败原因。T1 可以只有一次完整调用，但仍需把其产物映射到这些检查点。"));

children.push(heading("7. 首轮建模试验矩阵"));
children.push(para("建议先进行适配器一致性测试，再运行小规模正式试验："));
children.push(table(
  ["因素", "首轮设置"],
  [
    ["桥型", "6 种：cantilever box、precast small box、precast T girder、rigid frame、conventional box、hollow slab"],
    ["任务形态", "整桥生成、单模块生成、模块修改"],
    ["难度", "L1/L2，18 个任务中均衡覆盖"],
    ["重复", "2 个随机种子"],
    ["系统", "T1–T6"],
    ["总运行数", "18 个任务 × 2 seeds × 6 系统 = 216 次"]
  ],
  [2200, 7438]
));
children.push(para("现有约 95 个模板目录可作为开发集和案例池。隐藏测试集应使用参数扰动、邻近桥型组合、跨模块耦合、预应力/多工况和施工阶段变化，避免模型只记忆模板。"));
children.push(para("适配器通过后扩展完整矩阵："));
children.push(codeBlock("6 桥型 × 3 任务形态 × 3 难度 = 54 个任务单元"));

children.push(heading("8. 评价指标"));
children.push(heading("8.1 主要终点：完整成功率", 2));
children.push(para("一次运行只有在以下条件全部满足时才计为完整成功："));
[
  "脚本编译或执行通过。",
  "真实模型关键字段正确。",
  "求解器达到预设收敛条件。",
  "单位、边界和荷载约束正确。",
  "自动验证通过。",
  "输出产物完整且可追溯。"
].forEach(numbered);
children.push(heading("8.2 连续质量分数", 2));
children.push(para("建议预注册以下 0–100 分权重："));
children.push(table(
  ["维度", "权重"],
  [
    ["真实模型构造正确性", "40%"],
    ["执行与收敛", "20%"],
    ["结构字段完整性", "15%"],
    ["单位/边界/荷载正确性", "10%"],
    ["验证与数值合理性", "10%"],
    ["过程可追溯性", "5%"]
  ],
  [7000, 2638]
));
children.push(heading("8.3 过程指标", 2));
[
  "P0–P6 阶段通过率。",
  "首次成功率和最终成功率。",
  "修复轮数（诊断指标，不单独替代质量指标）。",
  "总耗时、输入/输出 token、工具调用次数和重试次数。",
  "模块修改的目标变更准确率。",
  "非目标模块误改比例。",
  "增量重跑范围和节省时间。",
  "重复运行一致性。",
  "文本相似度、代码结构 diff（次要指标）。"
].forEach(bullet);

children.push(heading("9. 公平性和可复现性"));
children.push(para("每次运行必须自动生成 manifest，至少包含："));
children.push(codeBlock("task_id\narchitecture_id\nframework_version\ncommit\nmodel_snapshot\nskill_bundle_sha256\ntool_schema_version\nsolver_version\ncontainer_digest\nseed\ntimeout_s\ntoken_budget\ntool_call_budget\nartifact_hashes\nfailure_code\nhuman_intervention"));
children.push(para("所有 LLM 调用、并行调用、工具调用、重试和子智能体都计入预算；禁止人工修改脚本、补参数或选择性重跑。正式实验采用公开开发集和隐藏测试集分离，实验前冻结权重、容差、超时、随机种子和最小重要差异 Δ。"));
children.push(para("统计分析建议以任务为独立单位、seed 嵌套在任务内：二元成功率使用 logistic GLMM，连续分数和耗时使用 LMM 或 Gamma 混合模型，报告架构效应、95% CI、任务聚类 bootstrap 和多重比较校正。只有当 OSIS 相对基线的 95% CI 下界超过预注册的 Δ，才表述为“在本实验条件下具有明确优势”。"));

children.push(heading("10. 消融和迁移实验"));
[
  "A1：去除分层 SKILL。",
  "A2：将技能扁平化。",
  "A3：关闭案例检索。",
  "A4：固定单一模型，关闭路由。",
  "A5：取消 PYOSIS 封装，直接使用原生命令。",
  "关闭共享状态。",
  "关闭自动验证/自动修复。",
  "关闭增量重跑。",
  "将 OSIS 技能注入 T3/T4，或用 T4 编排替换 OSIS 拓扑。"
].forEach(bullet);
children.push(para("其中迁移实验用于区分“架构贡献”和“技能、工具、模板或求解器优势”。简单一步任务作为负对照，用于检验 OSIS 的优势是否主要出现在复杂建模流程。"));

children.push(heading("11. 实施顺序与当前状态"));
[
  "从当前未提交的 OSIS 工作树建立干净快照。",
  "实现 SkillAdapter、OSISAdapter 和统一 evaluator。",
  "接入 T1–T5 包装器，完成 T6 原生入口。",
  "用 3 个简单建模任务做适配器 conformance 测试。",
  "运行首轮 18 任务、216 次试验。",
  "检查阶段日志、模型快照、失败码和评分器。",
  "冻结 manifest 后扩展完整 54 单元矩阵。"
].forEach(numbered);
children.push(para("本设计稿只改造 comparison repository，不修改 OSIS 父仓库中的现有文件。正式实验前必须记录 OSIS commit、容器 digest、求解器版本、模型快照、技能包哈希和工具白名单。"));

children.push(heading("12. 参考链接"));
[
  ["Liang et al., arXiv:2504.09754", "https://arxiv.org/abs/2504.09754"],
  ["Google FEABench", "https://github.com/google/feabench"],
  ["LangGraph", "https://github.com/langchain-ai/langgraph"],
  ["LangChain create_agent API", "https://reference.langchain.com/python/langchain/agents/factory/create_agent"],
  ["smolagents Agents API", "https://huggingface.co/docs/smolagents/main/reference/agents"],
  ["smolagents Building good agents", "https://huggingface.co/docs/smolagents/tutorials/building_good_agents"],
  ["OpenHands Agent Skills & Context", "https://docs.openhands.dev/sdk/guides/skill"],
  ["OpenHands skills overview", "https://docs.openhands.dev/overview/skills"],
  ["CrewAI documentation", "https://docs.crewai.com/"]
].forEach(([label, url]) => children.push(linkParagraph(label, url)));

const doc = new Document({
  styles: {
    default: { document: { run: { font, size: 21 }, paragraph: { spacing: { line: 320 } } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font, size: 30, bold: true, color: "1F4E79" },
        paragraph: { spacing: { before: 300, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font, size: 25, bold: true, color: "2F5597" },
        paragraph: { spacing: { before: 220, after: 120 }, outlineLevel: 1 } }
    ]
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 520, hanging: 260 } } } }] },
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 520, hanging: 260 } } } }] }
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838 },
        margin: { top: 1080, right: 1134, bottom: 1080, left: 1134 }
      }
    },
    headers: {
      default: new Header({ children: [new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [new TextRun({ text: "OSIS-AI 建模流程对比实验框架", font, size: 16, color: "7F8C8D" })]
      })] })
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "第 ", font, size: 16, color: "7F8C8D" }), new TextRun({ children: [PageNumber.CURRENT], font, size: 16, color: "7F8C8D" }), new TextRun({ text: " 页", font, size: 16, color: "7F8C8D" })]
      })] })
    },
    children
  }]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync(outFile, buffer);
  console.log(outFile);
}).catch(err => {
  console.error(err);
  process.exit(1);
});
