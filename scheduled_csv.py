"""Generate a curriculum from the applied project CSV and weekly materials CSV."""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys

from curriculum_generator import SIZE_HOURS, SLOT_MINUTES, display_minutes, to_minutes
from lesson_analysis import competency_lines, export_blocked_markdown, export_markdown, invoke, weekly_guides
from team_csv import read_rows


def read_inputs(project_path, materials_path, divisor, weeks):
    rows = read_rows(project_path.parent, project_path.name,
                     {"title", "package", "name", "do", "out", "hours", "core", "goal"})
    title, goal = rows[0]["title"], rows[0]["goal"]
    tasks = []
    for row in rows:
        if row["title"] != title or row["goal"] != goal or row["core"] not in {"핵심", "남으면"}:
            raise ValueError("프로젝트 CSV에 프로젝트 하나와 핵심/남으면 표시가 필요합니다")
        repeated = re.match(r"^\s*(\d+)주 동안\s", row["do"])
        tasks.append({"repeat_weeks": int(repeated[1]) if repeated else 0,
                      "id": row["package"], "title": row["name"], "do": row["do"],
                      "out": row["out"], "core": row["core"] == "핵심",
                      "competencies": row.get("competencies", ""),
                      "minutes": to_minutes(Decimal(row["hours"]) / divisor, row["package"], 60)})
    if len({t["id"] for t in tasks}) != len(tasks) or not any(t["core"] for t in tasks):
        raise ValueError("작업 ID는 고유해야 하며 핵심 작업이 하나 이상 필요합니다")
    rows = read_rows(materials_path.parent, materials_path.name, {"id", "title", "url", "topic"})
    materials = []
    for row in rows:
        match = re.fullmatch(r"(\d+)주차 · ([^·]+) · ([12])순위 · 배정 (.+)", row["topic"])
        if not match:
            raise ValueError(f"{row['id']}: topic은 '1주차 · W1 · 1순위 · 배정 20분' 형식이어야 합니다")
        week, work, priority, duration = match.groups()
        time = re.fullmatch(r"(?:(\d+)시간\s*)?(?:(\d+)분)?", duration)
        if not 1 <= int(week) <= weeks or not time or not any(time.groups()):
            raise ValueError(f"{row['id']}: 자료의 주차나 배정시간을 확인하세요")
        hours, minutes = (int(x or 0) for x in time.groups())
        materials.append({**row, "week": int(week), "work": work.strip(), "priority": int(priority),
                          "minutes": to_minutes(hours * 60 + minutes, row["id"], 1)})
    if len({row["id"] for row in materials}) != len(materials):
        raise ValueError("교육자료 ID는 중복될 수 없습니다")
    return {"title": title, "goal": goal}, tasks, materials


