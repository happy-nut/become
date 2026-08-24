import gzip
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from become import EDITOR_DIMENSIONS, Engine, Store
from mobile import CodexRunner, MobileServer, bind_is_loopback, state_revision


AT = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)


class BlockingRunner:
    def __init__(self, response="실제 역할 흐름을 완료했습니다.", needs_input=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.response = response
        self.needs_input = needs_input

    def run(self, workspace, message, reply_context, emit, cancelled):
        emit("role", "Tutor가 학습 흐름을 실행하고 있습니다.", "tutor")
        self.started.set()
        self.release.wait(5)
        if self.cancelled.is_set() or cancelled():
            raise InterruptedError("cancelled")
        request = (
            {
                "kind": "tutor_application", "prompt": "어디에 적용하시겠어요?",
                "workflow_id": None, "handoff_id": None, "resource_id": None,
            }
            if self.needs_input else None
        )
        return {
            "outcome": "needs_input" if request else "completed",
            "response": self.response,
            "input_request": request,
            "roles_advanced": [],
            "workflow_id": None,
        }

    def cancel(self):
        self.cancelled.set()
        self.release.set()


class DestructiveRunner:
    def run(self, workspace, message, reply_context, emit, cancelled):
        store = Store(workspace / ".become")
        state = store.load()
        state["materials"] = []
        store.save(state)
        return {
            "outcome": "completed", "response": "축소 완료", "input_request": None,
            "roles_advanced": [], "workflow_id": None,
        }


class ReviewRunner:
    def run(self, workspace, message, reply_context, emit, cancelled):
        engine = Engine(Store(workspace / ".become"))
        item = next(item for item in engine.knowledge() if item["title"] == "Template 결합")
        engine.review(
            item["id"], "good", [], [], "complete", "지연 인출 질문",
            "결합 경계를 보존한다", "새 사례에 정확히 적용했다", AT,
        )
        return {
            "outcome": "completed", "response": "복습 완료", "input_request": None,
            "roles_advanced": [], "workflow_id": None,
        }


class MobileTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "data"
        engine = Engine(Store(self.home))
        created = AT - timedelta(days=2)
        engine.advisor_init("Python 전문가", "숙련", ["t-string"], 0.9, created)
        source = Path(self.temporary.name) / "pep.txt"
        source.write_text("Template concatenation", encoding="utf-8")
        material = engine.material_add(
            "PEP 750", str(source), "결합 의미론", "결합 절을 직접 확인", created
        )
        anchor = engine.knowledge_add(
            "Python 숙련", "이미 알고 있는 연결 기준", "concept", [], [], [], created
        )
        self.knowledge = engine.knowledge_add(
            "Template 결합",
            "Template끼리는 결합할 수 있지만 str과의 암시적 결합은 금지된다.",
            "concept",
            ["빈 문자열 경계 보존"],
            [],
            [],
            created,
        )
        engine.knowledge_relate(self.knowledge["id"], anchor["id"], created)
        engine.teach(
            self.knowledge["id"],
            "왜 쓰는가: 결합 경계를 보존한다. 왜 이렇게 되었는가: Template과 str을 구분한다. "
            "왜 이 결과가 나오는가: 정적 슬롯이 의미를 갖기 때문이다. "
            "그래서 어디에 쓰는가: 안전한 Template 결합에 쓴다.",
            "Python 숙련과 연결",
            created,
        )
        engine.review(
            self.knowledge["id"], "good", [], [], "complete", "다른 결합 경계는?",
            "정적 슬롯을 보존한다", "설명 뒤 사례에 적용함", created,
        )
        self.server = MobileServer(("127.0.0.1", 0), self.home, clock=lambda: AT)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.status, response.headers, response.read()

    def post(self, path, value):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(value).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def json_get(self, path):
        status, _, body = self.get(path)
        return status, json.loads(body)


class MobileApiTests(MobileTestCase):
    def test_dashboard_is_fast_and_review_records_the_exact_answer(self):
        started = time.perf_counter()
        status, _, body = self.get("/api/dashboard")
        elapsed = time.perf_counter() - started
        dashboard = json.loads(body)
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 2)
        self.assertLess(dashboard["meta"]["server_ms"], 2000)
        self.assertEqual(dashboard["current"]["id"], self.knowledge["id"])
        self.assertEqual(dashboard["current"]["phase"], "retrieval")

        answer = "보간 뒤의 정적 슬롯을 보존하기 위해 빈 문자열이 남습니다."
        status, reviewed = self.post(
            "/api/review",
            {
                "knowledge_id": self.knowledge["id"], "answer": answer, "rating": "hard",
                "expected_revision": dashboard["state_revision"],
                "request_id": "review_mobile_api_0001",
                "base_sequence": dashboard["current"]["base_sequence"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["phase"], "retrieval")
        self.assertEqual(reviewed["confidence"], "partial")
        stored = next(
            item for item in Engine(Store(self.home)).knowledge()
            if item["id"] == self.knowledge["id"]
        )
        self.assertEqual(stored["last_interaction"]["answer"], answer)
        self.assertEqual(stored["memory"]["review_count"], 1)
        self.assertIn("빈 문자열 경계 보존", stored["weak_points"])

    def test_invalid_review_and_unknown_path_are_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.post("/api/review", {"answer": "missing id"})
        self.assertEqual(invalid.exception.code, 400)
        invalid.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.get("/../become.py")
        self.assertEqual(missing.exception.code, 404)
        missing.exception.close()


class MobileCampusApiTests(MobileTestCase):
    def test_today_curriculum_history_and_campus_are_phase_specific_and_lazy(self):
        _, today = self.json_get("/api/dashboard")
        self.assertEqual(today["status"], "ready")
        self.assertEqual(today["current"]["phase"], "retrieval")
        self.assertNotIn("explanation", today["current"])
        self.assertIn("primary_action", today)
        self.assertIn("progress", today)
        self.assertLess(len(json.dumps(today, ensure_ascii=False).encode()), 4096)

        with self.assertRaises(urllib.error.HTTPError) as hidden:
            self.get(f"/api/explanation?id={self.knowledge['id']}")
        self.assertEqual(hidden.exception.code, 410)
        hidden.exception.close()

        status, curriculum = self.json_get("/api/curriculum")
        self.assertEqual(status, 200)
        self.assertEqual(curriculum["status"], "empty")
        status, history = self.json_get("/api/history?limit=1&cursor=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["items"]), 1)
        self.assertIsNotNone(history["next_cursor"])
        status, campus = self.json_get("/api/campus")
        self.assertEqual(status, 200)
        self.assertEqual(campus["goal"], "Python 전문가")
        self.assertTrue(campus["state_revision"].startswith("sha256:"))
        self.assertEqual(campus["jobs"], [])

    def test_learner_artifact_submission_review_and_revision_are_real_versions(self):
        _, board = self.json_get("/api/dashboard")
        status, created = self.post("/api/artifacts", {
            "title": "운영 판단 메모", "purpose": "큐 용량 선택", "audience": "운영팀",
            "content": "처리량을 측정한다.", "milestone_id": None,
            "expected_revision": board["state_revision"],
        })
        self.assertEqual(status, 201)
        artifact_id = created["artifact"]["id"]
        criteria = {
            dimension: {"status": "pass", "note": "현재 버전에서 충족"}
            for dimension in EDITOR_DIMENSIONS
        }
        criteria["evidence"] = {
            "status": "revise", "note": "수치 근거 필요", "severity": "blocking",
            "evidence_span": "처리량을 측정한다.", "diagnosis": "측정값이 없다",
            "revision_action": "생산·소비 처리량 수치를 넣는다",
        }
        Engine(Store(self.home)).artifact_review(
            artifact_id, criteria, "revise", "측정 수치를 추가한다", AT
        )
        _, campus = self.json_get("/api/campus")
        artifact = next(item for item in campus["artifacts"] if item["id"] == artifact_id)
        self.assertEqual(artifact["status"], "needs_revision")
        self.assertEqual(artifact["findings"][0]["evidence_span"], "처리량을 측정한다.")
        status, revised = self.post(f"/api/artifacts/{artifact_id}/revision", {
            "content": "생산 120rps, 소비 100rps를 측정해 차이 20rps를 용량 근거로 쓴다.",
            "expected_revision": campus["state_revision"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(revised["artifact"]["version"], 2)
        stored = Engine(Store(self.home)).artifact_show(artifact_id)
        self.assertEqual(stored["versions"][-1]["author"], "learner")
        self.assertEqual(stored["current_version"], 2)


class MobileOfflineSyncTests(MobileTestCase):
    def test_same_offline_submission_syncs_exactly_once_and_duplicate_gets_receipt(self):
        _, board = self.json_get("/api/dashboard")
        submission = {
            "knowledge_id": self.knowledge["id"], "answer": "정적 슬롯 경계를 보존한다",
            "rating": "good", "request_id": "offline_sync_exact_0001",
            "base_sequence": board["current"]["base_sequence"],
            "expected_revision": board["state_revision"],
        }
        status, first = self.post("/api/reviews/sync", {"submissions": [submission]})
        self.assertEqual(status, 200)
        self.assertEqual(first["results"][0]["status"], "saved")
        self.assertFalse(first["results"][0]["receipt"]["duplicate"])
        status, duplicate = self.post("/api/reviews/sync", {"submissions": [submission]})
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["results"][0]["receipt"]["duplicate"])
        events = [
            json.loads(line) for line in Store(self.home).reviews_path.read_text().splitlines()
            if json.loads(line).get("request_id") == submission["request_id"]
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["answer"], submission["answer"])

    def test_stale_offline_answer_stays_a_visible_conflict_without_a_write(self):
        _, board = self.json_get("/api/dashboard")
        submission = {
            "knowledge_id": self.knowledge["id"], "answer": "오래된 답안", "rating": "hard",
            "request_id": "offline_sync_stale_0001",
            "base_sequence": board["current"]["base_sequence"] + 1,
            "expected_revision": board["state_revision"],
        }
        before = Store(self.home).reviews_path.read_bytes()
        _, result = self.post("/api/reviews/sync", {"submissions": [submission]})
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(result["results"][0]["error"]["code"], "attempt_stale")
        self.assertEqual(Store(self.home).reviews_path.read_bytes(), before)


class MobileCampusContractTests(MobileTestCase):
    def test_mobile_shell_has_four_areas_accessibility_modes_and_small_javascript(self):
        _, _, index = self.get("/")
        html = index.decode()
        self.assertEqual(html.count('class="bottom-nav"'), 1)
        for view in ("today", "curriculum", "history", "campus"):
            self.assertIn(f'data-view="{view}"', html)
        self.assertIn('class="skip-link"', html)
        self.assertIn('aria-label="주요 메뉴"', html)
        _, _, css = self.get("/styles.css")
        styles = css.decode()
        self.assertIn("min-height: 44px", styles)
        self.assertIn("prefers-color-scheme: dark", styles)
        self.assertIn("prefers-reduced-motion: reduce", styles)
        self.assertIn(':root[data-theme="dark"]', styles)
        self.assertIn(':root[data-motion="reduced"]', styles)
        self.assertIn("safe-area-inset-bottom", styles)
        self.assertIn(":focus-visible", styles)
        _, _, script = self.get("/app.js")
        self.assertLess(len(gzip.compress(script, compresslevel=9)), 100 * 1024)
        app = script.decode()
        self.assertIn("/api/reviews/sync", app)
        self.assertIn("request_id", app)
        self.assertIn("visualViewport", app)
        self.assertIn("applyPreferences", app)
        self.assertNotIn("/api/explanation?id=", app)


class MobileAgentApiTests(MobileTestCase):
    def wait_for(self, job_id, state, timeout=4):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, body = self.json_get(f"/api/jobs/{job_id}")
            if body["job"]["state"] == state:
                return body["job"]
            time.sleep(0.03)
        self.fail(f"job {job_id} did not reach {state}")

    def test_async_job_returns_immediately_persists_progress_and_recovers_by_id(self):
        runner = BlockingRunner()
        self.server.runner = runner
        _, board = self.json_get("/api/dashboard")
        started = time.perf_counter()
        status, body = self.post(
            "/api/jobs",
            {"message": "Tutor와 백프레셔를 이어서 배우자", "expected_revision": board["state_revision"]},
        )
        self.assertEqual(status, 202)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(body["job"]["state"], "queued")
        job_id = body["job"]["id"]
        self.assertTrue(runner.started.wait(2))
        running = self.wait_for(job_id, "running")
        self.assertEqual([event["seq"] for event in running["events"]], sorted(event["seq"] for event in running["events"]))
        self.assertTrue(any(event["role"] == "tutor" for event in running["events"]))
        runner.release.set()
        completed = self.wait_for(job_id, "completed")
        self.assertEqual(completed["result"]["response"], runner.response)
        self.assertEqual(completed["result"]["roles_advanced"], [])
        self.assertEqual(self.json_get(f"/api/jobs/{job_id}")[1]["job"]["id"], job_id)
        summary = self.json_get(f"/api/jobs/{job_id}?summary=1")[1]["job"]
        self.assertEqual(summary["response"], runner.response)
        self.assertNotIn("events", summary)
        self.assertNotIn("message", summary)
        self.assertEqual(self.home.joinpath("mobile-jobs.json").stat().st_mode & 0o777, 0o600)

    def test_concurrent_review_wins_and_agent_import_fails_closed(self):
        runner = BlockingRunner()
        self.server.runner = runner
        _, board = self.json_get("/api/dashboard")
        _, body = self.post(
            "/api/jobs", {"message": "상태를 갱신해줘", "expected_revision": board["state_revision"]},
        )
        job_id = body["job"]["id"]
        self.assertTrue(runner.started.wait(2))
        answer = "동시에 입력한 내 답은 보존되어야 합니다."
        self.post(
            "/api/review",
            {
                "knowledge_id": self.knowledge["id"], "answer": answer,
                "rating": "hard", "expected_revision": board["state_revision"],
                "request_id": "review_concurrent_0001",
                "base_sequence": board["current"]["base_sequence"],
            },
        )
        runner.release.set()
        failed = self.wait_for(job_id, "failed")
        self.assertEqual(failed["error"]["code"], "state_conflict")
        stored = Engine(Store(self.home))._find(
            Engine(Store(self.home)).knowledge(), self.knowledge["id"], "knowledge"
        )
        self.assertEqual(stored["last_interaction"]["answer"], answer)

    def test_valid_but_truncated_candidate_cannot_delete_live_learning_data(self):
        self.server.runner = DestructiveRunner()
        _, board = self.json_get("/api/dashboard")
        state_before = self.home.joinpath("state.json").read_bytes()
        reviews_before = self.home.joinpath("reviews.jsonl").read_bytes()
        _, body = self.post(
            "/api/jobs", {"message": "상태 축소", "expected_revision": board["state_revision"]},
        )
        failed = self.wait_for(body["job"]["id"], "failed")
        self.assertEqual(failed["error"]["code"], "candidate_data_loss")
        self.assertEqual(self.home.joinpath("state.json").read_bytes(), state_before)
        self.assertEqual(self.home.joinpath("reviews.jsonl").read_bytes(), reviews_before)
        self.assertEqual(state_revision(Store(self.home)), board["state_revision"])

    def test_snapshot_import_rolls_back_both_files_at_each_write_boundary(self):
        for failed_name in ("reviews.jsonl", "state.json"):
            with self.subTest(failed_name=failed_name):
                self.server.runner = ReviewRunner()
                _, board = self.json_get("/api/dashboard")
                state_before = self.home.joinpath("state.json").read_bytes()
                reviews_before = self.home.joinpath("reviews.jsonl").read_bytes()
                original = Store._replace_bytes
                injected = {"done": False}

                def fail_once(path, raw):
                    if path.parent == self.home and path.name == failed_name and not injected["done"]:
                        injected["done"] = True
                        raise OSError("injected write failure")
                    return original(path, raw)

                with mock.patch.object(Store, "_replace_bytes", new=staticmethod(fail_once)):
                    _, body = self.post(
                        "/api/jobs",
                        {"message": "복습 반영", "expected_revision": board["state_revision"]},
                    )
                    failed = self.wait_for(body["job"]["id"], "failed")
                self.assertEqual(failed["error"]["code"], "validation_failed")
                self.assertEqual(self.home.joinpath("state.json").read_bytes(), state_before)
                self.assertEqual(self.home.joinpath("reviews.jsonl").read_bytes(), reviews_before)
                self.assertEqual(state_revision(Store(self.home)), board["state_revision"])

    def test_external_cli_write_in_final_import_window_is_not_overwritten(self):
        runner = BlockingRunner()
        self.server.runner = runner
        _, board = self.json_get("/api/dashboard")
        reached = threading.Event()
        release_import = threading.Event()
        original_emit = self.server.jobs.emit

        def pause_before_transaction(job_id, kind, message, role=None):
            original_emit(job_id, kind, message, role)
            if message == "검증된 학습 상태를 저장하고 있습니다.":
                reached.set()
                release_import.wait(4)

        self.server.jobs.emit = pause_before_transaction
        try:
            _, body = self.post(
                "/api/jobs", {"message": "상태 갱신", "expected_revision": board["state_revision"]},
            )
            self.assertTrue(runner.started.wait(2))
            runner.release.set()
            self.assertTrue(reached.wait(2))
            Engine(Store(self.home)).advisor_init(
                "동시에 저장한 새 목표", "중급", ["동시성"], 0.9, AT
            )
            release_import.set()
            failed = self.wait_for(body["job"]["id"], "failed")
        finally:
            release_import.set()
            self.server.jobs.emit = original_emit
        self.assertEqual(failed["error"]["code"], "state_conflict")
        self.assertEqual(Store(self.home).load()["profile"]["goal"], "동시에 저장한 새 목표")

    def test_store_lock_serializes_cli_writer_that_starts_during_agent_commit(self):
        self.server.runner = ReviewRunner()
        _, board = self.json_get("/api/dashboard")
        original = Store._replace_bytes
        entered_commit = threading.Event()
        release_commit = threading.Event()
        paused = {"done": False}
        external_errors = []

        def pause_state_replace(path, raw):
            if path == self.home / "state.json" and not paused["done"]:
                paused["done"] = True
                entered_commit.set()
                release_commit.wait(4)
            return original(path, raw)

        def external_write():
            try:
                Engine(Store(self.home)).advisor_init(
                    "뒤늦은 외부 목표", "중급", ["동시성"], 0.9, AT
                )
            except Exception as error:
                external_errors.append(error)

        with mock.patch.object(Store, "_replace_bytes", new=staticmethod(pause_state_replace)):
            _, body = self.post(
                "/api/jobs", {"message": "복습 반영", "expected_revision": board["state_revision"]},
            )
            self.assertTrue(entered_commit.wait(2))
            external = threading.Thread(target=external_write)
            external.start()
            time.sleep(0.05)
            self.assertTrue(external.is_alive())
            release_commit.set()
            completed = self.wait_for(body["job"]["id"], "completed")
            external.join(timeout=2)
        self.assertFalse(external.is_alive())
        self.assertEqual(external_errors, [])
        self.assertEqual(Store(self.home).load()["profile"]["goal"], "뒤늦은 외부 목표")
        self.assertTrue(completed["result"]["result_revision"].startswith("sha256:"))

    def test_running_job_can_be_cancelled_without_import(self):
        runner = BlockingRunner()
        self.server.runner = runner
        _, board = self.json_get("/api/dashboard")
        _, body = self.post(
            "/api/jobs", {"message": "오래 걸리는 학습", "expected_revision": board["state_revision"]},
        )
        job_id = body["job"]["id"]
        self.assertTrue(runner.started.wait(2))
        status, _ = self.post(f"/api/jobs/{job_id}/cancel", {})
        self.assertEqual(status, 202)
        interrupted = self.wait_for(job_id, "interrupted")
        self.assertEqual(interrupted["error"]["code"], "cancelled")

    def test_stale_running_job_is_interrupted_on_server_restart(self):
        other_home = Path(self.temporary.name) / "restart-data"
        other_home.mkdir()
        job_id = "job-abcdefghijklmnopqrst"
        other_home.joinpath("mobile-jobs.json").write_text(json.dumps({
            "version": 1,
            "jobs": [{
                "id": job_id, "state": "running", "message": "old",
                "expected_revision": "sha256:old", "reply_to": None,
                "queued_at": AT.isoformat(), "started_at": AT.isoformat(), "finished_at": None,
                "cancel_requested_at": None,
                "events": [{
                    "seq": 1, "at": AT.isoformat(), "kind": "state", "state": "running",
                    "role": None, "message": "running",
                }],
                "first_event_seq": 1, "result": None, "error": None,
            }],
        }), encoding="utf-8")
        runner = BlockingRunner()
        restarted = MobileServer(("127.0.0.1", 0), other_home, clock=lambda: AT, runner=runner)
        try:
            recovered = restarted.jobs.get(job_id)
            self.assertEqual(recovered["state"], "interrupted")
            self.assertEqual(recovered["error"]["code"], "server_restarted")
        finally:
            restarted.server_close()

    def test_exposure_becomes_delayed_retrieval_only_after_the_exact_due_time(self):
        other_home = Path(self.temporary.name) / "phase-data"
        clock = {"now": AT}
        engine = Engine(Store(other_home))
        engine.advisor_init("시스템 설계자", "초급", ["백프레셔"], 0.9, AT)
        anchor = engine.knowledge_add("이벤트 루프", "이미 아는 기준", "concept", [], [], [], AT)
        item = engine.knowledge_add(
            "백프레셔", "생산 속도를 소비 속도에 맞춘다", "concept", ["용량 선택"], [], [], AT
        )
        engine.knowledge_relate(item["id"], anchor["id"], AT)
        engine.teach(
            item["id"],
            "왜 쓰는가: 과부하를 막는다. 왜 이렇게 되었는가: 생산과 소비 속도가 다르다. "
            "왜 이 결과가 나오는가: 경계에서 유입을 제한하기 때문이다. "
            "그래서 어디에 쓰는가: bounded queue에 쓴다.",
            "이벤트 루프와 연결", AT,
        )
        server = MobileServer(
            ("127.0.0.1", 0), other_home, clock=lambda: clock["now"], runner=BlockingRunner()
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, value=None):
            data = json.dumps(value).encode() if value is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            with urllib.request.urlopen(
                urllib.request.Request(base + path, data=data, headers=headers), timeout=2
            ) as response:
                return json.loads(response.read())

        try:
            before = request("/api/dashboard")
            self.assertEqual(before["current"]["phase"], "exposure")
            first = request("/api/review", {
                "knowledge_id": item["id"], "answer": "입구에서 생산률을 제한한다",
                "rating": "good", "expected_revision": before["state_revision"],
                "request_id": "review_exposure_0001",
                "base_sequence": before["current"]["base_sequence"],
            })
            self.assertEqual(first["phase"], "exposure")
            self.assertEqual(first["memory"]["review_count"], 0)
            due_at = first["memory"]["due_at"]
            clock["now"] = datetime.fromisoformat(due_at.replace("Z", "+00:00")) + timedelta(seconds=1)
            due = request("/api/dashboard")
            self.assertEqual(due["current"]["phase"], "retrieval")
            self.assertEqual(due["current"]["due_at"], due_at)
            second = request("/api/review", {
                "knowledge_id": item["id"], "answer": "새 사례에서도 유입률과 소비율을 비교한다",
                "rating": "good", "expected_revision": due["state_revision"],
                "request_id": "review_retrieval_0001",
                "base_sequence": due["current"]["base_sequence"],
            })
            self.assertEqual(second["phase"], "retrieval")
            self.assertEqual(second["memory"]["review_count"], 1)
            reviews = [
                json.loads(line) for line in Store(other_home).reviews_path.read_text().splitlines()
                if json.loads(line).get("interaction") == "review"
            ]
            self.assertEqual([event["phase"] for event in reviews], ["exposure", "retrieval"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_codex_runner_subprocess_contract_uses_actual_final_message(self):
        workspace = Path(self.temporary.name) / "runner-workspace"
        workspace.mkdir()
        executable = workspace / "fake-codex"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "args=sys.argv\n"
            "assert not ('--approve-for-me' in args and '--sandbox' in args)\n"
            "final=pathlib.Path(args[args.index('--output-last-message')+1])\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'turn.started'}), flush=True)\n"
            "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
            "final.write_text(json.dumps({'outcome':'completed','response':'REAL FINAL',"
            "'input_request':None,'roles_advanced':['advisor'],'workflow_id':None}))\n",
            encoding="utf-8",
        )
        os.chmod(executable, 0o755)
        events = []
        result = CodexRunner(str(executable), timeout=5).run(
            workspace, "시작하자", None,
            lambda kind, message, role=None: events.append((kind, message, role)),
            lambda: False,
        )
        self.assertEqual(result["response"], "REAL FINAL")
        self.assertTrue(any(kind == "stage" for kind, _, _ in events))


class MobileSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "secure"
        self.token = "test-secret-token"
        self.server = MobileServer(
            ("127.0.0.1", 0), self.home, clock=lambda: AT,
            runner=BlockingRunner(), token=self.token,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, token=None, method="GET", origin=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            data = b"{}"
        return urllib.request.urlopen(
            urllib.request.Request(self.base + path, data=data, headers=headers, method=method),
            timeout=2,
        )

    def test_static_shell_is_public_but_every_api_path_requires_exact_bearer(self):
        with self.request("/") as response:
            self.assertEqual(response.status, 200)
        for supplied in (None, "wrong"):
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.request("/api/dashboard", supplied)
            self.assertEqual(denied.exception.code, 401)
            self.assertEqual(denied.exception.headers["WWW-Authenticate"], "Bearer")
            denied.exception.close()
        with self.request("/api/dashboard", self.token) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        with self.assertRaises(urllib.error.HTTPError) as unknown:
            self.request("/api/does-not-exist")
        self.assertEqual(unknown.exception.code, 401)
        unknown.exception.close()

    def test_authenticated_cross_origin_write_is_rejected_and_token_is_not_persisted(self):
        before = list(self.home.glob("*"))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/jobs", self.token, "POST", "https://attacker.example")
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.assertEqual(before, list(self.home.glob("*")))
        self.assertTrue(bind_is_loopback("127.0.0.1"))
        self.assertFalse(bind_is_loopback("0.0.0.0"))


class MobilePwaTests(MobileTestCase):
    def test_manifest_offline_shell_and_mobile_controls_are_present(self):
        _, headers, body = self.get("/manifest.webmanifest")
        manifest = json.loads(body)
        self.assertIn("application/manifest+json", headers["Content-Type"])
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertTrue(manifest["icons"])

        _, _, service_worker = self.get("/sw.js")
        worker = service_worker.decode()
        for asset in ("/", "/styles.css", "/app.js", "/manifest.webmanifest", "/icon.svg"):
            self.assertIn(asset, worker)

        _, _, index = self.get("/")
        html = index.decode()
        self.assertIn("viewport-fit=cover", html)
        self.assertIn('data-view="today"', html)
        self.assertIn('data-view="curriculum"', html)
        self.assertIn('data-view="history"', html)
        self.assertIn('data-view="campus"', html)
        self.assertIn('id="auth-panel"', html)
        self.assertIn('class="skip-link"', html)
        self.assertIn('aria-label="주요 메뉴"', html)
        self.assertIn('aria-live="polite"', html)
        _, _, app = self.get("/app.js")
        script = app.decode()
        self.assertIn("sessionStorage", script)
        self.assertIn("expected_revision", script)
        self.assertIn("request_id", script)
        self.assertIn("/api/reviews/sync", script)
        self.assertIn('startsWith("/api/")', worker)
        self.assertIn('SKIP_WAITING', worker)


if __name__ == "__main__":
    unittest.main()
