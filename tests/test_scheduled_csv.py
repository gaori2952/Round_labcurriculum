import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class ScheduledCSVTests(unittest.TestCase):
    def test_csv_paths_large_preset_and_budget_feedback(self):
        import scheduled_csv

        with TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "project.csv"
            materials = root / "materials.csv"
            out = root / "result"
            with projects.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["title", "package", "name", "do", "out", "hours", "core", "goal"])
                writer.writerow(["사례", "W1", "구현", "기능 구현", "산출물", 52, "핵심", "기능"])
            with materials.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["id", "title", "url", "topic", "summary", "covers"])
                writer.writerow(["A", "핵심", "https://example.com/a", "1주차 · W1 · 1순위 · 배정 1시간", "", ""])
                writer.writerow(["B", "긴 참고", "https://example.com/b", "1주차 · W1 · 2순위 · 배정 1시간 10분", "", ""])
                writer.writerow(["C", "짧은 참고", "https://example.com/c", "1주차 · W1 · 2순위 · 배정 40분", "", ""])
            history = []

            def choose(records, *args):
                history.append(records)
                return ({"selected":[{"id":"B","reason":"실습"}], "excluded":[{"id":"C","reason":"보류"}],
                         "selected_minutes":70}
                        if len(history)==1 else
                        {"selected":[{"id":"C","reason":"예산 적합"}], "excluded":[{"id":"B","reason":"시간 초과"}],
                         "selected_minutes":40})

            def guides(weeks, *args):
                for week in weeks:
                    week["guide"] = {"goal":"목표","method":"방법","reason":"이유","origin":"llm"}

            with patch.object(scheduled_csv,"invoke",side_effect=choose), \
                    patch.object(scheduled_csv,"weekly_guides",side_effect=guides), \
                    patch.object(scheduled_csv,"export_markdown") as export:
                code = scheduled_csv.main([
                    "--project-csv",str(projects),"--materials-csv",str(materials),"--size","대형",
                    "--weeks","1","--project-hour-divisor","2","--bedrock-model","mock","--output-dir",str(out)])
            self.assertIn(code, (0,3))
            self.assertEqual(history[0]["optional_learning_budget_minutes"],60)
            self.assertEqual(history[1]["budget_validation"]["over_budget_minutes"],10)
            self.assertNotIn("feasible_bundles",history[1])
            self.assertEqual(export.call_count,1)
            result=json.loads((out/"curriculum.json").read_bytes())
            self.assertEqual(result["weeks"][0]["capacity_minutes"],1680)
            self.assertEqual(result["weeks"][0]["used_minutes"],1660)
            self.assertEqual(result["summary"]["scheduled_material_ids"],["A","C"])


    def test_explicit_parallel_work_remains_in_each_week(self):
        import scheduled_csv

        tasks=[{"id":"W1","title":"순차","do":"순차 구현","out":"기능","minutes":120,"repeat_weeks":0},
               {"id":"W10","title":"협업","do":"3주 동안 협업","out":"기록","minutes":120,"repeat_weeks":3}]
        weeks=scheduled_csv.place(tasks, [], 80, 3)
        recurring=[[a for a in week["activities"] if a.get("recurring")] for week in weeks]
        self.assertEqual([week["used_minutes"] for week in weeks],[80,80,80])
        self.assertEqual([parts[0]["minutes"] for parts in recurring],[40,40,40])
        self.assertEqual([parts[0]["completed_in_this_chunk"] for parts in recurring],[False,False,True])
