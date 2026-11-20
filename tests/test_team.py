import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curriculum_generator import generate, main
from team_csv import prepare_team, read_rows

DATA = Path(__file__).resolve().parents[1] / "data/aws_fde"


class TeamCurriculumTest(unittest.TestCase):
    def test_exports_twelve_populated_weeks_and_discloses_gaps(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main(["--hours-per-week", "7", "--output-dir", folder]), 3)
            result = json.loads((Path(folder) / "curriculum.json").read_text())
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(result["weeks"]), 12)
        self.assertTrue(all(0 < week["used_minutes"] <= 420 for week in result["weeks"]))
        self.assertTrue(all(a["minutes"] % 10 == 0 for w in result["weeks"] for a in w["activities"]))
        self.assertEqual(len(result["team_export"]["input_files"]), 2)
        self.assertTrue(all(unit["section"] for unit in result["learning_matches"]))
        self.assertTrue(any("The Flow Class" in unit["section"] for unit in result["learning_matches"]))
        self.assertTrue(any("원문 절 미확인" in unit["section"] for unit in result["learning_matches"]))
        self.assertFalse(result["checks"]["educational_coverage_complete"])
        self.assertIn("C14", result["competency_coverage"]["missing_materials"])

    def test_short_csv_row_reports_line_and_field(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_file = Path(folder) / "materials.csv"
            csv_file.write_text("id,covers\nM01\n")
            with self.assertRaisesRegex(ValueError, r"materials\.csv.*2.*covers"):
                read_rows(folder, csv_file.name, {"id", "covers"})

    def test_bedrock_proposal_changes_team_schedule(self):
        project, materials, _ = prepare_team(DATA)

        def proposal(tasks, units, *args):
            return {
                "tasks": [{"id": t.id, "quote": t.out, "requires": ["C52"] if t.id == "P01" else [],
                           "depends_on": ["P05"] if t.id == "P04" else [],
                           "completion_check": "테스트로 확인"} for t in tasks],
                "units": [{"id": u.id, "quote": u.title, "teaches": list(u.competencies),
                           "requires": [], "study_method": "비교 실습"} for u in units],
            }

        with patch("lesson_analysis.propose", side_effect=proposal), patch("lesson_analysis.weekly_guides"):
            result = generate(project, materials, weeks=12, hours_per_week=7, model="test-model")
        tasks = result["summary"]["included_packages"]
        self.assertLess(tasks.index("P05"), tasks.index("P04"))
        self.assertEqual(result["task_preparation"][0]["required"], ["C52"])
        self.assertEqual(result["task_preparation"][0]["study_before_task"], ["LRN6a568bbf93"])
        self.assertEqual(result["weeks"][0]["activities"][0]["study_method"], "비교 실습")

    def test_unconfirmed_llm_guidance_stays_in_generated_draft(self):
        project, materials, _ = prepare_team(DATA)
        from curriculum_generator import format_md

        def proposal(tasks, units, *args):
            return {
                "tasks": [{"id": t.id, "quote": "", "requires": list(t.requires),
                           "depends_on": [], "completion_check": t.completion_check} for t in tasks],
                "units": [{"id": u.id, "quote": "입력에서 확인되지 않는 문장", "teaches": list(u.competencies),
                           "requires": [], "study_method": "예제를 실행하고 결과를 비교합니다.",
                           "suggested_scope": "두 리전 구성의 차이를 살펴보기"} for u in units],
            }

        with patch("lesson_analysis.propose", side_effect=proposal), patch("lesson_analysis.weekly_guides"):
            result = generate(project, materials, weeks=12, hours_per_week=7, model="test-model")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(result["weeks"]), 12)
        unit = result["analysis_provenance"]["units"]["E03732779fa"]
        self.assertFalse(unit["input_quote_matched"])
        self.assertEqual(unit["suggested_scope"], "두 리전 구성의 차이를 살펴보기")
        self.assertNotIn("suggested_scope", result["analysis_provenance"]["units"]["LRN624bcb0ae0"])
        self.assertIn("LLM 생성 제안 · 원문 미확인", format_md(result))
        self.assertIn("두 리전 구성의 차이를 살펴보기", format_md(result))
        self.assertNotIn("입력에서 확인되지 않는 문장", format_md(result))
