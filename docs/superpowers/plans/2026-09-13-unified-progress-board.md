# Unified Progress Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Provide one read-only auto-refreshing HTML dashboard that aggregates every campaign run by form, bridge, index, and T1–T6 architecture.

**Architecture:** Extend the existing \`scripts/progress_board.py\` scanner rather than changing experiment runners. The scanner will accept one or more run roots, normalize every run directory into a task key, compute submitted/completed/running/queued/blocked counts, and render a compact summary plus per-task matrix. The existing single-campaign mode remains compatible.

**Tech Stack:** Python standard library, generated self-refreshing HTML/CSS, pytest.

## Global Constraints

- The dashboard is read-only and must never modify experiment artifacts.
- It must not require API keys, OSIS services, or additional dependencies.
- It must work with portable repository paths and Windows PowerShell.
- Existing \`--label\` behavior and output path remain compatible.

---

### Task 1: Define normalized task aggregation

**Files:**
- Modify: \`scripts/progress_board.py\`
- Create: \`tests/test_progress_board.py\`

**Interfaces:**
- \`scan_roots(run_roots: list[Path], expected: dict | None = None) -> dict\`
- A normalized row contains \`campaign\`, \`bridge\`, \`form\`, \`index\`, \`architecture\`, \`state\`, \`quality\`, \`complete\`, \`reasons\`, \`last_write\`, and \`run_dir\`.

- [x] **Step 1: Write failing tests**

Test that full index 0 and gen index 0 for the same bridge/architecture remain separate rows, and that optional expected-task counts expose queued work.

- [x] **Step 2: Run the focused tests and verify they fail**

Run: \`.\.venvs\main\Scripts\python.exe -m pytest tests/test_progress_board.py -q\`

Expected: FAIL because the aggregation interface does not yet exist.

- [x] **Step 3: Implement directory parsing and aggregation**

Parse names matching \`<bridge>__<form>__<index>__<architecture>__seed<seed>\`, retain the full key instead of collapsing by bridge/architecture, and classify states using existing evaluation/backend/generation files. Add optional expected-task counts so queued work is visible without inventing run artifacts.

- [x] **Step 4: Run focused tests**

Run: \`.\.venvs\main\Scripts\python.exe -m pytest tests/test_progress_board.py -q\`

Expected: PASS.

### Task 2: Render one unified dashboard

**Files:**
- Modify: \`scripts/progress_board.py\`
- Modify: \`tests/test_progress_board.py\`

**Interfaces:**
- \`render(data: dict, title: str) -> str\` remains the HTML entry point.
- CLI adds repeatable \`--runs-root\` and optional \`--expected-json\`; \`--label\` remains a shorthand for one root.

- [x] **Step 1: Add rendering assertions**

Assert generated HTML contains the aggregate labels (总任务, 已完成, 运行中, 排队, 失败) and distinct task identifiers for different forms/indices.

- [x] **Step 2: Implement summary and task table**

Render a top summary bar, progress percentage, and a table with columns 任务, 桥型, 形式, 索引, T1–T6, where each cell shows queued/running/done/blocked and quality score. Preserve the current dark theme and 8-second refresh behavior.

- [x] **Step 3: Run focused tests**

Run: \`.\.venvs\main\Scripts\python.exe -m pytest tests/test_progress_board.py -q\`

Expected: PASS.

### Task 3: Add unified CLI mode and verify against the live campaign

**Files:**
- Modify: \`scripts/progress_board.py\`
- Modify: \`tests/test_progress_board.py\`
- Modify: \`README.md\`

**Interfaces:**
- Example command uses repeatable \`--runs-root\` arguments for the active campaign roots and writes \`tmp/progress-unified.html\`.

- [x] **Step 1: Add CLI test for multiple roots**

Use two temporary run roots and assert the one-shot output includes rows from both roots.

- [x] **Step 2: Implement multi-root output naming**

When \`--runs-root\` is supplied, write \`tmp/progress-unified.html\`; otherwise retain \`tmp/progress-<label>.html\`.

- [x] **Step 3: Update README usage**

Document the single-window command and explain that it is read-only and safe to run while campaigns are active.

- [x] **Step 4: Run the complete verification suite**

Run: \`.\.venvs\main\Scripts\python.exe -m pytest -q\`

Expected: all existing tests plus progress-board tests pass.

- [x] **Step 5: Start the unified live board**

Run the multi-root command for the active campaigns and open \`tmp/progress-unified.html\`; do not restart or alter any experiment process.
