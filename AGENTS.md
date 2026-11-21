# RoundLab 실행 안내

이 저장소를 받아 커리큘럼을 생성하는 도구는 아래 CLI를 실행한다. 사용자가 지정한 파일과 시간 설정을 사용하고 결과 파일을 반환한다.

## 응용 프로젝트 CSV와 주차별 교육자료 CSV

1. Python 3.10 이상과 AWS CLI v2를 준비한다. AWS 인증과 지정한 Bedrock 모델 실행 권한을 확인한다.
2. 사용자가 제공한 프로젝트 CSV와 교육자료 CSV의 전체 경로를 받는다. 사람이 작성한 CSV와 이전 응답 파일은 입력으로 취급한다.
3. 아래 명령에 실제 값을 넣는다. 프로젝트 구현시간은 CSV의 `hours`를 기본으로 사용한다. 사용자가 시간 환산을 명시한 경우에만 선택 옵션을 적용한다. 소형은 7시간, 중형은 14시간, 대형은 28시간이며 `--hours-per-week`로 직접 입력할 수 있다.

```sh
python3 scheduled_csv.py \
  --project-csv "/absolute/path/프로젝트_응용.csv" \
  --materials-csv "/absolute/path/주차별_교육자료_배치.csv" \
  --size 대형 --weeks 12 \
  --bedrock-model global.anthropic.claude-sonnet-4-6 \
  --region ap-northeast-2 --output-dir result-large
```

명령에서 로컬 AWS 프로필이 필요하면 `--profile`을 추가한다. IAM 역할이나 환경의 표준 AWS 인증을 쓰면 생략할 수 있다. 실행마다 새 출력 디렉터리를 지정한다.

## 결과 확인

- `curriculum.json`의 `status`와 `weeks`를 함께 확인한다. 종료 코드 3은 검토용 일정이나 배정 보류에 공통으로 쓰인다.
- 2순위 자료의 ID·시간 검증 실패는 프로그램이 처리한다. 후보 전체와 이전 응답, 누락 ID나 실제 초과시간을 Bedrock에 한 번 더 보낸다. 후보가 8개 이하면 검증된 조합 중 하나를 Bedrock이 고른다. 호출 도구에서 자료 선택이나 시간표를 따로 고치지 않는다.
- 공유용 결과는 `curriculum_bedrock.md`이며 마지막 Bedrock 응답 전문은 `bedrock_markdown_response.json`이다. 결과 문장은 편집 없이 전달한다.
- 핵심 범위가 가용시간을 넘기거나 재입력 후에도 시간 검증을 통과하지 못하면 배정 보류 Markdown과 JSON을 전달한다. 공유용 생성만 실패한 경우에는 종료 코드 4와 기존 `curriculum.json`, `curriculum.md`를 확인한다.
- 교육자료 없이 작업을 임의로 줄이거나 입력의 구현시간을 재조정하지 않는다. 입력 형식이 다르면 README의 필요한 열을 안내한다.

선정 프로젝트 CSV와 선정 교육자료 CSV 두 장, 또는 구조화 JSON 두 장을 받는 기존 경로는 `curriculum_generator.py`를 사용한다.
