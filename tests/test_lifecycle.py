"""The analysis status lifecycle and the rule that only it changes a status."""

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
import unittest

from backend.app import lifecycle
from backend.app.lifecycle import ALLOWED_TRANSITIONS, InvalidTransition
from backend.app.models import JobSnapshot, JobStatus, ProgressEvent, TERMINAL_STATUSES
from test_mvp import TARGET


STATUSES: tuple[JobStatus, ...] = get_args(JobStatus)
FINAL: tuple[JobStatus, ...] = ("completed", "partial", "failed", "cancelled")
BACKEND = Path(__file__).resolve().parents[1] / "backend"


def snapshot_in(status: JobStatus) -> JobSnapshot:
    return JobSnapshot(
        job_id="a" * 32, issue_url=TARGET.url, status=status, created_at=datetime.now(UTC),
        events=[ProgressEvent(id=1, kind="status", message="Analysis queued.")],
    )


def change(snapshot: JobSnapshot, status: JobStatus) -> JobSnapshot:
    return lifecycle.start(snapshot) if status == "running" else lifecycle.finish(snapshot, status)


class TransitionRuleTests(unittest.TestCase):
    def test_the_table_covers_every_status_and_its_dead_ends_are_the_final_statuses(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(STATUSES))
        self.assertEqual(set(FINAL), TERMINAL_STATUSES)
        self.assertEqual({status for status, allowed in ALLOWED_TRANSITIONS.items() if not allowed}, TERMINAL_STATUSES)

    def test_a_final_status_never_changes(self):
        for current in FINAL:
            for target in STATUSES:
                if target == "queued":
                    continue  # No function returns an analysis to queued.
                with self.subTest(current=current, target=target), self.assertRaises(InvalidTransition):
                    change(snapshot_in(current), target)

    def test_a_queued_analysis_only_starts_or_is_cancelled(self):
        self.assertEqual(lifecycle.start(snapshot_in("queued")).status, "running")
        self.assertEqual(lifecycle.finish(snapshot_in("queued"), "cancelled").status, "cancelled")
        never: tuple[JobStatus, ...] = ("completed", "partial", "failed")
        for target in never:
            with self.subTest(target=target), self.assertRaises(InvalidTransition):
                lifecycle.finish(snapshot_in("queued"), target)

    def test_a_run_starts_once_and_ends_in_any_final_status(self):
        with self.assertRaises(InvalidTransition):
            lifecycle.start(snapshot_in("running"))
        for target in FINAL:
            with self.subTest(target=target):
                self.assertEqual(lifecycle.finish(snapshot_in("running"), target).status, target)

    def test_finish_only_accepts_a_final_status(self):
        not_final: tuple[JobStatus, ...] = ("queued", "running")
        for target in not_final:
            with self.subTest(target=target), self.assertRaises(InvalidTransition):
                lifecycle.finish(snapshot_in("queued"), target)


class TransitionRecordTests(unittest.TestCase):
    def test_start_returns_a_running_copy_without_events(self):
        queued = snapshot_in("queued")
        running = lifecycle.start(queued)
        self.assertEqual((running.status, running.events, running.error), ("running", queued.events, None))
        self.assertEqual(queued.status, "queued")
        self.assertIsNot(running.events, queued.events)

    def test_finish_records_the_warning_then_the_status_event_and_leaves_the_original(self):
        running = snapshot_in("running")
        ended = lifecycle.finish(
            running, "cancelled", error="Stopped.", warning="Stopped.", message="Analysis cancelled after an interruption.",
        )
        self.assertEqual((ended.status, ended.error), ("cancelled", "Stopped."))
        self.assertEqual(
            [(event.id, event.kind, event.message) for event in ended.events[1:]],
            [(2, "warning", "Stopped."), (3, "status", "Analysis cancelled after an interruption.")],
        )
        self.assertEqual((running.status, len(running.events)), ("running", 1))

    def test_the_default_status_event_names_the_final_status(self):
        ended = lifecycle.finish(snapshot_in("running"), "partial")
        self.assertEqual((ended.events[-1].id, ended.events[-1].message, ended.error), (2, "Analysis partial.", None))

    def test_a_new_analysis_is_queued_with_one_event(self):
        first, second = lifecycle.queued_snapshot(TARGET), lifecycle.queued_snapshot(TARGET)
        self.assertEqual((first.status, first.issue_url), ("queued", TARGET.url))
        self.assertEqual([(event.id, event.message) for event in first.events], [(1, "Analysis queued.")])
        self.assertNotEqual(first.job_id, second.job_id)

    def test_event_ids_continue_from_the_last_event(self):
        snapshot = lifecycle.queued_snapshot(TARGET)
        lifecycle.record(snapshot, "tool", "Calling get_issue.")
        self.assertEqual([event.id for event in snapshot.events], [1, 2])


class StatusOwnershipTests(unittest.TestCase):
    def test_only_the_lifecycle_module_assigns_a_status(self):
        # A rule checked by code: every status change goes through lifecycle.start or finish.
        # It finds assignments such as `snapshot.status = ...`, not setattr or model_copy(update=...).
        offenders = []
        for path in sorted(BACKEND.rglob("*.py")):
            if path.name == "lifecycle.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                else:
                    continue
                offenders.extend(
                    f"{path.relative_to(BACKEND.parent)}:{item.lineno}"
                    for target in targets for item in ast.walk(target)
                    if isinstance(item, ast.Attribute) and item.attr == "status"
                )
        self.assertEqual(offenders, [], "Change a status with lifecycle.start or lifecycle.finish.")
