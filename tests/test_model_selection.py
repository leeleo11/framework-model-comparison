"""Model and reasoning settings must survive every runner boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from baselines.t2_langgraph.adapter import OpenAICompatibleChatModel, T2Config
from baselines.t6_osisai import adapter as t6_adapter
from langchain_core.messages import HumanMessage
from scripts.run_all_parallel import build_command
from scripts.run_dataset import build_parser as dataset_parser


def test_dataset_parser_preserves_legacy_default_and_accepts_levels():
    base = ["--architecture", "T1", "--bridge", "osis-bridge-cantilever-box", "--form", "full"]
    assert dataset_parser().parse_args(base).reasoning_effort is None
    assert dataset_parser().parse_args(base + ["--reasoning-effort", "high"]).reasoning_effort == "high"


def test_batch_command_forwards_model_and_effort(tmp_path: Path):
    args = argparse.Namespace(
        bridge="osis-bridge-cantilever-box", form="full", index=0, seed=0,
        runs_dir=tmp_path, parent_repo=None, model="model-x", base_url="http://local/v1",
        temperature=0.2, reasoning_effort="low",
    )
    command = build_command(args, "T4")
    assert command[command.index("--model") + 1] == "model-x"
    assert command[command.index("--base-url") + 1] == "http://local/v1"
    assert command[command.index("--temperature") + 1] == "0.2"
    assert command[command.index("--reasoning-effort") + 1] == "low"


def test_t2_sends_effort_only_when_selected(monkeypatch):
    sent = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    def post(*args, **kwargs):
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("baselines.t2_langgraph.adapter.requests.post", post)
    for effort in (None, "high"):
        model = OpenAICompatibleChatModel(
            model="model-x", api_key="test", base_url="http://local/v1",
            reasoning_effort=effort,
        )
        model._generate([HumanMessage(content="test")])
    assert "reasoning_effort" not in sent[0]
    assert sent[1]["reasoning_effort"] == "high"
    assert T2Config.from_env(api_key="test", reasoning_effort="low").reasoning_effort == "low"


def test_t6_config_exposes_selected_variant_without_changing_default(tmp_path: Path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    packaged = tmp_path / "opencode" / ".agents"
    packaged.mkdir(parents=True)
    (packaged / "AGENTS.md").write_text("instructions", encoding="utf-8")
    monkeypatch.setattr(t6_adapter, "OPENCODE_DIR", packaged.parent)
    t6_adapter._prepare_isolated_env(
        tmp_path / "default", skills, "http://local/v1", model="model-x"
    )
    t6_adapter._prepare_isolated_env(
        tmp_path / "high", skills, "http://local/v1", model="model-x",
        reasoning_effort="high",
    )
    default = json.loads((tmp_path / "default" / ".agents" / "opencode.json").read_text())
    high = json.loads((tmp_path / "high" / ".agents" / "opencode.json").read_text())
    assert "variants" not in default["provider"]["comparison"]["models"]["model-x"]
    assert high["provider"]["comparison"]["models"]["model-x"]["variants"]["high"]["reasoning_effort"] == "high"
