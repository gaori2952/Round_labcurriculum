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
                writer.writerow(["title", "package", "name", "do", "out", "hours", "core", "goal", "competencies", "sources"])
                writer.writerow(["사례", "W1", "구현", "기능 구현", "산출물", 52, "핵심", "기능", "C01 기능 설계", "기능 설계 사례 → C01"])
            with materials.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["id", "title", "url", "topic", "summary", "covers"])
                writer.writerow(["A", "핵심", "https://example.com/a", "1주차 · W1 · 1순위 · 배정 1시간", "설계 문서 작성에 참고", "C01 기능 설계: 일부"])
                writer.writerow(["B", "긴 참고", "https://example.com/b", "1주차 · W1 · 2순위 · 배정 1시간 10분", "", ""])
                writer.writerow(["C", "짧은 참고", "https://example.com/c", "1주차 · W1 · 2순위 · 배정 40분", "", ""])
            history = []

            def choose(records, *args):
                history.append(records)
                if len(history) == 1:
                    return {"decisions": {"B": {"decision": "selected", "reason": "긴 자료"},
                                          "C": {"decision": "excluded", "reason": "짧은 자료"}}}
                bundle = next(k for k, v in records["feasible_bundles"].items() if v == ["C"])
                return {"bundle_id": bundle, "reasons": {"B": "길어서 보류", "C": "예산 적합"}}


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
            self.assertEqual(list(history[1]["feasible_bundles"].values()), [[], ["C"]])
            self.assertEqual(export.call_count,1)
            result=json.loads((out/"curriculum.json").read_bytes())
            self.assertEqual(result["weeks"][0]["capacity_minutes"],1680)
            self.assertEqual(result["weeks"][0]["used_minutes"],1660)
            self.assertEqual(result["summary"]["scheduled_material_ids"],["A","C"])
            self.assertEqual(result["material_selection"]["selected_minutes"],40)
            self.assertEqual(result["material_selection"]["excluded"][0]["id"],"B")
            activities = result["weeks"][0]["activities"]
            self.assertEqual(activities[0]["covers"], "C01 기능 설계: 일부")
            self.assertEqual(activities[-1]["competencies"], "C01 기능 설계")
            document = (out/"curriculum.md").read_text()
            self.assertIn("## 프로젝트 작업별 목표 역량", document)
            self.assertIn("C01 기능 설계: 일부", document)
            self.assertIn("작업 근거 · 기능 설계 사례 → C01", document)
            self.assertIn("배정 근거 · 설계 문서 작성에 참고", document)



    def test_explicit_parallel_work_remains_in_each_week(self):
        import scheduled_csv

        tasks=[{"id":"W1","title":"순차","do":"순차 구현","out":"기능","minutes":120,"repeat_weeks":0},
               {"id":"W10","title":"협업","do":"3주 동안 협업","out":"기록","minutes":120,"repeat_weeks":3}]
        weeks=scheduled_csv.place(tasks, [], 80, 3)
        recurring=[[a for a in week["activities"] if a.get("recurring")] for week in weeks]
        self.assertEqual([week["used_minutes"] for week in weeks],[80,80,80])
        self.assertEqual([parts[0]["minutes"] for parts in recurring],[40,40,40])
        self.assertEqual([parts[0]["completed_in_this_chunk"] for parts in recurring],[False,False,True])

    def test_budget_shortage_still_writes_bedrock_markdown(self):
        import scheduled_csv

        with TemporaryDirectory() as directory:
            root = Path(directory)
            project, materials, output = root/"project.csv", root/"materials.csv", root/"result"
            project.write_text("title,package,name,do,out,hours,core,goal\n사례,W1,작업,구현,산출물,16,핵심,기능\n")
            materials.write_text("id,title,url,topic\nA,학습,https://example.org,1주차 · W1 · 1순위 · 배정 1시간\n")
            responses = []

            def converse(request, *_):
                payload = json.loads(request["messages"][0]["content"][0]["text"])
                document = payload["template"].replace("{안내}", "핵심 범위와 시간 상한을 검토합니다.") + "\n"
                responses.append(document.encode())
                return json.dumps({"stopReason":"end_turn",
                                   "output":{"message":{"content":[{"text":document}]}}}).encode()

            with patch("lesson_analysis.converse", side_effect=converse) as call:
                code = scheduled_csv.main([
                    "--project-csv",str(project),"--materials-csv",str(materials),
                    "--size","소형","--weeks","1","--project-hour-divisor","2",
                    "--bedrock-model","mock","--output-dir",str(output)])
            self.assertEqual(code,3)
            self.assertEqual(call.call_count,1)
            self.assertEqual((output/"curriculum_bedrock.md").read_bytes(),responses[0])
            result=json.loads((output/"curriculum.json").read_bytes())
            self.assertEqual(result["weeks"],[])
            self.assertEqual(result["budget_shortfall"]["additional_minutes"],120)
            self.assertIn("2시간",(output/"curriculum_bedrock.md").read_text())


    def test_material_id_errors_are_retried_with_the_original_response(self):
        from collections import Counter
        import scheduled_csv

        options = [{"id":mid,"week":1,"minutes":minutes} for mid,minutes in
                   (("M04",10),("M07",30),("M15",20),("M18",20),("M24",30))]
        old = {"selected":[{"id":mid,"reason":"기존"} for mid in ("M04","M07","M24")],
               "excluded":[{"id":mid,"reason":"기존"} for mid in ("M07","M18")], "selected_minutes":60}
        first = {"decisions":{"M04":{"decision":"selected","reason":"합계 점검"},
                              "UNKNOWN":{"decision":"excluded","reason":"오류"}}}
        reasons = {m["id"]: "예산과 관련성 검토" for m in options}
        for initial, corrected in ((old, True), (first, True), (first, False)):
            def reply(records, *args):
                if "feasible_bundles" not in records:
                    return initial
                bundle = next(key for key, ids in records["feasible_bundles"].items()
                              if ids == ["M04", "M15", "M24"])
                return {"bundle_id": bundle if corrected else "UNKNOWN", "reasons": reasons}

            with self.subTest(initial=initial, corrected=corrected), TemporaryDirectory() as directory, \
                    patch.object(scheduled_csv,"invoke",side_effect=reply) as model:
                out = Path(directory)
                decision, validation = scheduled_csv.choose_optional(
                    options, Counter(), 360, 420, 1, out, "mock", None, None)
                self.assertEqual(model.call_count,2)
                retry = model.call_args_list[1].args[0]
                self.assertEqual(retry["previous_response"],initial)
                self.assertIn("M15",retry["budget_validation"]["missing_ids"])
                self.assertEqual(json.loads((out/"bedrock_material_selection_attempt_1.json").read_bytes()),initial)
                self.assertEqual(validation["status"],"accepted" if corrected else "blocked")
                if corrected:
                    self.assertEqual(decision["selected_minutes"],60)
                    self.assertEqual([row["id"] for row in decision["selected"]],["M04","M15","M24"])
                else:
                    self.assertIn("UNKNOWN",retry["budget_validation"]["unexpected_ids"])
                    self.assertTrue(validation["invalid_response_shape"])
