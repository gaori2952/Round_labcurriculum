import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from curriculum_generator import generate, main


class BedrockMarkdownTests(unittest.TestCase):
    def test_cli_exports_validated_markdown_bytes_and_raw_response(self):
        title = "테스트 프로젝트"
        project = {"title": title, "checks": {"passed": True}, "packages": [{
            "id": "W1", "do": "구현", "out": "결과", "core": True, "hours": 1,
            "requires": ["C01"], "competencies": ["C01"], "depends_on": [],
            "sources": ["입력 근거"], "completion_check": "동작 확인",
        }]}
        materials = [{"id": "M1", "role": "core", "title": "참고 학습", "minutes": 20,
                      "teaches": ["C01"], "evidence": "학습 근거", "url": "https://example.org/material"}]
        result = generate(project, materials, weeks=1, hours_per_week=14)
        for invalid in (False, True):
            with self.subTest(invalid=invalid), TemporaryDirectory() as folder:
                destination = Path(folder)

                def fake_converse(request, profile, region):
                    self.assertTrue((destination / "curriculum.json").is_file())
                    self.assertTrue((destination / "curriculum.md").is_file())
                    prompt = json.loads(request["messages"][0]["content"][0]["text"])
                    markdown = prompt["template"].replace("{안내}", "Bedrock 원문입니다.") + "\n"
                    if invalid:
                        markdown = markdown.replace("20분", "90분")
                    response = json.dumps({
                        "stopReason": "end_turn", "usage": {"outputTokens": 40},
                        "output": {"message": {"content": [{"text": markdown}]}},
                    }, ensure_ascii=False).encode()
                    expected.append((markdown.encode(), response))
                    return response

                expected = []
                with patch("curriculum_generator.generate", return_value=result), \
                        patch("lesson_analysis.converse", side_effect=fake_converse, create=True):
                    code = main(["--size", "중형", "--bedrock-model", "test-model",
                                 "--output-dir", folder])
                self.assertEqual(len(expected), 1)
                share = destination / "curriculum_bedrock.md"
                self.assertEqual((destination / "bedrock_markdown_response.json").read_bytes(), expected[0][1])
                if invalid:
                    self.assertEqual(code, 4)
                    self.assertFalse(share.exists())
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(share.read_bytes(), expected[0][0])

    def test_unfilled_guidance_is_retried_and_replaced(self):
        from lesson_analysis import write_markdown_response

        with TemporaryDirectory() as directory:
            out = Path(directory)
            template = "# 계획\n\n**목표** · {안내}"
            complete = template.replace("{안내}", "완성된 안내") + "\n"

            def response(text):
                return json.dumps({"stopReason": "end_turn",
                                   "output": {"message": {"content": [{"text": text}]}}}).encode()

            first = response(template)
            second = response(complete)
            with patch("lesson_analysis.converse", side_effect=[first, second]) as model:
                write_markdown_response({"template": template}, template.splitlines(), "문서를 작성한다",
                                        200, out, "mock", None, None)
            self.assertEqual(model.call_count, 2)
            self.assertEqual((out / "bedrock_markdown_response_attempt_1.json").read_bytes(), first)
            self.assertEqual((out / "bedrock_markdown_response.json").read_bytes(), second)
            self.assertEqual((out / "curriculum_bedrock.md").read_bytes(), complete.encode())
