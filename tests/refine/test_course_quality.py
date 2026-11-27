"""固定样本评估应发现内容退化、证据遗漏和缺失的图片。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def evaluate(sample):
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts" / "evaluate_course.py"),
            str(sample.workspace),
            "--expectations",
            str(ROOT / "tests" / "fixtures" / "review-course.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_fixed_course_keeps_formula_examples_evidence_and_figures(sample_course):
    code, report = evaluate(sample_course)
    assert code == 0 and report["passed"]
    assert report["metrics"] == {"examples": 1, "figures": 1, "teacher_points": 2}


@pytest.mark.parametrize("regression", ["formula", "steps", "evidence", "uncertainty", "image", "reference"])
def test_fixed_course_evaluation_rejects_regressions(sample_course, regression):
    record = sample_course.data["lesson"]
    if regression == "formula":
        record["examples"][0]["steps"][1] = "泛泛介绍改写。"
    elif regression == "steps":
        record["examples"][0]["steps"] = record["examples"][0]["steps"][:1]
    elif regression == "evidence":
        record["teacher_points"] = []
    elif regression == "uncertainty":
        record["examples"][0]["uncertainty"] = ""
        record["uncertainties"] = []
    elif regression == "image":
        sample_course.image.unlink()
    else:
        record["topics"][0]["figure_ids"] = ["unknown"]
    (sample_course.workspace / "lesson.json").write_text(json.dumps(record), encoding="utf-8")
    code, report = evaluate(sample_course)
    assert code == 1 and not report["passed"]