def save(output, name, value):
    (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def totals_by_week(materials):
    counts = Counter()
    for material in materials:
        counts[material["week"]] += material["minutes"]
    return counts


def recurring_activities(tasks):
    by_week = {}
    for task in tasks:
        span = task["repeat_weeks"]
        if not span:
            continue
        slots, remainder = divmod(task["minutes"] // SLOT_MINUTES, span)
        for index in range(min(span, task["minutes"] // SLOT_MINUTES)):
            minutes = (slots + int(index < remainder)) * SLOT_MINUTES
            activity = {"kind": "project", "id": task["id"], "title": task["title"],
                        "do": task["do"], "out": task["out"], "minutes": minutes,
                        "competencies": task.get("competencies", ""),
                        "recurring": True, "completed_in_this_chunk": index + 1 == min(span, task["minutes"] // SLOT_MINUTES)}
            by_week.setdefault(index + 1, []).append(activity)
    return by_week


def choose_optional(optional, required_by_week, project_minutes, cap, weeks, output, model, profile, region):
    by_id = {m["id"]: m for m in optional}
    budget = cap * weeks - project_minutes - sum(required_by_week.values())
    item = {"type": "object", "required": ["id", "reason"],
            "properties": {"id": {"type": "string", "enum": list(by_id)}, "reason": {"type": "string"}}}
    schema = {"type": "object", "required": ["selected", "excluded", "selected_minutes"],
              "properties": {"selected": {"type": "array", "items": item},
                             "excluded": {"type": "array", "items": item},
                             "selected_minutes": {"type": "integer", "minimum": 0, "maximum": budget}}}
    records = {"optional_learning_budget_minutes": budget,
               "weekly_optional_limits": {w: cap - required_by_week[w] for w in range(1, weeks + 1)},
               "priority_2_options": [{k: m.get(k, "") for k in
                                       ("id", "title", "week", "work", "minutes", "summary", "covers")}
                                      for m in optional]}
    if not optional:
        return {"selected": [], "excluded": [], "selected_minutes": 0}, None
    prompt = ("고정된 minutes와 배정 주차를 기준으로 2순위 교육자료를 직접 고른다. "
              "총 예산과 각 주의 상한을 모두 점검한다. 각 ID는 selected 또는 excluded에 한 번 넣고 사유를 쓴다. "
              "selected_minutes에는 선택한 자료의 분 합계를 적는다. "
              "이전 응답과 검증 결과가 들어오면 그 초과를 해소할 선택을 다시 제출한다. "
              "자료의 시간과 주차를 바꾸지 않는다.")
    for attempt in (1, 2):
        response = invoke(records, schema, prompt, "select_optional_materials", model, profile, region)
        save(output, f"bedrock_material_selection_attempt_{attempt}.json", response)
        selected_ids = [row["id"] for row in response["selected"]]
        all_ids = selected_ids + [row["id"] for row in response["excluded"]]
        if len(all_ids) != len(set(all_ids)) or set(all_ids) != set(by_id):
            raise ValueError("Bedrock의 자료 ID가 입력과 일치하지 않습니다")
        counts = totals_by_week([by_id[mid] for mid in selected_ids])
        actual = sum(counts.values())
        exceeded_weeks = {str(w): n + required_by_week[w] - cap for w, n in counts.items()
                          if n + required_by_week[w] > cap}
        reported = response["selected_minutes"]
        valid = type(reported) is int and reported == actual and actual <= budget and not exceeded_weeks
        validation = {"status": "accepted" if valid else ("retrying" if attempt == 1 else "blocked"),
                      "attempt": attempt, "optional_learning_budget_minutes": budget,
                      "bedrock_reported_minutes": reported, "actual_selected_minutes": actual,
                      "over_budget_minutes": max(0, actual - budget), "over_week_minutes": exceeded_weeks}
        save(output, f"budget_validation_attempt_{attempt}.json", validation)
        if valid:
            return response, validation
        if attempt == 1:
            records = {**records, "previous_response": response, "budget_validation": validation}
            print("예산 검증 실패 · Bedrock에 자동 재입력", flush=True)
    return response, validation


def place(tasks, materials, cap, weeks):
    counts = totals_by_week(materials)
    recurring = recurring_activities(tasks)
    recurring_minutes = {w: sum(a["minutes"] for a in activities) for w, activities in recurring.items()}
    tasks = [task for task in tasks if not task["repeat_weeks"]]
    remaining = sum(t["minutes"] for t in tasks)
    task_index, progress = 0, 0
    schedule = []
    for week in range(1, weeks + 1):
        activities = [{"kind": "learning", "id": m["id"], "title": m["title"], "url": m["url"],
                       "minutes": m["minutes"], "context": m.get("summary", ""), "covers": m.get("covers", "")}
                      for m in materials if m["week"] == week]
        available = cap - counts[week] - recurring_minutes.get(week, 0)
        future = sum(cap - counts[w] - recurring_minutes.get(w, 0) for w in range(week + 1, weeks + 1))
        remaining_weeks = weeks - week + 1
        even = SLOT_MINUTES * ((remaining // SLOT_MINUTES + remaining_weeks - 1) // remaining_weeks)
        room = min(available, max(even, remaining - future))
        while room and task_index < len(tasks):
            task = tasks[task_index]
            step = min(room, task["minutes"] - progress)
            remaining -= step
            room -= step
            progress += step
            complete = progress == task["minutes"]
            activities.append({"kind": "project", "id": task["id"], "title": task["title"], "do": task["do"],
                               "out": task["out"], "minutes": step, "completed_in_this_chunk": complete,
                               "competencies": task.get("competencies", "")})
            if complete:
                task_index += 1
                progress = 0
        activities.extend(recurring.get(week, []))
        used = sum(a["minutes"] for a in activities)
        schedule.append({"week": week, "capacity_minutes": cap, "used_minutes": used, "activities": activities,
                         "learning_minutes": counts[week], "implementation_minutes": used - counts[week],
                         "goals": list(dict.fromkeys(a["title"] for a in activities))})
    if remaining:
        raise ValueError("주차별 상한 안에 구현시간을 모두 배정할 수 없습니다")
    return schedule


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-csv", type=Path, required=True)
    parser.add_argument("--materials-csv", type=Path, required=True)
    parser.add_argument("--project-hour-divisor", type=Decimal, default=Decimal(1))
    parser.add_argument("--weeks", type=int, default=12)
    budget = parser.add_mutually_exclusive_group(required=True)
    budget.add_argument("--size", choices=SIZE_HOURS)
    budget.add_argument("--hours-per-week", type=Decimal)
    parser.add_argument("--bedrock-model", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.weeks <= 0 or not args.project_hour_divisor.is_finite() or args.project_hour_divisor <= 0:
        parser.error("주차 수와 프로젝트 시간 나눗값은 양수여야 합니다")
    out = args.output_dir
    if out.exists() and any(out.iterdir()):
        parser.error("결과를 보존할 새 --output-dir을 지정하세요")
    out.mkdir(parents=True, exist_ok=True)
    model_args = (args.bedrock_model, args.profile, args.region)
    try:
        cap = to_minutes(SIZE_HOURS[args.size] if args.size else args.hours_per_week, "주당 시간", 60, ceiling=True)
        project, tasks, materials = read_inputs(args.project_csv, args.materials_csv,
                                                args.project_hour_divisor, args.weeks)
        required = [m for m in materials if m["priority"] == 1]
        optional = [m for m in materials if m["priority"] == 2]
        required_by_week = totals_by_week(required)
        implementation_budget = cap * args.weeks - sum(required_by_week.values())
        scheduled_tasks = [t for t in tasks if t["core"]]
        project_minutes = sum(t["minutes"] for t in scheduled_tasks)
        reserved = required_by_week.copy()
        for w, activities in recurring_activities(scheduled_tasks).items():
            reserved[w] += sum(a["minutes"] for a in activities)
        reason, decision, validation = None, None, None
        if (any(w > args.weeks or n > cap for w, n in reserved.items())
                or any(t["repeat_weeks"] > args.weeks for t in scheduled_tasks)
                or project_minutes > implementation_budget):
            reason = "핵심 프로젝트와 1순위 학습이 시간 예산을 초과했습니다"
        else:
            for task in tasks:
                if task["core"] or task["repeat_weeks"] > args.weeks:
                    continue
                more = {w: sum(a["minutes"] for a in activities)
                        for w, activities in recurring_activities([task]).items()}
                if (project_minutes + task["minutes"] <= implementation_budget
                        and all(reserved[w] + n <= cap for w, n in more.items())):
                    scheduled_tasks.append(task)
                    project_minutes += task["minutes"]
                    reserved.update(more)
            sequential_minutes = project_minutes - (sum(reserved.values()) - sum(required_by_week.values()))
            decision, validation = choose_optional(optional, reserved, sequential_minutes, cap,
                                                  args.weeks, out, *model_args)
            if validation and validation["status"] == "blocked":
                reason = "자동 재입력 후에도 자료 선택이 시간 검증을 통과하지 못했습니다"
        selected_ids = {r["id"] for r in decision["selected"]} if decision else set()
        selected = [m for m in materials if m["priority"] == 1 or m["id"] in selected_ids]
        included = {t["id"] for t in scheduled_tasks}
        scheduled_tasks = [t for t in tasks if t["id"] in included]
        weeks = [] if reason else place(scheduled_tasks, selected, cap, args.weeks)
        if weeks:
            for start in range(0, len(weeks), 6):
                weekly_guides(weeks[start:start+6], *model_args)
        summary = {"included_packages": [t["id"] for t in scheduled_tasks],
                   "excluded_packages": [{"id": t["id"]} for t in tasks if t["id"] not in included],
                   "scheduled_material_ids": [m["id"] for m in selected] if weeks else [],
                   "total_minutes": sum(w["used_minutes"] for w in weeks)}
        result = {"status": "blocked" if reason else "needs_review", "project": project,
                  "constraints": {"weeks": args.weeks, "weekly_minutes": cap,
                                  "project_hour_divisor": str(args.project_hour_divisor)},
                  "weeks": weeks, "summary": summary, "warnings": [reason] if reason else [],
                  "material_selection": decision, "budget_validation": validation}
        if reason:
            learning_minutes = sum(m["minutes"] for m in selected)
            capacity_minutes = cap * args.weeks
            required_minutes = project_minutes + learning_minutes
            min_slots = (required_minutes // SLOT_MINUTES + args.weeks - 1) // args.weeks
            fixed_by_week = totals_by_week(selected)
            for week, activities in recurring_activities(scheduled_tasks).items():
                fixed_by_week[week] += sum(a["minutes"] for a in activities)
            result["budget_shortfall"] = {
                "capacity_minutes": capacity_minutes,
                "project_minutes": project_minutes,
                "learning_minutes": learning_minutes,
                "required_minutes": required_minutes,
                "additional_minutes": max(0, required_minutes - capacity_minutes),
                "minimum_weekly_minutes": max(min_slots * SLOT_MINUTES, max(fixed_by_week.values(), default=0)),
                "over_week_minutes": {str(w): n - cap for w, n in fixed_by_week.items() if n > cap},
            }
        save(out, "curriculum.json", result)
        lines = [f"# {project['title']}", ""]
        if reason:
            lines.append(reason)
        for week in weeks:
            lines.extend([f"## {week['week']}주차 · {display_minutes(week['used_minutes'])}", ""])
            if "guide" in week:
                lines.extend(week["guide"][k] for k in ("goal", "method", "reason"))
            for activity in week["activities"]:
                lines.append(f"- {activity['id']} · {activity['title']} · {display_minutes(activity['minutes'])}")
            lines.append("")
        lines.extend(competency_lines(weeks))
        (out / "curriculum.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except (ValueError, InvalidOperation, KeyError, OSError) as exc:
        print(f"입력 또는 분석 오류 · {exc}", file=sys.stderr)
        return 2
    exporter = export_blocked_markdown if reason else export_markdown
    try:
        exporter(result, out, *model_args, display_minutes)
    except (ValueError, OSError) as exc:
        print(f"Bedrock Markdown 생성 실패 · {exc}", file=sys.stderr)
        return 4
    print(f"결과 · {result['status']} · {out / 'curriculum.json'}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
