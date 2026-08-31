import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from become import Engine, Store, default_state, parse_time


ROOT = Path(__file__).resolve().parents[1]
AT = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)

CURRICULUM = {
    "destination": {
        "knowledge": ["백프레셔의 인과"],
        "capabilities": ["실측 부하로 안전한 큐 용량을 결정한다"],
        "use_context": "운영 중인 비동기 API",
        "rationale": "실제 운영 판단에 필요하다",
    },
    "baseline": {
        "can_do": ["이벤트 루프 순서 설명"],
        "assisted": ["bounded queue 구현"],
        "cannot_yet": ["큐 용량 계산"],
        "evidence": ["Tutor 과제에서 용량 계산에 실패했다"],
    },
    "sequence": [
        {
            "id": "measure",
            "order": 1,
            "outcome": "생산·소비 처리량을 측정한다",
            "rationale": "용량 계산의 입력이다",
            "prerequisites": [],
        },
        {
            "id": "bound",
            "order": 2,
            "outcome": "측정값으로 bounded queue를 설계한다",
            "rationale": "측정 없는 선택은 추측이다",
            "prerequisites": ["measure"],
        },
    ],
    "cut_list": [
        {
            "topic": "이벤트 루프 역사 연도",
            "reason": "운영 판단에 쓰이지 않는다",
            "reconsider_when": "역사 연구가 목표일 때",
        }
    ],
    "milestones": [
        {
            "id": "load-test",
            "step_id": "measure",
            "title": "부하 테스트로 큐 용량 선택을 방어한다",
            "proof_artifact": "학습자가 작성한 설계 답변",
            "pass_criteria": ["측정 근거", "대안 비교", "실패 경계"],
        },
        {
            "id": "queue-design",
            "step_id": "bound",
            "title": "bounded queue 설계를 운영 기준으로 제출한다",
            "proof_artifact": "학습자가 작성한 운영 설계서",
            "pass_criteria": ["용량 계산", "과부하 정책", "관측 지표"],
        }
    ],
}


def curation(
    curriculum_id, disposition="core", step_id="measure", priority=3,
    curriculum_version=1,
):
    usable = disposition != "reject"
    return {
        "curriculum_id": curriculum_id,
        "curriculum_version": curriculum_version,
        "step_id": step_id,
        "priority": priority,
        "relevance": {
            "decision": "belongs" if usable else "does_not_belong",
            "reason": "현재 단계에 직접 필요" if usable else "현재 경로 밖",
        },
        "credibility": {"decision": "credible", "reason": "원문 범위를 직접 확인"},
        "level_fit": {"decision": "appropriate", "reason": "현재 baseline에 맞음"},
        "signal": {
            "decision": "signal" if usable else "noise",
            "reason": "핵심 절차 포함" if usable else "중복 설명뿐",
        },
        "disposition": disposition,
        "disposition_reason": "현재 학습 선반 배치 판단",
    }


def editor_criteria(revise=False, evidence_span="항상 안전하다"):
    criteria = {
        name: {"status": "pass", "note": "현재 버전에서 확인"}
        for name in (
            "thinking", "logic", "evidence", "repetition", "structure", "precision", "accuracy"
        )
    }
    if revise:
        criteria["logic"] = {
            "status": "revise",
            "note": "실패 경계가 결론과 연결되지 않는다",
            "severity": "blocking",
            "evidence_span": evidence_span,
            "diagnosis": "과부하 반례를 검토하지 않았다",
            "revision_action": "처리량을 넘는 반례를 추가한다",
        }
    return criteria


def milestone_criteria(milestone_id, revise=False):
    names = {
        "load-test": ("측정 근거", "대안 비교", "실패 경계"),
        "queue-design": ("용량 계산", "과부하 정책", "관측 지표"),
    }[milestone_id]
    result = {name: {"status": "pass", "note": "결과물에서 확인"} for name in names}
    if revise:
        result[names[-1]] = {"status": "revise", "note": "증거가 빠짐"}
    return result


def why(topic):
    return (
        f"왜 쓰는가: {topic}의 실제 문제를 막는다. "
        f"왜 이렇게 되었는가: {topic}의 원인과 해결 경계가 연결된다. "
        f"왜 이 결과가 나오는가: 경계에서 피드백이 전달되기 때문이다. "
        f"그래서 어디에 쓰는가: {topic}의 실제 적용 판단에 쓴다."
    )


def prepare_retrieval(engine, item, at=AT):
    related_id = next(iter(item.get("related", [])), None)
    if not related_id:
        anchor = engine.knowledge_add(
            f"{item['title']} 선수 개념", "이미 알고 있는 연결 기준", "concept", [], [], [], at
        )
        engine.knowledge_relate(item["id"], anchor["id"], at)
    else:
        anchor = next(value for value in engine.knowledge() if value["id"] == related_id)
    engine.teach(
        item["id"], why(item["title"]), f"{anchor['title']}와 연결", at
    )
    return engine.review(
        item["id"], "good", [], [], "complete", "다른 사례에 적용하면?",
        "같은 원리의 경계를 찾아 적용한다", "설명 뒤 독립 사례에 적용함",
        at,
    )


