#!/usr/bin/env python3
"""Focused tests for the .pyxis roadmap scaffold and inspector."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyxis_roadmap


class PyxisRoadmapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = "019ff023-test-owner"
        self.environment = mock.patch.dict(
            os.environ, {pyxis_roadmap.SESSION_ENV: self.session_id}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            result = pyxis_roadmap.init_command(self.repo, "demo-roadmap", "Demo Project")
        self.assertEqual(result, 0)
        self.roadmap = self.repo / ".pyxis" / "roadmaps" / "demo-roadmap"
        for markdown in self.roadmap.rglob("*.md"):
            text = markdown.read_text(encoding="utf-8")
            text = pyxis_roadmap.PLACEHOLDER.sub("value", text)
            markdown.write_text(text, encoding="utf-8", newline="\n")

    def control(self) -> str:
        return (self.roadmap / "CONTROL.md").read_text(encoding="utf-8")

    def write_control(self, text: str) -> None:
        (self.roadmap / "CONTROL.md").write_text(text, encoding="utf-8", newline="\n")

    def test_init_creates_tracked_layout_and_runtime(self) -> None:
        expected = {
            "CONTROL.md",
            "tasks/T00.md",
            "tasks/T01.md",
            "tasks/T99.md",
            "policies/repositories.md",
            "policies/constraints.md",
            "policies/acceptance.md",
            "history/completion-log.md",
        }
        actual = {path.relative_to(self.roadmap).as_posix() for path in self.roadmap.rglob("*.md")}
        self.assertEqual(actual, expected)
        owner = (self.roadmap / pyxis_roadmap.OWNER_FILE).read_text(encoding="utf-8")
        self.assertIn(pyxis_roadmap.OWNER_SCHEMA, owner)
        self.assertNotIn(self.session_id, owner)
        self.assertTrue((self.repo / ".pyxis" / "scripts" / "roadmap.py").is_file())

    def test_initialized_and_populated_roadmap_is_valid(self) -> None:
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.current.task_id, "T00")

    def test_resume_packet_reads_control_current_task_and_policies_only(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return_code = pyxis_roadmap.inspect_command(self.roadmap, max_lines=800, recent=2)
        packet = stdout.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("VALIDATION: PASS", packet)
        self.assertIn("--- BEGIN CONTROL.md", packet)
        self.assertIn("--- BEGIN tasks/T00.md", packet)
        self.assertIn("--- BEGIN policies/repositories.md", packet)
        self.assertNotIn("--- BEGIN tasks/T01.md", packet)
        self.assertNotIn("--- BEGIN tasks/T99.md", packet)
        self.assertLess(len(packet.splitlines()), 400)

    def test_different_session_is_rejected_before_control_is_read(self) -> None:
        secret = "DO-NOT-LEAK-ROADMAP-CONTENT"
        self.write_control(self.control() + f"\n<!-- {secret} -->\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ, {pyxis_roadmap.SESSION_ENV: "different-codex-task"}
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = pyxis_roadmap.validate_roadmap(self.roadmap)
            return_code = pyxis_roadmap.inspect_command(
                self.roadmap, max_lines=800, recent=2
            )
        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(return_code, 1)
        self.assertEqual(result.control_text, "")
        self.assertTrue(any("different Codex task" in error for error in result.errors))
        self.assertNotIn(secret, output)

    def test_missing_session_identity_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.control_text, "")
        self.assertTrue(
            any(pyxis_roadmap.SESSION_ENV in error for error in result.errors)
        )

    def test_multiple_ready_tasks_fail(self) -> None:
        self.write_control(self.control().replace("| T01 | P0 | `value` | `value` | PENDING |", "| T01 | P0 | `value` | `value` | READY |"))
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertTrue(any("multiple READY tasks" in error for error in result.errors))

    def test_missing_and_orphan_task_files_fail(self) -> None:
        (self.roadmap / "tasks" / "T01.md").unlink()
        (self.roadmap / "tasks" / "T55.md").write_text("# T55 — Orphan\n", encoding="utf-8")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertTrue(any("missing file: tasks/T01.md" in error for error in result.errors))
        self.assertTrue(any("orphan task file" in error for error in result.errors))

    def test_unknown_dependency_fails(self) -> None:
        self.write_control(self.control().replace("| PENDING | T00 | `tasks/T01.md` |", "| PENDING | T404 | `tasks/T01.md` |"))
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertTrue(any("unknown dependency T404" in error for error in result.errors))

    def test_done_task_requires_receipt_and_non_pending_evidence(self) -> None:
        control = self.control()
        control = control.replace("| T00 | P0 | `value` | Persist `.pyxis` roadmap | READY |", "| T00 | P0 | `value` | Persist `.pyxis` roadmap | DONE |")
        control = control.replace("| T01 | P0 | `value` | `value` | PENDING |", "| T01 | P0 | `value` | `value` | READY |")
        self.write_control(control)
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertTrue(any("no completion-log receipt" in error for error in result.errors))
        self.assertTrue(any("pending evidence" in error for error in result.errors))

        task = self.roadmap / "tasks" / "T00.md"
        task.write_text(task.read_text(encoding="utf-8").replace("- Pending.", "- Inspector and staged gates passed."), encoding="utf-8", newline="\n")
        history = self.roadmap / "history" / "completion-log.md"
        with history.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n## T00 — 2026-08-12\n\n- Repository: value\n- Commit: this task's commit\n- Verification: passed\n- Evidence: exact receipt\n")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.current.task_id, "T01")

    def test_required_read_cannot_escape_roadmap(self) -> None:
        task = self.roadmap / "tasks" / "T01.md"
        task.write_text(task.read_text(encoding="utf-8").replace("Required reads: none", "Required reads: `../../outside.md`"), encoding="utf-8", newline="\n")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertTrue(any("path escapes roadmap directory" in error for error in result.errors))

    def test_multiline_required_reads_keeps_every_bullet(self) -> None:
        task = self.roadmap / "tasks" / "T00.md"
        task.write_text(task.read_text(encoding="utf-8").replace(
            "Required reads: none",
            "Required reads:\n\n- `tasks/T01.md`\n- `tasks/T99.md`",
        ), encoding="utf-8")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.documents["T00"].required_reads, ("tasks/T01.md", "tasks/T99.md"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(pyxis_roadmap.inspect_command(self.roadmap, 800, 2), 0)
        self.assertIn("--- BEGIN tasks/T99.md", stdout.getvalue())

    def test_blank_repository_field_does_not_consume_next_field(self) -> None:
        self.assertEqual(pyxis_roadmap.field_value("Repository:\nRequired reads: none\n", "Repository"), "")

    def test_done_evidence_can_describe_pending_business_states(self) -> None:
        self.complete_all_tasks()
        task = self.roadmap / "tasks" / "T01.md"
        with task.open("a", encoding="utf-8") as stream:
            stream.write("\n- Verified pending-reservation queries, pending_count=0, and no pending work.\n")
        self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).errors, [])

    def test_done_evidence_rejects_remaining_pending_sentinel(self) -> None:
        self.complete_all_tasks()
        task = self.roadmap / "tasks" / "T01.md"
        with task.open("a", encoding="utf-8") as stream:
            stream.write("\n- Pending.\n")
        self.assertTrue(any("pending evidence" in error for error in pyxis_roadmap.validate_roadmap(self.roadmap).errors))

    def test_done_task_rejects_unchecked_acceptance(self) -> None:
        self.complete_all_tasks()
        task = self.roadmap / "tasks" / "T01.md"
        task.write_text(task.read_text(encoding="utf-8").replace("## Acceptance\n", "## Acceptance\n\n* [ ] Runtime check\n"), encoding="utf-8")
        self.assertTrue(any("unchecked acceptance" in error for error in pyxis_roadmap.validate_roadmap(self.roadmap).errors))

    def test_packet_byte_budget_rejects_one_huge_line_without_partial_packet(self) -> None:
        task = self.roadmap / "tasks" / "T00.md"
        with task.open("a", encoding="utf-8") as stream:
            stream.write("\n- " + "证据" * 10000 + "\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = pyxis_roadmap.inspect_command(self.roadmap, 800, 2)
        self.assertEqual(result, 1)
        self.assertNotIn("VALIDATION: PASS", stdout.getvalue())
        self.assertIn("--max-bytes", stderr.getvalue())

    def test_packet_budget_fails_closed(self) -> None:
        constraints = self.roadmap / "policies" / "constraints.md"
        constraints.write_text("# Global constraints\n\n" + "\n".join(f"- rule {index}" for index in range(300)), encoding="utf-8", newline="\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = pyxis_roadmap.inspect_command(self.roadmap, max_lines=150, recent=2)
        self.assertEqual(return_code, 1)
        self.assertIn("resume packet would be", stderr.getvalue())

    def complete_all_tasks(self) -> None:
        control = self.control()
        control = control.replace(
            "| T00 | P0 | `value` | Persist `.pyxis` roadmap | READY |",
            "| T00 | P0 | `value` | Persist `.pyxis` roadmap | DONE |",
        )
        control = control.replace(
            "| T01 | P0 | `value` | `value` | PENDING |",
            "| T01 | P0 | `value` | `value` | DONE |",
        )
        control = control.replace(
            "| T99 | P0 | `value` | Final integrated audit | PENDING |",
            "| T99 | P0 | `value` | Final integrated audit | DONE |",
        )
        self.write_control(control)
        for task_id in ("T00", "T01", "T99"):
            task = self.roadmap / "tasks" / f"{task_id}.md"
            task.write_text(
                task.read_text(encoding="utf-8").replace(
                    "- Pending.", "- Verification and durable evidence passed."
                ),
                encoding="utf-8",
                newline="\n",
            )
        acceptance = self.roadmap / "policies" / "acceptance.md"
        acceptance.write_text(
            acceptance.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
            encoding="utf-8",
            newline="\n",
        )
        history = self.roadmap / "history" / "completion-log.md"
        receipts = "".join(
            f"\n## {task_id} — 2026-08-13\n\n"
            "- Repository: value\n"
            "- Commit: this task's commit\n"
            "- Verification: passed\n"
            "- Evidence: exact receipt\n"
            for task_id in ("T00", "T01", "T99")
        )
        with history.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(receipts)

    def test_close_refuses_incomplete_roadmap(self) -> None:
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "nonterminal"):
            pyxis_roadmap.close_command(self.roadmap, "demo-roadmap")
        self.assertTrue(self.roadmap.is_dir())

    def test_close_removes_completed_roadmap_and_it_cannot_be_reopened(self) -> None:
        self.complete_all_tasks()
        self.commit_audit()
        self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).errors, [])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return_code = pyxis_roadmap.close_command(
                self.roadmap, "demo-roadmap"
            )
        self.assertEqual(return_code, 0)
        self.assertIn("CLOSE: COMPLETE", stdout.getvalue())
        self.assertFalse(self.roadmap.exists())
        self.assertTrue((self.repo / ".pyxis" / "scripts" / "roadmap.py").is_file())
        reopened = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertTrue(any("does not exist" in error for error in reopened.errors))

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True, encoding="utf-8")
        return result.stdout.strip()

    def commit_audit(self, subject: str = "T99 finalize Demo Project") -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Pyxis Test")
        self.git("config", "user.email", "pyxis-test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", str(self.repo / "no-hooks"))
        self.git("add", "--", ".pyxis")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", subject)

    def test_close_refuses_uncommitted_audit(self) -> None:
        self.complete_all_tasks()
        self.git("init", "-q")
        with self.assertRaises(pyxis_roadmap.RoadmapError):
            pyxis_roadmap.close_command(self.roadmap, "demo-roadmap")
        self.assertTrue(self.roadmap.is_dir())

    def test_close_refuses_dirty_staged_untracked_and_ignored_evidence(self) -> None:
        self.complete_all_tasks()
        self.commit_audit()
        tracked = self.roadmap / "tasks" / "T01.md"
        original = tracked.read_bytes()
        for mode in ("unstaged", "staged", "untracked", "ignored"):
            with self.subTest(mode=mode):
                extra = self.roadmap / "audit.bin"
                if mode in ("unstaged", "staged"):
                    tracked.write_bytes(original + b"\n- Extra audit evidence.\n")
                    if mode == "staged":
                        self.git("add", "--", ".pyxis")
                else:
                    if mode == "ignored":
                        (self.repo / ".git" / "info" / "exclude").write_text("audit.bin\n", encoding="utf-8")
                    extra.write_bytes(b"irreplaceable evidence")
                with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "changes"):
                    pyxis_roadmap.close_command(self.roadmap, "demo-roadmap")
                self.assertTrue(self.roadmap.is_dir())
                tracked.write_bytes(original)
                self.git("reset", "-q", "HEAD", "--", ".pyxis")
                extra.unlink(missing_ok=True)

    def test_close_preserves_unrelated_dirty_and_staged_files(self) -> None:
        self.complete_all_tasks()
        self.commit_audit()
        unrelated = self.repo / "user.txt"
        unrelated.write_text("user-owned", encoding="utf-8")
        self.git("add", "--", "user.txt")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pyxis_roadmap.close_command(self.roadmap, "demo-roadmap"), 0)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user-owned")
        self.assertIn("user.txt", self.git("diff", "--cached", "--name-only"))

    def test_close_requires_final_audit_commit_subject(self) -> None:
        self.complete_all_tasks()
        self.commit_audit("T01 premature completion")
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "last roadmap commit"):
            pyxis_roadmap.close_command(self.roadmap, "demo-roadmap")

    def test_close_supports_linked_worktrees_without_touching_original(self) -> None:
        self.complete_all_tasks()
        self.commit_audit()
        linked = self.repo.parent / "linked"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        self.assertTrue((linked / ".git").is_file())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pyxis_roadmap.close_command(linked / ".pyxis" / "roadmaps" / "demo-roadmap", "demo-roadmap"), 0)
        self.assertTrue(self.roadmap.is_dir())

    def test_packet_budget_diagnoses_oversized_receipts(self) -> None:
        history = self.roadmap / "history" / "completion-log.md"
        with history.open("a", encoding="utf-8") as stream:
            stream.write("\n## T00 — 2026-10-01\n- Evidence: " + "x" * 50000 + "\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(pyxis_roadmap.inspect_command(self.roadmap, 800, 2), 1)
        self.assertIn("COMPONENT: recent receipts", stderr.getvalue())
        self.assertNotIn("VALIDATION: PASS", stdout.getvalue())

    def test_close_rejects_missing_or_alternate_unchecked_acceptance(self) -> None:
        self.complete_all_tasks()
        acceptance = self.roadmap / "policies" / "acceptance.md"
        for checklist in ("# Acceptance\nNo checks.\n", "* [ ] Pending approval\n", "1. [ ] Pending approval\n", "+ [ ] Pending approval\n"):
            with self.subTest(checklist=checklist):
                acceptance.write_text(checklist, encoding="utf-8")
                with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "acceptance"):
                    pyxis_roadmap.close_command(self.roadmap, "demo-roadmap")
                self.assertTrue(self.roadmap.is_dir())

    def test_close_rejects_wrong_session_without_deleting(self) -> None:
        self.complete_all_tasks()
        with mock.patch.dict(
            os.environ, {pyxis_roadmap.SESSION_ENV: "different-codex-task"}
        ):
            return_code = pyxis_roadmap.close_command(
                self.roadmap, "demo-roadmap"
            )
        self.assertEqual(return_code, 1)
        self.assertTrue(self.roadmap.is_dir())

    def evidence_file(self, text: str = "- Tests: 12 passed. Artifact hash verified.") -> Path:
        path = self.repo.parent / "evidence.md"
        path.write_text(text, encoding="utf-8")
        return path

    def finish(self, task_id: str = "T00") -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transition_command(self.roadmap, task_id, "DONE", self.evidence_file(), "12 tests passed")

    def test_new_and_legacy_placeholders_fail_but_cli_metavariables_pass(self) -> None:
        task = self.roadmap / "tasks" / "T00.md"
        original = task.read_text(encoding="utf-8")
        for text, invalid in (("<ALIAS>", False), ("<KEY>", False), ("{{PYXIS:CUSTOM}}", True), ("<PRIMARY_PATH>", True)):
            with self.subTest(text=text):
                task.write_text(original + "\n- " + text + "\n", encoding="utf-8")
                errors = pyxis_roadmap.validate_roadmap(self.roadmap).errors
                self.assertEqual(any("unresolved placeholders" in error for error in errors), invalid)

    def test_transition_completes_promotes_and_records_one_receipt(self) -> None:
        self.finish()
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.rows[0].status, "DONE")
        self.assertEqual(result.current.task_id, "T01")
        self.assertIn("12 passed", result.documents["T00"].sections["Evidence"])
        before = pyxis_roadmap.snapshot(self.roadmap)
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "first nonterminal"):
            self.finish()
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)
        self.assertFalse(pyxis_roadmap.transaction_path(self.roadmap).exists())

    def test_invalid_completion_keeps_all_files_unchanged(self) -> None:
        before = pyxis_roadmap.snapshot(self.roadmap)
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "candidate rejected"):
            pyxis_roadmap.transition_command(self.roadmap, "T00", "DONE", self.evidence_file("- Pending."), "claimed pass")
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)
        self.assertFalse(pyxis_roadmap.transaction_path(self.roadmap).exists())

    def test_block_and_resume_preserve_queue(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transition_command(self.roadmap, "T00", "BLOCKED", blocker="runtime must come online")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.current.status, "BLOCKED")
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transition_command(self.roadmap, "T00", "READY")
        self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).current.status, "READY")

    def remediation_file(self) -> Path:
        path = self.repo.parent / "T01A.md"
        text = (self.roadmap / "tasks" / "T01.md").read_text(encoding="utf-8")
        path.write_text(text.replace("# T01 — value", "# T01A — Repair defect"), encoding="utf-8")
        return path

    def test_insert_orders_prerequisites_and_supersede_records_replacement(self) -> None:
        self.finish()
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.insert_command(self.roadmap, "T01", self.remediation_file(), "T01A repair")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual([row.task_id for row in result.rows], ["T00", "T01A", "T01", "T99"])
        self.assertEqual(result.current.task_id, "T01A")
        self.assertEqual(result.rows[2].dependencies, ("T00", "T01A"))
        self.finish("T01A")
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transition_command(self.roadmap, "T01", "SUPERSEDED", summary="replacement covers original scope", replacement="T01A")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.current.task_id, "T99")
        self.assertIn("Superseded by T01A", result.documents["T01"].sections["Evidence"])

    def test_insert_does_not_clear_existing_external_blocker(self) -> None:
        self.finish()
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transition_command(self.roadmap, "T01", "BLOCKED", blocker="approval needed")
            pyxis_roadmap.insert_command(self.roadmap, "T01", self.remediation_file(), "T01A repair")
        self.finish("T01A")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.current.status, "BLOCKED")
        self.assertIn("approval needed", result.documents["T01"].sections["Blockers"])

    def test_insert_duplicate_or_invalid_task_does_not_change_state(self) -> None:
        before = pyxis_roadmap.snapshot(self.roadmap)
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "already exists"):
            pyxis_roadmap.insert_command(self.roadmap, "T01", self.roadmap / "tasks" / "T01.md", "duplicate")
        path = self.remediation_file()
        path.write_text(path.read_text(encoding="utf-8").replace("Repository: `value`", "Repository: unknown"), encoding="utf-8")
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "candidate rejected"):
            pyxis_roadmap.insert_command(self.roadmap, "T01", path, "T01A repair")
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)

    def interrupt_transition(self) -> dict[str, bytes]:
        before = pyxis_roadmap.snapshot(self.roadmap)
        write = pyxis_roadmap.atomic_write
        calls = 0

        def fail_after_one(path, data, scratch):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted write")
            write(path, data, scratch)

        with mock.patch.object(pyxis_roadmap, "atomic_write", side_effect=fail_after_one), self.assertRaisesRegex(OSError, "interrupted"):
            self.finish()
        return before

    def test_interrupted_write_blocks_readers_then_rolls_back_exact_bytes(self) -> None:
        before = self.interrupt_transition()
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.control_text, "")
        self.assertTrue(any("unfinished roadmap transaction" in e for e in result.errors))
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.recover_command(self.roadmap, "rollback")
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)
        self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).errors, [])

    def test_interrupted_write_can_finish_without_duplicate_receipt(self) -> None:
        self.interrupt_transition()
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.recover_command(self.roadmap, "apply")
        result = pyxis_roadmap.validate_roadmap(self.roadmap)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.current.task_id, "T01")
        entries = pyxis_roadmap.completion_entries((self.roadmap / "history" / "completion-log.md").read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)

    def test_recovery_refuses_foreign_owner_and_concurrent_edits(self) -> None:
        self.interrupt_transition()
        with mock.patch.dict(os.environ, {pyxis_roadmap.SESSION_ENV: "other"}), self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "different Codex task"):
            pyxis_roadmap.recover_command(self.roadmap, "apply")
        task = self.roadmap / "tasks" / "T01.md"
        task.write_bytes(task.read_bytes() + b"\nUser edit\n")
        before = pyxis_roadmap.snapshot(self.roadmap)
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "concurrent edit"):
            pyxis_roadmap.recover_command(self.roadmap, "rollback")
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)

    def test_transfer_requires_explicit_authorization_and_expected_owner_before_reading(self) -> None:
        digest = pyxis_roadmap.owner_digest(self.session_id)
        before = pyxis_roadmap.snapshot(self.roadmap)
        original_read = pyxis_roadmap.read_utf8

        def owner_only(path):
            self.assertEqual(path.name, ".owner", "must not read roadmap Markdown")
            return original_read(path)

        with mock.patch.dict(os.environ, {pyxis_roadmap.SESSION_ENV: "new-owner"}), mock.patch.object(pyxis_roadmap, "read_utf8", side_effect=owner_only):
            for owner, confirm, authorized in ((digest, "demo-roadmap", False), ("A" * 64, "demo-roadmap", True), (digest, "wrong", True)):
                with self.subTest(owner=owner, confirm=confirm, authorized=authorized), self.assertRaises(pyxis_roadmap.RoadmapError):
                    pyxis_roadmap.transfer_command(self.roadmap, owner, confirm, "user handoff", authorized)
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)

    def test_transfer_changes_only_owner_and_audit_and_revokes_old_owner(self) -> None:
        self.commit_audit("T00 initial control")
        before = pyxis_roadmap.snapshot(self.roadmap)
        digest = pyxis_roadmap.owner_digest(self.session_id)
        with mock.patch.dict(os.environ, {pyxis_roadmap.SESSION_ENV: "new-owner"}), contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transfer_command(self.roadmap, digest, "demo-roadmap", "user explicitly requested handoff", True)
            self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).errors, [])
        after = pyxis_roadmap.snapshot(self.roadmap)
        self.assertEqual({name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}, {".owner", "history/ownership-log.md"})
        self.assertNotIn(b"new-owner", after["history/ownership-log.md"])
        self.assertNotIn(self.session_id.encode(), after["history/ownership-log.md"])
        self.assertTrue(any("different Codex task" in e for e in pyxis_roadmap.validate_roadmap(self.roadmap).errors))

    def test_transfer_refuses_dirty_original_work(self) -> None:
        self.commit_audit("T00 initial control")
        task = self.roadmap / "tasks" / "T01.md"
        task.write_bytes(task.read_bytes() + b"\nProtected work\n")
        before = pyxis_roadmap.snapshot(self.roadmap)
        with mock.patch.dict(os.environ, {pyxis_roadmap.SESSION_ENV: "new-owner"}), self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "committed, clean"):
            pyxis_roadmap.transfer_command(self.roadmap, pyxis_roadmap.owner_digest(self.session_id), "demo-roadmap", "handoff", True)
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)

    def test_interrupted_transfer_recovers_under_new_owner(self) -> None:
        self.commit_audit("T00 initial control")
        before = pyxis_roadmap.snapshot(self.roadmap)
        write = pyxis_roadmap.atomic_write

        def fail_audit(path, data, scratch):
            if path.name == "ownership-log.md":
                raise OSError("interrupted transfer")
            write(path, data, scratch)

        with mock.patch.dict(os.environ, {pyxis_roadmap.SESSION_ENV: "new-owner"}):
            with mock.patch.object(pyxis_roadmap, "atomic_write", side_effect=fail_audit), self.assertRaisesRegex(OSError, "interrupted"):
                pyxis_roadmap.transfer_command(self.roadmap, pyxis_roadmap.owner_digest(self.session_id), "demo-roadmap", "handoff", True)
            with contextlib.redirect_stdout(io.StringIO()):
                pyxis_roadmap.recover_command(self.roadmap, "rollback")
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)

    def test_mutation_lock_rejects_competing_process(self) -> None:
        code = ("import sys; from pathlib import Path; import pyxis_roadmap; "
                "pyxis_roadmap.transition_command(Path(sys.argv[1]), 'T00', 'BLOCKED', blocker='wait')")

        @pyxis_roadmap.serialized_mutation
        def hold(roadmap):
            return subprocess.run([os.sys.executable, "-B", "-c", code, str(roadmap)],
                                  cwd=Path(pyxis_roadmap.__file__).parent, capture_output=True, text=True, check=False)

        result = hold(self.roadmap)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another roadmap mutation", result.stderr)
        self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).current.status, "READY")

    def test_external_completion_requires_full_commit_reference(self) -> None:
        self.finish()
        self.write_control(self.control().replace("| T01 | P0 | value |", "| T01 | P0 | external |"))
        task = self.roadmap / "tasks" / "T01.md"
        task.write_text(task.read_text(encoding="utf-8").replace("Repository: `value`", "Repository: external"), encoding="utf-8")
        policy = self.roadmap / "policies" / "repositories.md"
        policy.write_text(policy.read_text(encoding="utf-8").replace("## Protected", "| external | value | value | value | value |\n\n## Protected"), encoding="utf-8")
        before = pyxis_roadmap.snapshot(self.roadmap)
        with self.assertRaisesRegex(pyxis_roadmap.RoadmapError, "verified full SHA"):
            pyxis_roadmap.transition_command(self.roadmap, "T01", "DONE", self.evidence_file(), "passed")
        self.assertEqual(pyxis_roadmap.snapshot(self.roadmap), before)
        with contextlib.redirect_stdout(io.StringIO()):
            pyxis_roadmap.transition_command(self.roadmap, "T01", "DONE", self.evidence_file(), "passed", commit="a" * 40)
        self.assertEqual(pyxis_roadmap.validate_roadmap(self.roadmap).current.task_id, "T99")

    def test_cli_lifecycle_through_copied_runtime(self) -> None:
        self.commit_audit("pyxis: initial scaffold")
        runtime = self.repo / ".pyxis" / "scripts" / "roadmap.py"
        cli_owner = "cli-new-owner"

        def cli(*args, owner=cli_owner, expected=0):
            result = subprocess.run([os.sys.executable, "-X", "utf8", "-B", str(runtime), *map(str, args)],
                                    cwd=self.repo, env={**os.environ, pyxis_roadmap.SESSION_ENV: owner},
                                    capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return result.stdout + result.stderr

        cli("transfer", self.roadmap, "--from-owner", pyxis_roadmap.owner_digest(self.session_id),
            "--confirm", "demo-roadmap", "--reason", "user requested CLI handoff", "--authorized")
        self.assertIn("different Codex task", cli("inspect", self.roadmap, owner=self.session_id, expected=1))
        self.assertIn("VALIDATION: PASS", cli("inspect", self.roadmap))
        self.commit_audit("pyxis: transfer demo-roadmap")
        evidence = self.evidence_file()

        def complete(task, subject):
            cli("transition", self.roadmap, "--task", task, "--to", "DONE", "--evidence-file", evidence, "--summary", "CLI acceptance verified")
            self.commit_audit(subject)

        complete("T00", "T00 persist .pyxis roadmap")
        cli("insert", self.roadmap, "--before", "T01", "--task-file", self.remediation_file(), "--commit-title", "T01A repair")
        self.commit_audit("pyxis: insert remediation")
        complete("T01A", "T01A repair")
        cli("transition", self.roadmap, "--task", "T01", "--to", "SUPERSEDED", "--replacement", "T01A", "--summary", "replacement verified")
        self.commit_audit("T01 record replacement")
        acceptance = self.roadmap / "policies" / "acceptance.md"
        acceptance.write_text(acceptance.read_text(encoding="utf-8").replace("[ ]", "[x]"), encoding="utf-8")
        complete("T99", "T99 finalize Demo Project")
        self.assertIn("next=none", cli("check", self.roadmap))
        user_file = self.repo / "user.txt"
        user_file.write_text("preserve", encoding="utf-8")
        self.assertIn("CLOSE: COMPLETE", cli("close", self.roadmap, "--confirm", "demo-roadmap"))
        self.assertFalse(self.roadmap.exists())
        self.assertEqual(user_file.read_text(encoding="utf-8"), "preserve")
        self.assertTrue(runtime.is_file())
        self.git("add", "-u", "--", ".pyxis/roadmaps/demo-roadmap")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "pyxis: close demo-roadmap")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "pyxis: close demo-roadmap")


if __name__ == "__main__":
    unittest.main()
