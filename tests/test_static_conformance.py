from pathlib import Path

from common.static_conformance import evaluate_static_conformance


def test_static_conformance_reports_unavailable_parent_without_claiming_success(tmp_path: Path):
    candidate = tmp_path / "candidate"
    (candidate / "py" / "prep").mkdir(parents=True)
    (candidate / "py" / "prep" / "main.py").write_text("# candidate\n", encoding="utf-8")

    result = evaluate_static_conformance(
        candidate,
        bridge_type="cantilever_box",
        parent_repo=tmp_path / "missing-parent",
    )

    assert result["status"] == "evaluator_unavailable"
    assert result["available"] is False
    assert result["candidate_score"] is None