def perspective_request(problem="백프레셔"):
    return {
        "current_field": "분산 시스템",
        "current_problem": problem,
    }


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "data"
        self.store = Store(self.home)
        self.engine = Engine(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def init_profile(self):
        return self.engine.advisor_init(
            "분산 시스템 전문가", "중급", ["동시성", "장애 복구"], 0.9, AT
        )

    def init_curriculum(self):
        if not self.store.load()["profile"]:
            self.init_profile()
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.engine.advisor_interview(
                decision, f"{decision} 질문?", f"{decision}에 대한 실제 답", AT
            )
        return self.engine.advisor_curriculum(json.loads(json.dumps(CURRICULUM)), AT)

    def ready_shelf(self, count=3):
        curriculum = self.store.load()["profile"].get("curriculum") or self.init_curriculum()
        materials = []
        for index in range(count):
            source = Path(self.temporary.name) / f"source-{index}.txt"
            source.write_text(f"verified source {index}", encoding="utf-8")
            material = self.engine.material_add(
                f"자료 {index}", str(source), "현재 단계", "원문 직접 확인", AT
            )
            materials.append(
                self.engine.material_curate(
                    material["id"], curation(
                        curriculum["id"], curriculum_version=curriculum["version"]
                    ), AT
                )
            )
        shelf = self.engine.material_shelf(
            curriculum["id"], "measure", [item["id"] for item in materials], AT
        )
        return materials, shelf


class StorageTests(EngineTestCase):
    def test_state_is_created_atomically_and_ignored(self):
        self.assertEqual(self.store.load(), default_state())
        state = default_state()
        state["profile"] = {"goal": "test"}
        self.store.save(state)
        self.assertEqual(self.store.load()["profile"]["goal"], "test")
        self.assertEqual(list(self.home.glob("*.tmp")), [])
        ignored = subprocess.run(
            ["git", "check-ignore", ".become/state.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)

    def test_state_review_pair_recovers_after_process_exit_at_each_replace(self):
        self.init_profile()
        anchor = self.engine.knowledge_add(
            "이벤트 루프", "이미 아는 실행 기준", "concept", [], [], [], AT
        )
        item = self.engine.knowledge_add(
            "백프레셔", "생산 속도를 소비 속도에 맞춘다", "concept", [], [], [], AT
        )
        self.engine.knowledge_relate(item["id"], anchor["id"], AT)
        self.engine.teach(
            item["id"],
            "왜 쓰는가: 과부하를 막는다. 왜 이렇게 되었는가: 속도가 다르다. "
            "왜 이 결과가 나오는가: 경계에서 유입을 제한한다. "
            "그래서 어디에 쓰는가: bounded queue에 쓴다.",
            "이벤트 루프와 연결", AT,
        )
        before_state = self.store.state_path.read_bytes()
        before_reviews = self.store.reviews_path.read_bytes()

        for crash_name in ("reviews.jsonl", "state.json"):
            with self.subTest(crash_name=crash_name):
                child = os.fork()
                if child == 0:
                    original = Store._replace_bytes

                    def crash_after_replace(path, raw):
                        original(path, raw)
                        if path == self.home / crash_name:
                            os._exit(77)

                    Store._replace_bytes = staticmethod(crash_after_replace)
                    try:
                        Engine(Store(self.home)).review(
                            item["id"], "good", [], [], "complete", "지연 인출 질문",
                            "유입률을 소비율에 맞춘다", "새 사례에 적용했다", AT,
                        )
                    except Exception:
                        os._exit(88)
                    os._exit(99)
                _, status = os.waitpid(child, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 77)
                self.assertTrue(self.store.journal_path.exists())
                Store(self.home).load()
                self.assertEqual(self.store.state_path.read_bytes(), before_state)
                self.assertEqual(self.store.reviews_path.read_bytes(), before_reviews)
                self.assertFalse(self.store.journal_path.exists())

    def test_unknown_state_version_is_rejected(self):
        self.home.mkdir(parents=True)
        self.store.state_path.write_text('{"version": 999}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported state version"):
            self.store.load()

    def test_incomplete_state_is_rejected(self):
        self.home.mkdir(parents=True)
        self.store.state_path.write_text('{"version": 1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing fields"):
            self.store.load()

    def test_version_one_state_migrates_with_empty_handoffs(self):
        state = default_state()
        state["version"] = 1
        state.pop("handoffs")
        self.home.mkdir(parents=True)
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["handoffs"], [])
        self.assertEqual(migrated["perspectives"], [])
        self.assertEqual(migrated["shelves"], [])

    def test_version_two_state_migrates_renamed_and_structured_fields(self):
        state = default_state()
        state["version"] = 2
        state["artifacts"] = [{"id": "draft", "revisions": [{"content": "old"}]}]
        state["materials"] = [{"id": "source", "verified": True, "verification": "HTTP 200"}]
        state["handoffs"] = [
            {"id": "handoff-old", "to": "librarian", "status": "completed", "result": "done"}
        ]
        self.home.mkdir(parents=True)
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")

        migrated = self.store.load()

        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["artifacts"][0]["previous_versions"][0]["content"], "old")
        self.assertFalse(migrated["materials"][0]["verified"])
        self.assertEqual(migrated["handoffs"][0]["result"]["summary"], "done")

    def test_version_three_summary_only_handoff_is_preserved_as_legacy(self):
        state = default_state()
        state["version"] = 3
        state["handoffs"] = [
            {
                "id": "legacy-complete", "to": "tutor", "status": "completed",
                "result": {
                    "summary": "old completion", "next_role": None,
                    "resource_ids": [], "issues": [],
                },
            }
        ]
        self.home.mkdir(parents=True)
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        migrated = self.store.load()
        self.assertTrue(migrated["handoffs"][0]["legacy_unverified_output"])
        self.assertEqual(migrated["handoffs"][0]["result"]["summary"], "old completion")

    def test_v3_completed_outputs_cannot_be_trusted_as_new_dependencies(self):
        state = default_state()
        state["version"] = 3
        state["handoffs"] = [{
            "id": "legacy-complete", "to": "advisor", "status": "completed",
            "result": {
                "summary": "검증 전 결과", "next_role": None,
                "resource_ids": ["invented"], "issues": [],
            },
        }]
        self.home.mkdir(parents=True)
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertTrue(self.store.load()["handoffs"][0]["legacy_unverified_output"])
        dependent = self.engine.handoff_dispatch(
            "advisor", "후속 계획", "", AT, depends_on=["legacy-complete"],
            output_kind="advisor_update",
        )
        with self.assertRaisesRegex(ValueError, "unverified legacy"):
            self.engine.handoff_claim(dependent["id"], "advisor", AT)

    def test_invalid_v4_nested_role_state_is_rejected(self):
        self.init_curriculum()
        artifact = self.engine.artifact_add(
            "검증할 초안", "학습자 본문", "무결성 확인", "팀", None, AT, "learner"
        )
        self.engine.artifact_review(
            artifact["id"], editor_criteria(), "pass", "전달", AT
        )
        valid = self.store.load()
        state = json.loads(json.dumps(valid))
        state["artifacts"][0]["review_rounds"][0]["version"] = 99
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact review binding"):
            self.store.load()

        state = json.loads(json.dumps(valid))
        state["profile"]["curriculum"].pop("interview")
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "curriculum interview"):
            self.store.load()

        state = json.loads(json.dumps(valid))
        state["workflows"].append(
            {
                "id": "broken-workflow", "status": "completed",
                "steps": [{"id": "step-1", "order": 1, "status": "ready"}],
            }
        )
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed workflow"):
            self.store.load()

        self.store.save(valid)
        self.engine.workflow_start(
            "perspective", "외부 관점", AT, perspective_spec=perspective_request()
        )
        state = self.store.load()
        state["workflows"][-1]["request_spec"]["evil"] = "silently dropped"
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed workflow"):
            self.store.load()

    def test_v4_state_rejects_two_in_progress_handoffs_for_one_specialist(self):
        first = self.engine.handoff_dispatch(
            "advisor", "첫 경로", "", AT, output_kind="advisor_update"
        )
        second = self.engine.handoff_dispatch(
            "advisor", "둘째 경로", "", AT, output_kind="advisor_update"
        )
        state = self.store.load()
        for handoff_id in (first["id"], second["id"]):
            handoff = self.engine._find(state["handoffs"], handoff_id, "handoff")
            handoff["status"] = "in_progress"
            handoff["claimed_at"] = AT.isoformat()
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "multiple in-progress handoffs"):
            self.store.load()

    def test_v4_workflow_handoff_cannot_drop_or_change_its_output_contract(self):
        self.init_curriculum()
        self.ready_shelf()
        workflow = self.engine.workflow_start("learn", "계약 무결성", AT)
        handoff = self.engine.workflow_next(workflow["id"], "Tutor 계약", AT)["handoff"]
        self.engine.handoff_claim(handoff["id"], "tutor", AT)
        valid = self.store.load()

        missing = json.loads(json.dumps(valid))
        self.engine._find(missing["handoffs"], handoff["id"], "handoff").pop(
            "expected_output_kind"
        )
        self.store.state_path.write_text(json.dumps(missing), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "output contract fields are required"):
            self.store.load()

        downgraded = json.loads(json.dumps(valid))
        self.engine._find(
            downgraded["handoffs"], handoff["id"], "handoff"
        )["expected_output_kind"] = None
        self.store.state_path.write_text(json.dumps(downgraded), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed handoff"):
            self.store.load()

        swapped = json.loads(json.dumps(valid))
        self.engine._find(
            swapped["handoffs"], handoff["id"], "handoff"
        )["expected_resource_ids"] = ["other-knowledge"]
        self.store.state_path.write_text(json.dumps(swapped), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed workflow step"):
            self.store.load()

        self.store.save(valid)
        manual = self.engine.handoff_dispatch("tutor", "수동 Tutor 계약", "", AT)
        untyped = self.store.load()
        self.engine._find(
            untyped["handoffs"], manual["id"], "handoff"
        )["expected_output_kind"] = None
        self.store.state_path.write_text(json.dumps(untyped), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed handoff"):
            self.store.load()


class StateV4MigrationTests(EngineTestCase):
    def test_populated_v3_state_migrates_without_inventing_role_outputs(self):
        self.init_profile()
        source = Path(self.temporary.name) / "legacy.txt"
        source.write_text("legacy source", encoding="utf-8")
        material = self.engine.material_add(
            "기존 자료", str(source), "기존 메모", "원문 확인", AT
        )
        artifact = self.engine.artifact_add(
            "기존 글", "초안", "설명", "동료", None, AT, "learner"
        )
        handoff = self.engine.handoff_dispatch("librarian", "자료 확인", "", AT)
        state = self.store.load()
        state["version"] = 3
        for field in ("shelves", "perspectives", "workflows"):
            state.pop(field)
        for field in ("curriculum", "curriculum_history", "curriculum_interview"):
            state["profile"].pop(field)
        state["materials"][0].pop("curation")
        for field in (
            "purpose", "audience", "milestone_id", "curriculum_id", "curriculum_version",
            "versions", "current_version", "review_rounds", "status",
        ):
            state["artifacts"][0].pop(field)
        for field in ("depends_on", "workflow_id", "step_id"):
            state["handoffs"][0].pop(field)
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")

        migrated = self.store.load()
        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["materials"][0]["id"], material["id"])
        self.assertEqual(migrated["materials"][0]["curation"]["disposition"], "untriaged")
        self.assertEqual(migrated["artifacts"][0]["id"], artifact["id"])
        self.assertEqual(migrated["artifacts"][0]["status"], "draft")
        self.assertTrue(migrated["artifacts"][0]["migration_import_required"])
        self.assertEqual(migrated["artifacts"][0]["versions"][-1]["author"], "legacy_unknown")
        self.assertEqual(migrated["handoffs"][0]["id"], handoff["id"])
        self.assertEqual(migrated["handoffs"][0]["status"], "cancelled")
        self.assertIn("unverifiable", migrated["handoffs"][0]["migration_reason"])
        self.assertEqual(migrated["shelves"], [])
        self.assertEqual(self.store.load(), migrated)
        with self.assertRaisesRegex(ValueError, "learner resubmission"):
            self.engine.artifact_review(
                artifact["id"], editor_criteria(), "pass", "제출", AT
            )
        resubmitted = self.engine.artifact_revise(
            artifact["id"], "학습자가 다시 제출한 초안", AT, "learner"
        )
        self.assertFalse(resubmitted["migration_import_required"])
        self.assertEqual(resubmitted["versions"][-1]["author"], "learner")

    def test_legacy_curriculum_without_step_proof_mapping_is_archived_for_replanning(self):
        self.init_curriculum()
        state = self.store.load()
        state["version"] = 3
        for field in ("shelves", "perspectives", "workflows"):
            state.pop(field)
        curriculum = state["profile"]["curriculum"]
        for step in curriculum["sequence"]:
            step.pop("status")
            step.pop("completed_at")
        for milestone in curriculum["milestones"]:
            milestone.pop("step_id")
            milestone.pop("status")
            milestone.pop("evidence_artifact_id")
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")

        migrated = self.store.load()
        self.assertIsNone(migrated["profile"]["curriculum"])
        self.assertTrue(migrated["profile"]["curriculum_migration_required"])
        self.assertEqual(
            migrated["profile"]["curriculum_history"][-1]["migration_status"],
            "requires_step_milestone_replanning",
        )
        self.assertEqual(self.engine.route("learn", AT)["role"], "advisor")

    def test_v3_curriculum_without_committed_interview_is_archived_not_bricked(self):
        self.init_curriculum()
        state = self.store.load()
        state["version"] = 3
        state["profile"]["curriculum"].pop("interview")
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        migrated = self.store.load()
        self.assertIsNone(migrated["profile"]["curriculum"])
        self.assertEqual(
            migrated["profile"]["curriculum_history"][-1]["migration_status"],
            "requires_v4_evidence_replanning",
        )

    def test_v3_knowledge_without_subject_provenance_requires_explicit_import(self):
        self.init_profile()
        item = self.engine.knowledge_add("합의", "하나의 값을 고른다", "concept", [], [], [], AT)
        state = self.store.load()
        state["version"] = 3
        state["knowledge"][0].pop("subject_binding")
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        migrated = self.store.load()["knowledge"][0]
        self.assertFalse(migrated["active"])
        self.assertTrue(migrated["migration_import_required"])
        self.assertEqual(self.engine.tutor_context()["knowledge"], [])
        self.assertEqual(self.engine.due(AT + timedelta(days=30)), [])


class AdvisorCurriculumTests(EngineTestCase):
    def test_advisor_decides_five_curriculum_dimensions_without_learner_interview(self):
        self.init_profile()
        result = None
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            result = self.engine.advisor_decide(
                decision,
                f"{decision} choice",
                f"{decision} rationale",
                [f"{decision} evidence"],
                AT,
            )
        self.assertEqual(result["status"], "ready")
        curriculum = self.engine.advisor_curriculum(
            json.loads(json.dumps(CURRICULUM)), AT
        )
        self.assertEqual(curriculum["status"], "ready")
        decisions = [
            entry
            for entries in self.store.load()["profile"]["curriculum_interview"].values()
            for entry in entries
        ]
        self.assertEqual(len(decisions), 5)
        self.assertTrue(all(entry["decided_by"] == "advisor" for entry in decisions))
        self.assertTrue(all(entry["rationale"] and entry["evidence"] for entry in decisions))

    def test_profile_and_next_action_follow_current_state(self):
        profile = self.init_profile()
        self.assertEqual(profile["goal"], "분산 시스템 전문가")
        self.assertEqual(self.engine.advise(AT)["topic"], "동시성")

        item = self.engine.knowledge_add(
            "선형화 가능성", "동시 연산을 단일 순서처럼 설명한다.", "concept", ["선형화 지점"], [], [], AT
        )
        prepare_retrieval(self.engine, item)
        practice = self.engine.advise(AT)
        self.assertEqual(practice["action"], "practice")
        self.assertEqual(practice["knowledge_id"], item["id"])

        review = self.engine.advise(AT + timedelta(days=1))
        self.assertEqual(review["action"], "relearn")
        self.assertEqual(review["knowledge_id"], item["id"])

    def test_profile_rejects_invalid_retention(self):
        with self.assertRaisesRegex(ValueError, "between 0.80 and 0.95"):
            self.engine.advisor_init("goal", "", [], 0.5, AT)

    def test_target_change_recalculates_existing_due_time(self):
        self.init_profile()
        item = self.engine.knowledge_add("합의", "노드가 값에 동의한다.", "concept", [], [], [], AT)
        original_due = parse_time(item["memory"]["due_at"])
        self.engine.advisor_init("분산 시스템 전문가", "중급", ["합의"], 0.95, AT)
        updated = self.engine.knowledge()[0]
        self.assertLess(parse_time(updated["memory"]["due_at"]), original_due)

    def test_new_study_target_invalidates_the_old_path_without_deleting_history(self):
        curriculum = self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "백프레셔", "생산률을 제한한다", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        profile = self.engine.advisor_init(
            "프랑스 문학 연구", "입문", ["시 분석"], 0.9, AT
        )
        self.assertIsNone(profile["curriculum"])
        self.assertTrue(profile["curriculum_replan_required"])
        self.assertEqual(profile["curriculum_history"][-1]["id"], curriculum["id"])
        self.assertIn("goal or focus changed", profile["curriculum_history"][-1]["invalidation_reason"])
        self.assertTrue(all(not entries for entries in profile["curriculum_interview"].values()))
        kept = next(item for item in self.engine.knowledge() if item["id"] == knowledge["id"])
        self.assertTrue(kept["active"])
        self.assertNotIn(
            knowledge["id"], [item["id"] for item in self.engine.tutor_context()["knowledge"]]
        )
        self.assertEqual(
            self.engine.route("learn", AT + timedelta(days=2))["workflow"],
            ["advisor", "librarian", "tutor", "advisor"],
        )

    def test_new_study_target_without_focus_does_not_reuse_the_old_subject(self):
        self.init_profile()
        profile = self.engine.advisor_init("프랑스 문학 연구", "", [], 0.9, AT)
        self.assertEqual(profile["focus"], [])
        advice = self.engine.advise(AT)
        self.assertEqual(advice["action"], "choose-focus")
        self.assertEqual(advice["goal"], "프랑스 문학 연구")

    def test_five_decisions_are_required_before_a_curriculum_can_be_committed(self):
        self.init_profile()
        self.engine.advisor_interview("destination", "어디까지 할 수 있어야 합니까?", None, AT)
        with self.assertRaisesRegex(ValueError, "pending interview question"):
            self.engine.advisor_interview("baseline", "지금 무엇을 할 수 있습니까?", None, AT)
        self.engine.advisor_interview(
            "destination", "어디까지 할 수 있어야 합니까?", "운영 선택을 방어한다", AT
        )
        with self.assertRaisesRegex(ValueError, "interview is incomplete"):
            self.engine.advisor_curriculum(CURRICULUM, AT)

    def test_advisor_does_not_repeat_an_answered_interview_question(self):
        self.init_profile()
        self.engine.advisor_interview(
            "baseline", "지금 혼자 무엇을 할 수 있습니까?", "이벤트 루프 설명", AT
        )
        with self.assertRaisesRegex(ValueError, "do not repeat"):
            self.engine.advisor_interview(
                "baseline", "지금 혼자 무엇을 할 수 있습니까?", "같은 답", AT
            )

    def test_five_decisions_cannot_reuse_one_question_or_answer(self):
        self.init_profile()
        self.engine.advisor_interview("destination", "같은 질문?", "첫 답", AT)
        with self.assertRaisesRegex(ValueError, "repeat"):
            self.engine.advisor_interview("baseline", "같은-질문?", "둘째 답", AT)
        with self.assertRaisesRegex(ValueError, "distinct evidence"):
            self.engine.advisor_interview("baseline", "다른 질문?", "첫-답", AT)

    def test_curriculum_rejects_an_unanswered_follow_up_question(self):
        self.init_curriculum()
        self.engine.advisor_interview(
            "destination", "실제 장애에서도 할 수 있습니까?", None, AT
        )
        with self.assertRaisesRegex(ValueError, "unanswered pending"):
            self.engine.advisor_curriculum(CURRICULUM, AT)

    def test_cut_list_cannot_remove_a_required_sequence_step(self):
        self.init_profile()
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.engine.advisor_interview(
                decision, f"{decision} 질문?", f"{decision} 실제 답", AT
            )
        invalid = json.loads(json.dumps(CURRICULUM))
        invalid["cut_list"][0]["topic"] = "bounded queue"
        with self.assertRaisesRegex(ValueError, "cannot also be required"):
            self.engine.advisor_curriculum(invalid, AT)

        valid = json.loads(json.dumps(CURRICULUM))
        valid["sequence"][0]["id"] = "go"
        valid["sequence"][1]["prerequisites"] = ["go"]
        valid["milestones"][0]["step_id"] = "go"
        valid["cut_list"][0]["topic"] = "Django 내부 구조"
        self.assertEqual(self.engine.advisor_curriculum(valid, AT)["status"], "ready")

    def test_curriculum_has_ordered_path_cut_list_and_proof_milestone(self):
        curriculum = self.init_curriculum()
        self.assertEqual([step["order"] for step in curriculum["sequence"]], [1, 2])
        self.assertEqual(curriculum["cut_list"][0]["topic"], "이벤트 루프 역사 연도")
        artifact = self.engine.artifact_add(
            "설계 답변", "측정 근거와 실패 경계를 포함한다", "용량 선택 방어",
            "백엔드 팀", "load-test", AT, "learner",
        )
        with self.assertRaisesRegex(ValueError, "Editor-passed"):
            self.engine.advisor_milestone("load-test", artifact["id"], AT)
        self.engine.artifact_review(
            artifact["id"], editor_criteria(), "pass", "제출", AT + timedelta(minutes=1),
            milestone_criteria("load-test"),
        )
        milestone = self.engine.advisor_milestone(
            "load-test", artifact["id"], AT + timedelta(minutes=2)
        )
        self.assertEqual(milestone["status"], "completed")
        self.assertEqual(
            [step["status"] for step in self.store.load()["profile"]["curriculum"]["sequence"]],
            ["completed", "active"],
        )

    def test_revised_curriculum_rejects_evidence_from_an_older_path_version(self):
        curriculum = self.init_curriculum()
        artifact = self.engine.artifact_add(
            "이전 설계", "이전 경로의 답", "선택 방어", "팀", "load-test", AT, "learner"
        )
        self.engine.artifact_review(
            artifact["id"], editor_criteria(), "pass", "제출", AT,
            milestone_criteria("load-test"),
        )
        revised = json.loads(json.dumps(CURRICULUM))
        revised["destination"]["capabilities"] = ["새 기준으로 큐 용량을 결정한다"]
        self.assertEqual(self.engine.advisor_curriculum(revised, AT)["version"], 2)
        with self.assertRaisesRegex(ValueError, "different curriculum version"):
            self.engine.advisor_milestone("load-test", artifact["id"], AT)
        self.assertEqual(curriculum["version"], 1)

    def test_curriculum_revision_preserves_unchanged_completed_proof(self):
        curriculum = self.init_curriculum()
        artifact = self.engine.artifact_add(
            "완료 증거", "측정·대안·실패 경계", "첫 단계 증명", "팀",
            "load-test", AT, "learner",
        )
        self.engine.artifact_review(
            artifact["id"], editor_criteria(), "pass", "제출", AT,
            milestone_criteria("load-test"),
        )
        self.engine.advisor_milestone("load-test", artifact["id"], AT)

        unchanged = self.engine.advisor_curriculum(
            json.loads(json.dumps(CURRICULUM)), AT
        )
        self.assertEqual(unchanged["version"], 1)
        self.assertEqual(len(self.store.load()["profile"]["curriculum_history"]), 0)

        whitespace_only = json.loads(json.dumps(CURRICULUM))
        whitespace_only["destination"]["rationale"] += "   "
        self.assertEqual(self.engine.advisor_curriculum(whitespace_only, AT)["version"], 1)
        self.assertEqual(len(self.store.load()["profile"]["curriculum_history"]), 0)

        revised = json.loads(json.dumps(CURRICULUM))
        revised["destination"]["capabilities"] = ["실측값으로 운영 결정을 설명하고 방어한다"]
        updated = self.engine.advisor_curriculum(revised, AT)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["milestones"][0]["status"], "completed")
        self.assertEqual(updated["milestones"][0]["evidence_artifact_id"], artifact["id"])
        self.assertEqual(
            [step["status"] for step in updated["sequence"]], ["completed", "active"]
        )

        renamed = json.loads(json.dumps(revised))
        renamed["milestones"][0]["title"] = "새 이름과 새 검증 의미"
        renamed_result = self.engine.advisor_curriculum(renamed, AT)
        self.assertEqual(renamed_result["version"], 3)
        self.assertEqual(renamed_result["milestones"][0]["status"], "planned")
        self.assertIsNone(renamed_result["milestones"][0]["evidence_artifact_id"])

        changed_step = json.loads(json.dumps(renamed))
        changed_step["sequence"][0]["outcome"] = "프랑스 상징주의 시를 원문으로 비평한다"
        changed_step["sequence"][0]["rationale"] = "새 전공의 핵심 수행"
        invalidated = self.engine.advisor_curriculum(changed_step, AT)
        self.assertEqual(invalidated["version"], 4)
        self.assertEqual(invalidated["milestones"][0]["status"], "planned")
        self.assertIsNone(invalidated["milestones"][0]["evidence_artifact_id"])
        self.assertEqual(
            [step["status"] for step in invalidated["sequence"]], ["active", "waiting"]
        )

    def test_curriculum_schema_rejects_unknown_fields_and_duplicate_evidence(self):
        self.init_profile()
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.engine.advisor_interview(
                decision, f"{decision} 고유 질문?", f"{decision} 고유 근거", AT
            )
        unknown = json.loads(json.dumps(CURRICULUM))
        unknown["destination"]["_touch"] = "우회"
        with self.assertRaisesRegex(ValueError, "destination is required"):
            self.engine.advisor_curriculum(unknown, AT)
        duplicate = json.loads(json.dumps(CURRICULUM))
        duplicate["destination"]["capabilities"].append(
            duplicate["destination"]["capabilities"][0]
        )
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.engine.advisor_curriculum(duplicate, AT)

    def test_advisor_resource_ids_are_not_reused_across_targets_or_types(self):
        first_curriculum = self.init_curriculum()
        old_goal = self.engine.learning_goal_add(
            "같은 제목", "첫 전공 결과", "첫 근거", "practical", 3, None, AT
        )
        self.engine.advisor_init("사회학 연구자", "입문", ["권력"], 0.9, AT)
        new_goal = self.engine.learning_goal_add(
            "같은 제목", "둘째 전공 결과", "둘째 근거", "practical", 3, None, AT
        )
        self.assertNotEqual(old_goal["id"], new_goal["id"])
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.engine.advisor_interview(
                decision, f"사회학 {decision}?", f"사회학 {decision} 근거", AT
            )
        next_curriculum = self.engine.advisor_curriculum(CURRICULUM, AT)
        self.assertNotEqual(
            first_curriculum["milestones"][0]["id"], next_curriculum["milestones"][0]["id"]
        )

    def test_new_revision_milestone_cannot_collide_with_a_learning_goal(self):
        self.init_curriculum()
        goal = self.engine.learning_goal_add(
            "new proof", "증거 작성", "새 증거", "practical", 3, None, AT
        )
        revised = json.loads(json.dumps(CURRICULUM))
        revised["milestones"].append({
            "id": goal["id"], "step_id": "measure", "title": "추가 증거",
            "proof_artifact": "추가 답안", "pass_criteria": ["추가 근거"],
        })
        updated = self.engine.advisor_curriculum(revised, AT)
        self.assertNotIn(
            goal["id"], [item["id"] for item in updated["milestones"] if item["title"] == "추가 증거"]
        )

    def test_milestones_cannot_skip_prerequisite_steps(self):
        self.init_curriculum()
        future = self.engine.artifact_add(
            "운영 설계서", "용량과 정책과 지표", "운영 설계", "팀", "queue-design", AT, "learner"
        )
        self.engine.artifact_review(
            future["id"], editor_criteria(), "pass", "제출", AT,
            milestone_criteria("queue-design"),
        )
        with self.assertRaisesRegex(ValueError, "not currently active"):
            self.engine.advisor_milestone("queue-design", future["id"], AT)

        current = self.engine.artifact_add(
            "부하 시험", "측정과 대안과 실패", "첫 단계 증명", "팀", "load-test", AT, "learner"
        )
        self.engine.artifact_review(
            current["id"], editor_criteria(), "pass", "제출", AT,
            milestone_criteria("load-test"),
        )
        self.engine.advisor_milestone("load-test", current["id"], AT)
        self.engine.advisor_milestone("queue-design", future["id"], AT)
        curriculum = self.store.load()["profile"]["curriculum"]
        self.assertEqual(curriculum["status"], "completed")
        self.assertEqual(self.engine.route("learn", AT)["role"], "advisor")


class LibrarianCurationTests(EngineTestCase):
    def test_real_and_missing_sources_are_distinguished_and_recorded(self):
        source = Path(self.temporary.name) / "paper.txt"
        source.write_text("primary source", encoding="utf-8")
        valid = self.engine.material_add(
            "논문", str(source), "핵심 근거", "원문에서 합의 정의를 직접 확인", AT
        )
        missing = self.engine.material_add(
            "없는 논문",
            str(source.with_name("missing.txt")),
            "검증 실패",
            "제목과 본문 확인",
            AT,
        )
        self.assertTrue(valid["verified"])
        self.assertFalse(missing["verified"])
        self.assertIn("not found", missing["verification"]["check"])
        self.assertEqual(len(self.engine.materials()), 2)

        updated = self.engine.material_add(
            "논문 수정", str(source), "새 메모", "수정된 범위를 직접 확인", AT
        )
        self.assertEqual(updated["id"], valid["id"])
        self.assertEqual(len(self.engine.materials()), 2)

    def test_unsupported_source_scheme_is_not_claimed_as_verified(self):
        material = self.engine.material_add("가짜", "madeup://source", "", "원문 확인", AT)
        self.assertFalse(material["verified"])
        self.assertIn("unsupported", material["verification"]["check"])

    def test_reachable_source_without_content_evidence_is_not_verified(self):
        source = Path(self.temporary.name) / "paper.txt"
        source.write_text("primary source", encoding="utf-8")
        material = self.engine.material_add("논문", str(source), "용도", "", AT)
        self.assertTrue(material["verification"]["reachable"])
        self.assertFalse(material["verified"])

    def test_empty_source_is_not_verified(self):
        for index, content in enumerate((b"", b" \n\t")):
            source = Path(self.temporary.name) / f"empty-{index}.txt"
            source.write_bytes(content)
            material = self.engine.material_add(
                f"빈 자료 {index}", str(source), "내용 없음", "본문 직접 확인", AT
            )
            self.assertFalse(material["verified"])
            self.assertIn("no content", material["verification"]["check"])

    def test_only_triaged_signal_enters_a_three_to_four_source_shelf(self):
        curriculum = self.init_curriculum()
        accepted = []
        for index in range(6):
            source = Path(self.temporary.name) / f"accepted-{index}.txt"
            source.write_text(f"primary source {index}", encoding="utf-8")
            material = self.engine.material_add(
                f"핵심 자료 {index}", str(source), "현재 단계", "원문 직접 확인", AT
            )
            accepted.append(
                self.engine.material_curate(
                    material["id"],
                    curation(
                        curriculum["id"],
                        step_id="bound" if index == 5 else "measure",
                        priority=min(index + 1, 5),
                    ),
                    AT,
                )
            )
        rejected_source = Path(self.temporary.name) / "rejected.txt"
        rejected_source.write_text("noise", encoding="utf-8")
        rejected = self.engine.material_add(
            "잡음 자료", str(rejected_source), "범위 밖", "원문 직접 확인", AT
        )
        self.engine.material_curate(
            rejected["id"], curation(curriculum["id"], "reject"), AT
        )

        measure_candidates = [item["id"] for item in accepted[:5]] + [rejected["id"]]
        shelf = self.engine.material_shelf(
            curriculum["id"], "measure", measure_candidates, AT
        )
        self.assertEqual(shelf["status"], "ready")
        self.assertEqual(len(shelf["selected_material_ids"]), 4)
        self.assertNotIn(accepted[0]["id"], shelf["selected_material_ids"])
        self.assertIn(accepted[4]["id"], shelf["selected_material_ids"])
        self.assertNotIn(accepted[5]["id"], shelf["selected_material_ids"])
        self.assertIn(rejected["id"], shelf["rejected_material_ids"])
        self.assertNotIn(rejected["id"], shelf["selected_material_ids"])

        self.engine.material_add(
            "핵심 자료 4 재검증", accepted[4]["source"], "내용 변경", "새 본문 직접 확인", AT
        )
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")
        with self.assertRaisesRegex(ValueError, "every shelf candidate must be triaged"):
            self.engine.material_shelf(
                curriculum["id"], "measure", measure_candidates, AT
            )
        self.engine.material_curate(
            accepted[4]["id"], curation(curriculum["id"], "reject"), AT
        )
        rebuilt = self.engine.material_shelf(
            curriculum["id"], "measure", measure_candidates, AT
        )
        self.assertEqual(rebuilt["status"], "ready")
        self.assertNotIn(accepted[4]["id"], rebuilt["selected_material_ids"])

    def test_unverified_or_untriaged_material_cannot_be_promoted(self):
        curriculum = self.init_curriculum()
        missing = self.engine.material_add(
            "없는 자료", str(Path(self.temporary.name) / "missing.txt"), "", "확인", AT
        )
        with self.assertRaisesRegex(ValueError, "verified, relevant, credible"):
            self.engine.material_curate(
                missing["id"], curation(curriculum["id"]), AT
            )

    def test_too_basic_material_cannot_be_promoted_as_core_or_supplement(self):
        curriculum = self.init_curriculum()
        source = Path(self.temporary.name) / "too-basic.txt"
        source.write_text("introductory recap", encoding="utf-8")
        material = self.engine.material_add(
            "너무 쉬운 자료", str(source), "기초 복습", "본문 직접 확인", AT
        )
        for disposition in ("core", "supplement"):
            assessment = curation(curriculum["id"], disposition)
            assessment["level_fit"] = {
                "decision": "too_basic", "reason": "현재 baseline보다 낮음"
            }
            with self.assertRaisesRegex(ValueError, "level-fit signal"):
                self.engine.material_curate(material["id"], assessment, AT)

    def test_copied_sources_do_not_fake_a_three_source_shelf(self):
        curriculum = self.init_curriculum()
        materials = []
        for index in range(3):
            source = Path(self.temporary.name) / f"copy-{index}.txt"
            source.write_text("the same source bytes", encoding="utf-8")
            material = self.engine.material_add(
                f"복사본 {index}", str(source), "중복", "본문 직접 확인", AT
            )
            materials.append(
                self.engine.material_curate(material["id"], curation(curriculum["id"]), AT)
            )
        shelf = self.engine.material_shelf(
            curriculum["id"], "measure", [item["id"] for item in materials], AT
        )
        self.assertEqual(shelf["status"], "incomplete")
        self.assertEqual(len(shelf["selected_material_ids"]), 1)

    def test_ready_shelf_becomes_stale_when_a_source_disappears_or_changes(self):
        self.init_curriculum()
        materials, shelf = self.ready_shelf()
        self.assertEqual(shelf["status"], "ready")
        source = Path(materials[0]["source"])
        source.write_text("changed after verification", encoding="utf-8")
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")
        source.unlink()
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")


class SourceProbeTests(EngineTestCase):
    def test_unchanged_local_source_is_revalidated_without_rereading_it(self):
        source = Path(self.temporary.name) / "paper.txt"
        source.write_text("primary source", encoding="utf-8")
        observation, probe = Engine.read_source(str(source))
        self.assertTrue(observation[0])
        self.assertEqual(probe["kind"], "file")
        unchanged, repeated = Engine.read_source(str(source), validators=probe)
        self.assertIsNone(unchanged)
        self.assertEqual(repeated, probe)
        source.write_text("changed after verification", encoding="utf-8")
        changed, changed_probe = Engine.read_source(str(source), validators=probe)
        self.assertIsNotNone(changed)
        self.assertNotEqual(changed[3], observation[3])
        self.assertNotEqual(changed_probe, probe)

    def test_a_moved_source_is_reread_instead_of_reusing_another_paths_probe(self):
        first = Path(self.temporary.name) / "first.txt"
        second = Path(self.temporary.name) / "second.txt"
        first.write_text("shared bytes", encoding="utf-8")
        second.write_text("shared bytes", encoding="utf-8")
        _, probe = Engine.read_source(str(first))
        observation, _ = Engine.read_source(str(second), validators=probe)
        self.assertIsNotNone(observation)
        self.assertEqual(observation[2], str(second.resolve()))

    def test_probe_cache_crosses_engines_and_drops_unreachable_sources(self):
        source = Path(self.temporary.name) / "paper.txt"
        source.write_text("primary source", encoding="utf-8")
        first = self.engine.sources.inspect(str(source))
        cache_path = self.home / ".sources.json"
        self.assertTrue(cache_path.is_file())
        self.assertEqual(Engine(Store(self.home)).sources.inspect(str(source)), first)
        source.unlink()
        self.assertFalse(self.engine.sources.inspect(str(source))[0])
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertNotIn(str(source), cached["entries"])

    def test_inspect_many_answers_every_distinct_source_once(self):
        sources = []
        for index in range(3):
            source = Path(self.temporary.name) / f"batch-{index}.txt"
            source.write_text(f"batch {index}", encoding="utf-8")
            sources.append(str(source))
        observed = self.engine.sources.inspect_many(sources + [sources[0]])
        self.assertEqual(set(observed), set(sources))
        self.assertTrue(all(item[0] for item in observed.values()))

    def test_http_source_is_revalidated_with_a_conditional_request(self):
        url = "http://example.test/paper"
        probe = {"kind": "http", "identity": url, "etag": '"v1"', "last_modified": ""}
        sent = {}

        def not_modified(request, timeout=None):
            sent["if_none_match"] = request.get_header("If-none-match")
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

        with mock.patch("urllib.request.urlopen", not_modified):
            observation, returned = Engine.read_source(url, validators=probe)
        self.assertIsNone(observation)
        self.assertEqual(returned, probe)
        self.assertEqual(sent["if_none_match"], '"v1"')

    def test_http_source_that_changed_is_read_again_with_a_new_probe(self):
        url = "http://example.test/paper"
        probe = {"kind": "http", "identity": url, "etag": '"v1"', "last_modified": ""}

        class Response:
            status = 200
            headers = {"ETag": '"v2"'}

            def __init__(self):
                self.chunks = [b"remote primary source", b""]

            def read(self, size):
                return self.chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *details):
                return False

        with mock.patch("urllib.request.urlopen", lambda request, timeout=None: Response()):
            observation, returned = Engine.read_source(url, validators=probe)
        self.assertTrue(observation[0])
        self.assertEqual(observation[1], "HTTP 200")
        self.assertEqual(returned["etag"], '"v2"')

    def test_a_failing_http_source_stays_unreachable(self):
        url = "http://example.test/paper"

        def gone(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with mock.patch("urllib.request.urlopen", gone):
            observation, probe = Engine.read_source(url, validators={"kind": "http", "etag": '"v1"'})
        self.assertFalse(observation[0])
        self.assertIn("404", observation[1])
        self.assertEqual(probe, {})

    def test_a_shelf_the_stored_state_already_rejects_costs_no_source_read(self):
        self.init_curriculum()
        materials, shelf = self.ready_shelf()
        self.assertEqual(shelf["status"], "ready")
        # Re-registering a source resets its triage, so the shelf is stale on
        # stored state alone and its sources are not worth reading.
        self.engine.material_add(
            materials[0]["title"], materials[0]["source"], "", "원문 다시 확인", AT
        )

        class Refused:
            def inspect_many(self, sources):
                raise AssertionError("stored state already rejects this shelf")

            def inspect(self, source):
                raise AssertionError("stored state already rejects this shelf")

        self.engine.sources = Refused()
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")


class TutorTests(EngineTestCase):
    def test_knowledge_is_scheduled_and_reviewed_without_fixed_boxes(self):
        self.init_profile()
        item = self.engine.knowledge_add(
            "벡터 시계",
            "분산 이벤트의 부분 순서를 표현한다.",
            "concept",
            ["동시 이벤트 판별"],
            [],
            [],
            AT,
        )
        prepare_retrieval(self.engine, item)
        self.assertNotIn("card", item)
        self.assertEqual(self.engine.due(AT), [])
        self.assertEqual(self.engine.due(AT + timedelta(days=1))[0]["id"], item["id"])

        reviewed = self.engine.review(
            item["id"],
            "good",
            [],
            ["동시 이벤트 판별"],
            "complete",
            "두 이벤트가 동시인지 어떻게 판별합니까?",
            "벡터의 어느 쪽도 다른 쪽 이하가 아니면 동시입니다.",
            "부분 순서 판별을 정확히 설명함",
            AT + timedelta(days=1),
        )
        self.assertEqual(reviewed["memory"]["stability_days"], 2.0)
        self.assertEqual(reviewed["memory"]["review_count"], 1)
        self.assertEqual(reviewed["weak_points"], [])
        self.assertEqual(self.engine.due(AT + timedelta(days=2)), [])
        self.assertEqual(self.engine.due(AT + timedelta(days=3))[0]["id"], item["id"])
        event = json.loads(self.store.reviews_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["knowledge_id"], item["id"])
        self.assertEqual(event["rating"], "good")
        self.assertEqual(event["phase"], "retrieval")

    def test_verified_sources_and_bidirectional_relations_are_enforced(self):
        curriculum = self.init_curriculum()
        source = Path(self.temporary.name) / "source.txt"
        source.write_text("source", encoding="utf-8")
        material = self.engine.material_add("자료", str(source), "", "본문 직접 확인", AT)
        self.engine.material_curate(
            material["id"], curation(curriculum["id"]), AT
        )
        for index in range(2):
            extra = Path(self.temporary.name) / f"extra-{index}.txt"
            extra.write_text(f"source {index + 1}", encoding="utf-8")
            added = self.engine.material_add(
                f"추가 자료 {index}", str(extra), "", "본문 직접 확인", AT
            )
            self.engine.material_curate(
                added["id"], curation(curriculum["id"]), AT
            )
        self.engine.material_shelf(
            curriculum["id"], "measure",
            [material["id"]] + [
                item["id"] for item in self.engine.materials() if item["id"] != material["id"]
            ],
            AT,
        )
        first = self.engine.knowledge_add(
            "첫 개념", "첫 설명", "concept", [], [], [material["id"]], AT
        )
        second = self.engine.knowledge_add(
            "둘째 개념", "둘째 설명", "concept", [], [first["id"]], [], AT
        )
        stored_first = next(item for item in self.engine.knowledge() if item["id"] == first["id"])
        self.assertIn(second["id"], stored_first["related"])

        missing = self.engine.material_add(
            "없는 자료", str(source.with_name("missing.txt")), "", "본문 직접 확인", AT
        )
        with self.assertRaisesRegex(ValueError, "unselected sources"):
            self.engine.knowledge_add(
                "셋째 개념", "셋째 설명", "concept", [], [], [missing["id"]], AT
            )


class TutorAuditTests(EngineTestCase):
    def test_prompt_answer_and_rationale_are_saved_in_state_and_audit_log(self):
        self.init_profile()
        item = self.engine.knowledge_add("합의", "노드가 값에 동의한다.", "concept", [], [], [], AT)
        self.engine.review(
            item["id"],
            "hard",
            ["장애 노드 경계"],
            [],
            "partial",
            "장애가 있어도 합의가 필요한 이유는?",
            "서로 다른 결정을 막기 위해서입니다.",
            "안전성은 설명했지만 장애 허용 경계는 빠짐",
            AT + timedelta(minutes=5),
        )
        stored = self.engine.knowledge()[0]["last_interaction"]
        logged = json.loads(self.store.reviews_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["answer"], "서로 다른 결정을 막기 위해서입니다.")
        self.assertEqual(logged["prompt"], stored["prompt"])
        self.assertEqual(logged["rationale"], stored["rationale"])


class TutorConnectionTests(EngineTestCase):
    def test_context_exposes_known_anchors_and_relate_is_bidirectional(self):
        self.engine.advisor_init(
            "분산 시스템 전문가",
            "Python과 이벤트 루프는 알고 있음",
            ["백프레셔"],
            0.9,
            AT,
        )
        known = self.engine.knowledge_add(
            "이벤트 루프", "준비된 작업을 순서대로 실행한다.", "concept", [], [], [], AT
        )
        learning = self.engine.knowledge_add(
            "백프레셔", "생산 속도를 소비 가능량에 맞춘다.", "concept", ["흐름 제어"], [], [], AT
        )

        context = self.engine.tutor_context()
        self.assertEqual(context["current_level"], "Python과 이벤트 루프는 알고 있음")
        self.assertEqual(
            [item["title"] for item in context["knowledge"]], ["이벤트 루프", "백프레셔"]
        )
        self.assertEqual(context["knowledge"][1]["weak_points"], ["흐름 제어"])

        self.engine.knowledge_relate(known["id"], learning["id"], AT + timedelta(minutes=1))
        stored = {item["id"]: item for item in self.engine.knowledge()}
        self.assertIn(learning["id"], stored[known["id"]]["related"])
        self.assertIn(known["id"], stored[learning["id"]]["related"])
        with self.assertRaisesRegex(ValueError, "cannot relate to itself"):
            self.engine.knowledge_relate(known["id"], known["id"], AT)

    def test_related_anchor_side_effect_does_not_block_tutor_handoff(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        anchor = self.engine.knowledge_add(
            "이벤트 루프", "준비된 작업을 순서대로 실행한다", "concept", [], [], [], AT
        )
        target = self.engine.knowledge_add(
            "백프레셔", "생산 속도를 소비 가능량에 맞춘다", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        workflow = self.engine.workflow_start("learn", "연결해서 학습", AT)
        tutor = self.engine.workflow_next(workflow["id"], "원리 연결", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        self.engine.knowledge_relate(target["id"], anchor["id"], AT)
        self.engine.teach(target["id"], why("백프레셔"), "이벤트 루프", AT)
        self.engine.review(
            target["id"], "good", [], [], "complete", "다른 큐에 적용하면?",
            "생산률을 소비율에 맞춘다", "연결 원리를 독립 적용", AT,
        )
        completed = self.engine.handoff_complete(
            tutor["id"], "tutor",
            {
                "summary": "연결 학습 완료", "resource_ids": [target["id"]],
                "next_role": "advisor", "observations": ["새 큐에 독립 적용"],
                "recommendations": ["실측 처리량으로 용량 결정"],
            }, AT,
        )
        self.assertEqual(completed["status"], "completed")

    def test_tutor_contract_requires_confusion_why_chain_and_known_concepts(self):
        text = (ROOT / "agents" / "tutor.md").read_text(encoding="utf-8")
        for requirement in (
            "혼동을 진단한다",
            "왜 쓰는가",
            "왜 이렇게 되었는가",
            "왜 이 결과가 나오는가",
            "커넥팅 더 닷",
            "사용자가 아는 개념",
            "어디서 비유가 깨지는지도",
            "tutor context",
            "tutor relate",
        ):
            self.assertIn(requirement, text)

    def test_review_rejects_contradictory_rating_and_confusion_updates(self):
        self.init_profile()
        item = self.engine.knowledge_add(
            "합의 경계", "장애 수에 따라 정족수가 달라진다", "concept", ["장애 수"], [], [], AT
        )
        due = AT + timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "rating and confidence disagree"):
            self.engine.review(
                item["id"], "easy", ["전부 모름"], [], "failed", "경계는?", "모름",
                "독립 인출 실패", due,
            )
        with self.assertRaisesRegex(ValueError, "added and cleared"):
            self.engine.review(
                item["id"], "hard", ["장애 수"], ["장애 수"], "partial", "경계는?",
                "일부만 앎", "경계가 불완전", due,
            )
        stored = self.engine.knowledge()[0]
        self.assertEqual(stored["memory"]["stability_days"], 1.0)
        self.assertIsNone(stored["confusion_history"][0]["resolved_at"])

    def test_teaching_requires_a_connection_to_known_knowledge(self):
        self.init_profile()
        item = self.engine.knowledge_add("백프레셔", "흐름을 제한한다", "concept", [], [], [], AT)
        with self.assertRaisesRegex(ValueError, "connection"):
            self.engine.teach(item["id"], "왜 필요한지 설명", "", AT)
        with self.assertRaisesRegex(ValueError, "왜 쓰는가"):
            self.engine.teach(item["id"], "짧은 설명", "현재 분산 시스템 수준", AT)

    def test_same_title_in_a_new_subject_does_not_merge_memory_or_confusions(self):
        self.init_profile()
        old = self.engine.knowledge_add(
            "합의", "노드가 하나의 값을 고른다", "concept", ["쿼럼"], [], [], AT
        )
        self.engine.advisor_init("사회학 연구자", "입문", ["권력 관계"], 0.9, AT)
        new = self.engine.knowledge_add(
            "합의", "행위자들이 의미를 협상한다", "concept", ["권력 비대칭"], [], [], AT
        )
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["weak_points"], ["권력 비대칭"])
        self.assertEqual(new["memory"]["review_count"], 0)
        self.assertEqual([item["id"] for item in self.engine.tutor_context()["knowledge"]], [new["id"]])
        with self.assertRaisesRegex(ValueError, "exactly one related knowledge or curriculum baseline"):
            self.engine.teach(old["id"], why("옛 합의"), "현재 사회학 수준", AT)
        with self.assertRaisesRegex(ValueError, "current learning target"):
            self.engine.learning_goal_add(
                "옛 합의 실전", "분산 합의를 새 시스템에 적용한다", "새 학습 목표", "practical", 5,
                old["id"], AT,
            )
        with self.assertRaisesRegex(ValueError, "current learning target"):
            self.engine.knowledge_relate(old["id"], new["id"], AT)


class AdvisorTutorLoopTests(EngineTestCase):
    def test_level_evidence_and_practical_goals_drive_the_plan(self):
        self.engine.advisor_init("백엔드 전문가", "Python 문법", ["비동기 I/O"], 0.9, AT)
        observed = self.engine.advisor_observe(
            "이벤트 루프의 실행 순서는 설명하지만 부하 제어 적용은 어려움",
            "Tutor 과제에서 callback 순서는 설명했고 bounded queue 선택에는 실패함",
            AT + timedelta(minutes=1),
        )
        self.assertEqual(len(observed["level_evidence"]), 1)
        lower = self.engine.learning_goal_add(
            "비동기 작업 취소",
            "취소 전파와 정리 코드를 구현한다",
            "운영 중 자원 누수를 막는 데 필요",
            "practical",
            3,
            None,
            AT + timedelta(minutes=2),
        )
        higher = self.engine.learning_goal_add(
            "백프레셔 적용",
            "bounded queue로 생산 속도를 제한하고 병목을 진단한다",
            "Tutor가 확인한 부하 제어 적용 공백",
            "practical",
            5,
            None,
            AT + timedelta(minutes=3),
        )
        plan = self.engine.advise(AT + timedelta(minutes=4))
        self.assertEqual(plan["learning_goal"]["id"], higher["id"])
        self.assertIn("bounded queue", plan["learning_goal"]["outcome"])
        self.engine.learning_goal_status(higher["id"], "completed", AT + timedelta(minutes=5))
        self.assertEqual(self.engine.advise(AT + timedelta(minutes=6))["learning_goal"]["id"], lower["id"])

    def test_due_resolved_confusion_becomes_a_remedial_goal(self):
        self.init_profile()
        item = self.engine.knowledge_add(
            "선형화 가능성",
            "동시 연산이 한 시점에 일어난 것처럼 보이게 한다.",
            "concept",
            ["선형화 지점 선택"],
            [],
            [],
            AT,
        )
        prepare_retrieval(self.engine, item)
        reviewed = self.engine.review(
            item["id"],
            "good",
            [],
            ["선형화 지점 선택"],
            "complete",
            "서로 겹친 두 연산의 선형화 지점을 고르세요.",
            "호출과 응답 사이에서 관찰 결과와 일치하는 시점을 고릅니다.",
            "간격 뒤 경계와 적용을 독립 인출함",
            AT + timedelta(days=1),
        )
        self.assertEqual(reviewed["weak_points"], [])
        self.assertIsNotNone(reviewed["confusion_history"][0]["resolved_at"])

        self.assertEqual(self.engine.route("learn", AT + timedelta(days=3))["role"], "advisor")
        advice = self.engine.advise(AT + timedelta(days=3))
        self.assertEqual(advice["action"], "relearn")
        self.assertEqual(advice["learning_goal"]["kind"], "remedial")
        self.assertEqual(advice["learning_goal"]["knowledge_id"], item["id"])
        self.assertIn("previous confusion", advice["reason"])
        self.engine.advise(AT + timedelta(days=3, minutes=1))
        self.assertEqual(len(self.engine.learning_goals()), 1)

    def test_legacy_saved_weak_point_is_promoted_to_confusion_history(self):
        self.init_profile()
        item = self.engine.knowledge_add(
            "쿼럼", "과반수 교집합을 만든다.", "concept", ["교집합이 필요한 이유"], [], [], AT
        )
        prepare_retrieval(self.engine, item)
        state = self.store.load()
        state["knowledge"][0].pop("confusion_history")
        self.store.save(state)
        advice = self.engine.advise(AT + timedelta(days=1))
        self.assertEqual(advice["action"], "relearn")
        saved = self.engine.knowledge()[0]
        self.assertEqual(saved["confusion_history"][0]["point"], "교집합이 필요한 이유")
        self.assertEqual(advice["learning_goal"]["knowledge_id"], item["id"])

    def test_tutor_contract_has_teach_first_and_retrieval_modes(self):
        self.init_profile()
        anchor = self.engine.knowledge_add(
            "이벤트 루프", "이미 알고 있는 개념", "concept", [], [], [], AT
        )
        item = self.engine.knowledge_add("백프레셔", "생산 속도를 제한한다.", "concept", [], [], [], AT)
        self.engine.knowledge_relate(item["id"], anchor["id"], AT)
        taught = self.engine.teach(
            item["id"],
            why("백프레셔"),
            "이미 아는 이벤트 루프의 준비 큐와 연결",
            AT + timedelta(minutes=1),
        )
        self.assertEqual(taught["last_teaching"]["interaction"], "teaching")
        self.assertEqual(taught["last_teaching"]["phase"], "exposure")
        self.assertEqual(taught["memory"]["stability_days"], 1.0)
        self.assertEqual(taught["memory"]["review_count"], 0)
        contract = (ROOT / "agents" / "tutor.md").read_text(encoding="utf-8")
        for requirement in (
            "새 학습·설명 요청·만기 전 약점",
            "질문으로 시험하지 말고 `tutor teach`로 먼저 알려준다",
            "만기 후 retrieval",
            "힌트와 설명 없이 질문부터",
            "학습 세션 전체를 문답식 심문으로 만들지 않는다",
        ):
            self.assertIn(requirement, contract)

    def test_tutor_handoff_carries_observations_and_recommendations(self):
        self.init_profile()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "백프레셔", "생산률을 소비 가능량에 맞춘다", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        handoff = self.engine.handoff_dispatch("tutor", "적응형 학습", "실용 목표", AT)
        self.engine.handoff_claim(handoff["id"], "tutor", AT + timedelta(minutes=1))
        self.engine.teach(
            knowledge["id"], why("백프레셔"), "이벤트 루프 순서 설명과 연결",
            AT + timedelta(minutes=1),
        )
        self.engine.review(
            knowledge["id"], "hard", ["부하 수치 선택"], [], "partial",
            "실제 처리량으로 용량을 정해보세요", "수치 산정은 도움 필요",
            "원리는 설명했지만 적용 근거가 부족함", AT + timedelta(minutes=2),
        )
        completed = self.engine.handoff_complete(
            handoff["id"],
            "tutor",
            {
                "summary": "백프레셔 원리 교육과 적용 완료",
                "next_role": "advisor",
                "resource_ids": [knowledge["id"]],
                "issues": ["부하 수치 선택"],
                "observations": ["bounded queue 원리는 설명했으나 용량 산정은 도움 필요"],
                "recommendations": ["실제 처리량으로 queue capacity를 산정한다"],
            },
            AT + timedelta(minutes=2),
        )
        self.assertEqual(completed["result"]["next_role"], "advisor")
        self.assertEqual(len(completed["result"]["observations"]), 1)
        self.assertEqual(len(completed["result"]["recommendations"]), 1)


class TutorPhaseTests(EngineTestCase):
    def test_backdated_teaching_needs_a_backdated_application_before_retrieval(self):
        taught_at = AT - timedelta(days=10)
        applied_at = AT - timedelta(days=2)
        self.engine.advisor_init("시스템 설계자", "초급", ["백프레셔"], 0.9, taught_at)
        anchor = self.engine.knowledge_add(
            "이벤트 루프", "이미 아는 실행 기준", "concept", [], [], [], taught_at
        )
        item = self.engine.knowledge_add(
            "백프레셔", "생산 속도를 소비 속도에 맞춘다", "concept", [], [], [], taught_at
        )
        self.engine.knowledge_relate(item["id"], anchor["id"], taught_at)
        taught = self.engine.teach(
            item["id"], why("백프레셔"), "이벤트 루프와 연결", taught_at
        )

        self.assertLess(parse_time(taught["memory"]["due_at"]), AT)
        self.assertEqual(self.engine.review_phase(taught, AT), "exposure")
        self.assertEqual(self.engine.due(AT), [])

        applied = self.engine.review(
            item["id"], "good", [], [], "complete", "어디에 적용할까?",
            "입구에서 생산률을 제한한다", "설명과 다른 사례에 적용했다", applied_at,
        )
        self.assertEqual(applied["last_interaction"]["phase"], "exposure")
        self.assertEqual(parse_time(applied["memory"]["first_exposed_at"]), applied_at)
        self.assertLess(parse_time(applied["memory"]["due_at"]), AT)

        due = self.engine.due(AT)
        self.assertEqual([entry["id"] for entry in due], [item["id"]])
        retrieved = self.engine.review(
            item["id"], "good", [], [], "complete", "간격 뒤 다시 설명하세요",
            "소비 속도보다 빠른 유입을 경계에서 제한한다", "자료 없이 독립 인출했다", AT,
        )
        self.assertEqual(retrieved["last_interaction"]["phase"], "retrieval")
        self.assertEqual(retrieved["memory"]["review_count"], 1)

    def test_exposure_does_not_raise_stability_and_due_retrieval_does(self):
        self.init_profile()
        anchor = self.engine.knowledge_add(
            "분산 시스템", "이미 알고 있는 개념", "concept", [], [], [], AT
        )
        item = self.engine.knowledge_add("합의", "노드가 값에 동의한다.", "concept", [], [], [], AT)
        self.engine.knowledge_relate(item["id"], anchor["id"], AT)
        due_before = item["memory"]["due_at"]
        self.engine.teach(item["id"], why("합의"), "현재 분산 시스템 수준", AT)
        exposed = self.engine.review(
            item["id"],
            "good",
            [],
            [],
            "complete",
            "합의란 무엇입니까?",
            "노드가 하나의 값에 동의하는 것입니다.",
            "첫 설명 직후의 이해 확인",
            AT + timedelta(minutes=5),
        )
        self.assertEqual(exposed["last_interaction"]["phase"], "exposure")
        self.assertEqual(exposed["memory"]["stability_days"], 1.0)
        self.assertEqual(exposed["memory"]["review_count"], 0)
        self.assertEqual(exposed["memory"]["exposure_count"], 2)
        self.assertGreater(parse_time(exposed["memory"]["due_at"]), parse_time(due_before))

        retrieved = self.engine.review(
            item["id"],
            "good",
            [],
            [],
            "complete",
            "하루 뒤 합의를 다시 설명하세요.",
            "장애가 있어도 정상 노드가 같은 값을 결정하는 것입니다.",
            "간격 후 독립 인출 성공",
            AT + timedelta(days=1, minutes=5),
        )
        self.assertEqual(retrieved["last_interaction"]["phase"], "retrieval")
        self.assertEqual(retrieved["memory"]["stability_days"], 2.0)
        self.assertEqual(retrieved["memory"]["review_count"], 1)

    def test_first_application_starts_the_forgetting_clock_even_when_teaching_is_delayed(self):
        self.init_profile()
        item = self.engine.knowledge_add("지연 학습", "나중에 배운다", "concept", [], [], [], AT)
        anchor = self.engine.knowledge_add("선수 지식", "이미 안다", "concept", [], [], [], AT)
        self.engine.knowledge_relate(item["id"], anchor["id"], AT)
        taught_at = AT + timedelta(days=10)
        self.engine.teach(item["id"], why("지연 학습"), "선수 지식과 연결", taught_at)
        applied = self.engine.review(
            item["id"], "good", [], [], "complete", "어디에 적용할까?", "실제 경계",
            "설명 뒤 적용함", taught_at,
        )
        self.assertEqual(applied["last_interaction"]["phase"], "exposure")
        self.assertEqual(applied["memory"]["first_exposed_at"], applied["last_interaction"]["interacted_at"])
        self.assertEqual(self.engine.due(taught_at), [])
        self.assertEqual(self.engine.due(taught_at + timedelta(days=1))[0]["id"], item["id"])


class TutorConfidenceTests(EngineTestCase):
    def test_partial_confidence_requires_and_preserves_a_weak_point(self):
        self.init_profile()
        item = self.engine.knowledge_add("합의", "노드가 값에 동의한다.", "concept", [], [], [], AT)
        with self.assertRaisesRegex(ValueError, "requires at least one added weak point"):
            self.engine.review(
                item["id"],
                "hard",
                [],
                [],
                "partial",
                "장애 노드의 한계는?",
                "대략 알지만 정확한 수는 확실하지 않습니다.",
                "핵심 방향은 맞지만 경계가 불확실함",
                AT + timedelta(minutes=5),
            )
        reviewed = self.engine.review(
            item["id"],
            "hard",
            ["허용 가능한 장애 노드 수"],
            [],
            "partial",
            "장애 노드의 한계는?",
            "대략 알지만 정확한 수는 확실하지 않습니다.",
            "핵심 방향은 맞지만 경계가 불확실함",
            AT + timedelta(minutes=5),
        )
        self.assertEqual(reviewed["last_interaction"]["confidence"], "partial")
        self.assertEqual(reviewed["weak_points"], ["허용 가능한 장애 노드 수"])


class EditorLoopTests(EngineTestCase):
    def test_editor_findings_must_quote_the_current_learner_version(self):
        artifact = self.engine.artifact_add(
            "근거 검토", "ＡＢ 실제  본문", "근거 확인", "팀", None, AT, "learner"
        )
        with self.assertRaisesRegex(ValueError, "evidence span"):
            self.engine.artifact_review(
                artifact["id"], editor_criteria(True, "실제 본문"),
                "revise", "근거 수정", AT,
            )
        reviewed = self.engine.artifact_review(
            artifact["id"], editor_criteria(True, "AB"), "revise", "근거 수정", AT
        )
        self.assertEqual(reviewed["status"], "needs_revision")

    def test_artifact_feedback_and_revision_history_are_preserved(self):
        artifact = self.engine.artifact_add(
            "설계 문서", "초안", "운영 선택을 방어", "백엔드 팀", None, AT, "learner"
        )
        feedback = self.engine.artifact_review(
            artifact["id"],
            editor_criteria(revise=True, evidence_span="초안"),
            "revise",
            "실패 사례를 추가한다",
            AT + timedelta(minutes=5),
        )
        self.assertEqual(feedback["review_rounds"][0]["version"], 1)
        self.assertEqual(feedback["status"], "needs_revision")
        with self.assertRaisesRegex(ValueError, "substantively change"):
            self.engine.artifact_revise(
                artifact["id"], "  초안  ", AT + timedelta(minutes=9), "learner"
            )
        revised = self.engine.artifact_revise(
            artifact["id"], "근거가 포함된 수정안", AT + timedelta(minutes=10), "learner"
        )
        self.assertEqual(revised["content"], "근거가 포함된 수정안")
        self.assertEqual(revised["previous_versions"][0]["content"], "초안")
        self.assertEqual(revised["current_version"], 2)
        passed = self.engine.artifact_review(
            artifact["id"], editor_criteria(), "pass", "전달", AT + timedelta(minutes=15)
        )
        self.assertEqual(passed["status"], "passed")
        self.assertEqual([item["version"] for item in passed["review_rounds"]], [1, 2])
        self.assertEqual(passed["review_rounds"][0]["findings"][0]["status"], "resolved")
        self.assertTrue(all(item["author"] == "learner" for item in passed["versions"]))

    def test_editor_cannot_pass_findings_or_rewrite_without_a_review(self):
        artifact = self.engine.artifact_add(
            "설계 문서", "초안", "운영 선택 방어", "백엔드 팀", None, AT, "learner"
        )
        with self.assertRaisesRegex(ValueError, "only after a revise verdict"):
            self.engine.artifact_revise(
                artifact["id"], "아직 검토 전인 학습자 문장", AT, "learner"
            )
        with self.assertRaisesRegex(ValueError, "verdict must match"):
            self.engine.artifact_review(
                artifact["id"], editor_criteria(revise=True, evidence_span="초안"),
                "pass", "제출", AT
            )
        incomplete = editor_criteria()
        incomplete.pop("accuracy")
        with self.assertRaisesRegex(ValueError, "all seven criteria"):
            self.engine.artifact_review(artifact["id"], incomplete, "pass", "제출", AT)
        self.engine.artifact_review(
            artifact["id"], editor_criteria(revise=True, evidence_span="초안"),
            "revise", "학습자 수정", AT
        )
        with self.assertRaisesRegex(ValueError, "submitted by the learner"):
            self.engine.artifact_revise(
                artifact["id"], "Editor ghostwritten replacement", AT, "editor"
            )

    def test_code_indentation_is_a_substantive_learner_revision(self):
        artifact = self.engine.artifact_add(
            "코드 수정", 'if True:\nprint("x")', "실행 가능한 코드", "팀", None, AT, "learner"
        )
        self.engine.artifact_review(
            artifact["id"], editor_criteria(revise=True, evidence_span='print("x")'),
            "revise", "들여쓰기 수정", AT
        )
        revised = self.engine.artifact_revise(
            artifact["id"], 'if True:\n    print("x")', AT, "learner"
        )
        self.assertEqual(revised["current_version"], 2)

    def test_milestone_artifact_requires_each_proof_criterion(self):
        self.init_curriculum()
        artifact = self.engine.artifact_add(
            "증명 답안", "아직 지표가 없다", "milestone 증명", "팀", "load-test", AT, "learner"
        )
        with self.assertRaisesRegex(ValueError, "every milestone pass criterion"):
            self.engine.artifact_review(
                artifact["id"], editor_criteria(), "pass", "제출", AT
            )
        reviewed = self.engine.artifact_review(
            artifact["id"], editor_criteria(), "revise", "실패 경계 추가", AT,
            milestone_criteria("load-test", revise=True),
        )
        self.assertEqual(
            reviewed["review_rounds"][0]["milestone_criteria"]["실패 경계"]["status"],
            "revise",
        )

    def test_editor_records_a_resolved_finding_that_regresses(self):
        artifact = self.engine.artifact_add(
            "반복 약점 문서", "v1", "논리 검증", "팀", None, AT, "learner"
        )
        first = editor_criteria(revise=True, evidence_span="v1")
        first["structure"] = {
            "status": "revise", "note": "순서가 뒤섞임", "severity": "blocking",
            "evidence_span": "v1", "diagnosis": "근거가 결론보다 늦음",
            "revision_action": "근거를 결론 앞으로 이동",
        }
        self.engine.artifact_review(artifact["id"], first, "revise", "논리와 구조 수정", AT)
        self.engine.artifact_revise(artifact["id"], "v2", AT + timedelta(minutes=1), "learner")
        second = editor_criteria()
        second["structure"] = {**first["structure"], "evidence_span": "v2"}
        self.engine.artifact_review(
            artifact["id"], second, "revise", "구조 수정", AT + timedelta(minutes=2)
        )
        self.engine.artifact_revise(artifact["id"], "v3", AT + timedelta(minutes=3), "learner")
        regressed = self.engine.artifact_review(
            artifact["id"], editor_criteria(revise=True, evidence_span="v3"),
            "revise", "논리 재수정",
            AT + timedelta(minutes=4),
        )
        finding = regressed["review_rounds"][-1]["findings"][0]
        self.assertEqual(finding["status"], "regressed")
        self.assertTrue(finding["regression_of"].startswith("finding-"))


class OrchestratorContinuityTests(EngineTestCase):
    def test_active_and_ended_sessions_resume_from_saved_context(self):
        self.init_profile()
        fresh = self.engine.resume(AT)
        self.assertIsInstance(fresh["next_step"], str)
        session = self.engine.session_start("출근길 10분", AT)
        noted = self.engine.session_note(
            "벡터 시계까지 학습", "Lamport 시계와 비교", AT + timedelta(minutes=5)
        )
        self.assertEqual(noted["id"], session["id"])
        active = self.engine.resume()
        self.assertIsInstance(active["next_step"], str)
        self.assertEqual(active["next_step"], "Lamport 시계와 비교")
        with self.assertRaisesRegex(ValueError, "already active"):
            self.engine.session_start("중복 세션", AT + timedelta(minutes=6))

        ended = self.engine.session_end(
            "부분 순서 이해", "실제 장애 사례 적용", AT + timedelta(minutes=10)
        )
        self.assertEqual(ended["status"], "ended")
        resumed = self.engine.resume()
        self.assertIsInstance(resumed["next_step"], str)
        self.assertEqual(resumed["next_step"], "실제 장애 사례 적용")
        self.assertIsNone(self.store.load()["active_session_id"])

    def test_session_resume_tracks_the_live_workflow_step_and_handoff(self):
        workflow = self.engine.workflow_start(
            "perspective", "다른 분야로 보기", AT, perspective_spec=perspective_request()
        )
        session = self.engine.session_start(
            "출근길 관점 탐색", AT, workflow["id"]
        )
        self.assertEqual(session["current_step"]["role"], "roommate")
        self.assertIsNone(session["handoff"])

        handoff = self.engine.workflow_next(workflow["id"], "현재 문제", AT)["handoff"]
        resumed = self.engine.resume(AT)
        self.assertEqual(resumed["workflow"]["id"], workflow["id"])
        self.assertEqual(resumed["current_step"]["status"], "dispatched")
        self.assertEqual(resumed["handoff"]["id"], handoff["id"])

    def test_resume_supersedes_work_bound_to_an_old_learning_target(self):
        self.init_profile()
        workflow = self.engine.workflow_start(
            "perspective", "옛 전공 관점", AT, perspective_spec=perspective_request("합의")
        )
        handoff = self.engine.workflow_next(workflow["id"], "옛 전공", AT)["handoff"]
        self.engine.handoff_claim(handoff["id"], "roommate", AT)
        self.engine.session_start("옛 전공 세션", AT, workflow["id"])
        self.engine.advisor_init("프랑스 문학 연구", "입문", ["시 분석"], 0.9, AT)
        resumed = self.engine.resume(AT)
        state = self.store.load()
        self.assertNotIn("workflow", resumed)
        self.assertEqual(state["workflows"][0]["status"], "superseded")
        self.assertEqual(state["handoffs"][0]["status"], "cancelled")
        self.assertIn("current learning target", resumed["next_step"])

    def test_resume_preserves_the_plan_that_is_changing_the_target(self):
        self.init_profile()
        workflow = self.engine.workflow_start("plan", "새 전공 설계", AT)
        handoff = self.engine.workflow_next(workflow["id"], "새 전공", AT)["handoff"]
        self.engine.handoff_claim(handoff["id"], "advisor", AT)
        self.engine.session_start("전공 변경 중", AT, workflow["id"])
        self.engine.advisor_init("프랑스 문학 연구", "입문", ["시 분석"], 0.9, AT)
        resumed = self.engine.resume(AT)
        self.assertEqual(resumed["workflow"]["status"], "active")
        self.assertEqual(resumed["handoff"]["status"], "in_progress")


class RoommatePerspectiveTests(EngineTestCase):
    def test_roommate_records_a_cross_field_mapping_and_its_limit(self):
        self.init_profile()
        perspective = self.engine.perspective_start(
            "분산 시스템", "백프레셔", "도시 교통", "진입 램프가 포화를 늦춘다",
            "요청 진입률은 어디서 제한해야 할까?", AT,
        )
        answered = self.engine.perspective_answer(
            perspective["id"], "게이트웨이에서 제한한다", "insight",
            "입구 제어가 하류를 보호한다", "램프는 게이트웨이, 도로는 처리 파이프라인이다",
            "재시도 폭주는 이 비유만으로 설명되지 않는다", [], AT + timedelta(minutes=1),
        )
        self.assertEqual(answered["connection"]["status"], "insight")
        self.assertIn("재시도", answered["connection"]["limits"])

    def test_roommate_is_not_a_checkpoint_or_an_answer_generator(self):
        self.init_profile()
        with self.assertRaisesRegex(ValueError, "outside field must differ"):
            self.engine.perspective_start(
                "분산 시스템", "합의", "분산 시스템", "다른 구현", "무엇이 닮았을까?", AT
            )
        with self.assertRaisesRegex(ValueError, "outside field must differ"):
            self.engine.perspective_start(
                "분산 시스템", "합의", "분산-시스템", "표기만 변경", "무엇이 닮았을까?", AT
            )
        with self.assertRaisesRegex(ValueError, "one connection question"):
            self.engine.perspective_start(
                "분산 시스템", "합의", "재즈", "리듬", "무엇이 같나? 어디서 깨지나？", AT
            )
        perspective = self.engine.perspective_start(
            "분산 시스템", "합의", "재즈 합주", "리듬 합의", "리더 없이 어떻게 맞출까?", AT
        )
        with self.assertRaisesRegex(ValueError, "learner response is required"):
            self.engine.perspective_answer(
                perspective["id"], "", "insight", "답", "매핑", "한계", [], AT
            )
        with self.assertRaisesRegex(ValueError, "pending Roommate question"):
            self.engine.perspective_start(
                "분산 시스템", "복제", "생태학", "종 다양성", "어디서 회복력이 생길까?", AT
            )

    def test_roommate_does_not_repeat_an_answered_lens_and_question(self):
        perspective = self.engine.perspective_start(
            "분산 시스템", "합의", "재즈 합주", "리듬 동기화",
            "리더 없이 어떻게 맞출까?", AT,
        )
        self.engine.perspective_answer(
            perspective["id"], "서로 듣고 조정한다", "no_connection", "", "", "", [], AT
        )
        with self.assertRaisesRegex(ValueError, "new lens or connection question"):
            self.engine.perspective_start(
                "분산 시스템", "합의", "재즈 합주", "리듬 동기화",
                "리더 없이 어떻게 맞출까?", AT,
            )


class MaintenanceAcrossSubjectsTests(EngineTestCase):
    """전진은 한 전공, 유지는 모든 전공."""

    def _taught_backend_item(self):
        taught_at = AT - timedelta(days=10)
        applied_at = AT - timedelta(days=9)
        self.engine.advisor_init("백엔드 전문가", "중급", ["백프레셔"], 0.9, taught_at)
        anchor = self.engine.knowledge_add(
            "이벤트 루프", "이미 아는 실행 기준", "concept", [], [], [], taught_at
        )
        item = self.engine.knowledge_add(
            "백프레셔", "생산 속도를 소비 속도에 맞춘다", "concept", [], [], [], taught_at
        )
        self.engine.knowledge_relate(item["id"], anchor["id"], taught_at)
        self.engine.teach(item["id"], why("백프레셔"), "이벤트 루프와 연결", taught_at)
        self.engine.review(
            item["id"], "good", [], [], "complete", "어디에 적용할까?",
            "입구에서 생산률을 제한한다", "설명과 다른 사례에 적용했다", applied_at,
        )
        return item, anchor

    def test_switching_goals_keeps_old_subject_knowledge_due_and_reviewable(self):
        item, _ = self._taught_backend_item()
        self.engine.advisor_init(
            "퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, AT - timedelta(days=8)
        )
        self.assertIn(item["id"], [entry["id"] for entry in self.engine.due(AT)])
        self.assertEqual(
            self.engine.route("review", AT)["workflow"], ["advisor", "tutor", "advisor"]
        )
        self.assertEqual(
            self.engine.route("learn", AT)["workflow"],
            ["advisor", "librarian", "tutor", "advisor"],
        )
        retrieved = self.engine.review(
            item["id"], "good", [], [], "complete", "간격 뒤 다시 설명하세요",
            "소비 속도보다 빠른 유입을 경계에서 제한한다", "자료 없이 독립 인출했다", AT,
        )
        self.assertEqual(retrieved["last_interaction"]["phase"], "retrieval")
        self.assertEqual(retrieved["memory"]["review_count"], 1)
        self.assertNotIn(
            item["id"], [entry["id"] for entry in self.engine.tutor_context()["knowledge"]]
        )

    def test_returning_to_an_archived_goal_restores_focus_and_knowledge(self):
        item, anchor = self._taught_backend_item()
        self.engine.advisor_init("퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, AT)
        profile = self.engine.advisor_init(
            "백엔드 전문가", "", [], 0.9, AT + timedelta(minutes=1)
        )
        self.assertEqual(profile["focus"], ["백프레셔"])
        context_ids = [entry["id"] for entry in self.engine.tutor_context()["knowledge"]]
        self.assertIn(item["id"], context_ids)
        self.assertIn(anchor["id"], context_ids)
        self.assertTrue(all(entry["active"] for entry in self.engine.knowledge()))

    def test_maintenance_teach_anchors_to_its_own_subject(self):
        item, anchor = self._taught_backend_item()
        switch_at = AT - timedelta(days=8)
        self.engine.advisor_init("퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, switch_at)
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.engine.advisor_interview(
                decision, f"{decision} 질문?", f"{decision}에 대한 실제 답", switch_at
            )
        self.engine.advisor_curriculum(json.loads(json.dumps(CURRICULUM)), switch_at)
        current = self.engine.knowledge_add(
            "포지션 한도", "손실 한도로 크기를 정한다", "concept", [], [], [], switch_at
        )
        anchored = self.engine.teach(
            current["id"], why("포지션 한도"), "bounded queue 구현과 연결", switch_at
        )
        self.assertEqual(
            anchored["last_teaching"]["connection_basis"]["kind"], "curriculum_baseline"
        )
        with self.assertRaisesRegex(ValueError, "exactly one related knowledge or curriculum baseline"):
            self.engine.teach(item["id"], why("백프레셔"), "bounded queue 구현과 연결", AT)
        maintained = self.engine.teach(item["id"], why("백프레셔"), "이벤트 루프와 연결", AT)
        self.assertEqual(
            maintained["last_teaching"]["connection_basis"],
            {"kind": "knowledge", "id": anchor["id"]},
        )

    def test_remedial_goal_is_allowed_for_maintenance_knowledge(self):
        item, _ = self._taught_backend_item()
        self.engine.advisor_init(
            "퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, AT - timedelta(days=8)
        )
        goal = self.engine.learning_goal_add(
            "재학습: 백프레셔", "자료 없이 설명하고 새 사례에 적용한다",
            "만기 인출에서 예상 기억률이 낮다", "remedial", 5, item["id"], AT,
        )
        self.assertEqual(goal["kind"], "remedial")
        self.assertEqual(goal["knowledge_id"], item["id"])

    def test_roommate_can_use_another_stored_subject_as_lens(self):
        self._taught_backend_item()
        self.engine.advisor_init("퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, AT)
        route = self.engine.route("perspective", AT)
        self.assertIn("백엔드 전문가", route["other_majors"])
        perspective = self.engine.perspective_start(
            "퀀트 투자", "포지션 사이징", "백엔드 전문가", "백프레셔의 유입 제한",
            "포지션 한도는 백프레셔의 유입 제한과 어디까지 같은가?", AT,
        )
        self.assertEqual(perspective["outside_field"], "백엔드 전문가")

    def test_recall_surfaces_related_due_knowledge_with_a_casual_prompt(self):
        item, _ = self._taught_backend_item()
        self.engine.advisor_init(
            "퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, AT - timedelta(days=8)
        )
        related = self.engine.recall_candidates("주문 유입 백프레셔 제어", AT)
        self.assertEqual([entry["id"] for entry in related], [item["id"]])
        self.assertIn("저번에 배운", related[0]["prompt"])
        self.assertEqual(self.engine.recall_candidates("마케팅 퍼널 전환율", AT), [])

    def test_review_intent_requires_a_due_retrieval(self):
        self.init_profile()
        route = self.engine.route("review", AT)
        self.assertEqual(route["workflow"], [])
        with self.assertRaisesRegex(ValueError, "no due retrieval"):
            self.engine.workflow_start("review", "지금 복습", AT)

    def test_review_workflow_targets_the_due_item(self):
        item, _ = self._taught_backend_item()
        self.engine.advisor_init(
            "퀀트 투자 연구자", "입문", ["포지션 사이징"], 0.9, AT - timedelta(days=8)
        )
        workflow = self.engine.workflow_start("review", "저번 것 이어서 복습", AT)
        tutor_step = workflow["steps"][1]
        self.assertEqual(tutor_step["output_kind"], "retrieval_knowledge")
        self.assertEqual(tutor_step["expected_resource_ids"], [item["id"]])

    def test_previously_deactivated_bound_knowledge_is_restored_on_load(self):
        self._taught_backend_item()
        raw = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        bound, legacy = raw["knowledge"]
        bound["active"] = False
        legacy["active"] = False
        del legacy["subject_binding"]
        self.store.state_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        loaded = self.store.load()
        restored = {item["id"]: item for item in loaded["knowledge"]}
        self.assertTrue(restored[bound["id"]]["active"])
        self.assertFalse(restored[legacy["id"]]["active"])
        self.assertTrue(restored[legacy["id"]]["migration_import_required"])


class RoutingTests(EngineTestCase):
    def test_ready_learning_state_routes_directly_to_tutor(self):
        self.assertEqual(self.engine.route("learn", AT)["role"], "advisor")
        self.init_profile()
        self.assertEqual(self.engine.route("learn", AT)["role"], "advisor")
        self.init_curriculum()
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")
        self.ready_shelf()
        self.assertEqual(self.engine.route("learn", AT)["role"], "tutor")
        self.assertEqual(self.engine.route("artifact", AT)["role"], "editor")
        self.assertEqual(self.engine.route("perspective", AT)["role"], "roommate")
        self.assertEqual(self.engine.route("resume", AT)["role"], "orchestrator")


class OrchestratorWorkflowTests(EngineTestCase):
    def test_due_advisor_step_requires_a_remedial_goal_for_the_due_knowledge(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "만기 원리", "간격 뒤 다시 꺼낸다", "concept", [], [], [materials[0]["id"]], AT
        )
        prepare_retrieval(self.engine, knowledge)
        later = AT + timedelta(days=2)
        workflow = self.engine.workflow_start("review", "만기 복습", later)
        handoff = self.engine.workflow_next(workflow["id"], "복습 목표", later)["handoff"]
        self.engine.handoff_claim(handoff["id"], "advisor", later)
        practical = self.engine.learning_goal_add(
            "일반 적용", "적용한다", "만기와 무관한 종류", "practical", 5,
            knowledge["id"], later,
        )
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(
                handoff["id"], "advisor",
                {"summary": "일반 목표", "resource_ids": [practical["id"]]}, later,
            )

    def test_final_advisor_must_record_the_tutor_observation(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        workflow = self.engine.workflow_start("learn", "새 원리 학습", AT)
        tutor = self.engine.workflow_next(workflow["id"], "교육", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        knowledge = self.engine.knowledge_add(
            "새 원리", "실제 경계를 선택한다", "concept", ["경계 근거"], [],
            [materials[0]["id"]], AT,
        )
        self.engine.teach(knowledge["id"], why("경계"), "이벤트 루프 순서 설명과 연결", AT)
        self.engine.review(
            knowledge["id"], "hard", ["경계 근거"], [], "partial", "어떤 경계인가?",
            "실측 근거가 부족함", "원리는 알지만 적용 근거가 약함", AT,
        )
        self.engine.handoff_complete(
            tutor["id"], "tutor", {
                "summary": "교육", "resource_ids": [knowledge["id"]],
                "next_role": "advisor",
                "observations": ["경계 선택 근거가 부족함"],
                "recommendations": ["실측 근거로 경계를 선택"],
            }, AT,
        )
        advisor = self.engine.workflow_next(workflow["id"], "관찰 반영", AT)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", AT)
        goal = self.engine.learning_goal_add(
            "경계 근거 보강", "실측값으로 경계를 선택", "Tutor 관찰", "practical", 5,
            knowledge["id"], AT,
        )
        result = {"summary": "경로 갱신", "resource_ids": [goal["id"]]}
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(advisor["id"], "advisor", result, AT)
        self.engine.advisor_observe(
            "원리 이해, 경계 적용은 미숙", "경계 선택 근거가 부족함", AT
        )
        self.assertEqual(
            self.engine.handoff_complete(advisor["id"], "advisor", result, AT)["status"],
            "completed",
        )

    def test_one_tutor_observation_cannot_be_consumed_by_two_advisor_updates(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "소비율 한계", "소비율이 처리 한계를 만든다", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        tutor = self.engine.handoff_dispatch("tutor", "실제 적용 관찰", "", AT)
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        self.engine.teach(
            knowledge["id"], why("소비율 한계"), "이벤트 루프 순서 설명", AT
        )
        self.engine.review(
            knowledge["id"], "hard", ["용량 근거"], [], "partial",
            "용량을 어떻게 고르나요?", "수치는 도움 필요",
            "원리는 알지만 적용 근거가 부족함", AT,
        )
        observation = "원리는 설명하지만 용량 근거는 도움 필요"
        self.engine.handoff_complete(
            tutor["id"], "tutor",
            {
                "summary": "적용 관찰", "resource_ids": [knowledge["id"]],
                "next_role": "advisor", "observations": [observation],
                "recommendations": ["실측값으로 용량을 계산"],
            }, AT,
        )
        first = self.engine.handoff_dispatch(
            "advisor", "첫 경로 갱신", "", AT, depends_on=[tutor["id"]],
            output_kind="advisor_update",
        )
        second = self.engine.handoff_dispatch(
            "advisor", "둘째 경로 갱신", "", AT, depends_on=[tutor["id"]],
            output_kind="advisor_update",
        )
        self.engine.handoff_claim(first["id"], "advisor", AT)
        profile = self.engine.advisor_observe("적용 근거 보강 필요", observation, AT)
        evidence = profile["level_evidence"][-1]
        self.assertEqual(evidence["source_handoff_id"], tutor["id"])
        self.assertEqual(evidence["produced_by_handoff_id"], first["id"])
        goal = self.engine.learning_goal_add(
            "용량 근거 보강", "실측값으로 용량을 결정", "Tutor 관찰", "practical", 5,
            knowledge["id"], AT,
        )
        self.engine.handoff_complete(
            first["id"], "advisor",
            {"summary": "첫 갱신", "resource_ids": [goal["id"]]}, AT,
        )
        self.engine.handoff_claim(second["id"], "advisor", AT)
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.engine.advisor_observe("동일 관찰 재사용", observation, AT)

    def test_retention_change_supersedes_an_inflight_tutor_mode(self):
        self.engine.advisor_init("분산 시스템 전문가", "중급", ["동시성"], 0.8, AT)
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "기억 경계", "보존율에 따라 만기가 달라진다", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        prepare_retrieval(self.engine, knowledge)
        later = AT + timedelta(days=1)
        workflow = self.engine.workflow_start("learn", "새 학습", later)
        tutor = self.engine.workflow_next(workflow["id"], "교육", later)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", later)
        self.engine.advisor_init("분산 시스템 전문가", "중급", ["동시성"], 0.95, later)
        self.assertEqual([item["id"] for item in self.engine.due(later)], [knowledge["id"]])
        self.assertEqual(self.engine.workflow_show(workflow["id"])["status"], "superseded")
        self.engine.teach(knowledge["id"], why("보존율"), "이벤트 루프 순서 설명과 연결", later)
        with self.assertRaisesRegex(ValueError, "status cancelled"):
            self.engine.handoff_complete(
                tutor["id"], "tutor", {
                    "summary": "예전 모드 교육", "resource_ids": [knowledge["id"]],
                    "observations": ["보존율 변경"], "recommendations": ["새 인출 흐름"],
                }, later,
            )

    def test_due_retrieval_cannot_teach_the_answer_before_the_attempt(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "인출 순서", "먼저 답을 꺼낸다", "concept", [], [], [materials[0]["id"]], AT
        )
        prepare_retrieval(self.engine, knowledge)
        later = AT + timedelta(days=2)
        workflow = self.engine.workflow_start("review", "지연 인출", later)
        advisor = self.engine.workflow_next(workflow["id"], "복습 목표", later)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", later)
        goal = self.engine.learning_goal_add(
            "인출 순서 복원", "설명 없이 먼저 답한다", "망각 위험", "remedial", 5,
            knowledge["id"], later,
        )
        self.engine.handoff_complete(
            advisor["id"], "advisor", {"summary": "복습 목표", "resource_ids": [goal["id"]]}, later
        )
        tutor = self.engine.workflow_next(workflow["id"], "독립 인출", later)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", later)
        self.engine.teach(knowledge["id"], why("인출 순서"), "이벤트 루프 순서 설명과 연결", later)
        self.engine.review(
            knowledge["id"], "good", [], [], "complete", "무엇이 먼저인가?",
            "인출이 먼저", "공개된 답 뒤에 응답함", later,
        )
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(
                tutor["id"], "tutor", {
                    "summary": "순서 우회", "resource_ids": [knowledge["id"]],
                    "observations": ["답 공개 뒤 응답"], "recommendations": ["힌트 없이 재시도"],
                }, later,
            )

    def test_pre_due_weak_point_uses_teaching_not_retrieval_mode(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "만기 전 약점", "아직 첫 인출 전이다", "concept", ["적용 경계"], [],
            [materials[0]["id"]], AT,
        )
        workflow = self.engine.workflow_start("learn", "약점 보강", AT)
        tutor_step = workflow["steps"][1]
        self.assertEqual(tutor_step["output_kind"], "step_knowledge")
        self.assertEqual(tutor_step["expected_resource_ids"], [knowledge["id"]])
        advisor = self.engine.workflow_next(workflow["id"], "보강 목표", AT)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", AT)
        goal = self.engine.learning_goal_add(
            "적용 경계 보강", "새 사례에 경계를 적용", "저장된 약점", "practical", 5,
            knowledge["id"], AT,
        )
        self.engine.handoff_complete(
            advisor["id"], "advisor", {"summary": "보강 목표", "resource_ids": [goal["id"]]}, AT
        )
        tutor = self.engine.workflow_next(workflow["id"], "가르치기", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        self.engine.teach(knowledge["id"], why("경계"), "이벤트 루프 순서 설명과 연결", AT)
        self.engine.review(
            knowledge["id"], "hard", ["적용 경계"], [], "partial", "새 사례의 경계는?",
            "도움이 더 필요함", "적용 약점을 확인", AT,
        )
        completed = self.engine.handoff_complete(
            tutor["id"], "tutor", {
                "summary": "설명과 적용", "resource_ids": [knowledge["id"]],
                "next_role": "advisor",
                "observations": ["적용 경계는 아직 도움 필요"],
                "recommendations": ["다른 사례로 전이"],
            }, AT,
        )
        self.assertEqual(completed["status"], "completed")

    def test_tutor_must_declare_every_changed_knowledge_but_relation_counterpart_is_allowed(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        existing = self.engine.knowledge_add(
            "기존 큐", "준비 작업을 담는다", "concept", [], [], [materials[0]["id"]], AT
        )
        workflow = self.engine.workflow_start("learn", "연결 학습", AT)
        tutor = self.engine.workflow_next(workflow["id"], "교육", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        connected = self.engine.knowledge_add(
            "새 경계", "큐의 상한을 정한다", "concept", [], [existing["id"]],
            [materials[0]["id"]], AT,
        )
        hidden = self.engine.knowledge_add(
            "숨긴 변경", "별도 원리", "concept", [], [], [materials[0]["id"]], AT
        )
        self.engine.teach(connected["id"], why("연결 경계"), "이벤트 루프 순서 설명과 연결", AT)
        self.engine.teach(hidden["id"], why("숨은 개념"), "이벤트 루프 순서 설명과 연결", AT)
        self.engine.review(
            connected["id"], "good", [], [], "complete", "어디에 적용할까?",
            "큐 상한에 적용", "별개 사례 적용", AT,
        )
        self.engine.review(
            hidden["id"], "good", [], [], "complete", "어디에 적용할까?",
            "다른 사례", "별개 사례 적용", AT,
        )
        result = {
            "summary": "연결 교육", "resource_ids": [connected["id"]],
            "observations": ["경계 근거"], "recommendations": ["실제 큐 적용"],
        }
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(tutor["id"], "tutor", result, AT)

    def test_each_intent_routes_to_the_role_that_owns_the_output(self):
        expected = {
            "plan": "advisor",
            "artifact": "editor",
            "write": "editor",
            "perspective": "roommate",
            "resume": "orchestrator",
        }
        for intent, role in expected.items():
            self.assertEqual(self.engine.route(intent, AT)["role"], role)
        self.assertEqual(self.engine.route("material", AT)["role"], "advisor")
        self.assertEqual(self.engine.route("learn", AT)["role"], "advisor")
        self.init_curriculum()
        self.assertEqual(self.engine.route("material", AT)["role"], "librarian")

    def test_first_advisor_step_requires_a_curriculum_not_an_unrelated_goal(self):
        self.init_profile()
        goal = self.engine.learning_goal_add(
            "무관한 단어", "단어를 외운다", "우회 시도", "practical", 1, None, AT
        )
        workflow = self.engine.workflow_start("learn", "새 전공 시작", AT)
        handoff = self.engine.workflow_next(workflow["id"], "", AT)["handoff"]
        self.engine.handoff_claim(handoff["id"], "advisor", AT)
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                handoff["id"], "advisor",
                {"summary": "목표 하나", "resource_ids": [goal["id"]]}, AT,
            )
        curriculum = self.init_curriculum()
        completed = self.engine.handoff_complete(
            handoff["id"], "advisor",
            {"summary": "경로 완성", "resource_ids": [curriculum["id"]]}, AT,
        )
        self.assertEqual(completed["status"], "completed")

    def test_parallel_workflow_cannot_claim_the_same_specialist_twice(self):
        self.init_profile()
        first_workflow = self.engine.workflow_start("learn", "요청 A", AT)
        second_workflow = self.engine.workflow_start("learn", "요청 B", AT)
        first = self.engine.workflow_next(first_workflow["id"], "A", AT)["handoff"]
        second = self.engine.workflow_next(second_workflow["id"], "B", AT)["handoff"]
        self.engine.handoff_claim(first["id"], "advisor", AT)
        with self.assertRaisesRegex(ValueError, "already has an in-progress handoff"):
            self.engine.handoff_claim(second["id"], "advisor", AT)
        self.assertEqual(self.engine.handoff_list("in_progress")[0]["id"], first["id"])
        self.assertEqual(
            self.engine._find(self.store.load()["handoffs"], second["id"], "handoff")["status"],
            "pending",
        )

    def test_workflow_enforces_order_dependencies_and_real_role_outputs(self):
        curriculum = self.init_curriculum()
        workflow = self.engine.workflow_start("learn", "백프레셔를 실제 설계에 적용", AT)
        self.assertEqual(
            [step["role"] for step in workflow["steps"]],
            ["librarian", "tutor", "advisor"],
        )

        first = self.engine.workflow_next(workflow["id"], "현재 단계 자료", AT)["handoff"]
        self.engine.handoff_claim(first["id"], "librarian", AT)
        with self.assertRaisesRegex(ValueError, "real role resource id"):
            self.engine.handoff_complete(
                first["id"], "librarian", {"summary": "완료"}, AT
            )
        untriaged_source = Path(self.temporary.name) / "untriaged.txt"
        untriaged_source.write_text("source", encoding="utf-8")
        untriaged = self.engine.material_add(
            "미판정 자료", str(untriaged_source), "", "원문 확인", AT
        )
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(
                first["id"], "librarian",
                {"summary": "자료 하나", "resource_ids": [untriaged["id"]]}, AT,
            )
        materials, shelf = self.ready_shelf()
        self.engine.material_curate(
            untriaged["id"], curation(curriculum["id"], "reject"), AT
        )
        shelf = self.engine.material_shelf(
            curriculum["id"], "measure",
            [item["id"] for item in materials] + [untriaged["id"]], AT,
        )
        self.engine.handoff_complete(
            first["id"], "librarian",
            {"summary": "선반 준비", "resource_ids": [shelf["id"]]}, AT,
        )

        second = self.engine.workflow_next(workflow["id"], "선별 자료", AT)["handoff"]
        self.assertEqual(second["depends_on"], [first["id"]])
        self.engine.handoff_claim(second["id"], "tutor", AT)
        knowledge = self.engine.knowledge_add(
            "백프레셔", "생산률을 소비 가능량에 맞춘다", "concept", ["용량 선택"], [],
            [materials[0]["id"]], AT,
        )
        self.engine.teach(
            knowledge["id"], why("백프레셔"), "이벤트 루프 큐", AT
        )
        self.engine.review(
            knowledge["id"], "hard", ["용량 선택"], [], "partial", "용량은 어떻게 정할까?",
            "실측값이 더 필요하다", "원리는 알지만 수치 근거가 약함", AT,
        )
        self.engine.handoff_complete(
            second["id"], "tutor",
            {
                "summary": "학습 기록", "resource_ids": [knowledge["id"]],
                "next_role": "advisor",
                "observations": ["용량 선택 근거가 부족함"],
                "recommendations": ["실측 처리량으로 용량을 계산"],
            }, AT,
        )

        third = self.engine.workflow_next(workflow["id"], "Tutor 관찰", AT)["handoff"]
        self.assertIn("학습 기록", third["context"])
        self.engine.handoff_claim(third["id"], "advisor", AT)
        self.engine.advisor_observe(
            "원리는 이해하지만 용량 선택 근거는 부족함", "용량 선택 근거가 부족함", AT
        )
        goal = self.engine.learning_goal_add(
            "용량 근거 보강", "실측값으로 용량을 선택한다", "Tutor의 관찰", "practical", 5,
            knowledge["id"], AT,
        )
        self.engine.handoff_complete(
            third["id"], "advisor",
            {"summary": "경로 갱신", "resource_ids": [goal["id"]]}, AT,
        )
        finished = self.engine.workflow_show(workflow["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertTrue(all(step["status"] == "completed" for step in finished["steps"]))

    def test_manual_handoff_cannot_be_claimed_before_its_dependency(self):
        first = self.engine.handoff_dispatch(
            "advisor", "계획", "", AT, output_kind="curriculum"
        )
        second = self.engine.handoff_dispatch(
            "librarian", "자료", "", AT, depends_on=[first["id"]]
        )
        with self.assertRaisesRegex(ValueError, "dependencies are incomplete"):
            self.engine.handoff_claim(second["id"], "librarian", AT)

    def test_manual_tutor_handoff_cannot_bypass_curriculum_and_source_shelf(self):
        self.init_profile()
        with self.assertRaisesRegex(ValueError, "cannot bypass"):
            self.engine.handoff_dispatch("tutor", "새 지식 교육", "", AT)

    def test_manual_advisor_goal_can_unlock_its_matching_tutor_dependency(self):
        self.init_profile()
        knowledge = self.engine.knowledge_add(
            "약한 경계", "경계를 고른다", "concept", ["선택 근거"], [], [], AT
        )
        advisor = self.engine.handoff_dispatch(
            "advisor", "보강 목표", "", AT, output_kind="advisor_update"
        )
        self.engine.handoff_claim(advisor["id"], "advisor", AT)
        goal = self.engine.learning_goal_add(
            "경계 보강", "선택 근거를 설명한다", "저장된 혼동", "remedial", 5,
            knowledge["id"], AT,
        )
        self.engine.handoff_complete(
            advisor["id"], "advisor",
            {"summary": "보강 목표", "resource_ids": [goal["id"]]}, AT,
        )
        tutor = self.engine.handoff_dispatch(
            "tutor", "보강 교육", "", AT, depends_on=[advisor["id"]]
        )
        self.assertEqual(
            self.engine.handoff_claim(tutor["id"], "tutor", AT)["status"], "in_progress"
        )

    def test_one_role_output_cannot_complete_two_parallel_handoffs(self):
        spec = {
            "current_field": "분산 시스템", "current_problem": "과부하",
        }
        first = self.engine.handoff_dispatch(
            "roommate", "관점 A", "", AT, perspective_spec=spec
        )
        second = self.engine.handoff_dispatch(
            "roommate", "관점 B", "", AT, perspective_spec=spec
        )
        self.engine.handoff_claim(first["id"], "roommate", AT)
        with self.assertRaisesRegex(ValueError, "already has an in-progress handoff"):
            self.engine.handoff_claim(second["id"], "roommate", AT)
        perspective = self.engine.perspective_start(
            "분산 시스템", "과부하", "교통 공학", "램프 미터링",
            "진입량 제어와 어떤 원리가 닮았나요?", AT, first["id"],
        )
        self.engine.perspective_answer(
            perspective["id"], "입구에서 제한한다", "insight", "진입 제어",
            "램프와 게이트웨이", "재시도 정책은 다르다", [], AT,
        )
        self.engine.handoff_complete(
            first["id"], "roommate",
            {"summary": "첫 관점", "resource_ids": [perspective["id"]]}, AT,
        )
        self.engine.handoff_claim(second["id"], "roommate", AT)
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                second["id"], "roommate",
                {"summary": "중복 관점", "resource_ids": [perspective["id"]]}, AT,
            )

    def test_dependent_handoff_cannot_reuse_its_predecessors_output(self):
        self.init_profile()
        first = self.engine.handoff_dispatch(
            "advisor", "첫 갱신", "", AT, output_kind="advisor_update"
        )
        second = self.engine.handoff_dispatch(
            "advisor", "둘째 갱신", "", AT, depends_on=[first["id"]],
            output_kind="advisor_update",
        )
        self.engine.handoff_claim(first["id"], "advisor", AT)
        goal = self.engine.learning_goal_add(
            "첫 목표", "첫 결과", "첫 근거", "practical", 3, None, AT
        )
        self.engine.handoff_complete(
            first["id"], "advisor",
            {"summary": "첫 완료", "resource_ids": [goal["id"]]}, AT,
        )
        self.engine.handoff_claim(second["id"], "advisor", AT)
        claimed = self.engine.handoff_list("in_progress")[0]
        self.assertEqual(claimed["dependency_context"][0]["handoff_id"], first["id"])
        self.assertEqual(
            claimed["dependency_context"][0]["result"]["resource_ids"], [goal["id"]]
        )
        self.assertIn("Completed dependency results", claimed["context"])
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                second["id"], "advisor",
                {"summary": "재사용", "resource_ids": [goal["id"]]}, AT,
            )

    def test_future_and_previous_version_shelves_do_not_unlock_tutor(self):
        curriculum = self.init_curriculum()
        future_materials = []
        for index in range(3):
            source = Path(self.temporary.name) / f"future-{index}.txt"
            source.write_text(f"future step {index}", encoding="utf-8")
            material = self.engine.material_add(
                f"미래 자료 {index}", str(source), "bound 단계", "원문 확인", AT
            )
            future_materials.append(self.engine.material_curate(
                material["id"], curation(curriculum["id"], step_id="bound"), AT
            ))
        shelf = self.engine.material_shelf(
            curriculum["id"], "bound", [item["id"] for item in future_materials], AT
        )
        self.assertEqual(shelf["status"], "ready")
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")

        revised = json.loads(json.dumps(CURRICULUM))
        revised["destination"]["capabilities"] = ["v2 운영 판단"]
        self.assertEqual(self.engine.advisor_curriculum(revised, AT)["version"], 2)
        self.assertEqual(self.engine.route("learn", AT)["role"], "librarian")
        with self.assertRaisesRegex(ValueError, "unselected sources"):
            self.engine.knowledge_add(
                "낡은 자료 기반 지식", "이전 경로 설명", "concept", [], [],
                [future_materials[0]["id"]], AT,
            )

    def test_completed_path_replans_and_explicit_review_still_runs(self):
        curriculum = self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "오래된 혼동", "간격 뒤 다시 꺼낼 내용", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        prepare_retrieval(self.engine, knowledge)
        first_proof = self.engine.artifact_add(
            "첫 단계 증거", "측정·대안·실패", "측정 증명", "팀",
            "load-test", AT, "learner",
        )
        self.engine.artifact_review(
            first_proof["id"], editor_criteria(), "pass", "제출", AT,
            milestone_criteria("load-test"),
        )
        self.engine.advisor_milestone("load-test", first_proof["id"], AT)
        second_proof = self.engine.artifact_add(
            "둘째 단계 증거", "용량·정책·지표", "설계 증명", "팀",
            "queue-design", AT, "learner",
        )
        self.engine.artifact_review(
            second_proof["id"], editor_criteria(), "pass", "제출", AT,
            milestone_criteria("queue-design"),
        )
        self.engine.advisor_milestone("queue-design", second_proof["id"], AT)

        material_route = self.engine.route("material", AT)
        self.assertEqual(material_route["workflow"], ["advisor", "librarian"])
        material_workflow = self.engine.workflow_start("material", "새 단계 자료", AT)
        self.assertEqual(material_workflow["steps"][0]["output_kind"], "curriculum")

        later = AT + timedelta(days=2)
        review_route = self.engine.route("review", later)
        self.assertEqual(review_route["workflow"], ["advisor", "tutor", "advisor"])
        retrieval = self.engine.workflow_start("review", "기억 복원", later)
        tutor_step = retrieval["steps"][1]
        self.assertEqual(tutor_step["output_kind"], "retrieval_knowledge")
        self.assertEqual(tutor_step["expected_resource_ids"], [knowledge["id"]])

    def test_retrieval_step_rejects_a_different_recently_touched_knowledge_id(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        expected = self.engine.knowledge_add(
            "A 만기 지식", "반드시 복습할 내용", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        prepare_retrieval(self.engine, expected)
        unrelated = self.engine.knowledge_add(
            "Z 다른 지식", "이번 복습 대상이 아님", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        later = AT + timedelta(days=2)
        unrelated_goal = self.engine.learning_goal_add(
            "프랑스 시 암송", "시를 외운다", "무관한 목표", "practical", 1,
            None, later,
        )
        workflow = self.engine.workflow_start("review", "만기 복습", later)
        first = self.engine.workflow_next(workflow["id"], "복습 목표", later)["handoff"]
        self.engine.handoff_claim(first["id"], "advisor", later)
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                first["id"], "advisor",
                {"summary": "무관한 목표", "resource_ids": [unrelated_goal["id"]]}, later,
            )
        goal = self.engine.learning_goal_add(
            "만기 지식 복원", "자료 없이 설명", "예상 기억률 저하", "remedial", 5,
            expected["id"], later,
        )
        self.engine.handoff_complete(
            first["id"], "advisor", {"summary": "복습 목표", "resource_ids": [goal["id"]]}, later
        )
        second = self.engine.workflow_next(workflow["id"], "지연 인출", later)["handoff"]
        self.engine.handoff_claim(second["id"], "tutor", later)
        self.engine.teach(unrelated["id"], why("무관한 지식"), "이벤트 루프 순서 설명과 연결", later)
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(
                second["id"], "tutor",
                {"summary": "엉뚱한 결과", "resource_ids": [unrelated["id"]]}, later,
            )

    def test_tutor_cannot_complete_by_readding_identical_knowledge(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        existing = self.engine.knowledge_add(
            "기존 지식", "이미 저장된 설명", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        workflow = self.engine.workflow_start("learn", "실제 학습", AT)
        tutor = self.engine.workflow_next(workflow["id"], "가르치기", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        unchanged = self.engine.knowledge_add(
            "기존 지식", "이미 저장된 설명", "concept", [], [],
            [materials[0]["id"]], AT + timedelta(minutes=1),
        )
        self.assertEqual(unchanged["updated_at"], existing["updated_at"])
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                tutor["id"], "tutor",
                {"summary": "변경 없음", "resource_ids": [existing["id"]]}, AT,
            )

    def test_new_learning_requires_teaching_and_retrieval_requires_review(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        workflow = self.engine.workflow_start("learn", "새 원리 학습", AT)
        tutor = self.engine.workflow_next(workflow["id"], "가르치기", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        knowledge = self.engine.knowledge_add(
            "새 원리", "행만 만든 상태", "concept", [], [], [materials[0]["id"]], AT
        )
        self.assertEqual(self.engine.due(AT + timedelta(days=30)), [])
        result = {
            "summary": "교육", "resource_ids": [knowledge["id"]],
            "next_role": "advisor",
            "observations": ["새 원리 적용 전"], "recommendations": ["실제 사례에 적용"],
        }
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(tutor["id"], "tutor", result, AT)
        self.engine.teach(knowledge["id"], why("새 원리"), "이벤트 루프 순서 설명과 연결", AT)
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(tutor["id"], "tutor", result, AT)
        self.engine.review(
            knowledge["id"], "good", [], [], "complete", "새 사례에 어떻게 적용할까?",
            "경계에 제한을 둔다", "설명과 다른 사례에 적용함", AT,
        )
        self.assertEqual(
            self.engine.handoff_complete(tutor["id"], "tutor", result, AT)["status"],
            "completed",
        )

        later = AT + timedelta(days=2)
        retrieval = self.engine.workflow_start("review", "지연 인출", later)
        advisor = self.engine.workflow_next(retrieval["id"], "복습 목표", later)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", later)
        goal = self.engine.learning_goal_add(
            "새 원리 복원", "자료 없이 설명", "기억률 저하", "remedial", 5,
            knowledge["id"], later,
        )
        self.engine.handoff_complete(
            advisor["id"], "advisor", {"summary": "복습 목표", "resource_ids": [goal["id"]]}, later
        )
        tutor = self.engine.workflow_next(retrieval["id"], "힌트 없는 인출", later)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", later)
        retrieval_result = {
            "summary": "복습", "resource_ids": [knowledge["id"]],
            "next_role": "advisor",
            "observations": ["독립 설명 여부"], "recommendations": ["적용 문제"],
        }
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(tutor["id"], "tutor", retrieval_result, later)
        self.engine.review(
            knowledge["id"], "good", [], [], "complete", "왜 필요한가?",
            "과부하를 경계에서 제한한다", "원리와 사례를 독립 설명함", later,
        )
        self.engine.teach(
            knowledge["id"], why("인출 뒤 빈틈"), "이벤트 루프 순서 설명과 연결", later
        )
        self.assertEqual(
            self.engine.handoff_complete(tutor["id"], "tutor", retrieval_result, later)["status"],
            "completed",
        )

    def test_target_change_cancels_an_inflight_retrieval_workflow(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "옛 전공 지식", "분산 시스템 설명", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        prepare_retrieval(self.engine, knowledge)
        later = AT + timedelta(days=2)
        workflow = self.engine.workflow_start("review", "만기 복습", later)
        advisor = self.engine.workflow_next(workflow["id"], "복습 목표", later)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", later)
        goal = self.engine.learning_goal_add(
            "옛 지식 복원", "자료 없이 설명", "망각 위험", "remedial", 5,
            knowledge["id"], later,
        )
        self.engine.handoff_complete(
            advisor["id"], "advisor",
            {"summary": "복습 목표", "resource_ids": [goal["id"]]}, later,
        )
        tutor = self.engine.workflow_next(workflow["id"], "지연 인출", later)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", later)
        self.engine.advisor_init("프랑스 문학 연구", "입문", [], 0.9, later)
        self.assertEqual(self.engine.workflow_show(workflow["id"])["status"], "superseded")
        self.assertFalse(any(item["id"] == tutor["id"] for item in self.engine.handoff_inbox("tutor")))
        with self.assertRaisesRegex(ValueError, "status cancelled"):
            self.engine.handoff_complete(
                tutor["id"], "tutor",
                {"summary": "옛 전공 복습", "resource_ids": [knowledge["id"]]}, later,
            )
        self.assertEqual(self.engine.workflow_show(workflow["id"])["status"], "superseded")
        self.assertEqual(
            next(item for item in self.engine.handoff_list("cancelled") if item["id"] == tutor["id"])["id"],
            tutor["id"],
        )

    def test_plan_workflow_can_change_its_own_profile_target(self):
        self.init_curriculum()
        workflow = self.engine.workflow_start("plan", "프랑스 문학으로 전공 변경", AT)
        advisor = self.engine.workflow_next(workflow["id"], "새 전공 설계", AT)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", AT)
        self.engine.advisor_init("프랑스 문학 연구", "입문", ["시 분석"], 0.9, AT)
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.engine.advisor_interview(
                decision, f"새 {decision} 질문?", f"새 {decision} 근거", AT
            )
        curriculum = self.engine.advisor_curriculum(CURRICULUM, AT)
        completed = self.engine.handoff_complete(
            advisor["id"], "advisor",
            {"summary": "새 전공 경로", "resource_ids": [curriculum["id"]]}, AT,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.engine.workflow_show(workflow["id"])["status"], "completed")
        self.assertEqual(self.store.load()["profile"]["goal"], "프랑스 문학 연구")

    def test_editor_must_review_the_current_learner_revision(self):
        artifact = self.engine.artifact_add(
            "설계 메모", "v1 초안", "결정 설명", "팀", None, AT, "learner"
        )
        self.engine.artifact_review(
            artifact["id"], editor_criteria(True, "v1 초안"),
            "revise", "논리를 보강한다", AT
        )
        workflow = self.engine.workflow_start(
            "artifact", "수정본 검토", AT, [artifact["id"]]
        )
        editor = self.engine.workflow_next(workflow["id"], "현재 버전 검토", AT)["handoff"]
        self.engine.handoff_claim(editor["id"], "editor", AT)
        self.engine.artifact_revise(
            artifact["id"], "v2 학습자 수정본", AT + timedelta(minutes=1), "learner"
        )
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                editor["id"], "editor",
                {"summary": "v1 리뷰 재사용", "resource_ids": [artifact["id"]]}, AT,
            )
        self.engine.artifact_review(
            artifact["id"], editor_criteria(), "pass", "제출", AT + timedelta(minutes=2)
        )
        completed = self.engine.handoff_complete(
            editor["id"], "editor",
            {"summary": "v2 검토 완료", "resource_ids": [artifact["id"]]}, AT,
        )
        self.assertEqual(completed["status"], "completed")

    def test_editor_workflow_cannot_swap_the_requested_artifact(self):
        requested = self.engine.artifact_add(
            "요청 초안", "A", "설명", "팀", None, AT, "learner"
        )
        other = self.engine.artifact_add(
            "다른 초안", "B", "설명", "팀", None, AT, "learner"
        )
        workflow = self.engine.workflow_start(
            "artifact", "요청 초안 검토", AT, [requested["id"]]
        )
        handoff = self.engine.workflow_next(workflow["id"], "", AT)["handoff"]
        self.engine.handoff_claim(handoff["id"], "editor", AT)
        self.engine.artifact_review(other["id"], editor_criteria(), "pass", "제출", AT)
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(
                handoff["id"], "editor",
                {"summary": "다른 글 검토", "resource_ids": [other["id"]]}, AT,
            )

        passed = self.engine.artifact_add(
            "완료 글", "끝", "설명", "팀", None, AT, "learner"
        )
        self.engine.artifact_review(passed["id"], editor_criteria(), "pass", "제출", AT)
        with self.assertRaisesRegex(ValueError, "reviewable learner version"):
            self.engine.workflow_start("artifact", "완료 글 재검토", AT, [passed["id"]])
        with self.assertRaisesRegex(ValueError, "exactly one artifact id"):
            self.engine.handoff_dispatch("editor", "대상 없는 검토", "", AT)

    def test_manual_editor_handoff_cannot_swap_the_requested_artifact(self):
        requested = self.engine.artifact_add(
            "요청 초안", "A", "설명", "팀", None, AT, "learner"
        )
        other = self.engine.artifact_add(
            "다른 초안", "B", "설명", "팀", None, AT, "learner"
        )
        handoff = self.engine.handoff_dispatch(
            "editor", "요청 초안 검토", "", AT, resource_ids=[requested["id"]]
        )
        self.engine.handoff_claim(handoff["id"], "editor", AT)
        self.engine.artifact_review(other["id"], editor_criteria(), "pass", "제출", AT)
        with self.assertRaisesRegex(ValueError, "valid outputs produced or updated"):
            self.engine.handoff_complete(
                handoff["id"], "editor",
                {"summary": "다른 글 검토", "resource_ids": [other["id"]]}, AT,
            )

    def test_roommate_workflow_cannot_swap_the_requested_conversation(self):
        workflow = self.engine.workflow_start(
            "perspective", "이 관점 대화", AT, perspective_spec=perspective_request()
        )
        handoff = self.engine.workflow_next(workflow["id"], "", AT)["handoff"]
        self.engine.handoff_claim(handoff["id"], "roommate", AT)
        other = self.engine.perspective_start(
            "분산 시스템", "합의", "정치학", "숙의", "누가 결정할까?", AT
        )
        self.engine.perspective_answer(
            other["id"], "참여자", "insight", "대표성", "노드-참여자",
            "장애 모델은 다름", [], AT,
        )
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                handoff["id"], "roommate",
                {"summary": "다른 대화", "resource_ids": [other["id"]]}, AT,
            )
        with self.assertRaisesRegex(ValueError, "current field and problem"):
            self.engine.workflow_start("perspective", "무대상 대화", AT)
        pending = self.engine.perspective_start(
            "분산 시스템", "복제", "생물학", "유전", "무엇이 복제될까?", AT
        )
        self.assertIsNone(pending["connection"])
        with self.assertRaisesRegex(ValueError, "pending Roommate"):
            self.engine.workflow_start(
                "perspective", "새 관점", AT,
                perspective_spec=perspective_request("장애 복구"),
            )

    def test_manual_roommate_handoff_cannot_swap_the_requested_conversation(self):
        requested = perspective_request()
        handoff = self.engine.handoff_dispatch(
            "roommate", "백프레셔 외부 관점", "", AT, perspective_spec=requested
        )
        self.engine.handoff_claim(handoff["id"], "roommate", AT)
        other = self.engine.perspective_start(
            "분산 시스템", "합의", "정치학", "숙의", "누가 결정할까?", AT
        )
        self.engine.perspective_answer(
            other["id"], "참여자", "insight", "대표성", "노드-참여자",
            "장애 모델은 다름", [], AT,
        )
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                handoff["id"], "roommate",
                {"summary": "다른 대화", "resource_ids": [other["id"]]}, AT,
            )

    def test_advisor_cannot_complete_by_touching_an_unchanged_old_goal(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        old_goal = self.engine.learning_goal_add(
            "기존 목표", "기존 결과", "기존 이유", "practical", 3, None, AT
        )
        workflow = self.engine.workflow_start("learn", "새 학습", AT)
        tutor = self.engine.workflow_next(workflow["id"], "교육", AT)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", AT)
        knowledge = self.engine.knowledge_add(
            "새 혼동", "새로 드러난 원리", "concept", ["새 경계"], [],
            [materials[0]["id"]], AT,
        )
        self.engine.teach(knowledge["id"], why("새 경계"), "이벤트 루프 순서 설명과 연결", AT)
        self.engine.review(
            knowledge["id"], "hard", ["새 경계"], [], "partial", "새 사례의 경계는?",
            "근거가 부족함", "적용에서 혼동을 확인", AT,
        )
        self.engine.handoff_complete(
            tutor["id"], "tutor",
            {
                "summary": "새 혼동 발견", "resource_ids": [knowledge["id"]],
                "next_role": "advisor",
                "observations": ["새 경계를 설명하지 못함"],
                "recommendations": ["새 경계를 실제 사례에 적용"],
            }, AT,
        )
        advisor = self.engine.workflow_next(workflow["id"], "경로 갱신", AT)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", AT)
        unchanged = self.engine.learning_goal_add(
            "기존 목표", "기존 결과", "기존 이유", "practical", 3, None,
            AT + timedelta(minutes=1),
        )
        self.assertEqual(unchanged["updated_at"], old_goal["updated_at"])
        same_status = self.engine.learning_goal_status(
            old_goal["id"], "active", AT + timedelta(minutes=1)
        )
        self.assertEqual(same_status["updated_at"], old_goal["updated_at"])
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                advisor["id"], "advisor",
                {"summary": "변경 없음", "resource_ids": [old_goal["id"]]}, AT,
            )


class CliTestCase(EngineTestCase):
    def invoke(self, *arguments, at=AT, actor=None, scope_handoff=None):
        scope = ["--scope-handoff", scope_handoff] if scope_handoff else []
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "become.py"),
                "--home",
                str(self.home),
                "--now",
                at.isoformat(),
                "--actor",
                actor or arguments[0],
                *scope,
                *arguments,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def cli(self, *arguments, at=AT, actor=None, scope_handoff=None):
        completed = self.invoke(
            *arguments, at=at, actor=actor, scope_handoff=scope_handoff
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

class LimitationClosureTests(EngineTestCase):
    def invoke(self, *arguments):
        return subprocess.run(
            [
                sys.executable, str(ROOT / "become.py"), "--home", str(self.home),
                "--now", AT.isoformat(), "--actor", "advisor", *arguments,
            ],
            capture_output=True, text=True,
        )

    def test_unicode_ids_are_deterministic_and_existing_v4_ids_do_not_migrate(self):
        self.assertEqual(Engine._id("Queue Capacity", []), "queue-capacity")
        korean = Engine._id("버스트 큐 용량 계산 메모", [])
        mixed = Engine._id("Python 큐", [])
        self.assertRegex(korean, r"^u-[0-9a-f]{12}$")
        self.assertRegex(mixed, r"^python-[0-9a-f]{12}$")
        self.assertEqual(korean, Engine._id("버스트 큐 용량 계산 메모", []))
        self.assertEqual(
            Engine._id("Cafe\u0301", []), Engine._id("Café", [])
        )
        self.assertEqual(
            Engine._id("버스트 큐 용량 계산 메모", [{"id": korean}]), f"{korean}-2"
        )

        self.init_profile()
        item = self.engine.knowledge_add("기존 항목", "설명", "concept", [], [], [], AT)
        state = self.store.load()
        state["knowledge"][0]["id"] = "item"
        self.store.save(state)
        loaded = self.store.load()
        self.assertEqual(loaded["version"], 4)
        self.assertEqual(loaded["knowledge"][0]["id"], "item")
        self.assertNotEqual(item["id"], "item")

    def test_advisor_recommend_is_read_only_and_next_is_an_authorized_write(self):
        self.init_profile()
        item = self.engine.knowledge_add(
            "선형화", "관찰 가능한 단일 순서를 만든다", "concept", ["선형화 지점"], [], [], AT
        )
        before = self.store.state_path.read_bytes()
        recommendation = self.engine.recommend(AT)
        self.assertEqual(before, self.store.state_path.read_bytes())
        self.assertIn("proposed_learning_goal", recommendation)
        self.assertNotIn("id", recommendation["proposed_learning_goal"])
        self.engine.resume(AT)
        self.assertEqual(before, self.store.state_path.read_bytes())

        read = self.invoke("advisor", "recommend")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(before, self.store.state_path.read_bytes())
        rejected = self.invoke("advisor", "next")
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(before, self.store.state_path.read_bytes())

        handoff = self.engine.handoff_dispatch(
            "advisor", "취약점으로 다음 목표 조정", "", AT,
            output_kind="advisor_update",
        )
        self.engine.handoff_claim(handoff["id"], "advisor", AT)
        written = self.invoke("advisor", "next")
        self.assertEqual(written.returncode, 0, written.stderr)
        goal = json.loads(written.stdout)["learning_goal"]
        self.assertEqual(goal["knowledge_id"], item["id"])
        self.assertEqual(goal["produced_by_handoff_id"], handoff["id"])

    def test_advisor_help_names_the_read_and_write_boundaries(self):
        parent = subprocess.run(
            [sys.executable, str(ROOT / "become.py"), "--actor", "advisor", "advisor", "--help"],
            capture_output=True, text=True,
        )
        self.assertIn("read-only recommendation", parent.stdout)
        self.assertIn("write or update", parent.stdout)
        recommend = subprocess.run(
            [
                sys.executable, str(ROOT / "become.py"), "--actor", "advisor",
                "advisor", "recommend", "--help",
            ], capture_output=True, text=True,
        )
        self.assertIn("never writes state", recommend.stdout)


class SourcePrefetchTests(CliTestCase):
    def test_prefetch_needs_no_claimed_handoff_and_names_unreachable_sources(self):
        source = Path(self.temporary.name) / "paper.txt"
        source.write_text("primary source", encoding="utf-8")
        missing = Path(self.temporary.name) / "missing.txt"
        result = self.cli(
            "librarian", "prefetch", "--source", str(source), "--source", str(missing)
        )
        self.assertEqual(result["inspected"], 2)
        self.assertEqual(result["reachable"], [str(source)])
        self.assertEqual([item["source"] for item in result["unreachable"]], [str(missing)])
        self.assertIn("not found", result["unreachable"][0]["check"])

    def test_prefetch_warms_the_probe_used_by_a_later_add(self):
        source = Path(self.temporary.name) / "paper.txt"
        source.write_text("primary source", encoding="utf-8")
        self.cli("librarian", "prefetch", "--source", str(source))
        cached = json.loads((self.home / ".sources.json").read_text(encoding="utf-8"))
        fingerprint = cached["entries"][str(source)]["content_fingerprint"]
        material = self.engine.material_add("논문", str(source), "", "원문 직접 확인", AT)
        self.assertTrue(material["verified"])
        self.assertEqual(material["verification"]["content_fingerprint"], fingerprint)


class RoleContractTests(unittest.TestCase):
    def test_spec_describes_executable_roles_and_has_no_legacy_model(self):
        texts = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("AGENTS.md", "README.md", "README_KR.md")
        )

        def assert_no_legacy(value):
            legacy = ("card", "카드", "Leitner", "라이트너", "box1", "mock-interview", "면접 난사")
            found = [word for word in legacy if word in value]
            if found:
                raise AssertionError(f"legacy terms remain: {found}")

        with self.assertRaises(AssertionError):
            assert_no_legacy("this positive control contains 카드")
        assert_no_legacy(texts)
        for role in ("Orchestrator", "Advisor", "Librarian", "Tutor", "Editor", "Roommate"):
            self.assertIn(role, texts)
        for command in (
            "orchestrator route",
            "orchestrator workflow-start",
            "orchestrator workflow-next",
            "advisor init",
            "advisor observe",
            "advisor decide",
            "advisor curriculum",
            "advisor milestone",
            "advisor next",
            "librarian add",
            "librarian curate",
            "librarian shelf",
            "tutor context",
            "tutor relate",
            "tutor teach",
            "tutor review",
            "editor review",
            "editor revise",
            "roommate ask",
            "roommate answer",
            "orchestrator session-resume",
        ):
            self.assertIn(command, texts)
        for contract_term in (
            "--evidence",
            "--prompt",
            "--answer",
            "--rationale",
            "--confidence",
            "--summary",
            "--observation",
            "--recommendation",
            "--candidate-id",
            "--milestone-criteria",
            "--actor learner",
            "--workflow-id",
            "previous_versions",
            "exposure",
            "retrieval",
            "destination",
            "baseline",
            "sequencing",
            "cut list",
            "milestones",
            "relevance",
            "credibility",
            "level fit",
            "signal",
            "thinking",
            "logic",
            "evidence",
            "repetition",
            "structure",
            "precision",
            "accuracy",
            "no_connection",
            "needs_verification",
        ):
            self.assertIn(contract_term, texts)

    def test_cli_exposes_every_role(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "become.py"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        for role in ("orchestrator", "advisor", "librarian", "tutor", "editor", "roommate"):
            self.assertIn(role, completed.stdout)


class AgentSpecTests(unittest.TestCase):
    def test_six_agents_have_distinct_specs_and_domain_ownership(self):
        names = ("orchestrator", "advisor", "librarian", "tutor", "editor", "roommate")
        specs = {}
        for name in names:
            path = ROOT / "agents" / f"{name}.md"
            self.assertTrue(path.is_file(), f"missing agent spec: {path}")
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", text)
            self.assertIn(f"actor: {name}", text)
            self.assertIn("## Input contract", text)
            self.assertIn("## Output contract", text)
            specs[name] = text
        for name in names[1:]:
            self.assertIn(f"python3 become.py --actor {name} {name}", specs[name])
            for other in set(names[1:]) - {name}:
                self.assertNotIn(f"--actor {name} {other}", specs[name])
        self.assertIn("python3 become.py --actor orchestrator orchestrator dispatch", specs["orchestrator"])
        editor_allowed = specs["editor"].split("## Allowed commands", 1)[1].split(
            "## Learner submission interface", 1
        )[0]
        self.assertNotIn("--actor learner", editor_allowed)
        self.assertIn("`--actor learner`를 자칭해서는 안 된다", specs["editor"])

    def test_orchestrator_keeps_review_metadata_internal(self):
        contracts = (
            (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            (ROOT / "agents" / "orchestrator.md").read_text(encoding="utf-8"),
        )
        for contract in contracts:
            self.assertIn("rating·confidence·rationale", contract)
            self.assertIn("내부", contract)
            self.assertNotIn("원문 그대로 먼저 보여", contract)


class AuthorizationTests(CliTestCase):
    def test_different_specialists_can_claim_work_in_parallel(self):
        advisor = self.cli(
            "orchestrator", "dispatch", "--to", "advisor", "--task", "경로 갱신",
            "--output-kind", "advisor_update", actor="orchestrator",
        )
        librarian = self.cli(
            "orchestrator", "dispatch", "--to", "librarian", "--task", "자료 선별",
            actor="orchestrator",
        )
        self.cli("orchestrator", "claim", advisor["id"], actor="advisor")
        self.cli("orchestrator", "claim", librarian["id"], actor="librarian")
        self.assertEqual(
            {item["to"] for item in self.engine.handoff_list("in_progress")},
            {"advisor", "librarian"},
        )

    def test_identical_shelf_rebuild_cannot_reassign_a_previous_handoff_output(self):
        curriculum = self.init_curriculum()
        first = self.engine.handoff_dispatch("librarian", "첫 선반", "", AT)
        self.engine.handoff_claim(first["id"], "librarian", AT)
        materials, shelf = self.ready_shelf()
        self.engine.handoff_complete(
            first["id"], "librarian",
            {"summary": "첫 선반", "resource_ids": [shelf["id"]]}, AT,
        )
        state = self.store.load()
        stored = self.engine._find(state["shelves"], shelf["id"], "shelf")
        stored["candidate_material_ids"].reverse()
        self.store.save(state)
        second = self.engine.handoff_dispatch("librarian", "둘째 선반", "", AT)
        self.engine.handoff_claim(second["id"], "librarian", AT)
        replay = self.engine.material_shelf(
            curriculum["id"], "measure",
            list(reversed([item["id"] for item in materials])),
            AT + timedelta(minutes=1),
        )
        self.assertEqual(replay["produced_by_handoff_id"], first["id"])
        with self.assertRaisesRegex(ValueError, "not produced by this claimed handoff"):
            self.engine.handoff_complete(
                second["id"], "librarian",
                {"summary": "동일 선반 재사용", "resource_ids": [replay["id"]]}, AT,
            )

    def test_same_specialist_handoffs_are_serialized_to_prevent_composite_output_theft(self):
        first_workflow = self.cli(
            "orchestrator", "workflow-start", "--intent", "plan",
            "--request", "PLAN ONE", actor="orchestrator",
        )
        second_workflow = self.cli(
            "orchestrator", "workflow-start", "--intent", "plan",
            "--request", "PLAN TWO", actor="orchestrator",
        )
        first = self.cli(
            "orchestrator", "workflow-next", first_workflow["id"], actor="orchestrator"
        )["handoff"]
        second = self.cli(
            "orchestrator", "workflow-next", second_workflow["id"], actor="orchestrator"
        )["handoff"]
        self.cli("orchestrator", "claim", first["id"], actor="advisor")
        denied = self.invoke(
            "orchestrator", "claim", second["id"], actor="advisor"
        )
        self.assertEqual(denied.returncode, 2)
        self.assertIn("already has an in-progress handoff", denied.stderr)
        self.assertEqual(
            self.engine._find(self.store.load()["handoffs"], second["id"], "handoff")["status"],
            "pending",
        )
        self.cli(
            "advisor", "init", "--goal", "분산 시스템 전문가",
            actor="advisor", scope_handoff=first["id"],
        )
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.cli(
                "advisor", "interview", "--decision", decision,
                "--question", f"{decision} 질문?", "--answer", f"{decision} 실제 답",
                actor="advisor", scope_handoff=first["id"],
            )
        curriculum = self.cli(
            "advisor", "curriculum", "--spec", json.dumps(CURRICULUM, ensure_ascii=False),
            actor="advisor", scope_handoff=first["id"],
        )
        completed = self.cli(
            "orchestrator", "complete", first["id"], "--summary", "정당한 완료",
            "--resource-id", curriculum["id"], actor="advisor",
        )
        self.assertEqual(completed["status"], "completed")

    def test_specialist_write_requires_a_claimed_handoff(self):
        self.engine.advisor_init("분산 시스템 전문가", "", [], 0.9, AT)
        denied = self.invoke(
            "tutor", "add", "--title", "무단 지식", "--explanation", "껍데기"
        )
        self.assertEqual(denied.returncode, 2)
        self.assertIn("claimed handoff", denied.stderr)

    def test_claimed_handoff_rejects_mutation_of_another_role_resource(self):
        requested = self.cli(
            "editor", "add", "--title", "요청 글", "--content", "A",
            "--purpose", "검토", "--audience", "팀", actor="learner",
        )
        other = self.cli(
            "editor", "add", "--title", "다른 글", "--content", "B",
            "--purpose", "검토", "--audience", "팀", actor="learner",
        )
        handoff = self.cli(
            "orchestrator", "dispatch", "--to", "editor", "--task", "요청 글 검토",
            "--resource-id", requested["id"], actor="orchestrator",
        )
        self.cli("orchestrator", "claim", handoff["id"], actor="editor")
        denied = self.invoke(
            "editor", "review", other["id"], "--criteria",
            json.dumps(editor_criteria(), ensure_ascii=False), "--verdict", "pass",
        )
        self.assertEqual(denied.returncode, 2)
        self.assertIn("does not match", denied.stderr)
        self.assertEqual(self.engine.artifact_show(other["id"])["status"], "draft")

        roommate = self.cli(
            "orchestrator", "dispatch", "--to", "roommate", "--task", "관점",
            "--current-field", "분산 시스템", "--problem", "백프레셔",
            actor="orchestrator",
        )
        self.cli("orchestrator", "claim", roommate["id"], actor="roommate")
        wrong_question = self.invoke(
            "roommate", "ask", "--current-field", "분산 시스템", "--problem", "합의",
            "--outside-field", "정치학", "--lens", "숙의",
            "--question", "누가 결정할까?",
        )
        self.assertEqual(wrong_question.returncode, 2)
        self.assertEqual(self.engine.perspectives(), [])

    def test_manual_advisor_handoff_requires_and_enforces_output_kind(self):
        missing = self.invoke(
            "orchestrator", "dispatch", "--to", "advisor", "--task", "커리큘럼",
            actor="orchestrator",
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("output kind", missing.stderr)
        handoff = self.cli(
            "orchestrator", "dispatch", "--to", "advisor", "--task", "커리큘럼",
            "--output-kind", "curriculum", actor="orchestrator",
        )
        self.cli("orchestrator", "claim", handoff["id"], actor="advisor")
        wrong_command = self.invoke(
            "advisor", "goal", "--title", "무관한 목표", "--outcome", "무관",
            "--reason", "우회",
        )
        self.assertEqual(wrong_command.returncode, 2)
        self.assertIn("output kind", wrong_command.stderr)

    def test_retrieval_handoff_forbids_teaching_before_the_independent_answer(self):
        self.init_curriculum()
        materials, _ = self.ready_shelf()
        knowledge = self.engine.knowledge_add(
            "만기 원리", "간격 뒤 먼저 꺼낸다", "concept", [], [],
            [materials[0]["id"]], AT,
        )
        prepare_retrieval(self.engine, knowledge)
        later = AT + timedelta(days=2)
        workflow = self.engine.workflow_start("review", "만기 복습", later)
        advisor = self.engine.workflow_next(workflow["id"], "보강 목표", later)["handoff"]
        self.engine.handoff_claim(advisor["id"], "advisor", later)
        goal = self.engine.learning_goal_add(
            "만기 원리 보강", "간격 뒤 독립 인출한다", "망각 위험", "remedial", 5,
            knowledge["id"], later,
        )
        self.engine.handoff_complete(
            advisor["id"], "advisor",
            {"summary": "복습 목표", "resource_ids": [goal["id"]]}, later,
        )
        tutor = self.engine.workflow_next(workflow["id"], "독립 인출", later)["handoff"]
        self.engine.handoff_claim(tutor["id"], "tutor", later)
        before = self.engine.knowledge()[0]["last_teaching"]
        revealed = self.invoke(
            "tutor", "teach", knowledge["id"], "--explanation", why("만기 원리"),
            "--connection", "만기 원리 선수 개념과 연결", at=later,
        )
        self.assertEqual(revealed.returncode, 2)
        self.assertIn("retrieval review must happen before", revealed.stderr)
        current = next(item for item in self.engine.knowledge() if item["id"] == knowledge["id"])
        self.assertEqual(current["last_teaching"], before)

        self.cli(
            "tutor", "review", knowledge["id"], "hard", "--confidence", "partial",
            "--add-weak", "적용 경계", "--prompt", "간격 뒤 원리는?",
            "--answer", "경계 근거가 불완전하다", "--rationale", "독립 답변에서 혼동",
            at=later,
        )
        self.cli(
            "tutor", "teach", knowledge["id"], "--explanation", why("만기 원리"),
            "--connection", "만기 원리 선수 개념과 연결", at=later,
        )
        completed = self.engine.handoff_complete(
            tutor["id"], "tutor",
            {
                "summary": "인출 뒤 보강", "next_role": "advisor",
                "resource_ids": [knowledge["id"]], "observations": ["적용 경계 혼동"],
                "recommendations": ["다른 경계 사례에 적용"],
            }, later,
        )
        self.assertEqual(completed["status"], "completed")

    def test_cross_role_commands_and_orchestrator_impersonation_are_rejected(self):
        self.engine.advisor_init("전문가", "", [], 0.9, AT)
        allowed = self.invoke("librarian", "list")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

        cross_role = self.invoke("tutor", "list", actor="librarian")
        self.assertEqual(cross_role.returncode, 2)
        self.assertIn("not authorized", cross_role.stderr)

        impersonation = self.invoke("tutor", "list", actor="orchestrator")
        self.assertEqual(impersonation.returncode, 2)
        self.assertIn("not authorized", impersonation.stderr)

        routed = self.cli("orchestrator", "route", "--intent", "learn", actor="orchestrator")
        self.assertEqual(routed["role"], "advisor")
        denied_route = self.invoke("orchestrator", "route", "--intent", "learn", actor="tutor")
        self.assertEqual(denied_route.returncode, 2)
        self.assertIn("not authorized", denied_route.stderr)

        ghostwrite = self.invoke(
            "editor", "add", "--title", "대필", "--content", "Editor 문장",
            "--purpose", "테스트", "--audience", "팀", actor="editor",
        )
        self.assertEqual(ghostwrite.returncode, 2)
        submitted = self.cli(
            "editor", "add", "--title", "학습자 글", "--content", "학습자 문장",
            "--purpose", "테스트", "--audience", "팀", actor="learner",
        )
        self.assertEqual(submitted["versions"][0]["author"], "learner")


class HandoffTests(CliTestCase):
    def test_only_target_agent_can_claim_and_complete(self):
        handoff = self.cli(
            "orchestrator",
            "dispatch",
            "--to",
            "roommate",
            "--task",
            "외부 관점",
            "--current-field", "분산 시스템",
            "--problem", "백프레셔",
            actor="orchestrator",
        )
        inbox = self.cli("orchestrator", "inbox", actor="roommate")
        self.assertEqual(inbox[0]["id"], handoff["id"])

        wrong_claim = self.invoke("orchestrator", "claim", handoff["id"], actor="tutor")
        self.assertEqual(wrong_claim.returncode, 2)
        self.assertIn("belongs to roommate", wrong_claim.stderr)

        claimed = self.cli("orchestrator", "claim", handoff["id"], actor="roommate")
        self.assertEqual(claimed["status"], "in_progress")
        wrong_complete = self.invoke(
            "orchestrator", "complete", handoff["id"], "--summary", "가로채기", actor="editor"
        )
        self.assertEqual(wrong_complete.returncode, 2)
        perspective = self.engine.perspective_start(
            "분산 시스템", "백프레셔", "도시 교통", "진입 제한",
            "어디서 요청률을 제한할까?", AT,
        )
        pending = self.invoke(
            "orchestrator", "complete", handoff["id"], "--summary", "아직 미응답",
            "--resource-id", perspective["id"], actor="roommate",
        )
        self.assertEqual(pending.returncode, 2)
        self.assertIn("valid outputs produced or updated", pending.stderr)
        perspective = self.engine.perspective_answer(
            perspective["id"], "입구에서 제한한다", "insight", "입구 제어",
            "램프와 게이트웨이", "재시도는 다르다", [], AT,
        )
        completed = self.cli(
            "orchestrator",
            "complete",
            handoff["id"],
            "--summary",
            "외부 관점 연결 완료",
            "--next-role",
            "tutor",
            "--resource-id",
            perspective["id"],
            "--observation",
            "공식 문서의 핵심을 독립 설명함",
            "--recommendation",
            "공식 예제를 실제 코드에 적용",
            actor="roommate",
        )
        self.assertEqual(completed["status"], "completed")
        listed = self.cli("orchestrator", "list", "--status", "completed", actor="orchestrator")
        self.assertEqual(
            listed[0]["result"],
            {
                "summary": "외부 관점 연결 완료",
                "next_role": "tutor",
                "resource_ids": [perspective["id"]],
                "issues": [],
                "observations": ["공식 문서의 핵심을 독립 설명함"],
                "recommendations": ["공식 예제를 실제 코드에 적용"],
            },
        )


class VideoLevelAlterJourneyTests(CliTestCase):
    def dispatch(self, target, task):
        handoff = self.cli(
            "orchestrator", "dispatch", "--to", target, "--task", task, actor="orchestrator"
        )
        self.cli("orchestrator", "claim", handoff["id"], actor=target)
        return handoff

    def complete(self, handoff, result, resource_id):
        return self.cli(
            "orchestrator", "complete", handoff["id"], "--summary", result,
            "--resource-id", resource_id, actor=handoff["to"]
        )

    def test_tutor_feedback_updates_advisor_level_and_goal(self):
        self.engine.advisor_init(
            "운영 가능한 비동기 서비스", "이벤트 루프를 설명할 수 있음", [], 0.9, AT
        )
        self.init_curriculum()
        materials, _ = self.ready_shelf()

        tutor_handoff = self.dispatch("tutor", "백프레셔 교육과 적용")
        knowledge = self.cli(
            "tutor", "add", "--title", "백프레셔 적용", "--explanation",
            "생산률을 소비 가능량에 맞춘다",
            "--source", materials[0]["id"],
        )
        self.cli(
            "tutor", "teach", knowledge["id"], "--explanation",
            why("백프레셔"), "--connection", "이벤트 루프 큐",
        )
        self.cli(
            "tutor", "review", knowledge["id"], "hard", "--add-weak", "queue capacity 산정",
            "--confidence", "partial", "--prompt", "실측값으로 용량을 정해보세요",
            "--answer", "원리는 알지만 수치는 도움 필요", "--rationale", "적용 근거가 불완전",
        )
        tutor_result = self.cli(
            "orchestrator",
            "complete",
            tutor_handoff["id"],
            "--summary",
            "원리 교육 후 bounded queue 사례 적용",
            "--next-role",
            "advisor",
            "--observation",
            "이벤트 루프는 설명하지만 queue capacity 산정에는 도움 필요",
            "--recommendation",
            "실측 처리량으로 bounded queue 용량을 산정한다",
            "--resource-id",
            knowledge["id"],
            actor="tutor",
        )["result"]

        advisor_update = self.cli(
            "orchestrator",
            "dispatch",
            "--to",
            "advisor",
            "--task",
            "Tutor 관찰로 수준과 목표 갱신",
            "--context",
            json.dumps(tutor_result, ensure_ascii=False),
            "--output-kind",
            "advisor_update",
            "--depends-on",
            tutor_handoff["id"],
            actor="orchestrator",
        )
        self.cli("orchestrator", "claim", advisor_update["id"], actor="advisor")
        self.cli(
            "advisor",
            "observe",
            "--level",
            "이벤트 루프 원리 이해, 용량 산정 적용은 미숙",
            "--evidence",
            tutor_result["observations"][0],
        )
        goal = self.cli(
            "advisor",
            "goal",
            "--title",
            "bounded queue 용량 산정",
            "--outcome",
            "실측 생산·소비 처리량으로 queue capacity를 결정한다",
            "--reason",
            tutor_result["recommendations"][0],
            "--priority",
            "5",
            "--knowledge-id",
            knowledge["id"],
        )
        self.complete(advisor_update, f"실용 목표 {goal['id']} 저장", goal["id"])

        profile = self.cli("advisor", "status")["profile"]
        self.assertEqual(len(profile["level_evidence"]), 1)
        self.assertEqual(profile["learning_goals"][0]["id"], goal["id"])

    def test_six_agents_complete_a_handoff_driven_university_journey(self):
        workflow = self.cli(
            "orchestrator", "workflow-start", "--intent", "learn",
            "--request", "백프레셔를 운영 설계까지 익힌다", actor="orchestrator",
        )
        first = self.cli(
            "orchestrator", "workflow-next", workflow["id"], actor="orchestrator"
        )["handoff"]
        self.cli("orchestrator", "claim", first["id"], actor="advisor")
        self.cli("advisor", "init", "--goal", "분산 시스템 전문가", "--focus", "동시성")
        for decision in ("destination", "baseline", "sequencing", "cut_list", "milestones"):
            self.cli(
                "advisor", "interview", "--decision", decision,
                "--question", f"{decision} 질문?", "--answer", f"{decision} 실제 답",
            )
        curriculum = self.cli(
            "advisor", "curriculum", "--spec", json.dumps(CURRICULUM, ensure_ascii=False)
        )
        self.cli(
            "orchestrator", "complete", first["id"], "--summary", "개인 경로 완성",
            "--resource-id", curriculum["id"], actor="advisor",
        )

        second = self.cli(
            "orchestrator", "workflow-next", workflow["id"], actor="orchestrator"
        )["handoff"]
        self.cli("orchestrator", "claim", second["id"], actor="librarian")
        materials = []
        for index in range(3):
            source = Path(self.temporary.name) / f"journey-source-{index}.txt"
            source.write_text(f"verified source {index}", encoding="utf-8")
            material = self.cli(
                "librarian", "add", "--title", f"공식 자료 {index}",
                "--source", str(source), "--evidence", "본문 직접 확인",
            )
            materials.append(self.cli(
                "librarian", "curate", material["id"],
                "--assessment", json.dumps(curation(curriculum["id"]), ensure_ascii=False),
            ))
        shelf = self.cli(
            "librarian", "shelf", "--curriculum-id", curriculum["id"], "--step-id", "measure",
            "--candidate-id", materials[0]["id"], "--candidate-id", materials[1]["id"],
            "--candidate-id", materials[2]["id"],
        )
        self.cli(
            "orchestrator", "complete", second["id"], "--summary", "선별 선반 완성",
            "--resource-id", shelf["id"], actor="librarian",
        )

        third = self.cli(
            "orchestrator", "workflow-next", workflow["id"], actor="orchestrator"
        )["handoff"]
        self.cli("orchestrator", "claim", third["id"], actor="tutor")
        knowledge = self.cli(
            "tutor", "add", "--title", "백프레셔", "--explanation",
            "생산 속도를 소비 가능량에 맞춘다", "--weak", "용량 선택",
            "--source", materials[0]["id"],
        )
        self.cli(
            "tutor", "teach", knowledge["id"], "--explanation",
            why("백프레셔"),
            "--connection", "이미 아는 이벤트 루프의 준비 큐와 연결",
        )
        self.cli(
            "tutor", "review", knowledge["id"], "hard", "--add-weak", "용량 선택",
            "--confidence", "partial", "--prompt", "실측값으로 용량을 정해보세요",
            "--answer", "수치 근거가 더 필요하다", "--rationale", "적용 경계를 아직 방어하지 못함",
        )
        self.cli(
            "orchestrator", "complete", third["id"], "--summary", "원리 교육과 혼동 기록",
            "--next-role", "advisor",
            "--resource-id", knowledge["id"], "--observation", "용량 선택에 근거가 더 필요",
            "--recommendation", "부하 테스트 결과로 선택을 방어", actor="tutor",
        )

        roommate_handoff = self.cli(
            "orchestrator", "dispatch", "--to", "roommate", "--task", "외부 관점",
            "--current-field", "분산 시스템", "--problem", "백프레셔",
            actor="orchestrator",
        )
        self.cli("orchestrator", "claim", roommate_handoff["id"], actor="roommate")
        perspective = self.cli(
            "roommate", "ask", "--current-field", "분산 시스템", "--problem", "백프레셔",
            "--outside-field", "도시 교통", "--lens", "진입 램프가 포화를 늦춘다",
            "--question", "요청 진입률은 어디서 제한해야 할까?",
        )
        self.cli(
            "roommate", "answer", perspective["id"], "--response", "게이트웨이에서 제한한다",
            "--status", "insight", "--insight", "입구 제어가 하류를 보호한다",
            "--mapping", "램프는 게이트웨이, 도로는 처리 파이프라인이다",
            "--limits", "재시도 폭주는 교통 비유만으로 설명되지 않는다",
        )
        self.cli(
            "orchestrator", "complete", roommate_handoff["id"], "--summary", "외부 관점 연결",
            "--resource-id", perspective["id"], actor="roommate",
        )
        artifact = self.cli(
            "editor", "add", "--title", "큐 용량 설계 답변", "--content", "항상 안전하다",
            "--purpose", "운영 선택 방어", "--audience", "백엔드 팀",
            "--milestone-id", "load-test",
            actor="learner",
        )
        editor_handoff = self.cli(
            "orchestrator", "dispatch", "--to", "editor", "--task", "설계 답변 교정",
            "--resource-id", artifact["id"], actor="orchestrator",
        )
        self.cli("orchestrator", "claim", editor_handoff["id"], actor="editor")
        self.cli(
            "editor", "review", artifact["id"], "--criteria",
            json.dumps(editor_criteria(revise=True), ensure_ascii=False),
            "--milestone-criteria",
            json.dumps(milestone_criteria("load-test", revise=True), ensure_ascii=False),
            "--verdict", "revise", "--next", "과부하 반례 추가",
        )
        self.cli(
            "editor", "revise", artifact["id"],
            "--content", "측정 근거, 대안, 처리량을 넘는 실패 경계를 포함한다",
            actor="learner",
        )
        self.cli(
            "editor", "review", artifact["id"], "--criteria",
            json.dumps(editor_criteria(), ensure_ascii=False), "--milestone-criteria",
            json.dumps(milestone_criteria("load-test"), ensure_ascii=False),
            "--verdict", "pass",
        )
        self.cli(
            "orchestrator", "complete", editor_handoff["id"], "--summary", "현재 버전 통과",
            "--resource-id", artifact["id"], actor="editor",
        )

        fourth = self.cli(
            "orchestrator", "workflow-next", workflow["id"], actor="orchestrator"
        )["handoff"]
        self.cli("orchestrator", "claim", fourth["id"], actor="advisor")
        self.cli(
            "advisor", "observe", "--level", "원리 이해, 용량 선택 근거는 보강 필요",
            "--evidence", "용량 선택에 근거가 더 필요",
        )
        milestone = self.cli(
            "advisor", "milestone", "load-test", "--artifact-id", artifact["id"]
        )
        goal = self.cli(
            "advisor", "goal", "--title", "큐 용량 근거 보강", "--outcome",
            "부하 테스트로 용량을 방어한다", "--reason", "Tutor 관찰 반영",
            "--priority", "5", "--knowledge-id", knowledge["id"],
        )
        self.cli(
            "orchestrator", "complete", fourth["id"], "--summary", "증거로 성취 확인",
            "--resource-id", milestone["id"], "--resource-id", goal["id"], actor="advisor",
        )
        finished = self.cli(
            "orchestrator", "workflow-show", workflow["id"], actor="orchestrator"
        )
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(len(finished["steps"]), 4)
        curriculum_state = self.store.load()["profile"]["curriculum"]
        self.assertEqual(curriculum_state["status"], "ready")
        self.assertEqual(
            [step["status"] for step in curriculum_state["sequence"]],
            ["completed", "active"],
        )


if __name__ == "__main__":
    unittest.main()
