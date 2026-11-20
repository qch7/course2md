"""对已保存的课程工作区执行固定内容检查，不调用 ASR 或模型。

python scripts/evaluate_course.py <note_workspace> --expectations <fixture.json>
fixture 须包含 expected 字段；报告输出 JSON，未通过时退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from course2md.refine.conversation import load_context  # noqa: E402
from course2md.refine.lesson import _parse_lesson, _render, collect_evidence  # noqa: E402
from course2md.refine.llm import LlmError  # noqa: E402


def evaluate(workspace: Path, expected: dict) -> dict:
    context, record, segments, figures = load_context(workspace)
    evidence = collect_evidence(segments)
    lesson = _parse_lesson(json.dumps(record), segments, figures, evidence)
    pair = _render(context["title"], lesson, figures, evidence)
    checks = {
        **{f"detail_contains:{term}": term in pair.detailed for term in expected["terms"]},
        **{f"brief_contains:{term}": term in pair.brief for term in expected.get("brief_terms", [])},
        "example_count": len(lesson.examples) >= expected["min_examples"],
        "example_steps": all(len(item.steps) >= expected.get("min_steps", 1) for item in lesson.examples),
        "figure_count": len(pair.figures) >= expected["min_figures"],
        "figure_files": all(frame.path.is_file() for frame in pair.figures.values()),
        "teacher_evidence": set(expected.get("teacher_evidence", []))
        <= {point.evidence_id for point in lesson.teacher_points},
        **{f"uncertainty:{term}": term in pair.detailed for term in expected.get("uncertainties", [])},
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "metrics": {
            "examples": len(lesson.examples),
            "figures": len(pair.figures),
            "teacher_points": len(lesson.teacher_points),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    args = parser.parse_args()
    try:
        expected = json.loads(args.expectations.read_text(encoding="utf-8"))["expected"]
        report = evaluate(args.workspace, expected)
    except (OSError, ValueError, KeyError, LlmError) as exc:
        report = {"passed": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return int(not report["passed"])


if __name__ == "__main__":
    sys.exit(main())
