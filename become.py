#!/usr/bin/env python3
"""Local-first state engine for the five ALTER roles."""

from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


RATINGS = {"again", "hard", "good", "easy"}
CONFIDENCE = {"complete", "partial", "failed"}
KNOWLEDGE_TYPES = {"recall", "concept", "mixed"}
GOAL_KINDS = {"practical", "remedial"}
GOAL_STATUSES = {"active", "completed"}
CURRICULUM_DECISIONS = ("destination", "baseline", "sequencing", "cut_list", "milestones")
CURATION_AXES = {
    "relevance": {"belongs", "does_not_belong"},
    "credibility": {"credible", "unverified"},
    "level_fit": {"appropriate", "too_advanced", "too_basic"},
    "signal": {"signal", "noise"},
}
MATERIAL_DISPOSITIONS = {"core", "supplement", "reject"}
EDITOR_DIMENSIONS = {
    "thinking", "logic", "evidence", "repetition", "structure", "precision", "accuracy"
}
PERSPECTIVE_STATUSES = {"insight", "no_connection", "needs_verification"}
PERSPECTIVE_REQUEST_FIELDS = {"current_field", "current_problem"}
LEGACY_PERSPECTIVE_REQUEST_FIELDS = PERSPECTIVE_REQUEST_FIELDS | {
    "outside_field", "lens", "question"
}
SPECIALISTS = {"advisor", "librarian", "tutor", "editor", "roommate"}
ACTORS = SPECIALISTS | {"orchestrator", "learner"}
TEACHING_WHY_LABELS = (
    "왜 쓰는가:",
    "왜 이렇게 되었는가:",
    "왜 이 결과가 나오는가:",
    "그래서 어디에 쓰는가:",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime:
    if not value:
        return now_utc()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def canonical_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def strip_nested_strings(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value).strip()
    if isinstance(value, list):
        return [strip_nested_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_nested_strings(item) for key, item in value.items()}
    return value


def teaching_why_chain(value: str) -> dict[str, str]:
    text = unicodedata.normalize("NFKC", value).strip()
    positions = [text.find(label) for label in TEACHING_WHY_LABELS]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise ValueError(
            "teaching explanation requires 왜 쓰는가, 왜 이렇게 되었는가, "
            "왜 이 결과가 나오는가, 그래서 어디에 쓰는가"
        )
    sections = {}
    for index, label in enumerate(TEACHING_WHY_LABELS):
        start = positions[index] + len(label)
        end = positions[index + 1] if index + 1 < len(positions) else len(text)
        content = text[start:end].strip()
        if not content:
            raise ValueError(f"teaching explanation section is empty: {label}")
        sections[label[:-1]] = content
    return sections


def default_state() -> dict:
    return {
        "version": 4,
        "profile": None,
        "knowledge": [],
        "materials": [],
        "shelves": [],
        "artifacts": [],
        "perspectives": [],
        "sessions": [],
        "active_session_id": None,
        "handoffs": [],
        "workflows": [],
    }


def normalize_curriculum_profile(profile: dict) -> None:
    profile.setdefault("curriculum", None)
    profile.setdefault("curriculum_history", [])
    profile.setdefault("curriculum_migration_required", False)
    profile.setdefault("curriculum_replan_required", False)
    profile.setdefault("profile_history", [])
    curriculum = profile.get("curriculum")
    if not curriculum:
        return
    sequence = curriculum.get("sequence", [])
    milestones = curriculum.get("milestones", [])
    step_ids = {
        step.get("id") for step in sequence if isinstance(step, dict) and step.get("id")
    }
    covered = {
        item.get("step_id")
        for item in milestones
        if isinstance(item, dict) and item.get("step_id") in step_ids
    }
    if not sequence or not milestones or covered != step_ids:
        legacy = copy.deepcopy(curriculum)
        legacy["migration_status"] = "requires_step_milestone_replanning"
        profile["curriculum_history"].append(legacy)
        profile["curriculum"] = None
        profile["curriculum_migration_required"] = True
        return
    curriculum.setdefault("version", 1)
    for milestone in milestones:
        milestone.setdefault("status", "planned")
        milestone.setdefault("evidence_artifact_id", None)
    completed_steps = {
        step["id"]
        for step in sequence
        if all(
            item.get("status") == "completed"
            for item in milestones
            if item["step_id"] == step["id"]
        )
    }
    active_assigned = False
    for step in sequence:
        step.setdefault("completed_at", None)
        if step["id"] in completed_steps:
            step["status"] = "completed"
        elif not active_assigned and all(
            prerequisite in completed_steps for prerequisite in step.get("prerequisites", [])
        ):
            step["status"] = "active"
            active_assigned = True
        else:
            step["status"] = "waiting"
    curriculum["status"] = (
        "completed" if all(step["status"] == "completed" for step in sequence) else "ready"
    )


_UNSET = object()


class Store:
    """One-user JSON store with atomic replacement."""

    def __init__(self, home: Path):
        self.home = home
        self.state_path = home / "state.json"
        self.reviews_path = home / "reviews.jsonl"
        self.lock_path = home / ".store.lock"
        self.journal_path = home / ".store-transaction.json"
        self._expected_state_token: str | None | object = _UNSET
        self._thread_lock = threading.RLock()
        self._transaction = threading.local()

    @staticmethod
    def _token(raw: bytes | None) -> str | None:
        return hashlib.sha256(raw).hexdigest() if raw is not None else None

    @staticmethod
    def _replace_bytes(path: Path, raw: bytes) -> None:
        handle, temporary_name = tempfile.mkstemp(
            prefix=f"{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_name, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @contextmanager
    def transaction(self):
        """Serialize every writer that targets this local state directory."""
        self.home.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            depth = getattr(self._transaction, "depth", 0)
            if depth:
                self._transaction.depth = depth + 1
                try:
                    yield
                finally:
                    self._transaction.depth -= 1
                return
            with self.lock_path.open("a+b") as lock:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self._transaction.depth = 1
                try:
                    yield
                finally:
                    self._transaction.depth = 0
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _state_raw(self) -> bytes | None:
        return self.state_path.read_bytes() if self.state_path.exists() else None

    def _clear_journal(self) -> None:
        if self.journal_path.exists():
            self.journal_path.unlink()
            directory = os.open(self.home, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _recover_locked(self) -> None:
        if not self.journal_path.exists():
            return
        if (
            self.journal_path.is_symlink() or not self.journal_path.is_file()
            or self.journal_path.stat().st_size > 32 * 1024 * 1024
        ):
            raise ValueError("invalid store transaction journal")
        value = json.loads(self.journal_path.read_bytes())
        if not isinstance(value, dict) or set(value) != {
            "version", "state_exists", "state", "reviews_exist", "reviews"
        } or value["version"] != 1 or not all(
            isinstance(value[key], bool) for key in ("state_exists", "reviews_exist")
        ) or not all(isinstance(value[key], str) for key in ("state", "reviews")):
            raise ValueError("invalid store transaction journal")
        try:
            state_raw = base64.b64decode(value["state"], validate=True)
            reviews_raw = base64.b64decode(value["reviews"], validate=True)
        except ValueError as error:
            raise ValueError("invalid store transaction journal") from error
        if value["reviews_exist"]:
            self._replace_bytes(self.reviews_path, reviews_raw)
        elif self.reviews_path.exists():
            self.reviews_path.unlink()
        if value["state_exists"]:
            self._replace_bytes(self.state_path, state_raw)
        elif self.state_path.exists():
            self.state_path.unlink()
        self._clear_journal()

    def _write_journal(
        self, base_state: bytes | None, base_reviews: bytes, reviews_exist: bool
    ) -> None:
        raw = json.dumps(
            {
                "version": 1,
                "state_exists": base_state is not None,
                "state": base64.b64encode(base_state or b"").decode(),
                "reviews_exist": reviews_exist,
                "reviews": base64.b64encode(base_reviews).decode(),
            },
            sort_keys=True,
        ).encode()
        self._replace_bytes(self.journal_path, raw)

    def _commit(
        self,
        state: dict,
        *,
        reviews: bytes | object = _UNSET,
        review_event: dict | object = _UNSET,
    ) -> None:
        state_raw = (
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        with self.transaction():
            self._recover_locked()
            base_state = self._state_raw()
            current_token = self._token(base_state)
            if (
                self._expected_state_token is _UNSET and base_state is not None
                or self._expected_state_token is not _UNSET
                and current_token != self._expected_state_token
            ):
                raise ValueError("state_conflict")
            base_reviews_exists = self.reviews_path.exists()
            base_reviews = self.reviews_path.read_bytes() if base_reviews_exists else b""
            next_reviews = reviews
            if review_event is not _UNSET:
                next_reviews = base_reviews + (
                    json.dumps(review_event, ensure_ascii=False, sort_keys=True) + "\n"
                ).encode()
            pair_commit = next_reviews is not _UNSET and next_reviews != base_reviews
            try:
                if pair_commit:
                    self._write_journal(base_state, base_reviews, base_reviews_exists)
                    self._replace_bytes(self.reviews_path, next_reviews)
                self._replace_bytes(self.state_path, state_raw)
                if pair_commit:
                    self._clear_journal()
            except Exception as error:
                try:
                    if self.journal_path.exists():
                        self._recover_locked()
                    else:
                        if base_reviews_exists:
                            self._replace_bytes(self.reviews_path, base_reviews)
                        elif self.reviews_path.exists():
                            self.reviews_path.unlink()
                        if base_state is not None:
                            self._replace_bytes(self.state_path, base_state)
                        elif self.state_path.exists():
                            self.state_path.unlink()
                except Exception as rollback_error:
                    raise RuntimeError("rollback_failed") from rollback_error
                raise error
            self._expected_state_token = self._token(state_raw)

    @staticmethod
    def _validate_nested(state: dict) -> None:
        profile = state.get("profile") or {}
        curriculum = profile.get("curriculum")
        if curriculum:
            Engine._validate_curriculum_spec(curriculum, stored=True)
            if (
                not str(curriculum.get("id", "")).strip()
                or isinstance(curriculum.get("version"), bool)
                or not isinstance(curriculum.get("version"), int)
                or curriculum["version"] < 1
                or curriculum.get("status") not in {"ready", "completed"}
            ):
                raise ValueError("invalid state; malformed current curriculum identity")
            interview = curriculum.get("interview")
            if not isinstance(interview, list) or any(
                not isinstance(item, dict)
                or item.get("decision") not in CURRICULUM_DECISIONS
                or not str(item.get("question", "")).strip()
                or not str(item.get("answer", "")).strip()
                or not str(item.get("asked_at", "")).strip()
                or not str(item.get("answered_at", "")).strip()
                for item in interview
            ):
                raise ValueError("invalid state; malformed committed curriculum interview")
            if any(
                not 1 <= sum(item["decision"] == decision for item in interview) <= 5
                for decision in CURRICULUM_DECISIONS
            ):
                raise ValueError("invalid state; committed curriculum lacks five-decision evidence")
            questions = [canonical_text(item["question"]) for item in interview]
            answers = [canonical_text(item["answer"]) for item in interview]
            if len(questions) != len(set(questions)) or len(answers) != len(set(answers)):
                raise ValueError("invalid state; curriculum decisions need distinct evidence")
            for milestone in curriculum["milestones"]:
                if milestone.get("status") not in {"planned", "completed"}:
                    raise ValueError("invalid state; malformed milestone status")
                evidence = milestone.get("evidence_artifact_id")
                if (milestone["status"] == "completed") != bool(evidence):
                    raise ValueError("invalid state; milestone completion and evidence disagree")
            completed_steps = {
                step["id"]
                for step in curriculum["sequence"]
                if all(
                    milestone["status"] == "completed"
                    for milestone in curriculum["milestones"]
                    if milestone["step_id"] == step["id"]
                )
            }
            active_assigned = False
            expected_statuses = []
            for step in curriculum["sequence"]:
                if step["id"] in completed_steps:
                    expected = "completed"
                elif not active_assigned and all(
                    prerequisite in completed_steps
                    for prerequisite in step.get("prerequisites", [])
                ):
                    expected = "active"
                    active_assigned = True
                else:
                    expected = "waiting"
                expected_statuses.append(expected)
            if [step.get("status") for step in curriculum["sequence"]] != expected_statuses:
                raise ValueError("invalid state; curriculum step progression is inconsistent")
            expected_curriculum_status = (
                "completed" if all(status == "completed" for status in expected_statuses)
                else "ready"
            )
            if curriculum["status"] != expected_curriculum_status:
                raise ValueError("invalid state; curriculum status is inconsistent")
        advisor_ids = [goal.get("id") for goal in profile.get("learning_goals", [])]
        if curriculum:
            advisor_ids.extend(
                [curriculum.get("id")]
                + [milestone.get("id") for milestone in curriculum.get("milestones", [])]
            )
        if len(advisor_ids) != len(set(advisor_ids)):
            raise ValueError("invalid state; Advisor resource ids must be unique")

        for material in state["materials"]:
            verification = material.get("verification", {})
            if not isinstance(verification, dict):
                raise ValueError("invalid state; material verification must be an object")
            verification.setdefault("source_identity", "")
            verification.setdefault("content_fingerprint", "")
            curation = material.get("curation", {})
            if not isinstance(curation, dict):
                raise ValueError("invalid state; material curation must be an object")
            disposition = curation.get("disposition", "untriaged")
            if disposition not in MATERIAL_DISPOSITIONS | {"untriaged"}:
                raise ValueError("invalid state; malformed material disposition")
            if disposition == "untriaged":
                continue
            for axis, allowed in CURATION_AXES.items():
                decision = curation.get(axis)
                if (
                    not isinstance(decision, dict)
                    or decision.get("decision") not in allowed
                    or not str(decision.get("reason", "")).strip()
                ):
                    raise ValueError(f"invalid state; malformed curation {axis}")
            priority = curation.get("priority")
            if (
                not str(curation.get("curriculum_id", "")).strip()
                or isinstance(curation.get("curriculum_version"), bool)
                or not isinstance(curation.get("curriculum_version"), int)
                or not str(curation.get("step_id", "")).strip()
                or isinstance(priority, bool)
                or not isinstance(priority, int)
                or not 1 <= priority <= 5
                or not str(curation.get("disposition_reason", "")).strip()
                or not str(curation.get("evaluated_at", "")).strip()
            ):
                raise ValueError("invalid state; malformed material curation binding")
            if disposition in {"core", "supplement"} and not material.get("verified"):
                raise ValueError("invalid state; unverified material cannot be shelf signal")

        material_ids = {item["id"] for item in state["materials"]}
        for shelf in state["shelves"]:
            selected = shelf.get("selected_material_ids")
            candidates = shelf.get("candidate_material_ids")
            if (
                shelf.get("status") not in {"ready", "incomplete"}
                or not isinstance(selected, list)
                or not isinstance(candidates, list)
                or not all(isinstance(item_id, str) for item_id in selected + candidates)
                or not set(selected) <= set(candidates) <= material_ids
                or (shelf["status"] == "ready" and not 3 <= len(selected) <= 4)
                or (shelf["status"] == "incomplete" and len(selected) >= 3)
            ):
                raise ValueError("invalid state; malformed source shelf")

        for item in state["knowledge"]:
            weak_points = item.get("weak_points")
            history = item.get("confusion_history")
            subject_binding = item.get("subject_binding")
            if (
                not isinstance(weak_points, list)
                or len(weak_points) != len(set(weak_points))
                or not all(isinstance(point, str) and point.strip() for point in weak_points)
                or not isinstance(history, list)
                or any(
                    not isinstance(entry, dict)
                    or not str(entry.get("point", "")).strip()
                    or isinstance(entry.get("times_seen"), bool)
                    or not isinstance(entry.get("times_seen"), int)
                    or entry["times_seen"] < 1
                    for entry in history
                )
                or len(history) != len({entry["point"] for entry in history})
                or set(weak_points)
                != {entry["point"] for entry in history if entry.get("resolved_at") is None}
                or subject_binding is not None
                and (
                    not isinstance(subject_binding, dict)
                    or not str(subject_binding.get("goal", "")).strip()
                    or not isinstance(subject_binding.get("focus"), list)
                )
            ):
                raise ValueError("invalid state; weak points and confusion history disagree")

        for artifact in state["artifacts"]:
            versions = artifact.get("versions")
            current = artifact.get("current_version")
            migration_import_required = artifact.get("migration_import_required", False)
            if (
                artifact.get("status") not in {"draft", "needs_revision", "passed"}
                or not isinstance(migration_import_required, bool)
                or isinstance(current, bool)
                or not isinstance(current, int)
                or current < 1
                or not isinstance(versions, list)
                or not all(isinstance(item, dict) for item in versions)
                or [item.get("version") for item in versions] != list(range(1, current + 1))
                or any(
                    item.get("author") not in {"learner", "legacy_unknown"}
                    or not isinstance(item.get("content"), str)
                    or not str(item.get("submitted_at", "")).strip()
                    for item in versions
                )
                or versions[-1].get("content") != artifact.get("content")
                or migration_import_required
                and (
                    versions[-1].get("author") != "legacy_unknown"
                    or artifact.get("status") != "draft"
                    or artifact.get("review_rounds")
                )
                or not migration_import_required
                and versions[-1].get("author") != "learner"
            ):
                raise ValueError("invalid state; malformed learner artifact versions")
            reviewed_versions: set[int] = set()
            for index, round_ in enumerate(artifact.get("review_rounds", []), 1):
                if not isinstance(round_, dict):
                    raise ValueError("invalid state; artifact review rounds must be objects")
                version = round_.get("version")
                if (
                    round_.get("round") != index
                    or not isinstance(version, int)
                    or not 1 <= version <= current
                    or version in reviewed_versions
                    or round_.get("verdict") not in {"revise", "pass"}
                    or set(round_.get("criteria", {})) != EDITOR_DIMENSIONS
                ):
                    raise ValueError("invalid state; malformed artifact review binding")
                reviewed_versions.add(version)
                for dimension, criterion in round_["criteria"].items():
                    if not isinstance(criterion, dict):
                        raise ValueError(f"invalid state; malformed editor criterion {dimension}")
                    if (
                        criterion.get("status") not in {"pass", "revise"}
                        or not str(criterion.get("note", "")).strip()
                    ):
                        raise ValueError(f"invalid state; malformed editor criterion {dimension}")
                    if criterion["status"] == "revise" and (
                        criterion.get("severity") not in {"blocking", "non_blocking"}
                        or any(
                            not str(criterion.get(field, "")).strip()
                            for field in ("evidence_span", "diagnosis", "revision_action")
                        )
                    ):
                        raise ValueError(f"invalid state; incomplete editor finding {dimension}")
                findings = round_.get("findings", [])
                if not isinstance(findings, list) or any(
                    not isinstance(finding, dict)
                    or finding.get("dimension") not in EDITOR_DIMENSIONS
                    or finding.get("status") not in {"open", "resolved", "regressed"}
                    or finding.get("severity") not in {"blocking", "non_blocking"}
                    or finding.get("status") == "regressed"
                    and not str(finding.get("regression_of", "")).strip()
                    or any(
                        not str(finding.get(field, "")).strip()
                        for field in ("evidence_span", "diagnosis", "revision_action")
                    )
                    for finding in findings
                ):
                    raise ValueError("invalid state; malformed editor finding")
            current_round = next(
                (
                    round_
                    for round_ in artifact.get("review_rounds", [])
                    if round_["version"] == current
                ),
                None,
            )
            expected_status = (
                "passed" if current_round and current_round["verdict"] == "pass"
                else "needs_revision" if current_round else "draft"
            )
            if artifact["status"] != expected_status:
                raise ValueError("invalid state; artifact status disagrees with current review")

        for perspective in state["perspectives"]:
            turns = perspective.get("turns")
            connection = perspective.get("connection")
            if (
                not isinstance(turns, list)
                or not turns
                or perspective.get("handoff_id") is not None
                and not isinstance(perspective.get("handoff_id"), str)
            ):
                raise ValueError("invalid state; perspective needs a dialogue turn")
            for turn in turns:
                if not isinstance(turn, dict):
                    raise ValueError("invalid state; perspective turns must be objects")
                response = turn.get("learner_response")
                if (
                    not str(turn.get("question", "")).strip()
                    or (response is not None and not str(response).strip())
                    or (response is None) != (turn.get("answered_at") is None)
                ):
                    raise ValueError("invalid state; malformed perspective turn")
            if connection is None:
                if turns[-1]["learner_response"] is not None:
                    raise ValueError("invalid state; answered perspective needs a connection result")
                continue
            if not isinstance(connection, dict):
                raise ValueError("invalid state; perspective connection must be an object")
            if (
                connection.get("status") not in PERSPECTIVE_STATUSES
                or turns[-1]["learner_response"] is None
                or (
                    connection["status"] == "insight"
                    and any(
                        not str(connection.get(field, "")).strip()
                        for field in ("learner_insight", "mapping", "limits")
                    )
                )
                or (
                    connection["status"] == "needs_verification"
                    and not perspective.get("recommendations")
                )
            ):
                raise ValueError("invalid state; malformed perspective connection")

        handoffs = {item["id"]: item for item in state["handoffs"]}
        workflows = {item["id"]: item for item in state["workflows"]}
        claimed_roles = [
            item.get("to") for item in state["handoffs"] if item.get("status") == "in_progress"
        ]
        if len(claimed_roles) != len(set(claimed_roles)):
            raise ValueError("invalid state; a specialist has multiple in-progress handoffs")
        profile = state.get("profile") or {}
        level_evidence = profile.get("level_evidence", [])
        if not isinstance(level_evidence, list):
            raise ValueError("invalid state; level evidence must be a list")
        consumptions = []
        for entry in level_evidence:
            source_id = entry.get("source_handoff_id") if isinstance(entry, dict) else None
            producer_id = entry.get("produced_by_handoff_id") if isinstance(entry, dict) else None
            if (
                not isinstance(entry, dict)
                or not str(entry.get("at", "")).strip()
                or not str(entry.get("level", "")).strip()
                or not str(entry.get("evidence", "")).strip()
                or (source_id is None) != (producer_id is None)
                or source_id is not None
                and (not isinstance(source_id, str) or not isinstance(producer_id, str))
            ):
                raise ValueError("invalid state; malformed Advisor level evidence")
            if source_id is None:
                continue
            source = handoffs.get(source_id, {})
            producer = handoffs.get(producer_id, {})
            if (
                source.get("to") != "tutor"
                or source.get("status") != "completed"
                or producer.get("to") != "advisor"
                or producer.get("status") not in {"in_progress", "completed", "cancelled"}
                or canonical_text(entry["evidence"])
                not in {
                    canonical_text(value)
                    for value in (source.get("result") or {}).get("observations", [])
                }
            ):
                raise ValueError("invalid state; Advisor evidence source does not match handoffs")
            if producer.get("status") != "cancelled":
                consumptions.append((source_id, canonical_text(entry["evidence"])))
        if len(consumptions) != len(set(consumptions)):
            raise ValueError("invalid state; Tutor observation was consumed more than once")
        if any(
            item.get("handoff_id") is not None
            and (
                item["handoff_id"] not in handoffs
                or handoffs[item["handoff_id"]].get("to") != "roommate"
            )
            for item in state["perspectives"]
        ):
            raise ValueError("invalid state; perspective references an unknown handoff")
        advisor_resources = list(profile.get("learning_goals", []))
        if profile.get("curriculum"):
            advisor_resources.extend(
                [profile["curriculum"], *profile["curriculum"].get("milestones", [])]
            )
        role_resources = {
            "advisor": advisor_resources,
            "librarian": state["materials"] + state["shelves"],
            "tutor": state["knowledge"],
            "editor": state["artifacts"],
        }
        if any(
            resource.get("produced_by_handoff_id") is not None
            and (
                resource["produced_by_handoff_id"] not in handoffs
                or handoffs[resource["produced_by_handoff_id"]].get("to") != role
            )
            for role, resources in role_resources.items()
            for resource in resources
        ):
            raise ValueError("invalid state; role resource producer does not match its handoff")
        allowed_outputs = {
            "advisor": {"curriculum", "advisor_update"},
            "librarian": {"ready_shelf"},
            "tutor": {"knowledge", "step_knowledge", "retrieval_knowledge"},
            "editor": {"reviewed_artifact"},
            "roommate": {"answered_perspective"},
        }
        for handoff in state["handoffs"]:
            dependencies = handoff.get("depends_on")
            dependency_context = handoff.get("dependency_context")
            request_spec = handoff.get("request_spec")
            output_fingerprints = handoff.get("output_fingerprints", {})
            profile_binding = handoff.get("profile_binding")
            resource_snapshot = handoff.get("resource_snapshot", {})
            level_evidence_count = handoff.get("level_evidence_count", 0)
            expected_kind = handoff.get("expected_output_kind")
            expected_ids = handoff.get("expected_resource_ids")
            legacy_untyped = expected_kind is None and (
                handoff.get("legacy_unverified_output")
                or bool(handoff.get("migration_reason"))
            )
            if (
                handoff.get("to") not in SPECIALISTS
                or handoff.get("status") not in {"pending", "in_progress", "completed", "cancelled"}
                or expected_kind not in allowed_outputs.get(handoff.get("to"), set())
                and not legacy_untyped
                or not isinstance(expected_ids, list)
                or not all(isinstance(resource_id, str) for resource_id in expected_ids)
                or not isinstance(dependencies, list)
                or not isinstance(dependency_context, list)
                or request_spec is not None
                and (
                    handoff.get("to") != "roommate"
                    or not isinstance(request_spec, dict)
                    or set(request_spec) != PERSPECTIVE_REQUEST_FIELDS
                    or not all(
                        isinstance(request_spec[field], str) and request_spec[field].strip()
                        for field in PERSPECTIVE_REQUEST_FIELDS
                    )
                )
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"handoff_id", "result"}
                    or item["handoff_id"] not in dependencies
                    or not isinstance(item["result"], dict)
                    for item in dependency_context
                )
                or len(dependency_context)
                != len({item["handoff_id"] for item in dependency_context})
                or not isinstance(output_fingerprints, dict)
                or not isinstance(resource_snapshot, dict)
                or isinstance(level_evidence_count, bool)
                or not isinstance(level_evidence_count, int)
                or level_evidence_count < 0
                or any(
                    not isinstance(resource_id, str) or not isinstance(fingerprint, str)
                    for resource_id, fingerprint in resource_snapshot.items()
                )
                or profile_binding is not None
                and (
                    not isinstance(profile_binding, dict)
                    or not str(profile_binding.get("goal", "")).strip()
                    or not isinstance(profile_binding.get("focus"), list)
                    or not all(isinstance(value, str) for value in profile_binding["focus"])
                    or isinstance(profile_binding.get("target_retention"), bool)
                    or not isinstance(profile_binding.get("target_retention"), (int, float))
                )
                or any(
                    not isinstance(resource_id, str) or not isinstance(fingerprint, str)
                    for resource_id, fingerprint in output_fingerprints.items()
                )
                or any(item_id not in handoffs or item_id == handoff["id"] for item_id in dependencies)
                or (handoff["status"] == "completed") != isinstance(handoff.get("result"), dict)
                or handoff["status"] != "completed" and output_fingerprints
                or (
                    handoff["status"] == "completed"
                    and not handoff["result"].get("resource_ids")
                    and not handoff.get("legacy_unverified_output")
                )
                or (
                    handoff["status"] == "completed"
                    and not set(output_fingerprints)
                    <= set(handoff["result"].get("resource_ids", []))
                )
            ):
                raise ValueError("invalid state; malformed handoff")
            try:
                decoded_snapshots = [
                    json.loads(fingerprint) for fingerprint in resource_snapshot.values()
                ]
            except json.JSONDecodeError as error:
                raise ValueError("invalid state; malformed handoff snapshot") from error
            if any(not isinstance(snapshot, dict) for snapshot in decoded_snapshots):
                raise ValueError("invalid state; malformed handoff snapshot")
            if handoff.get("workflow_id") is not None and handoff["workflow_id"] not in workflows:
                raise ValueError("invalid state; handoff references an unknown workflow")

        for workflow in state["workflows"]:
            steps = workflow.get("steps")
            binding = workflow.get("curriculum_binding")
            profile_binding = workflow.get("profile_binding")
            request_resource_ids = workflow.get("request_resource_ids")
            request_spec = workflow.get("request_spec")
            if (
                workflow.get("status") not in {"active", "completed", "superseded"}
                or not isinstance(steps, list)
                or not steps
                or not isinstance(request_resource_ids, list)
                or not all(isinstance(value, str) for value in request_resource_ids)
                or request_spec is not None
                and (
                    workflow.get("intent") != "perspective"
                    or not isinstance(request_spec, dict)
                    or set(request_spec) != PERSPECTIVE_REQUEST_FIELDS
                    or not all(
                        isinstance(request_spec[field], str) and request_spec[field].strip()
                        for field in PERSPECTIVE_REQUEST_FIELDS
                    )
                )
                or [step.get("order") for step in steps] != list(range(1, len(steps) + 1))
                or len({step.get("id") for step in steps}) != len(steps)
                or profile_binding is not None
                and (
                    not isinstance(profile_binding, dict)
                    or not str(profile_binding.get("goal", "")).strip()
                    or not isinstance(profile_binding.get("focus"), list)
                    or not all(isinstance(value, str) for value in profile_binding["focus"])
                    or isinstance(profile_binding.get("target_retention"), bool)
                    or not isinstance(profile_binding.get("target_retention"), (int, float))
                )
                or binding is not None
                and (
                    not isinstance(binding, dict)
                    or not str(binding.get("id", "")).strip()
                    or not isinstance(binding.get("version"), int)
                )
            ):
                raise ValueError("invalid state; malformed workflow")
            for step in steps:
                role = step.get("role")
                status = step.get("status")
                handoff_id = step.get("handoff_id")
                if (
                    role not in SPECIALISTS
                    or step.get("output_kind") not in allowed_outputs[role]
                    or status not in {"waiting", "ready", "dispatched", "completed", "cancelled"}
                    or (status in {"waiting", "ready"} and handoff_id is not None)
                    or (status in {"dispatched", "completed"} and handoff_id not in handoffs)
                    or (
                        handoff_id in handoffs
                        and (
                            handoffs[handoff_id].get("workflow_id") != workflow["id"]
                            or handoffs[handoff_id].get("step_id") != step["id"]
                            or handoffs[handoff_id].get("expected_output_kind")
                            != step.get("output_kind")
                            or handoffs[handoff_id].get("expected_resource_ids")
                            != step.get("expected_resource_ids")
                        )
                    )
                    or (status == "completed" and not step.get("resource_ids"))
                ):
                    raise ValueError("invalid state; malformed workflow step")
            if workflow["status"] == "completed":
                if any(step["status"] != "completed" for step in steps):
                    raise ValueError("invalid state; completed workflow has unfinished steps")
            elif workflow["status"] == "active" and sum(
                step["status"] in {"ready", "dispatched"} for step in steps
            ) != 1:
                raise ValueError("invalid state; active workflow needs exactly one current step")

        sessions = {item["id"]: item for item in state["sessions"]}
        active_sessions = []
        for session in state["sessions"]:
            if (
                session.get("status") not in {"active", "ended"}
                or session.get("workflow_id") is not None
                and session["workflow_id"] not in workflows
                or not isinstance(session.get("notes"), list)
                or any(
                    not isinstance(note, dict) or not str(note.get("text", "")).strip()
                    for note in session.get("notes", [])
                )
            ):
                raise ValueError("invalid state; malformed session")
            if session["status"] == "active":
                active_sessions.append(session["id"])
                if session.get("ended_at") is not None:
                    raise ValueError("invalid state; active session cannot have an end time")
            elif not str(session.get("ended_at", "")).strip():
                raise ValueError("invalid state; ended session needs an end time")
        if active_sessions != ([state["active_session_id"]] if state["active_session_id"] else []):
            raise ValueError("invalid state; active session pointer is inconsistent")

    def load(self) -> dict:
        with self.transaction():
            self._recover_locked()
        if not self.state_path.exists():
            self._expected_state_token = None
            return default_state()
        raw = self.state_path.read_bytes()
        self._expected_state_token = self._token(raw)
        state = json.loads(raw)
        if state.get("version") == 1:
            state["version"] = 2
            state["handoffs"] = []
        if state.get("version") == 2:
            for material in state.get("materials", []):
                previous = material.get("verification", "")
                material["verification"] = {
                    "reachable": bool(material.get("verified")),
                    "check": previous if isinstance(previous, str) else "legacy verification",
                    "evidence": "",
                }
                material["verified"] = False
            for item in state.get("knowledge", []):
                memory = item.get("memory", {})
                first_exposed_at = memory.get("last_reviewed_at") or item.get("created_at")
                memory["first_exposed_at"] = first_exposed_at
                memory["exposure_count"] = 0
                if memory.get("review_count", 0) == 0:
                    memory["last_reviewed_at"] = None
                item["last_interaction"] = None
            for artifact in state.get("artifacts", []):
                artifact["previous_versions"] = artifact.pop("revisions", [])
            for handoff in state.get("handoffs", []):
                result = handoff.get("result")
                handoff["legacy_unverified_output"] = bool(
                    isinstance(result, str) and result.strip()
                )
                handoff["result"] = (
                    {
                        "summary": result,
                        "next_role": None,
                        "resource_ids": [],
                        "issues": [],
                    }
                    if isinstance(result, str) and result.strip()
                    else None
                )
            state["version"] = 3
            self.save(state)
        if state.get("version") == 3:
            state["version"] = 4
            state.setdefault("shelves", [])
            state.setdefault("perspectives", [])
            state.setdefault("workflows", [])
            if state.get("profile"):
                profile = state["profile"]
                normalize_curriculum_profile(profile)
                curriculum = profile.get("curriculum")
                if curriculum:
                    interview = curriculum.get("interview")
                    try:
                        Engine._validate_curriculum_spec(curriculum, stored=True)
                        if (
                            not isinstance(interview, list)
                            or {item.get("decision") for item in interview}
                            != set(CURRICULUM_DECISIONS)
                            or any(not str(item.get("answer", "")).strip() for item in interview)
                        ):
                            raise ValueError("legacy curriculum lacks committed decisions")
                    except (AttributeError, TypeError, ValueError):
                        legacy = copy.deepcopy(curriculum)
                        legacy["migration_status"] = "requires_v4_evidence_replanning"
                        profile["curriculum_history"].append(legacy)
                        profile["curriculum"] = None
                        profile["curriculum_migration_required"] = True
                profile.setdefault(
                    "curriculum_interview", {name: [] for name in CURRICULUM_DECISIONS}
                )
            for material in state.get("materials", []):
                material.setdefault(
                    "curation",
                    {
                        "relevance": None,
                        "credibility": None,
                        "level_fit": None,
                        "signal": None,
                        "curriculum_id": None,
                        "curriculum_version": None,
                        "step_id": None,
                        "priority": None,
                        "disposition": "untriaged",
                        "disposition_reason": "",
                        "evaluated_at": None,
                    },
                )
            for artifact in state.get("artifacts", []):
                previous = artifact.setdefault("previous_versions", [])
                artifact.setdefault("content", "")
                artifact.setdefault("created_at", "1970-01-01T00:00:00Z")
                artifact.setdefault("updated_at", artifact["created_at"])
                artifact.setdefault("purpose", "")
                artifact.setdefault("audience", "")
                artifact.setdefault("milestone_id", None)
                artifact.setdefault("curriculum_id", None)
                artifact.setdefault("curriculum_version", None)
                artifact.setdefault(
                    "versions",
                    [
                        {
                            "version": index,
                            "content": version.get("content", ""),
                            "author": "legacy_unknown",
                            "submitted_at": version.get("replaced_at") or artifact.get("created_at", ""),
                        }
                        for index, version in enumerate(previous, 1)
                    ]
                    + [
                        {
                            "version": len(previous) + 1,
                            "content": artifact.get("content", ""),
                            "author": "legacy_unknown",
                            "submitted_at": artifact.get("updated_at") or artifact.get("created_at", ""),
                        }
                    ],
                )
                artifact.setdefault("current_version", len(artifact["versions"]))
                artifact.setdefault("review_rounds", [])
                artifact.setdefault("status", "draft")
                artifact["migration_import_required"] = True
            for handoff in state.get("handoffs", []):
                handoff.setdefault("depends_on", [])
                handoff.setdefault("dependency_context", [])
                handoff.setdefault("request_spec", None)
                handoff.setdefault("workflow_id", None)
                handoff.setdefault("step_id", None)
                handoff.setdefault("profile_binding", None)
                if handoff.get("status") in {"pending", "in_progress"}:
                    handoff["status"] = "cancelled"
                    handoff["cancelled_at"] = (
                        (state.get("profile") or {}).get("updated_at")
                        or handoff.get("created_at")
                        or "1970-01-01T00:00:00Z"
                    )
                    handoff["migration_reason"] = "legacy handoff target binding was unverifiable"
                handoff.setdefault("resource_snapshot", {})
                handoff.setdefault("output_fingerprints", {})
                handoff.setdefault("level_evidence_count", 0)
                handoff.setdefault("expected_output_kind", None)
                handoff.setdefault("expected_resource_ids", [])
                handoff["legacy_unverified_output"] = handoff.get("status") == "completed"
            for item in state.get("knowledge", []):
                if "subject_binding" not in item:
                    item["active"] = False
                    item["subject_binding"] = None
                    item["migration_import_required"] = True
            self.save(state)
        if state.get("version") != 4:
            raise ValueError(f"unsupported state version: {state.get('version')}")
        expected = default_state()
        missing = expected.keys() - state.keys()
        if missing:
            raise ValueError(f"invalid state; missing fields: {', '.join(sorted(missing))}")
        for name in (
            "knowledge",
            "materials",
            "shelves",
            "artifacts",
            "perspectives",
            "sessions",
            "handoffs",
            "workflows",
        ):
            if not isinstance(state[name], list):
                raise ValueError(f"invalid state; {name} must be a list")
            if any(not isinstance(item, dict) for item in state[name]):
                raise ValueError(f"invalid state; {name} entries must be objects")
            ids = [item.get("id") for item in state[name] if "id" in item]
            if (
                len(ids) != len(state[name])
                or any(not isinstance(item_id, str) or not item_id for item_id in ids)
                or len(ids) != len(set(ids))
            ):
                raise ValueError(f"invalid state; {name} ids must be nonempty and unique")
        if state["profile"] is not None and not isinstance(state["profile"], dict):
            raise ValueError("invalid state; profile must be an object or null")
        handoff_contract = {
            "depends_on", "dependency_context", "request_spec", "workflow_id", "step_id",
            "profile_binding", "resource_snapshot", "output_fingerprints",
            "level_evidence_count", "expected_output_kind", "expected_resource_ids",
            "legacy_unverified_output",
        }
        if any(handoff_contract - set(handoff) for handoff in state["handoffs"]):
            raise ValueError("invalid state; handoff output contract fields are required")
        for material in state["materials"]:
            material.setdefault("produced_by_handoff_id", None)
            curation = material.setdefault("curation", {})
            curation.setdefault("curriculum_id", None)
            curation.setdefault("curriculum_version", None)
            curation.setdefault("step_id", None)
            curation.setdefault("priority", None)
        for artifact in state["artifacts"]:
            artifact.setdefault("curriculum_id", None)
            artifact.setdefault("curriculum_version", None)
            artifact.setdefault("migration_import_required", False)
            artifact.setdefault("produced_by_handoff_id", None)
            for round_ in artifact.get("review_rounds", []):
                for finding in round_.get("findings", []):
                    if finding.get("status") == "superseded":
                        finding["status"] = "open"
                        finding.pop("superseded_in_version", None)
        for perspective in state["perspectives"]:
            perspective.setdefault("handoff_id", None)
        for shelf in state["shelves"]:
            shelf.setdefault("curriculum_version", None)
            shelf.setdefault("produced_by_handoff_id", None)
        for handoff in state["handoffs"]:
            handoff.setdefault("depends_on", [])
            handoff.setdefault("dependency_context", [])
            handoff.setdefault("request_spec", None)
            handoff.setdefault("workflow_id", None)
            handoff.setdefault("step_id", None)
            handoff.setdefault("profile_binding", None)
            handoff.setdefault("resource_snapshot", {})
            handoff.setdefault("output_fingerprints", {})
            handoff.setdefault("level_evidence_count", 0)
            handoff.setdefault("expected_output_kind", None)
            handoff.setdefault("expected_resource_ids", [])
            handoff.setdefault("legacy_unverified_output", False)
            if (
                handoff.get("to") == "roommate"
                and isinstance(handoff["request_spec"], dict)
                and set(handoff["request_spec"]) == LEGACY_PERSPECTIVE_REQUEST_FIELDS
            ):
                handoff["request_spec"] = {
                    key: handoff["request_spec"].get(key, "")
                    for key in PERSPECTIVE_REQUEST_FIELDS
                }
            if isinstance(handoff.get("result"), dict):
                handoff["result"].setdefault("observations", [])
                handoff["result"].setdefault("recommendations", [])
        for item in state["knowledge"]:
            item.setdefault("active", True)
            item.setdefault("produced_by_handoff_id", None)
            item.setdefault(
                "interaction_count",
                item.get("memory", {}).get("exposure_count", 0)
                + item.get("memory", {}).get("review_count", 0),
            )
            if "subject_binding" not in item:
                item["active"] = False
                item["subject_binding"] = None
                item["migration_import_required"] = True
            if "confusion_history" not in item:
                item["confusion_history"] = [
                    {
                        "point": point,
                        "first_seen_at": item.get("created_at", "1970-01-01T00:00:00Z"),
                        "last_seen_at": item.get("updated_at", item.get("created_at", "1970-01-01T00:00:00Z")),
                        "times_seen": 1,
                        "resolved_at": None,
                    }
                    for point in unique(item.get("weak_points", []))
                ]
        for session in state["sessions"]:
            session.setdefault("workflow_id", None)
        for workflow in state["workflows"]:
            workflow.setdefault("curriculum_binding", None)
            workflow.setdefault("profile_binding", None)
            workflow.setdefault("superseded_at", None)
            workflow.setdefault("request_resource_ids", [])
            workflow.setdefault("request_spec", None)
            if (
                workflow.get("intent") == "perspective"
                and isinstance(workflow["request_spec"], dict)
                and set(workflow["request_spec"]) == LEGACY_PERSPECTIVE_REQUEST_FIELDS
            ):
                workflow["request_spec"] = {
                    key: workflow["request_spec"].get(key, "")
                    for key in PERSPECTIVE_REQUEST_FIELDS
                }
        if state.get("profile"):
            profile = state["profile"]
            for evidence in profile.get("level_evidence", []):
                evidence.setdefault("source_handoff_id", None)
                evidence.setdefault("produced_by_handoff_id", None)
            for goal in profile.get("learning_goals", []):
                goal.setdefault("produced_by_handoff_id", None)
            if profile.get("curriculum"):
                profile["curriculum"].setdefault("produced_by_handoff_id", None)
                for milestone in profile["curriculum"].get("milestones", []):
                    milestone.setdefault("produced_by_handoff_id", None)
        self._validate_nested(state)
        return state

    def save(self, state: dict) -> None:
        self._commit(state)

    def save_with_review(self, state: dict, event: dict) -> None:
        self._commit(state, review_event=event)

    def replace_state_and_reviews(self, state: dict, reviews: bytes) -> None:
        self._commit(state, reviews=reviews)

    def append_review(self, event: dict) -> None:
        with self.transaction():
            raw = self.reviews_path.read_bytes() if self.reviews_path.exists() else b""
            raw += (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode()
            self._replace_bytes(self.reviews_path, raw)


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.write_scope: str | None = None

    def _producer(self, state: dict, role: str) -> str | None:
        if self.write_scope:
            handoff = self._find(state["handoffs"], self.write_scope, "write scope")
            if handoff["to"] != role or handoff["status"] != "in_progress":
                raise ValueError("write scope is not an in-progress handoff for this role")
            return handoff["id"]
        candidates = [
            handoff["id"]
            for handoff in state["handoffs"]
            if handoff["to"] == role and handoff["status"] == "in_progress"
        ]
        if len(candidates) > 1:
            raise ValueError("multiple claimed handoffs require an explicit write scope")
        return candidates[0] if candidates else None

    def _stamp(self, state: dict, resource: dict, role: str) -> None:
        resource["produced_by_handoff_id"] = self._producer(state, role)

    @staticmethod
    def _id(title: str, collection: list[dict]) -> str:
        canonical = unicodedata.normalize("NFKC", title).strip().casefold()
        normalized = unicodedata.normalize("NFKD", canonical).encode("ascii", "ignore").decode()
        stem = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:40]
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
        qualified = f"{stem or 'u'}-{digest}"
        existing = {item["id"] for item in collection}
        candidate = stem if stem and canonical.isascii() else qualified
        if candidate in existing:
            candidate = qualified
        suffix = 2
        while candidate in existing:
            candidate = f"{qualified}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _find(collection: list[dict], item_id: str, label: str) -> dict:
        for item in collection:
            if item["id"] == item_id:
                return item
        raise ValueError(f"{label} not found: {item_id}")

    @staticmethod
    def _profile(state: dict) -> dict:
        if not state["profile"]:
            raise ValueError("profile is not initialized; run `advisor init`")
        state["profile"].setdefault("level_evidence", [])
        state["profile"].setdefault("learning_goals", [])
        state["profile"].setdefault("curriculum", None)
        state["profile"].setdefault("curriculum_history", [])
        state["profile"].setdefault("curriculum_migration_required", False)
        state["profile"].setdefault("curriculum_replan_required", False)
        state["profile"].setdefault("profile_history", [])
        interview = state["profile"].setdefault(
            "curriculum_interview", {name: [] for name in CURRICULUM_DECISIONS}
        )
        for name in CURRICULUM_DECISIONS:
            interview.setdefault(name, [])
        return state["profile"]

    @staticmethod
    def _subject_binding(state: dict) -> dict:
        profile = Engine._profile(state)
        return {"goal": profile["goal"], "focus": copy.deepcopy(profile["focus"])}

    @classmethod
    def _current_knowledge(cls, state: dict, item_id: str, label: str = "knowledge") -> dict:
        item = cls._find(state["knowledge"], item_id, label)
        if not item.get("active", True) or item.get("subject_binding") != cls._subject_binding(state):
            raise ValueError(f"{label} does not belong to the current learning target: {item_id}")
        return item

    # Advisor
    def advisor_init(
        self,
        goal: str,
        level: str,
        focus: list[str],
        target_retention: float,
        at: datetime,
    ) -> dict:
        if not goal.strip():
            raise ValueError("goal is required")
        if not 0.80 <= target_retention <= 0.95:
            raise ValueError("target retention must be between 0.80 and 0.95")
        state = self.store.load()
        previous = state["profile"] or {}
        requested_focus = unique(focus)
        goal_changed = bool(previous) and goal.strip() != previous.get("goal")
        next_focus = (
            requested_focus
            if requested_focus
            else ([] if goal_changed else previous.get("focus", []))
        )
        target_changed = bool(previous) and (
            goal_changed or next_focus != previous.get("focus", [])
        )
        workflow_binding_changed = target_changed or bool(previous) and (
            target_retention != previous.get("target_retention")
        )
        curriculum_history = copy.deepcopy(previous.get("curriculum_history", []))
        profile_history = copy.deepcopy(previous.get("profile_history", []))
        if target_changed:
            profile_history.append(
                {
                    "goal": previous.get("goal", ""),
                    "current_level": previous.get("current_level", ""),
                    "focus": copy.deepcopy(previous.get("focus", [])),
                    "level_evidence": copy.deepcopy(previous.get("level_evidence", [])),
                    "learning_goals": copy.deepcopy(previous.get("learning_goals", [])),
                    "archived_at": iso(at),
                }
            )
            if previous.get("curriculum"):
                archived = copy.deepcopy(previous["curriculum"])
                archived.update(
                    {
                        "invalidated_at": iso(at),
                        "invalidation_reason": "profile goal or focus changed",
                    }
                )
                curriculum_history.append(archived)
            for item in state["knowledge"]:
                item["active"] = False
        state["profile"] = {
            "goal": goal.strip(),
            "current_level": level.strip() or (
                "" if target_changed else previous.get("current_level", "")
            ),
            "focus": next_focus,
            "target_retention": target_retention,
            "level_evidence": [] if target_changed else previous.get("level_evidence", []),
            "learning_goals": [] if target_changed else previous.get("learning_goals", []),
            "curriculum": None if target_changed else previous.get("curriculum"),
            "curriculum_history": curriculum_history,
            "profile_history": profile_history,
            "curriculum_migration_required": previous.get(
                "curriculum_migration_required", False
            ),
            "curriculum_replan_required": target_changed or previous.get(
                "curriculum_replan_required", False
            ),
            "curriculum_interview": (
                {name: [] for name in CURRICULUM_DECISIONS}
                if target_changed
                else previous.get(
                    "curriculum_interview", {name: [] for name in CURRICULUM_DECISIONS}
                )
            ),
            "created_at": previous.get("created_at", iso(at)),
            "updated_at": iso(at),
        }
        if workflow_binding_changed:
            preserved_workflows = {
                handoff.get("workflow_id")
                for handoff in state["handoffs"]
                if handoff.get("expected_output_kind") == "curriculum"
                and handoff["status"] in {"pending", "in_progress"}
            }
            for workflow in state["workflows"]:
                if workflow["status"] == "active" and workflow["id"] not in preserved_workflows:
                    self._supersede_workflow(state, workflow, at)
            for handoff in state["handoffs"]:
                if (
                    handoff.get("workflow_id") is None
                    and handoff["status"] in {"pending", "in_progress"}
                ):
                    handoff["status"] = "cancelled"
                    handoff["cancelled_at"] = iso(at)
        for item in state["knowledge"]:
            memory = item["memory"]
            interval = self.due_interval(memory["stability_days"], target_retention)
            anchor = memory["last_reviewed_at"] or memory["first_exposed_at"]
            memory["due_at"] = iso(parse_time(anchor) + timedelta(days=interval))
        self.store.save(state)
        return state["profile"]

    def advisor_observe(
        self,
        level: str,
        evidence: str,
        at: datetime,
        source_handoff_id: str | None = None,
    ) -> dict:
        if not level.strip() or not evidence.strip():
            raise ValueError("observed level and Tutor evidence are required")
        state = self.store.load()
        profile = self._profile(state)
        producer_id = self._producer(state, "advisor")
        source_id = None
        if producer_id:
            handoff = self._find(state["handoffs"], producer_id, "Advisor handoff")
            dependencies = [
                self._find(state["handoffs"], dependency_id, "handoff dependency")
                for dependency_id in handoff.get("depends_on", [])
            ]
            matches = [
                dependency
                for dependency in dependencies
                if dependency["to"] == "tutor"
                and canonical_text(evidence)
                in {
                    canonical_text(value)
                    for value in dependency["result"].get("observations", [])
                }
                and (
                    source_handoff_id is None
                    or dependency["id"] == source_handoff_id
                )
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Advisor evidence must match exactly one completed Tutor dependency"
                )
            source_id = matches[0]["id"]
            handoffs = {item["id"]: item for item in state["handoffs"]}
            if any(
                entry.get("source_handoff_id") == source_id
                and canonical_text(entry.get("evidence", "")) == canonical_text(evidence)
                and (
                    entry.get("produced_by_handoff_id") == producer_id
                    or handoffs[entry["produced_by_handoff_id"]]["status"] != "cancelled"
                )
                for entry in profile["level_evidence"]
                if entry.get("produced_by_handoff_id") is not None
            ):
                raise ValueError("Tutor observation was already consumed by an Advisor handoff")
        elif source_handoff_id:
            raise ValueError("Tutor evidence source requires a claimed Advisor handoff")
        profile["current_level"] = level.strip()
        profile["level_evidence"].append(
            {
                "at": iso(at),
                "level": level.strip(),
                "evidence": evidence.strip(),
                "source_handoff_id": source_id,
                "produced_by_handoff_id": producer_id,
            }
        )
        profile["updated_at"] = iso(at)
        self.store.save(state)
        return profile

    def advisor_interview(
        self, decision: str, question: str, answer: str | None, at: datetime
    ) -> dict:
        if decision not in CURRICULUM_DECISIONS:
            raise ValueError(
                f"curriculum decision must be one of: {', '.join(CURRICULUM_DECISIONS)}"
            )
        if not question.strip():
            raise ValueError("interview question is required")
        state = self.store.load()
        profile = self._profile(state)
        interview = profile["curriculum_interview"]
        pending = [
            item
            for entries in interview.values()
            for item in entries
            if item["answer"] is None
        ]
        entries = interview[decision]
        all_entries = [item for values in interview.values() for item in values]
        duplicate = any(
            canonical_text(item["question"]) == canonical_text(question)
            for item in all_entries
        )
        if not pending and duplicate:
            raise ValueError("do not repeat an answered curriculum interview question")
        if answer is None:
            if pending:
                raise ValueError("answer the pending interview question before asking another")
            if len(entries) >= 5:
                raise ValueError("a curriculum decision cannot exceed five interview questions")
            entry = {
                "decision": decision,
                "question": question.strip(),
                "answer": None,
                "asked_at": iso(at),
                "answered_at": None,
            }
            entries.append(entry)
        else:
            if not answer.strip():
                raise ValueError("interview answer cannot be empty")
            if any(
                item.get("answer")
                and canonical_text(item["answer"]) == canonical_text(answer)
                for item in all_entries
            ):
                raise ValueError("each curriculum decision needs distinct evidence")
            if pending:
                entry = pending[0]
                if entry["decision"] != decision or entry["question"] != question.strip():
                    raise ValueError("answer must match the pending interview question")
                entry["answer"] = answer.strip()
                entry["answered_at"] = iso(at)
            else:
                if len(entries) >= 5:
                    raise ValueError("a curriculum decision cannot exceed five interview questions")
                entry = {
                    "decision": decision,
                    "question": question.strip(),
                    "answer": answer.strip(),
                    "asked_at": iso(at),
                    "answered_at": iso(at),
                }
                entries.append(entry)
        profile["updated_at"] = iso(at)
        self.store.save(state)
        return {
            "entry": entry,
            "status": "ready" if all(
                any(item["answer"] for item in interview[name])
                for name in CURRICULUM_DECISIONS
            ) else "interviewing",
            "next_decision": next(
                (
                    name
                    for name in CURRICULUM_DECISIONS
                    if not any(item["answer"] for item in interview[name])
                ),
                None,
            ),
        }

    @staticmethod
    def _validate_curriculum_spec(spec: dict, stored: bool = False) -> None:
        if not isinstance(spec, dict):
            raise ValueError("curriculum spec must be an object")
        base_fields = {"destination", "baseline", "sequence", "cut_list", "milestones"}
        stored_fields = {
            "id", "version", "status", "interview", "created_at", "updated_at",
            "produced_by_handoff_id",
        }
        if (not stored and set(spec) != base_fields) or (
            stored and (not base_fields <= set(spec) or not set(spec) <= base_fields | stored_fields)
        ):
            raise ValueError("curriculum spec has unknown or missing fields")
        destination = spec.get("destination")
        if not isinstance(destination, dict) or set(destination) != {
            "knowledge", "capabilities", "use_context", "rationale"
        }:
            raise ValueError("curriculum destination is required")
        if not destination.get("capabilities") or not all(
            isinstance(value, str) and value.strip()
            for value in destination.get("capabilities", [])
        ):
            raise ValueError("destination needs observable capabilities")
        for field in ("use_context", "rationale"):
            if not str(destination.get(field, "")).strip():
                raise ValueError(f"destination {field} is required")
        knowledge = destination.get("knowledge", [])
        if not isinstance(knowledge, list) or not all(
            isinstance(value, str) and value.strip() for value in knowledge
        ):
            raise ValueError("destination knowledge must be a list of strings")
        for field in ("knowledge", "capabilities"):
            values = destination[field]
            if len(values) != len({value.casefold() for value in values}):
                raise ValueError(f"destination {field} must not contain duplicates")

        baseline = spec.get("baseline")
        if (
            not isinstance(baseline, dict)
            or set(baseline) != {"can_do", "assisted", "cannot_yet", "evidence"}
            or not baseline.get("evidence")
        ):
            raise ValueError("baseline needs performance evidence")
        for field in ("can_do", "assisted", "cannot_yet", "evidence"):
            values = baseline.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"baseline {field} must be a list of strings")
            if len(values) != len({value.casefold() for value in values}):
                raise ValueError(f"baseline {field} must not contain duplicates")

        sequence = spec.get("sequence")
        if not isinstance(sequence, list) or not sequence:
            raise ValueError("curriculum sequence is required")
        ids = [str(step.get("id", "")).strip() for step in sequence if isinstance(step, dict)]
        if len(ids) != len(sequence) or not all(ids) or len(set(ids)) != len(ids):
            raise ValueError("curriculum sequence needs unique ids")
        orders = [step.get("order") for step in sequence]
        if orders != list(range(1, len(sequence) + 1)):
            raise ValueError("curriculum sequence order must be consecutive")
        seen: set[str] = set()
        for step in sequence:
            required_step_fields = {"id", "order", "outcome", "rationale", "prerequisites"}
            allowed_step_fields = required_step_fields | {"topic"}
            if stored:
                allowed_step_fields |= {"status", "completed_at"}
            if not required_step_fields <= set(step) or not set(step) <= allowed_step_fields:
                raise ValueError("curriculum sequence step has unknown or missing fields")
            if not str(step.get("outcome", "")).strip() or not str(
                step.get("rationale", "")
            ).strip():
                raise ValueError("each sequence step needs an outcome and rationale")
            prerequisites = step.get("prerequisites", [])
            if not isinstance(prerequisites, list) or any(value not in seen for value in prerequisites):
                raise ValueError("sequence prerequisites must refer to earlier steps")
            if len(prerequisites) != len(set(prerequisites)):
                raise ValueError("sequence prerequisites must not contain duplicates")
            seen.add(step["id"])

        cut_list = spec.get("cut_list")
        if not isinstance(cut_list, list) or not cut_list:
            raise ValueError("curriculum cut list is required")
        required_topics = {
            re.sub(r"[\W_]+", "", value.casefold())
            for step in sequence
            for value in (step["outcome"], step.get("topic", ""))
            if value
        }
        for item in cut_list:
            if not isinstance(item, dict) or set(item) != {
                "topic", "reason", "reconsider_when"
            } or any(
                not str(item.get(field, "")).strip()
                for field in ("topic", "reason", "reconsider_when")
            ):
                raise ValueError("each cut-list item needs topic, reason, and reconsider_when")
            topic = re.sub(r"[\W_]+", "", item["topic"].casefold())
            if any(topic in required or required in topic for required in required_topics):
                raise ValueError("cut-list topics cannot also be required sequence steps")
        cut_topics = [canonical_text(item["topic"]) for item in cut_list]
        if len(cut_topics) != len(set(cut_topics)):
            raise ValueError("cut-list topics must not contain duplicates")

        milestones = spec.get("milestones")
        if not isinstance(milestones, list) or not milestones:
            raise ValueError("curriculum milestones are required")
        milestone_ids: set[str] = set()
        covered_steps: set[str] = set()
        for item in milestones:
            required_milestone_fields = {
                "id", "step_id", "title", "proof_artifact", "pass_criteria"
            }
            allowed_milestone_fields = set(required_milestone_fields)
            if stored:
                allowed_milestone_fields |= {
                    "status", "evidence_artifact_id", "completed_at",
                    "produced_by_handoff_id",
                }
            if (
                not isinstance(item, dict)
                or not required_milestone_fields <= set(item)
                or not set(item) <= allowed_milestone_fields
                or any(
                    not str(item.get(field, "")).strip()
                    for field in ("id", "step_id", "title", "proof_artifact")
                )
            ):
                raise ValueError("each milestone needs id, step_id, title, and proof_artifact")
            if item["id"] in milestone_ids:
                raise ValueError("milestone ids must be unique")
            if item["step_id"] not in ids:
                raise ValueError("milestone step_id must reference a sequence step")
            milestone_ids.add(item["id"])
            covered_steps.add(item["step_id"])
            criteria = item.get("pass_criteria")
            if not isinstance(criteria, list) or not criteria or not all(
                isinstance(value, str) and value.strip() for value in criteria
            ):
                raise ValueError("each milestone needs pass criteria")
            if len(criteria) != len({value.casefold() for value in criteria}):
                raise ValueError("milestone pass criteria must not contain duplicates")
        if covered_steps != set(ids):
            raise ValueError("every curriculum step needs a proof milestone")

    def advisor_curriculum(self, spec: dict, at: datetime) -> dict:
        spec = strip_nested_strings(spec)
        self._validate_curriculum_spec(spec)
        for field in ("knowledge", "capabilities"):
            spec["destination"][field].sort(key=str.casefold)
        for field in ("can_do", "assisted", "cannot_yet", "evidence"):
            spec["baseline"][field].sort(key=str.casefold)
        for step in spec["sequence"]:
            step["prerequisites"].sort(key=str.casefold)
        spec["cut_list"].sort(key=lambda item: canonical_text(item["topic"]))
        order = {step["id"]: step["order"] for step in spec["sequence"]}
        spec["milestones"].sort(key=lambda item: (order[item["step_id"]], item["id"]))
        for milestone in spec["milestones"]:
            milestone["pass_criteria"].sort(key=str.casefold)
        state = self.store.load()
        profile = self._profile(state)
        previous = profile.get("curriculum")
        old_milestone_ids = {
            item["id"] for item in previous.get("milestones", [])
        } if previous else set()
        reserved = [
            {"id": resource_id}
            for resource_id in {
                *[goal["id"] for goal in profile.get("learning_goals", [])],
                *[
                    goal["id"]
                    for archived in profile.get("profile_history", [])
                    for goal in archived.get("learning_goals", [])
                ],
                *[
                    resource_id
                    for archived in profile.get("curriculum_history", [])
                    for resource_id in [
                        archived.get("id"),
                        *[item.get("id") for item in archived.get("milestones", [])],
                    ]
                    if resource_id
                ],
                *([previous["id"]] if previous else []),
            }
        ]
        for milestone in spec["milestones"]:
            if (
                milestone["id"] not in old_milestone_ids
                and milestone["id"] in {item["id"] for item in reserved}
            ):
                milestone["id"] = self._id(milestone["id"], reserved)
            reserved.append({"id": milestone["id"]})
        interview = profile["curriculum_interview"]
        missing = [
            name
            for name in CURRICULUM_DECISIONS
            if not any(item["answer"] for item in interview[name])
        ]
        if missing:
            raise ValueError(f"curriculum interview is incomplete: {', '.join(missing)}")
        unresolved = [
            item["question"]
            for entries in interview.values()
            for item in entries
            if item["answer"] is None
        ]
        if unresolved:
            raise ValueError("curriculum interview has an unanswered pending question")
        if previous:
            previous_spec = {
                "destination": copy.deepcopy(previous["destination"]),
                "baseline": copy.deepcopy(previous["baseline"]),
                "sequence": [
                    {
                        key: copy.deepcopy(value)
                        for key, value in step.items()
                        if key not in {"status", "completed_at"}
                    }
                    for step in previous["sequence"]
                ],
                "cut_list": copy.deepcopy(previous["cut_list"]),
                "milestones": [
                    {
                        key: copy.deepcopy(value)
                        for key, value in milestone.items()
                        if key not in {
                            "status", "evidence_artifact_id", "completed_at",
                            "produced_by_handoff_id",
                        }
                    }
                    for milestone in previous["milestones"]
                ],
            }
            if previous_spec == spec:
                return previous
            previous_milestones = {
                milestone["id"]: milestone for milestone in previous["milestones"]
            }
            previous_steps = {step["id"]: step for step in previous["sequence"]}
        else:
            previous_milestones = {}
            previous_steps = {}
        if previous:
            profile["curriculum_history"].append(copy.deepcopy(previous))
        curriculum = copy.deepcopy(spec)
        curriculum.update(
            {
                "id": previous["id"] if previous else f"curriculum-{uuid.uuid4().hex[:8]}",
                "version": previous["version"] + 1 if previous else 1,
                "status": "ready",
                "interview": [
                    copy.deepcopy(item)
                    for name in CURRICULUM_DECISIONS
                    for item in interview[name]
                ],
                "created_at": previous.get("created_at", iso(at)) if previous else iso(at),
                "updated_at": iso(at),
            }
        )
        for step in curriculum["sequence"]:
            step["status"] = "waiting"
            step["completed_at"] = previous_steps.get(step["id"], {}).get("completed_at")
        for milestone in curriculum["milestones"]:
            old = previous_milestones.get(milestone["id"])
            step = next(
                item for item in curriculum["sequence"] if item["id"] == milestone["step_id"]
            )
            old_step = previous_steps.get(milestone["step_id"])
            preserved = old and old.get("status") == "completed" and all(
                old.get(field) == milestone.get(field)
                for field in ("step_id", "title", "proof_artifact", "pass_criteria")
            ) and old_step and all(
                old_step.get(field) == step.get(field)
                for field in ("topic", "outcome", "rationale", "prerequisites")
            )
            milestone["status"] = "completed" if preserved else "planned"
            milestone["evidence_artifact_id"] = (
                old.get("evidence_artifact_id") if preserved else None
            )
            if preserved and old.get("completed_at"):
                milestone["completed_at"] = old["completed_at"]
        profile["curriculum"] = curriculum
        self._stamp(state, curriculum, "advisor")
        normalize_curriculum_profile(profile)
        profile["curriculum_migration_required"] = False
        profile["curriculum_replan_required"] = False
        profile["updated_at"] = iso(at)
        self.store.save(state)
        return curriculum

    def advisor_milestone(self, milestone_id: str, artifact_id: str, at: datetime) -> dict:
        state = self.store.load()
        profile = self._profile(state)
        curriculum = profile.get("curriculum")
        if not curriculum:
            raise ValueError("curriculum is not ready")
        milestone = self._find(curriculum["milestones"], milestone_id, "milestone")
        if milestone["status"] == "completed":
            raise ValueError("milestone is already completed")
        step = self._find(curriculum["sequence"], milestone["step_id"], "curriculum step")
        if step["status"] != "active":
            raise ValueError("milestone step is not currently active")
        artifact = self._find(state["artifacts"], artifact_id, "artifact")
        if artifact.get("status") != "passed":
            raise ValueError("milestone evidence must be an Editor-passed artifact")
        if artifact.get("milestone_id") not in {None, milestone_id}:
            raise ValueError("artifact belongs to a different milestone")
        if (
            artifact.get("curriculum_id") != curriculum["id"]
            or artifact.get("curriculum_version") != curriculum["version"]
        ):
            raise ValueError("artifact belongs to a different curriculum version")
        latest_review = artifact.get("review_rounds", [])[-1]
        proof = latest_review.get("milestone_criteria") or {}
        if set(proof) != set(milestone["pass_criteria"]) or any(
            value.get("status") != "pass" for value in proof.values()
        ):
            raise ValueError("milestone pass criteria were not all proven")
        milestone["status"] = "completed"
        milestone["evidence_artifact_id"] = artifact_id
        milestone["completed_at"] = iso(at)
        step_milestones = [
            item for item in curriculum["milestones"] if item["step_id"] == step["id"]
        ]
        if all(item["status"] == "completed" for item in step_milestones):
            step["status"] = "completed"
            step["completed_at"] = iso(at)
            completed = {item["id"] for item in curriculum["sequence"] if item["status"] == "completed"}
            next_step = next(
                (
                    item for item in curriculum["sequence"]
                    if item["status"] == "waiting"
                    and all(value in completed for value in item["prerequisites"])
                ),
                None,
            )
            if next_step:
                next_step["status"] = "active"
        if all(item["status"] == "completed" for item in curriculum["sequence"]):
            curriculum["status"] = "completed"
        curriculum["updated_at"] = iso(at)
        profile["updated_at"] = iso(at)
        self._stamp(state, milestone, "advisor")
        self.store.save(state)
        return milestone

    def _upsert_learning_goal(
        self,
        state: dict,
        title: str,
        outcome: str,
        reason: str,
        kind: str,
        priority: int,
        knowledge_id: str | None,
        at: datetime,
    ) -> dict:
        if not title.strip() or not outcome.strip() or not reason.strip():
            raise ValueError("learning goal title, outcome, and reason are required")
        if kind not in GOAL_KINDS:
            raise ValueError(f"learning goal kind must be one of: {', '.join(sorted(GOAL_KINDS))}")
        if not 1 <= priority <= 5:
            raise ValueError("learning goal priority must be between 1 and 5")
        if knowledge_id:
            self._current_knowledge(state, knowledge_id)
        profile = self._profile(state)
        existing = next(
            (
                goal
                for goal in profile["learning_goals"]
                if goal["title"].casefold() == title.strip().casefold()
                and goal.get("knowledge_id") == knowledge_id
            ),
            None,
        )
        values = {
            "title": title.strip(),
            "outcome": outcome.strip(),
            "reason": reason.strip(),
            "kind": kind,
            "priority": priority,
            "knowledge_id": knowledge_id,
            "status": "active",
        }
        if existing:
            if all(existing.get(key) == value for key, value in values.items()):
                return existing
            values["updated_at"] = iso(at)
            existing.update(values)
            self._stamp(state, existing, "advisor")
            return existing
        advisor_resources = list(profile["learning_goals"])
        curriculum = profile.get("curriculum")
        if curriculum:
            advisor_resources.extend([curriculum, *curriculum.get("milestones", [])])
        advisor_resources.extend(
            goal
            for archived in profile.get("profile_history", [])
            for goal in archived.get("learning_goals", [])
        )
        advisor_resources.extend(profile.get("curriculum_history", []))
        advisor_resources.extend(
            milestone
            for archived in profile.get("curriculum_history", [])
            for milestone in archived.get("milestones", [])
        )
        goal = {
            "id": self._id(title, advisor_resources),
            **values,
            "created_at": iso(at),
            "updated_at": iso(at),
        }
        profile["learning_goals"].append(goal)
        self._stamp(state, goal, "advisor")
        return goal

    def learning_goal_add(
        self,
        title: str,
        outcome: str,
        reason: str,
        kind: str,
        priority: int,
        knowledge_id: str | None,
        at: datetime,
    ) -> dict:
        state = self.store.load()
        goal = self._upsert_learning_goal(
            state, title, outcome, reason, kind, priority, knowledge_id, at
        )
        self.store.save(state)
        return goal

    def learning_goals(self) -> list[dict]:
        state = self.store.load()
        goals = self._profile(state)["learning_goals"]
        return sorted(
            goals,
            key=lambda goal: (
                goal["status"] != "active",
                -goal["priority"],
                goal["updated_at"],
            ),
        )

    def learning_goal_status(self, goal_id: str, status: str, at: datetime) -> dict:
        if status not in GOAL_STATUSES:
            raise ValueError(f"learning goal status must be one of: {', '.join(sorted(GOAL_STATUSES))}")
        state = self.store.load()
        profile = self._profile(state)
        goal = self._find(profile["learning_goals"], goal_id, "learning goal")
        if goal["status"] == status:
            return goal
        goal["status"] = status
        goal["updated_at"] = iso(at)
        profile["updated_at"] = iso(at)
        self._stamp(state, goal, "advisor")
        self.store.save(state)
        return goal

    def status(self, at: datetime) -> dict:
        state = self.store.load()
        profile = self._profile(state)
        due = self._due_from_state(state, at)
        active = None
        if state["active_session_id"]:
            active = self._find(state["sessions"], state["active_session_id"], "session")
        return {
            "profile": profile,
            "counts": {
                "knowledge": len(state["knowledge"]),
                "due": len(due),
                "materials": len(state["materials"]),
                "shelves": len(state["shelves"]),
                "artifacts": len(state["artifacts"]),
                "perspectives": len(state["perspectives"]),
                "sessions": len(state["sessions"]),
                "workflows": len(state["workflows"]),
            },
            "active_session": active,
        }

    def _advisor_recommendation(self, state: dict, at: datetime) -> dict:
        profile = self._profile(state)
        due = self._due_from_state(state, at)
        if due:
            due_item = due[0]
            item = self._find(state["knowledge"], due_item["id"], "knowledge")
            retention = due_item["retention"]
            history = item.get("confusion_history", [])
            point = (
                max(history, key=lambda entry: entry["last_seen_at"])["point"]
                if history
                else item["weak_points"][0] if item["weak_points"] else None
            )
            if point:
                proposal = {
                    "title": f"재학습: {item['title']} — {point}",
                    "outcome": f"자료 없이 {point}을 설명하고 실제 사례에 적용한다",
                    "reason": (
                        f"estimated retention {retention:.1%}; previous confusion: {point}"
                    ),
                    "kind": "remedial",
                    "priority": 5,
                    "knowledge_id": item["id"],
                }
                return {
                    "role": "Advisor",
                    "action": "relearn",
                    "proposed_learning_goal": proposal,
                    "knowledge_id": item["id"],
                    "title": item["title"],
                    "reason": proposal["reason"],
                }
            return {
                "role": "Advisor",
                "action": "review",
                "knowledge_id": item["id"],
                "title": item["title"],
                "reason": f"estimated retention {retention:.1%}",
            }
        weak = [
            item
            for item in state["knowledge"]
            if item.get("active", True) and item["weak_points"]
        ]
        if weak:
            weak.sort(key=lambda item: (-len(item["weak_points"]), item["updated_at"]))
            item = weak[0]
            point = item["weak_points"][0]
            proposal = {
                "title": f"보강: {item['title']} — {point}",
                "outcome": f"{point}을 원리부터 설명하고 새로운 실제 사례에 적용한다",
                "reason": f"current confusion: {point}",
                "kind": "remedial",
                "priority": 5,
                "knowledge_id": item["id"],
            }
            return {
                "role": "Advisor",
                "action": "practice",
                "proposed_learning_goal": proposal,
                "knowledge_id": item["id"],
                "title": item["title"],
                "reason": point,
            }
        active_goals = [goal for goal in profile["learning_goals"] if goal["status"] == "active"]
        if active_goals:
            goal = sorted(active_goals, key=lambda value: (-value["priority"], value["updated_at"]))[0]
            return {
                "role": "Advisor",
                "action": "relearn" if goal["kind"] == "remedial" else "learn",
                "learning_goal": goal,
                "knowledge_id": goal.get("knowledge_id"),
                "topic": goal["title"],
                "reason": goal["reason"],
            }
        curriculum = profile.get("curriculum")
        if curriculum and curriculum["status"] == "ready":
            active_step = next(
                (item for item in curriculum["sequence"] if item["status"] == "active"), None
            )
            milestone = next(
                (
                    item for item in curriculum["milestones"]
                    if item["status"] == "planned"
                    and active_step
                    and item["step_id"] == active_step["id"]
                ),
                None,
            )
            if milestone:
                return {
                    "role": "Advisor",
                    "action": "learn",
                    "curriculum_id": curriculum["id"],
                    "milestone": milestone,
                    "topic": milestone["title"],
                    "reason": f"proof required: {milestone['proof_artifact']}",
                }
        if profile["focus"]:
            return {
                "role": "Advisor",
                "action": "learn",
                "topic": profile["focus"][0],
                "reason": "highest profile focus",
            }
        return {
            "role": "Advisor",
            "action": "choose-focus",
            "goal": profile["goal"],
            "reason": "no due or weak knowledge",
        }

    def recommend(self, at: datetime) -> dict:
        """Return Advisor's next recommendation without changing persistent state."""
        return self._advisor_recommendation(self.store.load(), at)

    def advise(self, at: datetime) -> dict:
        """Choose the next action and persist a remedial goal when one is proposed."""
        state = self.store.load()
        advice = self._advisor_recommendation(state, at)
        proposal = advice.pop("proposed_learning_goal", None)
        if not proposal:
            return advice
        item = self._find(state["knowledge"], proposal["knowledge_id"], "knowledge")
        if not item.get("confusion_history") and item["weak_points"]:
            self._record_confusions(item, item["weak_points"], [], at)
        goal = self._upsert_learning_goal(state, at=at, **proposal)
        advice["learning_goal"] = goal
        self.store.save(state)
        return advice

    # Librarian
    @staticmethod
    def inspect_source(source: str, timeout: float = 5.0) -> tuple[bool, str, str, str]:
        parsed = urlparse(source)
        identity = parsed._replace(fragment="").geturl() if parsed.scheme else ""
        try:
            if parsed.scheme in {"http", "https"}:
                request = urllib.request.Request(source, headers={"User-Agent": "become/2"})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    digest = hashlib.sha256()
                    has_content = False
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        digest.update(chunk)
                        has_content = has_content or bool(chunk.strip())
                    reachable = 200 <= response.status < 400 and has_content
                    return (
                        reachable,
                        f"HTTP {response.status}"
                        if has_content
                        else "HTTP response has no content",
                        identity,
                        digest.hexdigest() if has_content else "",
                    )
            if parsed.scheme == "file":
                path = Path(unquote(parsed.path))
            elif not parsed.scheme:
                path = Path(source).expanduser()
            else:
                return False, f"unsupported source scheme: {parsed.scheme}", identity, ""
            resolved = path.resolve()
            identity = str(resolved)
            if not resolved.is_file():
                return False, "local file not found", identity, ""
            digest = hashlib.sha256()
            has_content = False
            with resolved.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(chunk)
                    has_content = has_content or bool(chunk.strip())
            return (
                (True, str(resolved), identity, digest.hexdigest())
                if has_content
                else (False, "local file has no content", identity, "")
            )
        except (OSError, urllib.error.URLError, ValueError) as error:
            return False, str(error), identity, ""

    @staticmethod
    def verify_source(source: str, timeout: float = 5.0) -> tuple[bool, str]:
        reachable, check, _, _ = Engine.inspect_source(source, timeout)
        return reachable, check

    def material_add(
        self, title: str, source: str, note: str, evidence: str, at: datetime
    ) -> dict:
        if not title.strip() or not source.strip():
            raise ValueError("material title and source are required")
        reachable, check, source_identity, content_fingerprint = self.inspect_source(source)
        evidence = evidence.strip()
        state = self.store.load()
        existing = next((item for item in state["materials"] if item["source"] == source), None)
        material = existing or {
            "id": self._id(title, state["materials"]),
            "created_at": iso(at),
        }
        material.update(
            {
                "title": title.strip(),
                "source": source.strip(),
                "note": note.strip(),
                "verified": reachable and bool(evidence),
                "verification": {
                    "reachable": reachable,
                    "check": check,
                    "evidence": evidence,
                    "source_identity": source_identity,
                    "content_fingerprint": content_fingerprint,
                },
                "verified_at": iso(at),
            }
        )
        # Re-registration means the source or its reading evidence may have changed.
        material["curation"] = {
            "relevance": None,
            "credibility": None,
            "level_fit": None,
            "signal": None,
            "curriculum_id": None,
            "curriculum_version": None,
            "step_id": None,
            "priority": None,
            "disposition": "untriaged",
            "disposition_reason": "",
            "evaluated_at": None,
        }
        if not existing:
            state["materials"].append(material)
        self._stamp(state, material, "librarian")
        self.store.save(state)
        return material

    def materials(self) -> list[dict]:
        return self.store.load()["materials"]

    @staticmethod
    def _active_curriculum_step(curriculum: dict | None) -> dict | None:
        if not curriculum or curriculum.get("status") != "ready":
            return None
        return next(
            (step for step in curriculum.get("sequence", []) if step.get("status") == "active"),
            None,
        )

    @staticmethod
    def _shelf_is_ready(
        state: dict, shelf: dict, curriculum: dict, step_id: str | None = None
    ) -> bool:
        if (
            shelf.get("status") != "ready"
            or shelf.get("curriculum_id") != curriculum["id"]
            or shelf.get("curriculum_version") != curriculum["version"]
            or (step_id is not None and shelf.get("step_id") != step_id)
        ):
            return False
        selected = shelf.get("selected_material_ids", [])
        if not 3 <= len(selected) <= 4:
            return False
        materials = {item["id"]: item for item in state["materials"]}
        candidates = shelf.get("candidate_material_ids", [])
        if not candidates or not set(selected) <= set(candidates):
            return False
        candidates_valid = all(
            material_id in materials
            and materials[material_id].get("curation", {}).get("disposition")
            in MATERIAL_DISPOSITIONS - {"untriaged"}
            and materials[material_id].get("curation", {}).get("curriculum_id")
            == curriculum["id"]
            and materials[material_id].get("curation", {}).get("curriculum_version")
            == curriculum["version"]
            and materials[material_id].get("curation", {}).get("step_id")
            == shelf.get("step_id")
            for material_id in candidates
        )
        selected_materials = [materials.get(material_id) for material_id in selected]
        identities: set[str] = set()
        fingerprints: set[str] = set()
        selected_valid = True
        for material in selected_materials:
            if not material:
                selected_valid = False
                continue
            reachable, _, identity, fingerprint = Engine.inspect_source(material["source"])
            verification = material.get("verification", {})
            if (
                not reachable
                or identity != verification.get("source_identity")
                or fingerprint != verification.get("content_fingerprint")
                or identity in identities
                or fingerprint in fingerprints
            ):
                selected_valid = False
            identities.add(identity)
            fingerprints.add(fingerprint)
        return candidates_valid and selected_valid and all(
            material_id in materials
            and materials[material_id].get("verified")
            and materials[material_id].get("curation", {}).get("curriculum_id")
            == curriculum["id"]
            and materials[material_id].get("curation", {}).get("curriculum_version")
            == curriculum["version"]
            and materials[material_id].get("curation", {}).get("step_id")
            == shelf.get("step_id")
            and materials[material_id].get("curation", {}).get("disposition")
            in {"core", "supplement"}
            for material_id in selected
        )

    def material_curate(self, material_id: str, assessment: dict, at: datetime) -> dict:
        if not isinstance(assessment, dict):
            raise ValueError("curation assessment must be an object")
        required = {
            *CURATION_AXES,
            "curriculum_id",
            "curriculum_version",
            "step_id",
            "priority",
            "disposition",
            "disposition_reason",
        }
        if set(assessment) != required:
            raise ValueError(
                "curation requires curriculum_id, curriculum_version, step_id, priority, all four axes, "
                "disposition, and disposition_reason"
            )
        normalized: dict = {}
        for axis, allowed in CURATION_AXES.items():
            value = assessment[axis]
            if not isinstance(value, dict) or set(value) != {"decision", "reason"}:
                raise ValueError(f"curation {axis} needs decision and reason")
            decision = value["decision"]
            reason = str(value["reason"]).strip()
            if decision not in allowed or not reason:
                raise ValueError(f"invalid or reasonless curation {axis}")
            normalized[axis] = {"decision": decision, "reason": reason}
        disposition = assessment["disposition"]
        disposition_reason = str(assessment["disposition_reason"]).strip()
        if disposition not in MATERIAL_DISPOSITIONS or not disposition_reason:
            raise ValueError("invalid or reasonless material disposition")
        state = self.store.load()
        profile = self._profile(state)
        curriculum = profile.get("curriculum")
        if (
            not curriculum
            or assessment["curriculum_id"] != curriculum["id"]
            or assessment["curriculum_version"] != curriculum["version"]
        ):
            raise ValueError("curation curriculum does not match the active curriculum")
        if assessment["step_id"] not in {step["id"] for step in curriculum["sequence"]}:
            raise ValueError("curation step does not exist in the active curriculum")
        priority = assessment["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
            raise ValueError("curation priority must be an integer between 1 and 5")
        material = self._find(state["materials"], material_id, "material")
        usable = (
            material["verified"]
            and normalized["relevance"]["decision"] == "belongs"
            and normalized["credibility"]["decision"] == "credible"
            and normalized["level_fit"]["decision"] == "appropriate"
            and normalized["signal"]["decision"] == "signal"
        )
        if disposition in {"core", "supplement"} and not usable:
            raise ValueError("only verified, relevant, credible, level-fit signal can enter a shelf")
        material["curation"] = {
            **normalized,
            "curriculum_id": curriculum["id"],
            "curriculum_version": curriculum["version"],
            "step_id": assessment["step_id"],
            "priority": priority,
            "disposition": disposition,
            "disposition_reason": disposition_reason,
            "evaluated_at": iso(at),
        }
        self._stamp(state, material, "librarian")
        self.store.save(state)
        return material

    def material_shelf(
        self, curriculum_id: str, step_id: str, candidate_ids: list[str], at: datetime
    ) -> dict:
        state = self.store.load()
        profile = self._profile(state)
        curriculum = profile.get("curriculum")
        if not curriculum or curriculum["id"] != curriculum_id:
            raise ValueError("curriculum not found for shelf")
        if step_id not in {step["id"] for step in curriculum["sequence"]}:
            raise ValueError("curriculum step not found for shelf")
        candidate_ids = sorted(set(candidate_ids))
        if not candidate_ids:
            raise ValueError("shelf candidate ids are required")
        candidates = [
            self._find(state["materials"], material_id, "shelf candidate")
            for material_id in candidate_ids
        ]
        invalid_candidates = [
            material["id"]
            for material in candidates
            if material.get("curation", {}).get("disposition") == "untriaged"
            or material.get("curation", {}).get("curriculum_id") != curriculum_id
            or material.get("curation", {}).get("curriculum_version") != curriculum["version"]
            or material.get("curation", {}).get("step_id") != step_id
        ]
        if invalid_candidates:
            raise ValueError(
                "every shelf candidate must be triaged for this curriculum version and step: "
                + ", ".join(invalid_candidates)
            )
        usable = [
            material
            for material in candidates
            if material["verified"]
            and material.get("curation", {}).get("curriculum_id") == curriculum_id
            and material.get("curation", {}).get("curriculum_version") == curriculum["version"]
            and material.get("curation", {}).get("step_id") == step_id
            and material.get("curation", {}).get("disposition") in {"core", "supplement"}
        ]
        usable.sort(
            key=lambda item: (
                item["curation"]["disposition"] != "core",
                -item["curation"]["priority"],
                item["title"].casefold(),
            )
        )
        selected = []
        identities: set[str] = set()
        fingerprints: set[str] = set()
        for material in usable:
            verification = material.get("verification", {})
            identity = verification.get("source_identity", "")
            fingerprint = verification.get("content_fingerprint", "")
            if not identity or not fingerprint or identity in identities or fingerprint in fingerprints:
                continue
            selected.append(material)
            identities.add(identity)
            fingerprints.add(fingerprint)
            if len(selected) == 4:
                break
        shelf = next(
            (
                item
                for item in state["shelves"]
                if item["curriculum_id"] == curriculum_id
                and item.get("curriculum_version") == curriculum["version"]
                and item["step_id"] == step_id
            ),
            None,
        )
        values = {
            "status": "ready" if len(selected) >= 3 else "incomplete",
            "candidate_material_ids": candidate_ids,
            "selected_material_ids": [item["id"] for item in selected],
            "core_material_ids": [
                item["id"]
                for item in selected
                if item["curation"]["disposition"] == "core"
            ],
            "supplement_material_ids": [
                item["id"]
                for item in selected
                if item["curation"]["disposition"] == "supplement"
            ],
            "rejected_material_ids": sorted(
                item["id"]
                for item in candidates
                if item.get("curation", {}).get("curriculum_id") == curriculum_id
                and item.get("curation", {}).get("curriculum_version") == curriculum["version"]
                and item.get("curation", {}).get("step_id") == step_id
                and item.get("curation", {}).get("disposition") == "reject"
            ),
            "missing_count": max(0, 3 - len(selected)),
        }
        unordered = {"candidate_material_ids", "rejected_material_ids"}
        if shelf and all(
            set(shelf.get(key, [])) == set(value)
            if key in unordered
            else shelf.get(key) == value
            for key, value in values.items()
        ):
            return shelf
        if not shelf:
            shelf = {
                "id": f"shelf-{uuid.uuid4().hex[:8]}",
                "curriculum_id": curriculum_id,
                "curriculum_version": curriculum["version"],
                "step_id": step_id,
                "created_at": iso(at),
            }
            state["shelves"].append(shelf)
        shelf.update({**values, "updated_at": iso(at)})
        self._stamp(state, shelf, "librarian")
        self.store.save(state)
        return shelf

    # Tutor
    @staticmethod
    def retention(memory: dict, at: datetime) -> float:
        anchor = memory["last_reviewed_at"] or memory["first_exposed_at"]
        elapsed = max(0.0, (at - parse_time(anchor)).total_seconds() / 86400)
        return 0.9 ** (elapsed / memory["stability_days"])

    @staticmethod
    def due_interval(stability_days: float, target_retention: float) -> float:
        return stability_days * math.log(target_retention) / math.log(0.9)

    @staticmethod
    def _ready_for_retrieval(item: dict) -> bool:
        if item.get("memory", {}).get("review_count", 0) > 0:
            return True
        teaching = item.get("last_teaching") or {}
        application = item.get("last_interaction") or {}
        return (
            bool(teaching.get("why_chain"))
            and bool(teaching.get("connection_basis"))
            and application.get("phase") == "exposure"
            and application.get("sequence", 0) > teaching.get("sequence", 0)
        )

    @classmethod
    def review_phase(cls, item: dict, at: datetime) -> str:
        return (
            "retrieval"
            if cls._ready_for_retrieval(item) and parse_time(item["memory"]["due_at"]) <= at
            else "exposure"
        )

    def _due_from_state(self, state: dict, at: datetime) -> list[dict]:
        due = []
        for item in state["knowledge"]:
            if (
                item.get("active", True)
                and self._ready_for_retrieval(item)
                and parse_time(item["memory"]["due_at"]) <= at
            ):
                due.append({**item, "retention": self.retention(item["memory"], at)})
        return sorted(due, key=lambda item: (item["retention"], item["title"].casefold()))

    @staticmethod
    def _record_confusions(
        item: dict, added: list[str], cleared: list[str], at: datetime
    ) -> None:
        history = item.setdefault("confusion_history", [])
        for point in unique(added):
            existing = next((entry for entry in history if entry["point"] == point), None)
            if existing:
                existing.update(
                    {"last_seen_at": iso(at), "times_seen": existing["times_seen"] + 1, "resolved_at": None}
                )
            else:
                history.append(
                    {
                        "point": point,
                        "first_seen_at": iso(at),
                        "last_seen_at": iso(at),
                        "times_seen": 1,
                        "resolved_at": None,
                    }
                )
        for point in unique(cleared):
            existing = next((entry for entry in history if entry["point"] == point), None)
            if not existing:
                existing = {
                    "point": point,
                    "first_seen_at": item.get("created_at", iso(at)),
                    "last_seen_at": item.get("created_at", iso(at)),
                    "times_seen": 1,
                    "resolved_at": None,
                }
                history.append(existing)
            existing["resolved_at"] = iso(at)

    def knowledge_add(
        self,
        title: str,
        explanation: str,
        knowledge_type: str,
        weak_points: list[str],
        related: list[str],
        sources: list[str],
        at: datetime,
    ) -> dict:
        if not title.strip() or not explanation.strip():
            raise ValueError("knowledge title and explanation are required")
        if knowledge_type not in KNOWLEDGE_TYPES:
            raise ValueError(f"knowledge type must be one of: {', '.join(sorted(KNOWLEDGE_TYPES))}")
        state = self.store.load()
        profile = self._profile(state)
        curriculum = profile.get("curriculum")
        active_step = self._active_curriculum_step(curriculum)
        known_ids = {
            item["id"]
            for item in state["knowledge"]
            if item.get("active", True)
            and item.get("subject_binding") == self._subject_binding(state)
        }
        unknown_related = set(related) - known_ids
        if unknown_related:
            raise ValueError(f"unknown related knowledge: {', '.join(sorted(unknown_related))}")
        selected_ids = {
            material_id
            for shelf in state["shelves"]
            if curriculum
            and active_step
            and self._shelf_is_ready(state, shelf, curriculum, active_step["id"])
            for material_id in shelf["selected_material_ids"]
        }
        curated_sources = {
            value
            for material in state["materials"]
            if material["id"] in selected_ids
            for value in (material["id"], material["source"])
        }
        unknown_sources = set(sources) - curated_sources
        if unknown_sources:
            raise ValueError(f"unselected sources: {', '.join(sorted(unknown_sources))}")
        existing = next(
            (
                item
                for item in state["knowledge"]
                if item.get("active", True)
                and item.get("subject_binding")
                == {"goal": profile["goal"], "focus": profile["focus"]}
                and item["title"].casefold() == title.strip().casefold()
            ),
            None,
        )
        if existing:
            if existing["id"] in related:
                raise ValueError("knowledge cannot relate to itself")
            values = {
                "explanation": explanation.strip(),
                "type": knowledge_type,
                "weak_points": unique(existing["weak_points"] + weak_points),
                "related": unique(existing["related"] + related),
                "sources": unique(existing["sources"] + sources),
                "active": True,
            }
            if all(existing.get(key) == value for key, value in values.items()):
                return existing
            existing.update({**values, "updated_at": iso(at)})
            item = existing
            self._record_confusions(item, weak_points, [], at)
        else:
            stability = 1.0
            interval = self.due_interval(stability, profile["target_retention"])
            item = {
                "id": self._id(title, state["knowledge"]),
                "title": title.strip(),
                "explanation": explanation.strip(),
                "type": knowledge_type,
                "weak_points": unique(weak_points),
                "related": unique(related),
                "sources": unique(sources),
                "created_at": iso(at),
                "updated_at": iso(at),
                "last_interaction": None,
                "last_teaching": None,
                "interaction_count": 0,
                "active": True,
                "subject_binding": {
                    "goal": profile["goal"], "focus": copy.deepcopy(profile["focus"])
                },
                "confusion_history": [],
                "memory": {
                    "stability_days": stability,
                    "first_exposed_at": iso(at),
                    "last_reviewed_at": None,
                    "due_at": iso(at + timedelta(days=interval)),
                    "exposure_count": 0,
                    "review_count": 0,
                    "lapse_count": 0,
                },
            }
            self._record_confusions(item, weak_points, [], at)
            state["knowledge"].append(item)
        self._stamp(state, item, "tutor")
        for related_id in item["related"]:
            related_item = self._current_knowledge(state, related_id)
            related_item["related"] = unique(related_item["related"] + [item["id"]])
            related_item["updated_at"] = iso(at)
        self.store.save(state)
        return item

    def knowledge(self) -> list[dict]:
        return self.store.load()["knowledge"]

    def tutor_context(self) -> dict:
        state = self.store.load()
        profile = self._profile(state)
        return {
            "current_level": profile["current_level"],
            "focus": profile["focus"],
            "knowledge": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "weak_points": item["weak_points"],
                    "related": item["related"],
                    "review_count": item["memory"]["review_count"],
                    "last_confidence": (
                        item["last_interaction"].get("confidence")
                        if item["last_interaction"]
                        else None
                    ),
                    "last_answer": (
                        item["last_interaction"].get("answer") if item["last_interaction"] else None
                    ),
                    "last_teaching": item.get("last_teaching"),
                    "confusion_history": item.get("confusion_history", []),
                }
                for item in state["knowledge"]
                if item.get("active", True)
            ],
        }

    def knowledge_relate(self, left_id: str, right_id: str, at: datetime) -> dict:
        if left_id == right_id:
            raise ValueError("knowledge cannot relate to itself")
        state = self.store.load()
        self._profile(state)
        left = self._current_knowledge(state, left_id)
        right = self._current_knowledge(state, right_id)
        left["related"] = unique(left["related"] + [right_id])
        right["related"] = unique(right["related"] + [left_id])
        left["updated_at"] = right["updated_at"] = iso(at)
        self._stamp(state, left, "tutor")
        self._stamp(state, right, "tutor")
        self.store.save(state)
        return {"left": left_id, "right": right_id}

    def due(self, at: datetime) -> list[dict]:
        state = self.store.load()
        self._profile(state)
        return self._due_from_state(state, at)

    def teach(self, item_id: str, explanation: str, connection: str, at: datetime) -> dict:
        if not explanation.strip() or not connection.strip():
            raise ValueError("teaching explanation and connection are required")
        why_chain = teaching_why_chain(explanation)
        state = self.store.load()
        profile = self._profile(state)
        item = self._current_knowledge(state, item_id)
        related = [
            self._current_knowledge(state, related_id)
            for related_id in item.get("related", [])
        ]
        baseline = (profile.get("curriculum") or {}).get("baseline", {})
        candidates = [
            ({"kind": "knowledge", "id": known["id"]}, known["title"])
            for known in related
        ] + [
            ({"kind": "curriculum_baseline", "value": value}, value)
            for value in baseline.get("can_do", []) + baseline.get("assisted", [])
        ]
        connection_tokens = set(
            re.findall(r"[0-9a-z가-힣]+", unicodedata.normalize("NFKC", connection).casefold())
        )
        scored = [
            (
                1 if canonical_text(anchor) in canonical_text(connection) else 0,
                len(
                    connection_tokens
                    & set(
                        re.findall(
                            r"[0-9a-z가-힣]+",
                            unicodedata.normalize("NFKC", anchor).casefold(),
                        )
                    )
                ),
                basis,
            )
            for basis, anchor in candidates
            if connection_tokens
            & set(re.findall(r"[0-9a-z가-힣]+", unicodedata.normalize("NFKC", anchor).casefold()))
        ]
        best = max((score[:2] for score in scored), default=None)
        matches = [basis for exact, overlap, basis in scored if (exact, overlap) == best]
        if len(matches) != 1:
            raise ValueError(
                "teaching connection must name exactly one related knowledge or curriculum baseline"
            )
        basis = matches[0]
        memory = item["memory"]
        item["interaction_count"] += 1
        event = {
            "knowledge_id": item_id,
            "interacted_at": iso(at),
            "interaction": "teaching",
            "phase": "exposure",
            "teaching": explanation.strip(),
            "why_chain": why_chain,
            "connection": connection.strip(),
            "connection_basis": basis,
            "sequence": item["interaction_count"],
            "stability_before": memory["stability_days"],
            "stability_after": memory["stability_days"],
        }
        memory["exposure_count"] += 1
        item["last_teaching"] = event
        item["updated_at"] = iso(at)
        self._stamp(state, item, "tutor")
        self.store.save_with_review(state, event)
        return item

    def review(
        self,
        item_id: str,
        rating: str,
        add_weak: list[str],
        clear_weak: list[str],
        confidence: str,
        prompt: str,
        answer: str,
        rationale: str,
        at: datetime,
        request_id: str | None = None,
    ) -> dict:
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of: {', '.join(sorted(RATINGS))}")
        if confidence not in CONFIDENCE:
            raise ValueError(f"confidence must be one of: {', '.join(sorted(CONFIDENCE))}")
        allowed_confidence = {
            "again": {"failed", "partial"},
            "hard": {"partial", "complete"},
            "good": {"complete"},
            "easy": {"complete"},
        }
        if confidence not in allowed_confidence[rating]:
            raise ValueError("rating and confidence disagree")
        added = unique(add_weak)
        cleared = unique(clear_weak)
        if set(added) & set(cleared):
            raise ValueError("a weak point cannot be added and cleared in the same review")
        if confidence != "complete" and not unique(add_weak):
            raise ValueError("partial or failed confidence requires at least one added weak point")
        if confidence == "complete" and added:
            raise ValueError("complete confidence cannot add a weak point")
        if not prompt.strip() or not answer.strip() or not rationale.strip():
            raise ValueError("review prompt, answer, and rationale are required")
        state = self.store.load()
        profile = self._profile(state)
        item = self._current_knowledge(state, item_id)
        memory = item["memory"]
        ready_before = self._ready_for_retrieval(item)
        item["interaction_count"] += 1
        before = memory["stability_days"]
        phase = self.review_phase(item, at)
        after = before
        if phase == "retrieval":
            if rating == "again":
                after = max(0.25, min(1.0, before * 0.5))
                memory["lapse_count"] += 1
            elif rating == "hard":
                after = max(0.5, before * 1.2)
            elif rating == "good":
                after = max(1.0, before * 2.0)
            else:
                after = max(2.0, before * 3.0)
        cleared_set = set(cleared)
        self._record_confusions(item, added, cleared, at)
        item["weak_points"] = unique(
            [point for point in item["weak_points"] if point not in cleared_set] + added
        )
        if phase == "retrieval":
            memory.update(
                {
                    "stability_days": round(after, 6),
                    "last_reviewed_at": iso(at),
                    "due_at": iso(
                        at + timedelta(days=self.due_interval(after, profile["target_retention"]))
                    ),
                    "review_count": memory["review_count"] + 1,
                }
            )
        else:
            memory["exposure_count"] += 1
            teaching = item.get("last_teaching") or {}
            if not ready_before and teaching.get("sequence", 0) < item["interaction_count"]:
                memory["first_exposed_at"] = iso(at)
                memory["due_at"] = iso(
                    at
                    + timedelta(
                        days=self.due_interval(
                            memory["stability_days"], profile["target_retention"]
                        )
                    )
                )
        item["updated_at"] = iso(at)
        event = {
            "knowledge_id": item_id,
            "interacted_at": iso(at),
            "interaction": "review",
            "phase": phase,
            "rating": rating,
            "confidence": confidence,
            "prompt": prompt.strip(),
            "answer": answer.strip(),
            "rationale": rationale.strip(),
            "sequence": item["interaction_count"],
            "stability_before": before,
            "stability_after": memory["stability_days"],
        }
        if request_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
                raise ValueError("invalid review request id")
            event.update(
                {
                    "request_id": request_id,
                    "due_at_after": memory["due_at"],
                    "weak_points_after": copy.deepcopy(item["weak_points"]),
                }
            )
        item["last_interaction"] = event
        self._stamp(state, item, "tutor")
        self.store.save_with_review(state, event)
        return item

    # Editor
    def artifact_add(
        self,
        title: str,
        content: str,
        purpose: str,
        audience: str,
        milestone_id: str | None,
        at: datetime,
        submitted_by: str,
    ) -> dict:
        if submitted_by != "learner":
            raise ValueError("artifact versions must be submitted by the learner")
        if not all(value.strip() for value in (title, content, purpose, audience)):
            raise ValueError("artifact title, content, purpose, and audience are required")
        state = self.store.load()
        curriculum_id = None
        curriculum_version = None
        if milestone_id:
            profile = self._profile(state)
            curriculum = profile.get("curriculum")
            if not curriculum:
                raise ValueError("artifact milestone requires a curriculum")
            self._find(curriculum["milestones"], milestone_id, "milestone")
            curriculum_id = curriculum["id"]
            curriculum_version = curriculum["version"]
        artifact = {
            "id": self._id(title, state["artifacts"]),
            "title": title.strip(),
            "purpose": purpose.strip(),
            "audience": audience.strip(),
            "milestone_id": milestone_id,
            "curriculum_id": curriculum_id,
            "curriculum_version": curriculum_version,
            "content": content,
            "current_version": 1,
            "versions": [
                {
                    "version": 1,
                    "content": content,
                    "author": "learner",
                    "submitted_at": iso(at),
                }
            ],
            "status": "draft",
            "migration_import_required": False,
            "produced_by_handoff_id": None,
            "review_rounds": [],
            "created_at": iso(at),
            "updated_at": iso(at),
            "previous_versions": [],
            "feedback": [],
        }
        state["artifacts"].append(artifact)
        self.store.save(state)
        return artifact

    def artifact_show(self, artifact_id: str) -> dict:
        state = self.store.load()
        return self._find(state["artifacts"], artifact_id, "artifact")

    def artifact_review(
        self,
        artifact_id: str,
        criteria: dict,
        verdict: str,
        next_action: str,
        at: datetime,
        milestone_criteria: dict | None = None,
    ) -> dict:
        if verdict not in {"revise", "pass"}:
            raise ValueError("editor verdict must be revise or pass")
        if not isinstance(criteria, dict) or set(criteria) != EDITOR_DIMENSIONS:
            raise ValueError("editor review requires all seven criteria exactly once")
        normalized = {}
        findings = []
        for dimension in sorted(EDITOR_DIMENSIONS):
            value = criteria[dimension]
            if not isinstance(value, dict) or value.get("status") not in {"pass", "revise"}:
                raise ValueError(f"invalid editor criterion: {dimension}")
            note = str(value.get("note", "")).strip()
            if not note:
                raise ValueError(f"editor criterion note is required: {dimension}")
            item = {"status": value["status"], "note": note}
            if value["status"] == "revise":
                severity = value.get("severity")
                if severity not in {"blocking", "non_blocking"}:
                    raise ValueError("editor finding severity must be blocking or non_blocking")
                for field in ("evidence_span", "diagnosis", "revision_action"):
                    if not str(value.get(field, "")).strip():
                        raise ValueError(f"editor revise criterion needs {field}")
                item.update(
                    {
                        "severity": severity,
                        "evidence_span": str(value["evidence_span"]).strip(),
                        "diagnosis": str(value["diagnosis"]).strip(),
                        "revision_action": str(value["revision_action"]).strip(),
                    }
                )
                findings.append(
                    {
                        "id": f"finding-{uuid.uuid4().hex[:8]}",
                        "dimension": dimension,
                        **{key: item[key] for key in (
                            "severity", "evidence_span", "diagnosis", "revision_action"
                        )},
                        "status": "open",
                    }
                )
            normalized[dimension] = item
        state = self.store.load()
        artifact = self._find(state["artifacts"], artifact_id, "artifact")
        if artifact.get("migration_import_required"):
            raise ValueError("legacy artifact requires explicit learner resubmission before review")
        current_content = unicodedata.normalize("NFKC", artifact["content"]).replace("\r\n", "\n")
        if any(
            unicodedata.normalize("NFKC", finding["evidence_span"]).replace("\r\n", "\n")
            not in current_content
            for finding in findings
        ):
            raise ValueError("editor finding evidence span must occur in the current learner version")
        if artifact.get("status") == "passed":
            raise ValueError("passed artifact cannot be reviewed again")
        if any(
            round_["version"] == artifact["current_version"]
            for round_ in artifact.get("review_rounds", [])
        ):
            raise ValueError("artifact version has already been reviewed")
        normalized_milestone = None
        milestone_failed = False
        if artifact.get("milestone_id"):
            profile = self._profile(state)
            curriculum = profile.get("curriculum")
            if (
                not curriculum
                or artifact.get("curriculum_id") != curriculum["id"]
                or artifact.get("curriculum_version") != curriculum["version"]
            ):
                raise ValueError("artifact belongs to a different curriculum version")
            milestone = self._find(
                curriculum["milestones"], artifact["milestone_id"], "milestone"
            )
            expected = set(milestone["pass_criteria"])
            if not isinstance(milestone_criteria, dict) or set(milestone_criteria) != expected:
                raise ValueError("editor review requires every milestone pass criterion")
            normalized_milestone = {}
            for name in milestone["pass_criteria"]:
                value = milestone_criteria[name]
                if not isinstance(value, dict) or value.get("status") not in {"pass", "revise"}:
                    raise ValueError(f"invalid milestone criterion: {name}")
                note = str(value.get("note", "")).strip()
                if not note:
                    raise ValueError(f"milestone criterion note is required: {name}")
                normalized_milestone[name] = {"status": value["status"], "note": note}
                milestone_failed = milestone_failed or value["status"] == "revise"
        elif milestone_criteria is not None:
            raise ValueError("milestone criteria require a milestone-linked artifact")
        needs_revision = bool(findings) or milestone_failed
        if needs_revision != (verdict == "revise"):
            raise ValueError("editor verdict must match criterion findings")
        if verdict == "revise" and not next_action.strip():
            raise ValueError("revision verdict needs a next action")
        prior_findings = [
            finding
            for previous_round in artifact.get("review_rounds", [])
            for finding in previous_round.get("findings", [])
        ]
        for finding in findings:
            resolved = next(
                (
                    previous
                    for previous in reversed(prior_findings)
                    if previous["dimension"] == finding["dimension"]
                    and previous.get("status") == "resolved"
                ),
                None,
            )
            if resolved:
                finding["status"] = "regressed"
                finding["regression_of"] = resolved["id"]
        for previous_round in artifact.get("review_rounds", []):
            for finding in previous_round.get("findings", []):
                if finding.get("status") not in {"open", "regressed"}:
                    continue
                if normalized[finding["dimension"]]["status"] == "pass":
                    finding["status"] = "resolved"
                    finding["resolved_in_version"] = artifact["current_version"]
        round_ = {
            "round": len(artifact["review_rounds"]) + 1,
            "version": artifact["current_version"],
            "criteria": normalized,
            "milestone_criteria": normalized_milestone,
            "findings": findings,
            "verdict": verdict,
            "next_action": next_action.strip(),
            "reviewed_at": iso(at),
        }
        artifact["review_rounds"].append(round_)
        artifact["status"] = "needs_revision" if verdict == "revise" else "passed"
        artifact["updated_at"] = iso(at)
        self._stamp(state, artifact, "editor")
        self.store.save(state)
        return artifact

    def artifact_revise(
        self, artifact_id: str, content: str, at: datetime, submitted_by: str
    ) -> dict:
        if submitted_by != "learner":
            raise ValueError("artifact versions must be submitted by the learner")
        if not content.strip():
            raise ValueError("revised content is required")
        state = self.store.load()
        artifact = self._find(state["artifacts"], artifact_id, "artifact")
        if artifact.get("status") != "needs_revision" and not artifact.get(
            "migration_import_required"
        ):
            raise ValueError("artifact can be revised only after a revise verdict")
        normalized_content = (
            unicodedata.normalize("NFKC", content)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
        previous_content = (
            unicodedata.normalize("NFKC", artifact["content"])
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
        if normalized_content == previous_content:
            raise ValueError("revised content must substantively change the learner draft")
        artifact["previous_versions"].append(
            {"replaced_at": iso(at), "content": artifact["content"]}
        )
        artifact["content"] = content
        artifact["current_version"] += 1
        artifact["versions"].append(
            {
                "version": artifact["current_version"],
                "content": content,
                "author": "learner",
                "submitted_at": iso(at),
            }
        )
        artifact["status"] = "draft"
        artifact["migration_import_required"] = False
        artifact["produced_by_handoff_id"] = None
        artifact["updated_at"] = iso(at)
        self.store.save(state)
        return artifact

    # Orchestrator continuity
    def session_start(
        self, context: str, at: datetime, workflow_id: str | None = None
    ) -> dict:
        state = self.store.load()
        if state["active_session_id"]:
            raise ValueError("a session is already active")
        if workflow_id:
            workflow = self._find(state["workflows"], workflow_id, "session workflow")
            if workflow["status"] != "active":
                raise ValueError("session workflow must be active")
            if workflow.get("profile_binding") != self._profile_binding(state):
                self._supersede_workflow(state, workflow, at)
                self.store.save(state)
                raise ValueError("session workflow profile target was superseded")
        session = {
            "id": f"session-{uuid.uuid4().hex[:8]}",
            "started_at": iso(at),
            "ended_at": None,
            "context": context.strip(),
            "workflow_id": workflow_id,
            "notes": [],
            "summary": "",
            "next_step": "",
            "status": "active",
        }
        state["sessions"].append(session)
        state["active_session_id"] = session["id"]
        self.store.save(state)
        return self._session_view(state, session)

    @classmethod
    def _session_view(cls, state: dict, session: dict) -> dict:
        view = copy.deepcopy(session)
        workflow_id = session.get("workflow_id")
        if not workflow_id:
            return view
        workflow = cls._find(state["workflows"], workflow_id, "session workflow")
        current_step = next(
            (
                step
                for status in ("dispatched", "ready", "waiting")
                for step in workflow["steps"]
                if step["status"] == status
            ),
            None,
        )
        handoff = None
        if current_step and current_step.get("handoff_id"):
            handoff = cls._find(
                state["handoffs"], current_step["handoff_id"], "session handoff"
            )
        view.update(
            {
                "workflow": copy.deepcopy(workflow),
                "current_step": copy.deepcopy(current_step),
                "handoff": copy.deepcopy(handoff),
            }
        )
        return view

    def session_note(self, note: str, next_step: str, at: datetime) -> dict:
        if not note.strip():
            raise ValueError("session note is required")
        state = self.store.load()
        if not state["active_session_id"]:
            raise ValueError("no active session")
        session = self._find(state["sessions"], state["active_session_id"], "session")
        session["notes"].append({"at": iso(at), "text": note.strip()})
        if next_step.strip():
            session["next_step"] = next_step.strip()
        self.store.save(state)
        return session

    def session_end(self, summary: str, next_step: str, at: datetime) -> dict:
        if not summary.strip():
            raise ValueError("session summary is required")
        state = self.store.load()
        if not state["active_session_id"]:
            raise ValueError("no active session")
        session = self._find(state["sessions"], state["active_session_id"], "session")
        session.update(
            {
                "ended_at": iso(at),
                "summary": summary.strip(),
                "next_step": next_step.strip(),
                "status": "ended",
            }
        )
        state["active_session_id"] = None
        self.store.save(state)
        return session

    def resume(self, at: datetime | None = None) -> dict:
        at = at or now_utc()
        state = self.store.load()
        session = (
            self._find(state["sessions"], state["active_session_id"], "session")
            if state["active_session_id"]
            else state["sessions"][-1] if state["sessions"] else None
        )
        if session:
            workflow_id = session.get("workflow_id")
            if workflow_id:
                workflow = self._find(state["workflows"], workflow_id, "session workflow")
                if workflow.get("profile_binding") != self._profile_binding(state):
                    active_handoff = next(
                        (
                            handoff
                            for handoff in state["handoffs"]
                            if handoff.get("workflow_id") == workflow_id
                            and handoff.get("expected_output_kind") == "curriculum"
                            and handoff["status"] in {"pending", "in_progress"}
                        ),
                        None,
                    )
                    if active_handoff:
                        return self._session_view(state, session)
                    if workflow["status"] == "active":
                        self._supersede_workflow(state, workflow, at)
                    session["workflow_id"] = None
                    session["next_step"] = "start a new workflow for the current learning target"
                    self.store.save(state)
                    view = self._session_view(state, session)
                    view["recommendation"] = self.recommend(at) if state["profile"] else self.route("learn", at)
                    return view
            return self._session_view(state, session)
        if state["profile"]:
            advice = self.recommend(at)
            subject = advice.get("title") or advice.get("topic") or advice.get("goal", "")
            return {
                "status": "new",
                "next_step": f"{advice['action']}: {subject}".rstrip(": "),
                "recommendation": advice,
            }
        route = self.route("learn", at)
        return {
            "status": "new",
            "next_step": "start the Advisor profile and five-decision curriculum",
            "recommendation": route,
        }

    # Roommate
    def perspective_start(
        self,
        current_field: str,
        current_problem: str,
        outside_field: str,
        lens: str,
        question: str,
        at: datetime,
        handoff_id: str | None = None,
    ) -> dict:
        values = (current_field, current_problem, outside_field, lens, question)
        if not all(value.strip() for value in values):
            raise ValueError("perspective fields and one connection question are required")
        if canonical_text(current_field) == canonical_text(outside_field):
            raise ValueError("Roommate outside field must differ from the current field")
        if unicodedata.normalize("NFKC", question).count("?") > 1:
            raise ValueError("Roommate asks one connection question at a time")
        state = self.store.load()
        matching_handoffs = [
            handoff
            for handoff in state["handoffs"]
            if handoff["to"] == "roommate"
            and handoff["status"] == "in_progress"
            and all(
                canonical_text(value) == canonical_text(handoff.get("request_spec", {}).get(key, ""))
                for key, value in {
                    "current_field": current_field,
                    "current_problem": current_problem,
                }.items()
            )
        ]
        if handoff_id:
            handoff = self._find(matching_handoffs, handoff_id, "Roommate handoff scope")
            if any(item.get("handoff_id") == handoff["id"] for item in state["perspectives"]):
                raise ValueError("Roommate handoff already produced its connection question")
        elif len(matching_handoffs) == 1:
            handoff_id = matching_handoffs[0]["id"]
        if any(item.get("connection") is None for item in state["perspectives"]):
            raise ValueError("answer the pending Roommate question before asking another")
        signature = tuple(canonical_text(value) for value in (current_problem, lens, question))
        if any(
            signature
            == tuple(
                canonical_text(value)
                for value in (
                    item["current_problem"], item["lens"], item["turns"][0]["question"],
                )
            )
            for item in state["perspectives"]
        ):
            raise ValueError("Roommate must use a new lens or connection question")
        perspective = {
            "id": f"perspective-{uuid.uuid4().hex[:8]}",
            "handoff_id": handoff_id,
            "current_field": current_field.strip(),
            "current_problem": current_problem.strip(),
            "outside_field": outside_field.strip(),
            "lens": lens.strip(),
            "turns": [
                {
                    "question": question.strip(),
                    "learner_response": None,
                    "asked_at": iso(at),
                    "answered_at": None,
                }
            ],
            "connection": None,
            "recommendations": [],
            "created_at": iso(at),
            "updated_at": iso(at),
        }
        state["perspectives"].append(perspective)
        self.store.save(state)
        return perspective

    def perspective_answer(
        self,
        perspective_id: str,
        response: str,
        status: str,
        learner_insight: str,
        mapping: str,
        limits: str,
        recommendations: list[str],
        at: datetime,
    ) -> dict:
        if not response.strip():
            raise ValueError("learner response is required before recording a connection")
        if status not in PERSPECTIVE_STATUSES:
            raise ValueError(
                f"perspective status must be one of: {', '.join(sorted(PERSPECTIVE_STATUSES))}"
            )
        if status == "insight" and not all(
            value.strip() for value in (learner_insight, mapping, limits)
        ):
            raise ValueError("insight needs learner insight, mapping, and limits")
        if status == "needs_verification" and not unique(recommendations):
            raise ValueError("needs_verification requires a recommendation")
        state = self.store.load()
        perspective = self._find(state["perspectives"], perspective_id, "perspective")
        if perspective["connection"] is not None:
            raise ValueError("perspective question has already been answered")
        turn = perspective["turns"][-1]
        if turn["learner_response"] is not None:
            raise ValueError("perspective turn has already been answered")
        turn["learner_response"] = response.strip()
        turn["answered_at"] = iso(at)
        perspective["connection"] = {
            "status": status,
            "learner_insight": learner_insight.strip(),
            "mapping": mapping.strip(),
            "limits": limits.strip(),
        }
        perspective["recommendations"] = unique(recommendations)
        perspective["updated_at"] = iso(at)
        self.store.save(state)
        return perspective

    def perspectives(self) -> list[dict]:
        return self.store.load()["perspectives"]

    # Orchestrator handoffs
    def route(self, intent: str, at: datetime) -> dict:
        if intent not in {"plan", "material", "learn", "artifact", "write", "perspective", "resume"}:
            raise ValueError(
                "route intent must be plan, material, learn, artifact, perspective, or resume"
            )
        if intent == "resume":
            return {
                "role": "orchestrator",
                "action": "resume",
                "workflow": [],
                "reason": "continuity belongs to the Orchestrator",
            }
        if intent == "plan":
            return {
                "role": "advisor",
                "workflow": ["advisor"],
                "reason": "explicit personalized curriculum request",
            }
        if intent == "material":
            state = self.store.load()
            profile = state.get("profile") or {}
            curriculum = profile.get("curriculum")
            if not self._active_curriculum_step(curriculum):
                return {
                    "role": "advisor",
                    "workflow": ["advisor", "librarian"],
                    "reason": "source curation needs an active curriculum version and step first",
                }
            return {
                "role": "librarian",
                "workflow": ["librarian"],
                "reason": "explicit source curation request",
            }
        if intent in {"artifact", "write"}:
            return {
                "role": "editor",
                "workflow": ["editor"],
                "reason": "explicit learner work review request",
            }
        if intent == "perspective":
            return {
                "role": "roommate",
                "workflow": ["roommate"],
                "reason": "explicit outside-field perspective request",
            }
        state = self.store.load()
        due_or_weak = self._due_from_state(state, at) or any(
            item.get("active", True) and item["weak_points"] for item in state["knowledge"]
        )
        if due_or_weak:
            return {
                "role": "advisor",
                "workflow": ["advisor", "tutor", "advisor"],
                "reason": "due knowledge or a saved confusion needs an adaptive learning goal",
            }
        if not state["profile"]:
            return {
                "role": "advisor",
                "workflow": ["advisor", "librarian", "tutor", "advisor"],
                "reason": "profile and five-decision curriculum are not initialized",
            }
        profile = self._profile(state)
        if not profile.get("curriculum"):
            return {
                "role": "advisor",
                "workflow": ["advisor", "librarian", "tutor", "advisor"],
                "reason": "five-decision curriculum is not ready",
            }
        curriculum = profile["curriculum"]
        active_step = self._active_curriculum_step(curriculum)
        if curriculum.get("status") == "completed" or not active_step:
            return {
                "role": "advisor",
                "workflow": ["advisor", "librarian", "tutor", "advisor"],
                "reason": "the completed or blocked path must be replanned before learning continues",
            }
        if any(
            self._shelf_is_ready(state, shelf, curriculum, active_step["id"])
            for shelf in state["shelves"]
        ):
            return {
                "role": "tutor",
                "workflow": ["tutor", "advisor"],
                "reason": "curriculum and a curated source shelf are ready",
            }
        return {
            "role": "librarian",
            "workflow": ["librarian", "tutor", "advisor"],
            "reason": "no ready curated source shelf exists for the curriculum",
        }

    @staticmethod
    def _profile_binding(state: dict) -> dict | None:
        profile = state.get("profile")
        if not profile:
            return None
        return {
            "goal": profile["goal"],
            "focus": copy.deepcopy(profile["focus"]),
            "target_retention": profile["target_retention"],
        }

    @staticmethod
    def _supersede_workflow(state: dict, workflow: dict, at: datetime) -> None:
        workflow["status"] = "superseded"
        workflow["superseded_at"] = iso(at)
        workflow["updated_at"] = iso(at)
        for step in workflow["steps"]:
            if step["status"] != "completed":
                step["status"] = "cancelled"
        for handoff in state["handoffs"]:
            if (
                handoff.get("workflow_id") == workflow["id"]
                and handoff["status"] in {"pending", "in_progress"}
            ):
                handoff["status"] = "cancelled"
                handoff["cancelled_at"] = iso(at)

    @staticmethod
    def _append_handoff(
        state: dict,
        target: str,
        task: str,
        context: str,
        depends_on: list[str],
        workflow_id: str | None,
        step_id: str | None,
        at: datetime,
        expected_output_kind: str | None = None,
        expected_resource_ids: list[str] | None = None,
        request_spec: dict | None = None,
    ) -> dict:
        handoff = {
            "id": f"handoff-{uuid.uuid4().hex[:8]}",
            "from": "orchestrator",
            "to": target,
            "task": task.strip(),
            "context": context.strip(),
            "depends_on": unique(depends_on),
            "dependency_context": [],
            "request_spec": copy.deepcopy(request_spec),
            "workflow_id": workflow_id,
            "step_id": step_id,
            "profile_binding": Engine._profile_binding(state),
            "resource_snapshot": Engine._role_resource_fingerprints(state, target),
            "output_fingerprints": {},
            "level_evidence_count": len((state.get("profile") or {}).get("level_evidence", [])),
            "expected_output_kind": expected_output_kind,
            "expected_resource_ids": unique(expected_resource_ids or []),
            "legacy_unverified_output": False,
            "status": "pending",
            "created_at": iso(at),
            "claimed_at": None,
            "completed_at": None,
            "result": None,
        }
        state["handoffs"].append(handoff)
        return handoff

    def handoff_dispatch(
        self,
        target: str,
        task: str,
        context: str,
        at: datetime,
        depends_on: list[str] | None = None,
        resource_ids: list[str] | None = None,
        perspective_spec: dict | None = None,
        output_kind: str | None = None,
    ) -> dict:
        if target not in SPECIALISTS:
            raise ValueError(f"handoff target must be one of: {', '.join(sorted(SPECIALISTS))}")
        if not task.strip():
            raise ValueError("handoff task is required")
        state = self.store.load()
        dependencies = unique(depends_on or [])
        known = {handoff["id"] for handoff in state["handoffs"]}
        unknown = set(dependencies) - known
        if unknown:
            raise ValueError(f"unknown handoff dependencies: {', '.join(sorted(unknown))}")
        requested_ids = unique(resource_ids or [])
        request_spec = None
        if target == "editor":
            if len(requested_ids) != 1:
                raise ValueError("manual Editor handoff requires exactly one artifact id")
            artifact = self._find(state["artifacts"], requested_ids[0], "artifact request")
            if artifact.get("migration_import_required"):
                raise ValueError("legacy artifact requires explicit learner resubmission")
            if artifact.get("status") not in {"draft", "needs_revision"}:
                raise ValueError("artifact workflow requires a reviewable learner version")
        elif requested_ids:
            raise ValueError(f"manual {target} handoff does not accept resource ids")
        if target == "roommate":
            if (
                not isinstance(perspective_spec, dict)
                or set(perspective_spec) != PERSPECTIVE_REQUEST_FIELDS
            ):
                raise ValueError("manual Roommate handoff requires current field and problem")
            request_spec = {key: str(value).strip() for key, value in perspective_spec.items()}
            if not all(request_spec.values()):
                raise ValueError("perspective request fields are required")
            if any(item.get("connection") is None for item in state["perspectives"]):
                raise ValueError("answer the pending Roommate question before dispatch")
        elif perspective_spec:
            raise ValueError(f"manual {target} handoff does not accept a perspective request")
        if target == "advisor":
            if output_kind not in {"curriculum", "advisor_update"}:
                raise ValueError(
                    "manual Advisor handoff requires output kind curriculum or advisor_update"
                )
        elif output_kind is not None:
            raise ValueError(f"manual {target} handoff does not accept an output kind")
        output_kind = output_kind or {
            "advisor": "advisor_update",
            "librarian": "ready_shelf",
            "tutor": "step_knowledge",
            "editor": "reviewed_artifact",
            "roommate": "answered_perspective",
        }[target]
        expected_ids: list[str] = []
        if target == "tutor":
            due = self._due_from_state(state, at)
            weak = [
                item for item in state["knowledge"]
                if item.get("active", True) and item.get("weak_points")
            ]
            if self.route("learn", at).get("role") != "tutor" and not (
                dependencies and (due or weak)
            ):
                raise ValueError(
                    "manual Tutor work cannot bypass the Advisor/Librarian learning route"
                )
            output_kind = "retrieval_knowledge" if due else "step_knowledge"
            expected_ids = [item["id"] for item in (due[:1] or weak[:1])]
        handoff = self._append_handoff(
            state,
            target,
            task,
            context,
            dependencies,
            None,
            None,
            at,
            output_kind,
            requested_ids or expected_ids,
            request_spec,
        )
        self.store.save(state)
        return handoff

    def workflow_start(
        self,
        intent: str,
        request: str,
        at: datetime,
        resource_ids: list[str] | None = None,
        perspective_spec: dict | None = None,
    ) -> dict:
        if not request.strip():
            raise ValueError("workflow request is required")
        requested_ids = unique(resource_ids or [])
        route = self.route(intent, at)
        roles = route.get("workflow", [])
        if not roles:
            raise ValueError("resume does not create a specialist workflow")
        state = self.store.load()
        bound_collection = state["artifacts"] if intent in {"artifact", "write"} else None
        if bound_collection is not None:
            if len(requested_ids) != 1:
                raise ValueError(f"{intent} workflow requires exactly one resource id")
            requested = self._find(
                bound_collection, requested_ids[0], f"{intent} request resource"
            )
            if requested.get("migration_import_required"):
                raise ValueError("legacy artifact requires explicit learner resubmission")
            if requested.get("status") not in {"draft", "needs_revision"}:
                raise ValueError("artifact workflow requires a reviewable learner version")
        elif requested_ids:
            raise ValueError(f"{intent} workflow does not accept request resource ids")
        request_spec = None
        if intent == "perspective":
            if (
                not isinstance(perspective_spec, dict)
                or set(perspective_spec) != PERSPECTIVE_REQUEST_FIELDS
            ):
                raise ValueError("perspective workflow requires current field and problem")
            request_spec = {key: str(value).strip() for key, value in perspective_spec.items()}
            if not all(request_spec.values()):
                raise ValueError("perspective workflow request fields are required")
            if any(item.get("connection") is None for item in state["perspectives"]):
                raise ValueError(
                    "answer the pending Roommate question before starting another workflow"
                )
        elif perspective_spec:
            raise ValueError(f"{intent} workflow does not accept a perspective request")
        profile = state.get("profile") or {}
        needs_curriculum = not self._active_curriculum_step(profile.get("curriculum"))
        due = self._due_from_state(state, at)
        retrieval_ids = [due[0]["id"]] if due else []
        tutor_target_ids = retrieval_ids or [
            item["id"]
            for item in state["knowledge"]
            if item.get("active", True) and item.get("weak_points")
        ][:1]
        workflow = {
            "id": f"workflow-{uuid.uuid4().hex[:8]}",
            "intent": intent,
            "request": request.strip(),
            "request_resource_ids": requested_ids,
            "request_spec": request_spec,
            "reason": route["reason"],
            "status": "active",
            "profile_binding": self._profile_binding(state),
            "curriculum_binding": (
                {
                    "id": profile["curriculum"]["id"],
                    "version": profile["curriculum"]["version"],
                }
                if intent in {"learn", "material", "plan"}
                and not retrieval_ids
                and profile.get("curriculum")
                else None
            ),
            "superseded_at": None,
            "steps": [
                {
                    "id": f"step-{index}",
                    "order": index,
                    "role": role,
                    "task": {
                        "advisor": "build or update the evidence-backed learning path",
                        "librarian": "curate a grounded source shelf for the current path",
                        "tutor": "teach, diagnose, and record learner performance",
                        "editor": "review the learner-owned deliverable",
                        "roommate": "introduce an outside-field lens",
                    }[role],
                    "output_kind": (
                        "curriculum"
                        if role == "advisor" and (
                            intent == "plan"
                            or (index == 1 and needs_curriculum and not retrieval_ids)
                        )
                        else {
                            "advisor": "advisor_update",
                            "librarian": "ready_shelf",
                            "tutor": "retrieval_knowledge" if retrieval_ids else "step_knowledge",
                            "editor": "reviewed_artifact",
                            "roommate": "answered_perspective",
                        }[role]
                    ),
                    "expected_resource_ids": (
                        tutor_target_ids
                        if role == "tutor"
                        else requested_ids
                        if role == "editor"
                        else []
                    ),
                    "status": "ready" if index == 1 else "waiting",
                    "handoff_id": None,
                    "resource_ids": [],
                }
                for index, role in enumerate(roles, 1)
            ],
            "created_at": iso(at),
            "updated_at": iso(at),
        }
        state["workflows"].append(workflow)
        self.store.save(state)
        return workflow

    def workflow_next(self, workflow_id: str, context: str, at: datetime) -> dict:
        state = self.store.load()
        workflow = self._find(state["workflows"], workflow_id, "workflow")
        if workflow["status"] != "active":
            raise ValueError("workflow is not active")
        if workflow.get("profile_binding") != self._profile_binding(state):
            self._supersede_workflow(state, workflow, at)
            self.store.save(state)
            raise ValueError("workflow profile target was superseded; start a new workflow")
        step = next((item for item in workflow["steps"] if item["status"] == "ready"), None)
        if not step:
            if any(item["status"] == "dispatched" for item in workflow["steps"]):
                raise ValueError("workflow is waiting for a dispatched handoff")
            raise ValueError("workflow has no ready step")
        binding = workflow.get("curriculum_binding")
        if step["order"] > 1 and binding:
            curriculum = (state.get("profile") or {}).get("curriculum")
            if not curriculum or (curriculum["id"], curriculum["version"]) != (
                binding["id"], binding["version"]
            ):
                workflow["status"] = "superseded"
                workflow["superseded_at"] = iso(at)
                workflow["updated_at"] = iso(at)
                self.store.save(state)
                raise ValueError("workflow curriculum was superseded; start a new workflow")
        dependencies = [
            item["handoff_id"]
            for item in workflow["steps"]
            if item["order"] < step["order"] and item["handoff_id"]
        ]
        dependency_results = [
            self._find(state["handoffs"], dependency, "handoff dependency")["result"]
            for dependency in dependencies[-1:]
        ]
        inherited = (
            json.dumps(dependency_results, ensure_ascii=False, sort_keys=True)
            if dependency_results
            else ""
        )
        handoff_context = context.strip() or workflow["request"]
        if inherited:
            handoff_context = f"{handoff_context}\n\nPrevious completed result:\n{inherited}"
        handoff = self._append_handoff(
            state,
            step["role"],
            step["task"],
            handoff_context,
            dependencies[-1:],
            workflow["id"],
            step["id"],
            at,
            step["output_kind"],
            step["expected_resource_ids"],
            workflow.get("request_spec") if step["role"] == "roommate" else None,
        )
        step["status"] = "dispatched"
        step["handoff_id"] = handoff["id"]
        workflow["updated_at"] = iso(at)
        self.store.save(state)
        return {"workflow": workflow, "handoff": handoff}

    def workflow_show(self, workflow_id: str) -> dict:
        return self._find(self.store.load()["workflows"], workflow_id, "workflow")

    def handoff_list(self, status: str | None = None) -> list[dict]:
        handoffs = self.store.load()["handoffs"]
        if status:
            if status not in {"pending", "in_progress", "completed", "cancelled"}:
                raise ValueError("handoff status must be pending, in_progress, completed, or cancelled")
            handoffs = [handoff for handoff in handoffs if handoff["status"] == status]
        return handoffs

    def handoff_inbox(self, actor: str) -> list[dict]:
        if actor not in SPECIALISTS:
            raise ValueError("only specialist agents have an inbox")
        return [
            handoff
            for handoff in self.store.load()["handoffs"]
            if handoff["to"] == actor and handoff["status"] in {"pending", "in_progress"}
        ]

    def handoff_claim(self, handoff_id: str, actor: str, at: datetime) -> dict:
        state = self.store.load()
        handoff = self._find(state["handoffs"], handoff_id, "handoff")
        if handoff["to"] != actor:
            raise ValueError(f"handoff belongs to {handoff['to']}, not {actor}")
        if handoff["status"] != "pending":
            raise ValueError(f"handoff cannot be claimed from status {handoff['status']}")
        if handoff.get("profile_binding") != self._profile_binding(state):
            if handoff.get("workflow_id"):
                workflow = self._find(state["workflows"], handoff["workflow_id"], "workflow")
                self._supersede_workflow(state, workflow, at)
            else:
                handoff["status"] = "cancelled"
                handoff["cancelled_at"] = iso(at)
            self.store.save(state)
            raise ValueError("handoff profile target was superseded; start a new handoff")
        active = next(
            (
                item
                for item in state["handoffs"]
                if item["to"] == actor and item["status"] == "in_progress"
            ),
            None,
        )
        if active:
            raise ValueError(
                f"{actor} already has an in-progress handoff: {active['id']}"
            )
        dependencies = [
            self._find(state["handoffs"], dependency, "handoff dependency")
            for dependency in handoff.get("depends_on", [])
        ]
        incomplete = [item["id"] for item in dependencies if item["status"] != "completed"]
        if incomplete:
            raise ValueError(f"handoff dependencies are incomplete: {', '.join(incomplete)}")
        unverified = [item["id"] for item in dependencies if item.get("legacy_unverified_output")]
        if unverified:
            raise ValueError(f"handoff dependencies are unverified legacy outputs: {', '.join(unverified)}")
        if actor == "tutor" and not handoff.get("workflow_id"):
            target_ids = set(handoff.get("expected_resource_ids", []))
            goals = {
                goal["id"]: goal
                for goal in (state.get("profile") or {}).get("learning_goals", [])
            }
            dependency_goals = [
                goals[resource_id]
                for dependency in dependencies
                if dependency["to"] == "advisor"
                for resource_id in dependency["result"].get("resource_ids", [])
                if resource_id in goals
            ]
            if self.route("learn", at).get("role") != "tutor" and not any(
                goal.get("knowledge_id") in target_ids
                and (
                    handoff.get("expected_output_kind") != "retrieval_knowledge"
                    or goal.get("kind") == "remedial"
                )
                for goal in dependency_goals
            ):
                raise ValueError(
                    "manual Tutor dependency must provide the matching Advisor learning goal"
                )
        handoff["dependency_context"] = [
            {"handoff_id": item["id"], "result": copy.deepcopy(item["result"])}
            for item in dependencies
        ]
        if handoff["dependency_context"]:
            inherited = json.dumps(
                handoff["dependency_context"], ensure_ascii=False, sort_keys=True
            )
            handoff["context"] = (
                f"{handoff['context']}\n\nCompleted dependency results:\n{inherited}"
            ).strip()
        handoff["resource_snapshot"] = self._role_resource_fingerprints(state, actor)
        handoff["level_evidence_count"] = len(
            (state.get("profile") or {}).get("level_evidence", [])
        )
        handoff["status"] = "in_progress"
        handoff["claimed_at"] = iso(at)
        self.store.save(state)
        return handoff

    @staticmethod
    def _role_resources(state: dict, role: str) -> list[dict]:
        if role == "advisor":
            profile = state.get("profile") or {}
            resources = list(profile.get("learning_goals", []))
            if profile.get("curriculum"):
                resources.append(profile["curriculum"])
                resources.extend(profile["curriculum"].get("milestones", []))
            return resources
        collections = {
            "librarian": state["materials"] + state["shelves"],
            "tutor": [item for item in state["knowledge"] if item.get("active", True)],
            "editor": state["artifacts"],
            "roommate": state["perspectives"],
        }
        return collections[role]

    @staticmethod
    def _role_resource_ids(state: dict, role: str) -> set[str]:
        return {item["id"] for item in Engine._role_resources(state, role)}

    @staticmethod
    def _role_resource_fingerprints(state: dict, role: str) -> dict[str, str]:
        resources = Engine._role_resources(state, role)
        return {
            item["id"]: json.dumps(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"updated_at", "produced_by_handoff_id"}
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in resources
        }

    def _workflow_resource_ids(
        self, state: dict, role: str, output_kind: str | None
    ) -> set[str]:
        profile = state.get("profile") or {}
        curriculum = profile.get("curriculum")
        if role == "advisor":
            if output_kind == "curriculum":
                return {curriculum["id"]} if curriculum else set()
            return self._role_resource_ids(state, role)
        if role == "librarian":
            if not curriculum:
                return set()
            active_step = self._active_curriculum_step(curriculum)
            return {
                shelf["id"]
                for shelf in state["shelves"]
                if active_step
                and self._shelf_is_ready(state, shelf, curriculum, active_step["id"])
            }
        if role == "tutor":
            if output_kind == "step_knowledge":
                if not curriculum:
                    return set()
                active_step = self._active_curriculum_step(curriculum)
                selected_material_ids = {
                    material_id
                    for shelf in state["shelves"]
                    if active_step
                    and self._shelf_is_ready(state, shelf, curriculum, active_step["id"])
                    for material_id in shelf["selected_material_ids"]
                }
                selected_sources = {
                    value
                    for material in state["materials"]
                    if material["id"] in selected_material_ids
                    for value in (material["id"], material["source"])
                }
                return {
                    item["id"]
                    for item in state["knowledge"]
                    if item.get("active", True)
                    and set(item.get("sources", [])) & selected_sources
                }
            return {
                item["id"] for item in state["knowledge"] if item.get("active", True)
            }
        if role == "editor":
            return {
                item["id"]
                for item in state["artifacts"]
                if item.get("status") in {"needs_revision", "passed"}
                and any(
                    round_["version"] == item["current_version"]
                    for round_ in item.get("review_rounds", [])
                )
            }
        return {
            item["id"]
            for item in state["perspectives"]
            if item.get("connection") is not None
        }

    def handoff_complete(self, handoff_id: str, actor: str, result: dict, at: datetime) -> dict:
        if not isinstance(result, dict) or not str(result.get("summary", "")).strip():
            raise ValueError("handoff result summary is required")
        next_role = result.get("next_role")
        if next_role is not None and next_role not in SPECIALISTS:
            raise ValueError(f"handoff next role must be one of: {', '.join(sorted(SPECIALISTS))}")
        for field in ("resource_ids", "issues", "observations", "recommendations"):
            if not isinstance(result.get(field, []), list) or not all(
                isinstance(value, str) for value in result.get(field, [])
            ):
                raise ValueError(f"handoff {field} must be a list of strings")
        normalized = {
            "summary": str(result["summary"]).strip(),
            "next_role": next_role,
            "resource_ids": unique(result.get("resource_ids", [])),
            "issues": unique(result.get("issues", [])),
            "observations": unique(result.get("observations", [])),
            "recommendations": unique(result.get("recommendations", [])),
        }
        state = self.store.load()
        handoff = self._find(state["handoffs"], handoff_id, "handoff")
        if handoff["to"] != actor:
            raise ValueError(f"handoff belongs to {handoff['to']}, not {actor}")
        if handoff["status"] != "in_progress":
            raise ValueError(f"handoff cannot be completed from status {handoff['status']}")
        if handoff.get("workflow_id"):
            workflow = self._find(state["workflows"], handoff["workflow_id"], "workflow")
            step = self._find(workflow["steps"], handoff["step_id"], "workflow step")
            if (
                handoff["expected_output_kind"] != step["output_kind"]
                or handoff["expected_resource_ids"] != step["expected_resource_ids"]
            ):
                raise ValueError("workflow handoff output contract does not match its step")
        current_profile_binding = self._profile_binding(state)
        profile_changed = handoff.get("profile_binding") != current_profile_binding
        if profile_changed and handoff.get("expected_output_kind") != "curriculum":
            if handoff.get("workflow_id"):
                workflow = self._find(state["workflows"], handoff["workflow_id"], "workflow")
                self._supersede_workflow(state, workflow, at)
            else:
                handoff["status"] = "cancelled"
                handoff["cancelled_at"] = iso(at)
            self.store.save(state)
            raise ValueError("handoff profile target was superseded; start a new handoff")
        if handoff.get("workflow_id") and handoff.get("expected_output_kind") != "curriculum":
            workflow = self._find(state["workflows"], handoff["workflow_id"], "workflow")
            binding = workflow.get("curriculum_binding")
            curriculum = (state.get("profile") or {}).get("curriculum")
            if binding and (
                not curriculum
                or (curriculum["id"], curriculum["version"])
                != (binding["id"], binding["version"])
            ):
                self._supersede_workflow(state, workflow, at)
                self.store.save(state)
                raise ValueError("workflow curriculum was superseded; start a new workflow")
        if not normalized["resource_ids"]:
            raise ValueError("handoff completion requires a real role resource id")
        unknown_resources = set(normalized["resource_ids"]) - self._role_resource_ids(state, actor)
        if unknown_resources:
            raise ValueError(
                f"unknown {actor} resource ids: {', '.join(sorted(unknown_resources))}"
            )
        role_resources = {item["id"]: item for item in self._role_resources(state, actor)}
        stolen = {
            resource_id
            for resource_id in normalized["resource_ids"]
            if (
                role_resources[resource_id].get("handoff_id")
                if actor == "roommate"
                else role_resources[resource_id].get("produced_by_handoff_id")
            )
            != handoff["id"]
        }
        if stolen:
            raise ValueError(
                "handoff resources were not produced by this claimed handoff: "
                + ", ".join(sorted(stolen))
            )
        fingerprints = self._role_resource_fingerprints(state, actor)
        changed_resources = {
            resource_id
            for resource_id, fingerprint in fingerprints.items()
            if handoff.get("resource_snapshot", {}).get(resource_id) != fingerprint
        }
        unchanged = {
            resource_id
            for resource_id in normalized["resource_ids"]
            if handoff.get("resource_snapshot", {}).get(resource_id)
            == fingerprints.get(resource_id)
        }
        if handoff.get("expected_output_kind"):
            invalid = set(normalized["resource_ids"]) - self._workflow_resource_ids(
                state, actor, handoff.get("expected_output_kind")
            )
            expected_ids = set(handoff.get("expected_resource_ids", []))
            if expected_ids and not set(normalized["resource_ids"]) <= expected_ids:
                invalid.update(set(normalized["resource_ids"]) - expected_ids)
            if actor == "tutor":
                knowledge = {item["id"]: item for item in state["knowledge"]}
                for resource_id in normalized["resource_ids"]:
                    item = knowledge.get(resource_id, {})
                    before = json.loads(
                        handoff.get("resource_snapshot", {}).get(resource_id, "{}")
                    )
                    if handoff["expected_output_kind"] == "retrieval_knowledge":
                        interaction = item.get("last_interaction") or {}
                        teaching = item.get("last_teaching") or {}
                        if (
                            interaction.get("phase") != "retrieval"
                            or interaction == before.get("last_interaction")
                            or interaction.get("sequence")
                            != before.get("interaction_count", 0) + 1
                            or item.get("memory", {}).get("review_count", 0)
                            <= before.get("memory", {}).get("review_count", 0)
                            or not teaching.get("why_chain")
                            or not teaching.get("connection_basis")
                            or teaching == before.get("last_teaching")
                            or teaching.get("sequence", 0) <= interaction.get("sequence", 0)
                        ):
                            invalid.add(resource_id)
                    elif handoff["expected_output_kind"] in {"knowledge", "step_knowledge"}:
                        teaching = item.get("last_teaching")
                        if (
                            not teaching
                            or not teaching.get("why_chain")
                            or not teaching.get("connection_basis")
                            or teaching == before.get("last_teaching")
                            or teaching.get("sequence", 0)
                            <= before.get("interaction_count", 0)
                            or (item.get("last_interaction") or {}).get("phase") != "exposure"
                            or (item.get("last_interaction") or {}).get("sequence", 0)
                            <= teaching.get("sequence", 0)
                            or item.get("memory", {}).get("exposure_count", 0)
                            <= before.get("memory", {}).get("exposure_count", 0)
                        ):
                            invalid.add(resource_id)
                if (
                    normalized["next_role"] != "advisor"
                    or not normalized["observations"]
                    or not normalized["recommendations"]
                ):
                    invalid.update(normalized["resource_ids"])
            if actor == "advisor" and handoff["expected_output_kind"] == "advisor_update":
                tutor_dependencies = [
                    self._find(state["handoffs"], dependency_id, "handoff dependency")
                    for dependency_id in handoff.get("depends_on", [])
                    if self._find(state["handoffs"], dependency_id, "handoff dependency")["to"]
                    == "tutor"
                ]
                tutor_resources = {
                    resource_id
                    for dependency in tutor_dependencies
                    for resource_id in dependency["result"].get("resource_ids", [])
                }
                retrieval_resources: set[str] = set()
                advisor_before_retrieval = False
                if handoff.get("workflow_id"):
                    workflow = self._find(state["workflows"], handoff["workflow_id"], "workflow")
                    advisor_step = self._find(workflow["steps"], handoff["step_id"], "workflow step")
                    for step in workflow["steps"]:
                        if step["role"] == "tutor" and step["output_kind"] == "retrieval_knowledge":
                            retrieval_resources.update(step.get("expected_resource_ids", []))
                            advisor_before_retrieval = advisor_before_retrieval or (
                                advisor_step["order"] < step["order"]
                            )
                    tutor_resources.update(retrieval_resources)
                goals = {
                    goal["id"]: goal
                    for goal in (state.get("profile") or {}).get("learning_goals", [])
                }
                matching_goals = [
                    goals[resource_id]
                    for resource_id in normalized["resource_ids"]
                    if resource_id in goals
                    and goals[resource_id].get("knowledge_id") in tutor_resources
                ]
                if tutor_resources and not matching_goals:
                    invalid.update(normalized["resource_ids"])
                if advisor_before_retrieval and not any(
                    goal.get("kind") == "remedial"
                    and goal.get("knowledge_id") in retrieval_resources
                    for goal in matching_goals
                ):
                    invalid.update(normalized["resource_ids"])
                dependency_observations = {
                    dependency["id"]: {
                        canonical_text(observation)
                        for observation in dependency["result"].get("observations", [])
                    }
                    for dependency in tutor_dependencies
                }
                new_evidence = (state.get("profile") or {}).get("level_evidence", [
                ])[handoff.get("level_evidence_count", 0):]
                if tutor_dependencies and not any(
                    entry.get("produced_by_handoff_id") == handoff["id"]
                    and entry.get("source_handoff_id") in dependency_observations
                    and canonical_text(entry.get("evidence", ""))
                    in dependency_observations[entry["source_handoff_id"]]
                    for entry in new_evidence
                ):
                    invalid.update(normalized["resource_ids"])
            allowed_changed = set(normalized["resource_ids"])
            if actor == "librarian":
                materials = {item["id"]: item for item in state["materials"]}
                changed_materials = {
                    material_id
                    for material_id in materials
                    if handoff.get("resource_snapshot", {}).get(material_id)
                    != fingerprints.get(material_id)
                }
                ready_shelf_ids = self._workflow_resource_ids(
                    state, actor, handoff.get("expected_output_kind")
                )
                declared_candidates = {
                    material_id
                    for resource_id in normalized["resource_ids"]
                    if resource_id in ready_shelf_ids
                    for material_id in self._find(
                        state["shelves"], resource_id, "workflow shelf"
                    ).get("candidate_material_ids", [])
                }
                invalid.update(changed_materials - declared_candidates)
                allowed_changed.update(declared_candidates)
            elif actor == "advisor":
                curriculum = (state.get("profile") or {}).get("curriculum")
                if curriculum:
                    milestone_ids = {item["id"] for item in curriculum.get("milestones", [])}
                    if curriculum["id"] in allowed_changed:
                        allowed_changed.update(milestone_ids)
                    if allowed_changed & milestone_ids:
                        allowed_changed.add(curriculum["id"])
            elif actor == "tutor":
                knowledge = {item["id"]: item for item in state["knowledge"]}
                for resource_id in changed_resources - allowed_changed:
                    before = json.loads(
                        handoff.get("resource_snapshot", {}).get(resource_id, "{}")
                    )
                    current = copy.deepcopy(knowledge.get(resource_id, {}))
                    current.pop("updated_at", None)
                    current.pop("produced_by_handoff_id", None)
                    before_related = set(before.pop("related", []))
                    current_related = set(current.pop("related", []))
                    if (
                        current == before
                        and current_related - before_related <= allowed_changed
                        and before_related <= current_related
                    ):
                        allowed_changed.add(resource_id)
            elif actor == "roommate":
                expected = handoff.get("request_spec")
                if not expected:
                    invalid.update(normalized["resource_ids"])
                else:
                    perspectives = {item["id"]: item for item in state["perspectives"]}
                    for resource_id in normalized["resource_ids"]:
                        perspective = perspectives.get(resource_id, {})
                        actual = {
                            "current_field": perspective.get("current_field", ""),
                            "current_problem": perspective.get("current_problem", ""),
                        }
                        if perspective.get("handoff_id") != handoff["id"] or any(
                            canonical_text(actual[key]) != canonical_text(expected[key])
                            for key in expected
                        ):
                            invalid.add(resource_id)
            invalid.update(changed_resources - allowed_changed)
            if invalid or unchanged:
                rejected = sorted(invalid | unchanged)
                raise ValueError(
                    "handoff resources must be valid outputs produced or updated by this step: "
                    + ", ".join(rejected)
                )
        else:
            invalid = changed_resources - set(normalized["resource_ids"])
            if actor == "tutor":
                knowledge = {item["id"]: item for item in state["knowledge"]}
                for resource_id in normalized["resource_ids"]:
                    item = knowledge.get(resource_id, {})
                    before = json.loads(
                        handoff.get("resource_snapshot", {}).get(resource_id, "{}")
                    )
                    if (
                        item.get("last_teaching") == before.get("last_teaching")
                        and item.get("last_interaction") == before.get("last_interaction")
                    ):
                        invalid.add(resource_id)
            elif actor == "editor":
                artifacts = {item["id"]: item for item in state["artifacts"]}
                invalid.update(
                    resource_id
                    for resource_id in normalized["resource_ids"]
                    if not any(
                        round_["version"] == artifacts.get(resource_id, {}).get("current_version")
                        for round_ in artifacts.get(resource_id, {}).get("review_rounds", [])
                    )
                )
            elif actor == "roommate":
                perspectives = {item["id"]: item for item in state["perspectives"]}
                invalid.update(
                    resource_id
                    for resource_id in normalized["resource_ids"]
                    if perspectives.get(resource_id, {}).get("connection") is None
                )
            if invalid or unchanged:
                rejected = sorted(invalid | unchanged)
                raise ValueError(
                    "handoff resources must be valid outputs produced or updated by this step: "
                    + ", ".join(rejected)
                )
        reused = {
            resource_id
            for previous in state["handoffs"]
            if previous["status"] == "completed" and previous["to"] == actor
            for resource_id in normalized["resource_ids"]
            if previous.get("output_fingerprints", {}).get(resource_id)
            == fingerprints.get(resource_id)
        }
        if reused:
            raise ValueError(
                "handoff resources already satisfied another request at this version: "
                + ", ".join(sorted(reused))
            )
        handoff["status"] = "completed"
        handoff["completed_at"] = iso(at)
        handoff["result"] = normalized
        handoff["output_fingerprints"] = {
            resource_id: fingerprints[resource_id]
            for resource_id in normalized["resource_ids"]
        }
        if handoff.get("workflow_id"):
            workflow = self._find(state["workflows"], handoff["workflow_id"], "workflow")
            step = self._find(workflow["steps"], handoff["step_id"], "workflow step")
            if step["output_kind"] == "curriculum":
                curriculum = (state.get("profile") or {}).get("curriculum")
                workflow["curriculum_binding"] = {
                    "id": curriculum["id"], "version": curriculum["version"]
                }
                workflow["profile_binding"] = current_profile_binding
                if profile_changed:
                    for other in state["workflows"]:
                        if (
                            other["id"] != workflow["id"]
                            and other["status"] == "active"
                            and other.get("profile_binding") != current_profile_binding
                        ):
                            self._supersede_workflow(state, other, at)
                    for other_handoff in state["handoffs"]:
                        if (
                            other_handoff.get("workflow_id") is None
                            and other_handoff["status"] in {"pending", "in_progress"}
                            and other_handoff.get("profile_binding")
                            != current_profile_binding
                        ):
                            other_handoff["status"] = "cancelled"
                            other_handoff["cancelled_at"] = iso(at)
            step["status"] = "completed"
            step["resource_ids"] = normalized["resource_ids"]
            next_step = next(
                (item for item in workflow["steps"] if item["status"] == "waiting"), None
            )
            if next_step:
                next_step["status"] = "ready"
            else:
                workflow["status"] = "completed"
            workflow["updated_at"] = iso(at)
        self.store.save(state)
        return handoff


def output(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def add_list_argument(parser: argparse.ArgumentParser, name: str, help_text: str) -> None:
    parser.add_argument(name, action="append", default=[], help=help_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("BECOME_HOME", ".become")),
        help="local data directory (default: .become or BECOME_HOME)",
    )
    parser.add_argument("--now", help="ISO timestamp override for deterministic runs")
    parser.add_argument("--actor", required=True, choices=sorted(ACTORS))
    parser.add_argument("--scope-handoff", help="claimed handoff that authorizes a specialist write")
    roles = parser.add_subparsers(dest="role", required=True)

    orchestrator = roles.add_parser("orchestrator")
    orchestrator_commands = orchestrator.add_subparsers(dest="command", required=True)
    dispatch = orchestrator_commands.add_parser("dispatch")
    dispatch.add_argument("--to", required=True, choices=sorted(SPECIALISTS))
    dispatch.add_argument("--task", required=True)
    dispatch.add_argument("--context", default="")
    add_list_argument(dispatch, "--depends-on", "repeat for each prerequisite handoff id")
    add_list_argument(dispatch, "--resource-id", "bind the requested Editor artifact")
    dispatch.add_argument("--current-field", default="")
    dispatch.add_argument("--problem", default="")
    dispatch.add_argument("--output-kind", choices=("curriculum", "advisor_update"))
    route = orchestrator_commands.add_parser("route")
    route.add_argument(
        "--intent",
        choices=("plan", "material", "learn", "artifact", "write", "perspective", "resume"),
        default="learn",
    )
    workflow_start = orchestrator_commands.add_parser("workflow-start")
    workflow_start.add_argument(
        "--intent",
        required=True,
        choices=("plan", "material", "learn", "artifact", "write", "perspective"),
    )
    workflow_start.add_argument("--request", required=True)
    add_list_argument(workflow_start, "--resource-id", "bind the requested artifact")
    workflow_start.add_argument("--current-field", default="")
    workflow_start.add_argument("--problem", default="")
    workflow_next = orchestrator_commands.add_parser("workflow-next")
    workflow_next.add_argument("workflow_id")
    workflow_next.add_argument("--context", default="")
    workflow_show = orchestrator_commands.add_parser("workflow-show")
    workflow_show.add_argument("workflow_id")
    session_start = orchestrator_commands.add_parser("session-start")
    session_start.add_argument("--context", default="")
    session_start.add_argument("--workflow-id")
    session_note = orchestrator_commands.add_parser("session-note")
    session_note.add_argument("--note", required=True)
    session_note.add_argument("--next", default="")
    session_end = orchestrator_commands.add_parser("session-end")
    session_end.add_argument("--summary", required=True)
    session_end.add_argument("--next", default="")
    orchestrator_commands.add_parser("session-resume")
    handoff_list = orchestrator_commands.add_parser("list")
    handoff_list.add_argument(
        "--status", choices=("pending", "in_progress", "completed", "cancelled")
    )
    orchestrator_commands.add_parser("inbox")
    claim = orchestrator_commands.add_parser("claim")
    claim.add_argument("handoff_id")
    complete = orchestrator_commands.add_parser("complete")
    complete.add_argument("handoff_id")
    complete.add_argument("--summary", required=True)
    complete.add_argument("--next-role", choices=sorted(SPECIALISTS))
    add_list_argument(complete, "--resource-id", "repeat for each created or updated resource id")
    add_list_argument(complete, "--issue", "repeat for each remaining issue")
    add_list_argument(complete, "--observation", "repeat for each learner-level observation")
    add_list_argument(complete, "--recommendation", "repeat for each recommended learning goal")

    advisor = roles.add_parser("advisor")
    advisor_commands = advisor.add_subparsers(dest="command", required=True)
    advisor_init = advisor_commands.add_parser("init")
    advisor_init.add_argument("--goal", required=True)
    advisor_init.add_argument("--level", default="")
    add_list_argument(advisor_init, "--focus", "repeat for each focus area")
    advisor_init.add_argument("--target-retention", type=float, default=0.9)
    observe = advisor_commands.add_parser("observe")
    observe.add_argument("--level", required=True)
    observe.add_argument("--evidence", required=True)
    observe.add_argument("--source-handoff")
    interview = advisor_commands.add_parser("interview")
    interview.add_argument("--decision", required=True, choices=CURRICULUM_DECISIONS)
    interview.add_argument("--question", required=True)
    interview.add_argument("--answer")
    curriculum = advisor_commands.add_parser("curriculum")
    curriculum.add_argument("--spec", required=True, help="JSON curriculum specification")
    milestone = advisor_commands.add_parser("milestone")
    milestone.add_argument("milestone_id")
    milestone.add_argument("--artifact-id", required=True)
    goal = advisor_commands.add_parser("goal")
    goal.add_argument("--title", required=True)
    goal.add_argument("--outcome", required=True)
    goal.add_argument("--reason", required=True)
    goal.add_argument("--kind", choices=sorted(GOAL_KINDS), default="practical")
    goal.add_argument("--priority", type=int, default=3)
    goal.add_argument("--knowledge-id")
    advisor_commands.add_parser("goals")
    goal_status = advisor_commands.add_parser("goal-status")
    goal_status.add_argument("goal_id")
    goal_status.add_argument("status", choices=sorted(GOAL_STATUSES))
    advisor_commands.add_parser("status")
    advisor_commands.add_parser(
        "recommend",
        help="read-only recommendation; never writes state",
        description="Read-only Advisor recommendation. This command never writes state.",
    )
    advisor_commands.add_parser(
        "next",
        help="write or update the next/remedial learning goal",
        description="Choose the next action and write or update a remedial learning goal when needed.",
    )

    librarian = roles.add_parser("librarian")
    librarian_commands = librarian.add_subparsers(dest="command", required=True)
    material_add = librarian_commands.add_parser("add")
    material_add.add_argument("--title", required=True)
    material_add.add_argument("--source", required=True)
    material_add.add_argument("--note", default="")
    material_add.add_argument("--evidence", default="")
    curate = librarian_commands.add_parser("curate")
    curate.add_argument("material_id")
    curate.add_argument("--assessment", required=True, help="JSON four-axis assessment")
    shelf = librarian_commands.add_parser("shelf")
    shelf.add_argument("--curriculum-id", required=True)
    shelf.add_argument("--step-id", required=True)
    add_list_argument(shelf, "--candidate-id", "repeat for every triaged candidate")
    librarian_commands.add_parser("list")

    tutor = roles.add_parser("tutor")
    tutor_commands = tutor.add_subparsers(dest="command", required=True)
    knowledge_add = tutor_commands.add_parser("add")
    knowledge_add.add_argument("--title", required=True)
    knowledge_add.add_argument("--explanation", required=True)
    knowledge_add.add_argument("--type", choices=sorted(KNOWLEDGE_TYPES), default="concept")
    add_list_argument(knowledge_add, "--weak", "repeat for each weak point")
    add_list_argument(knowledge_add, "--related", "repeat for each related knowledge id")
    add_list_argument(knowledge_add, "--source", "repeat for each selected shelf material id or source")
    tutor_commands.add_parser("list")
    tutor_commands.add_parser("context")
    tutor_commands.add_parser("due")
    relate = tutor_commands.add_parser("relate")
    relate.add_argument("knowledge_id")
    relate.add_argument("related_id")
    teach = tutor_commands.add_parser("teach")
    teach.add_argument("knowledge_id")
    teach.add_argument("--explanation", required=True)
    teach.add_argument("--connection", default="")
    review = tutor_commands.add_parser("review")
    review.add_argument("knowledge_id")
    review.add_argument("rating", choices=sorted(RATINGS))
    review.add_argument("--confidence", choices=sorted(CONFIDENCE), required=True)
    add_list_argument(review, "--add-weak", "repeat for each newly weak point")
    add_list_argument(review, "--clear-weak", "repeat for each cleared weak point")
    review.add_argument("--prompt", required=True)
    review.add_argument("--answer", required=True)
    review.add_argument("--rationale", required=True)

    editor = roles.add_parser("editor")
    editor_commands = editor.add_subparsers(dest="command", required=True)
    artifact_add = editor_commands.add_parser("add")
    artifact_add.add_argument("--title", required=True)
    artifact_add.add_argument("--content", required=True)
    artifact_add.add_argument("--purpose", required=True)
    artifact_add.add_argument("--audience", required=True)
    artifact_add.add_argument("--milestone-id")
    artifact_show = editor_commands.add_parser("show")
    artifact_show.add_argument("artifact_id")
    review_artifact = editor_commands.add_parser("review")
    review_artifact.add_argument("artifact_id")
    review_artifact.add_argument("--criteria", required=True, help="JSON seven-axis review")
    review_artifact.add_argument("--milestone-criteria", help="JSON milestone proof review")
    review_artifact.add_argument("--verdict", required=True, choices=("revise", "pass"))
    review_artifact.add_argument("--next", default="")
    revise = editor_commands.add_parser("revise")
    revise.add_argument("artifact_id")
    revise.add_argument("--content", required=True)

    roommate = roles.add_parser("roommate")
    roommate_commands = roommate.add_subparsers(dest="command", required=True)
    ask = roommate_commands.add_parser("ask")
    ask.add_argument("--current-field", required=True)
    ask.add_argument("--problem", required=True)
    ask.add_argument("--outside-field", required=True)
    ask.add_argument("--lens", required=True)
    ask.add_argument("--question", required=True)
    answer = roommate_commands.add_parser("answer")
    answer.add_argument("perspective_id")
    answer.add_argument("--response", required=True)
    answer.add_argument("--status", required=True, choices=sorted(PERSPECTIVE_STATUSES))
    answer.add_argument("--insight", default="")
    answer.add_argument("--mapping", default="")
    answer.add_argument("--limits", default="")
    add_list_argument(answer, "--recommendation", "repeat for each follow-up recommendation")
    roommate_commands.add_parser("list")
    return parser


def authorize(actor: str, role: str, command: str) -> None:
    if role in SPECIALISTS:
        if role == "editor" and command in {"add", "revise"}:
            if actor != "learner":
                raise ValueError(f"actor {actor} is not authorized for learner submission")
            return
        if actor != role:
            raise ValueError(f"actor {actor} is not authorized for {role} commands")
        return
    if command in {
        "dispatch", "list", "route", "workflow-start", "workflow-next", "workflow-show",
        "session-start", "session-note", "session-end", "session-resume",
    }:
        if actor != "orchestrator":
            raise ValueError(f"actor {actor} is not authorized to {command} handoffs")
        return
    if actor not in SPECIALISTS:
        raise ValueError(f"actor {actor} has no specialist inbox")


def authorize_specialist_write(engine: Engine, args: argparse.Namespace) -> None:
    reads = {
        "advisor": {"goals", "status", "recommend"},
        "librarian": {"list"},
        "tutor": {"list", "context", "due"},
        "editor": {"show", "add", "revise"},
        "roommate": {"list"},
    }
    if args.role not in SPECIALISTS or args.command in reads[args.role]:
        return
    state = engine.store.load()
    candidates = [
        handoff
        for handoff in state["handoffs"]
        if handoff["to"] == args.role and handoff["status"] == "in_progress"
    ]
    if args.scope_handoff:
        handoff = engine._find(candidates, args.scope_handoff, "claimed handoff scope")
    elif len(candidates) == 1:
        handoff = candidates[0]
    else:
        raise ValueError(
            "specialist writes require exactly one claimed handoff or --scope-handoff"
        )
    args.authorized_handoff_id = handoff["id"]
    engine.write_scope = handoff["id"]
    kind = handoff.get("expected_output_kind")
    if args.role == "advisor":
        curriculum_commands = {"init", "interview", "curriculum"}
        if (args.command in curriculum_commands) != (kind == "curriculum"):
            raise ValueError("Advisor command does not match the claimed handoff output kind")
    expected_ids = set(handoff.get("expected_resource_ids", []))
    if args.role == "editor" and args.artifact_id not in expected_ids:
        raise ValueError("Editor command target does not match the claimed handoff artifact")
    if args.role == "tutor" and expected_ids:
        if args.command == "add":
            raise ValueError("Tutor cannot add unrelated knowledge in a target-bound handoff")
        if args.command in {"teach", "review", "relate"} and args.knowledge_id not in expected_ids:
            raise ValueError("Tutor command target does not match the claimed handoff knowledge")
        if kind == "retrieval_knowledge" and args.command in {"teach", "review", "relate"}:
            item = engine._find(state["knowledge"], args.knowledge_id, "knowledge")
            before = json.loads(
                handoff.get("resource_snapshot", {}).get(args.knowledge_id, "{}")
            )
            interaction = item.get("last_interaction") or {}
            retrieved = (
                interaction.get("phase") == "retrieval"
                and item.get("memory", {}).get("review_count", 0)
                > before.get("memory", {}).get("review_count", 0)
            )
            if args.command == "review" and retrieved:
                raise ValueError("retrieval handoff already recorded its independent answer")
            if args.command != "review" and not retrieved:
                raise ValueError("retrieval review must happen before teaching or relating")
    if args.role == "roommate":
        expected = handoff.get("request_spec")
        if not expected:
            raise ValueError("Roommate handoff has no structured request; dispatch a new handoff")
        if args.command == "ask":
            actual = {
                "current_field": args.current_field,
                "current_problem": args.problem,
            }
            if any(item.get("handoff_id") == handoff["id"] for item in state["perspectives"]):
                raise ValueError("Roommate handoff already produced its connection question")
        else:
            perspective = engine._find(state["perspectives"], args.perspective_id, "perspective")
            if perspective.get("handoff_id") != handoff["id"]:
                raise ValueError("Roommate answer does not belong to the claimed handoff")
            actual = {
                "current_field": perspective.get("current_field", ""),
                "current_problem": perspective.get("current_problem", ""),
            }
        if any(canonical_text(actual[key]) != canonical_text(expected[key]) for key in expected):
            raise ValueError("Roommate command does not match the claimed handoff request")


def run(args: argparse.Namespace) -> object:
    engine = Engine(Store(args.home))
    at = parse_time(args.now)
    authorize(args.actor, args.role, args.command)
    authorize_specialist_write(engine, args)
    if args.role == "orchestrator":
        if args.command == "dispatch":
            perspective_spec = (
                {
                    "current_field": args.current_field,
                    "current_problem": args.problem,
                }
                if args.to == "roommate"
                else None
            )
            return engine.handoff_dispatch(
                args.to, args.task, args.context, at, args.depends_on,
                args.resource_id, perspective_spec, args.output_kind,
            )
        if args.command == "route":
            return engine.route(args.intent, at)
        if args.command == "workflow-start":
            perspective_spec = (
                {
                    "current_field": args.current_field,
                    "current_problem": args.problem,
                }
                if args.intent == "perspective"
                else None
            )
            return engine.workflow_start(
                args.intent, args.request, at, args.resource_id, perspective_spec
            )
        if args.command == "workflow-next":
            return engine.workflow_next(args.workflow_id, args.context, at)
        if args.command == "workflow-show":
            return engine.workflow_show(args.workflow_id)
        if args.command == "session-start":
            return engine.session_start(args.context, at, args.workflow_id)
        if args.command == "session-note":
            return engine.session_note(args.note, args.next, at)
        if args.command == "session-end":
            return engine.session_end(args.summary, args.next, at)
        if args.command == "session-resume":
            return engine.resume(at)
        if args.command == "list":
            return engine.handoff_list(args.status)
        if args.command == "inbox":
            return engine.handoff_inbox(args.actor)
        if args.command == "claim":
            return engine.handoff_claim(args.handoff_id, args.actor, at)
        return engine.handoff_complete(
            args.handoff_id,
            args.actor,
            {
                "summary": args.summary,
                "next_role": args.next_role,
                "resource_ids": args.resource_id,
                "issues": args.issue,
                "observations": args.observation,
                "recommendations": args.recommendation,
            },
            at,
        )
    if args.role == "advisor":
        if args.command == "init":
            return engine.advisor_init(args.goal, args.level, args.focus, args.target_retention, at)
        if args.command == "observe":
            return engine.advisor_observe(
                args.level, args.evidence, at, args.source_handoff
            )
        if args.command == "interview":
            return engine.advisor_interview(args.decision, args.question, args.answer, at)
        if args.command == "curriculum":
            return engine.advisor_curriculum(json.loads(args.spec), at)
        if args.command == "milestone":
            return engine.advisor_milestone(args.milestone_id, args.artifact_id, at)
        if args.command == "goal":
            return engine.learning_goal_add(
                args.title,
                args.outcome,
                args.reason,
                args.kind,
                args.priority,
                args.knowledge_id,
                at,
            )
        if args.command == "goals":
            return engine.learning_goals()
        if args.command == "goal-status":
            return engine.learning_goal_status(args.goal_id, args.status, at)
        if args.command == "status":
            return engine.status(at)
        if args.command == "recommend":
            return engine.recommend(at)
        return engine.advise(at)
    if args.role == "librarian":
        if args.command == "add":
            return engine.material_add(args.title, args.source, args.note, args.evidence, at)
        if args.command == "curate":
            return engine.material_curate(args.material_id, json.loads(args.assessment), at)
        if args.command == "shelf":
            return engine.material_shelf(
                args.curriculum_id, args.step_id, args.candidate_id, at
            )
        return engine.materials()
    if args.role == "tutor":
        if args.command == "add":
            return engine.knowledge_add(
                args.title,
                args.explanation,
                args.type,
                args.weak,
                args.related,
                args.source,
                at,
            )
        if args.command == "list":
            return engine.knowledge()
        if args.command == "context":
            return engine.tutor_context()
        if args.command == "due":
            return engine.due(at)
        if args.command == "relate":
            return engine.knowledge_relate(args.knowledge_id, args.related_id, at)
        if args.command == "teach":
            return engine.teach(args.knowledge_id, args.explanation, args.connection, at)
        return engine.review(
            args.knowledge_id,
            args.rating,
            args.add_weak,
            args.clear_weak,
            args.confidence,
            args.prompt,
            args.answer,
            args.rationale,
            at,
        )
    if args.role == "editor":
        if args.command == "add":
            return engine.artifact_add(
                args.title,
                args.content,
                args.purpose,
                args.audience,
                args.milestone_id,
                at,
                args.actor,
            )
        if args.command == "show":
            return engine.artifact_show(args.artifact_id)
        if args.command == "review":
            return engine.artifact_review(
                args.artifact_id,
                json.loads(args.criteria),
                args.verdict,
                args.next,
                at,
                json.loads(args.milestone_criteria) if args.milestone_criteria else None,
            )
        return engine.artifact_revise(args.artifact_id, args.content, at, args.actor)
    if args.command == "ask":
        return engine.perspective_start(
            args.current_field,
            args.problem,
            args.outside_field,
            args.lens,
            args.question,
            at,
            args.authorized_handoff_id,
        )
    if args.command == "answer":
        return engine.perspective_answer(
            args.perspective_id,
            args.response,
            args.status,
            args.insight,
            args.mapping,
            args.limits,
            args.recommendation,
            at,
        )
    return engine.perspectives()


def main(argv: list[str] | None = None) -> int:
    try:
        output(run(build_parser().parse_args(argv)))
        return 0
    except (ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
