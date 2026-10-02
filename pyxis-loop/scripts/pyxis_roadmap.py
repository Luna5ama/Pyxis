#!/usr/bin/env python3
"""Scaffold, validate, inspect, and close task-owned .pyxis roadmaps."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

SCHEMA = "pyxis-loop/v2"
OWNER_SCHEMA = "pyxis-loop-owner/v1"
OWNER_FILE = ".owner"
SESSION_ENV = "CODEX_THREAD_ID"
ALLOWED_STATUSES = {"READY", "PENDING", "BLOCKED", "DONE", "SUPERSEDED"}
TERMINAL_STATUSES = {"DONE", "SUPERSEDED"}
CONTROL_SECTIONS = ("Goal", "Continuation protocol", "Required reads", "Status board", "Queue rules")
TASK_SECTIONS = ("Scope", "Non-scope", "Acceptance", "Verification", "Blockers", "Evidence")
CONTROL_FIELDS = ("Schema", "Roadmap ID", "Owner repository", "Created", "Source handoff", "Runtime inspector")
# Legacy templates remain readable; generic CLI metavariables such as <ALIAS> are not scaffolding.
LEGACY_PLACEHOLDERS = ["PROJECT", "ROADMAP_SLUG", "OWNER_REPOSITORY_KEY", "YYYY-MM-DD", "PATH_OR_DESCRIPTION", "ONE_PARAGRAPH_OBSERVABLE_OUTCOME", "FIRST_TASK", "EXPECTED_SUBJECT", "GLOBAL_ACCEPTANCE_1", "GLOBAL_ACCEPTANCE_2", "CONSTRAINT_1", "CONSTRAINT_2", "ABSOLUTE_OWNER_REPOSITORY", "ABSOLUTE_OWNER_WORKTREE", "EXACT_OWNER_BRANCH", "FULL_BASELINE_SHA", "PATH_OR_DIRTY_STATE", "PRESERVE_OR_ALLOWED_ACTION", "USER_OR_TASK", "PRIMARY_PATH", "EXACT_IN_SCOPE_CHANGE", "EXPLICIT_EXCLUSION", "OBSERVABLE_ACCEPTANCE_CRITERION", "EXACT_COMMAND_OR_MANUAL_GATE", "FINAL_AUDIT_PATHS", "FINAL_FOCUSED_AND_INTEGRATED_COMMANDS"]
PLACEHOLDER = re.compile(r"\{\{PYXIS:[A-Z][A-Z0-9_-]*\}\}|<(?:" + "|".join(LEGACY_PLACEHOLDERS) + r")>")
TASK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
TASK_HEADING = re.compile(r"^#\s+(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s+(?:—|-)\s+(?P<title>.+?)\s*$", re.MULTILINE)
RECEIPT_HEADING = re.compile(r"^##\s+(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s+(?:—|-)\s+.+$", re.MULTILINE)
PENDING_EVIDENCE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:\[[ \t]\][ \t]+)?pending[.!]?[ \t]*$", re.MULTILINE | re.IGNORECASE)
CHECKBOX = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[([ xX])\](?:[ \t]+|$)", re.MULTILINE)


class RoadmapError(RuntimeError):
    """A user-actionable roadmap error."""


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    priority: str
    repository: str
    title: str
    status: str
    dependencies: tuple[str, ...]
    task_file: str
    commit_title: str
    line: int


@dataclass(frozen=True)
class TaskDocument:
    path: Path
    text: str
    task_id: str
    title: str
    repository: str
    required_reads: tuple[str, ...]
    primary_files: str
    sections: dict[str, str]


@dataclass
class Validation:
    roadmap: Path
    control_path: Path
    control_text: str = ""
    control_fields: dict[str, str] = field(default_factory=dict)
    control_reads: tuple[str, ...] = ()
    rows: list[TaskRow] = field(default_factory=list)
    documents: dict[str, TaskDocument] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def current(self) -> TaskRow | None:
        ready = [row for row in self.rows if row.status == "READY"]
        if ready:
            return ready[0]
        return next((row for row in self.rows if row.status not in TERMINAL_STATUSES), None)


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RoadmapError(f"cannot read {path}: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def current_session_id() -> str:
    session_id = os.environ.get(SESSION_ENV, "").strip()
    if not session_id:
        raise RoadmapError(
            f"{SESSION_ENV} is unavailable; refuse to create, read, modify, or close a roadmap "
            "without a Codex task identity"
        )
    return session_id


def owner_digest(session_id: str) -> str:
    return sha256_bytes(session_id.encode("utf-8"))


def owner_marker(session_id: str) -> str:
    return f"{OWNER_SCHEMA}\nSHA256={owner_digest(session_id)}\n"


def verify_owner_session(roadmap: Path) -> None:
    """Fail before reading roadmap content when the current Codex task is not the owner."""
    marker_path = roadmap / OWNER_FILE
    try:
        marker = read_utf8(marker_path).splitlines()
    except RoadmapError as exc:
        raise RoadmapError(
            f"roadmap ownership marker is missing or unreadable; refuse to read roadmap contents: {exc}"
        ) from exc
    if len(marker) != 2 or marker[0] != OWNER_SCHEMA or not marker[1].startswith("SHA256="):
        raise RoadmapError("invalid roadmap ownership marker; refuse to read roadmap contents")
    expected = marker[1].removeprefix("SHA256=").strip()
    actual = owner_digest(current_session_id())
    if not hmac.compare_digest(expected, actual):
        raise RoadmapError(
            "roadmap belongs to a different Codex task; refuse to read, modify, or close its contents"
        )


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def strip_code(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        return value[1:-1].strip()
    return value


def section(text: str, name: str) -> str | None:
    match = re.search(
        rf"^##\s+{re.escape(name)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group("body").strip() if match else None


def field_value(text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}:[ \t]*(?P<value>[^\r\n]*?)[ \t]*$", text, re.MULTILINE)
    return strip_code(match.group("value")) if match else None


def field_block(text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}:[ \t]*(?P<inline>.*)$", text, re.MULTILINE)
    if not match:
        return None
    inline = match.group("inline").strip()
    if inline:
        return inline
    remainder = text[match.end() :]
    end = re.search(r"^(?:[A-Za-z][A-Za-z ]+:\s*|##\s+)", remainder, re.MULTILINE)
    return remainder[: end.start()].strip() if end else remainder.strip()


def parse_path_list(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    clean = value.strip()
    if not clean or clean.lower() in {"none", "n/a", "-"}:
        return ()
    bullets = re.findall(r"^-\s+(.+?)\s*$", clean, re.MULTILINE)
    parts = bullets if bullets else clean.split(",")
    return tuple(strip_code(part.strip()) for part in parts if part.strip())


def parse_dependencies(value: str) -> tuple[str, ...]:
    clean = strip_code(value)
    if not clean or clean.lower() in {"none", "n/a", "-"}:
        return ()
    return tuple(strip_code(part) for part in clean.split(",") if part.strip())


def parse_status_board(text: str, errors: list[str]) -> list[TaskRow]:
    body = section(text, "Status board")
    if body is None:
        return []
    rows: list[TaskRow] = []
    seen: set[str] = set()
    section_offset = text.find(body)
    for match in re.finditer(r"^\|(?P<row>.+)\|\s*$", body, re.MULTILINE):
        cells = [cell.strip() for cell in match.group("row").split("|")]
        if not cells or cells[0] == "ID" or set(cells[0]) <= {"-", ":"}:
            continue
        row_line = line_number(text, section_offset + match.start())
        if len(cells) != 8:
            errors.append(f"CONTROL.md:{row_line}: status row must have 8 cells, found {len(cells)}")
            continue
        task_id = strip_code(cells[0])
        status = strip_code(cells[4]).upper()
        if not TASK_ID.fullmatch(task_id):
            errors.append(f"CONTROL.md:{row_line}: invalid task ID {task_id!r}")
        if task_id in seen:
            errors.append(f"CONTROL.md:{row_line}: duplicate task ID {task_id}")
        seen.add(task_id)
        rows.append(
            TaskRow(
                task_id=task_id,
                priority=strip_code(cells[1]),
                repository=strip_code(cells[2]),
                title=strip_code(cells[3]),
                status=status,
                dependencies=parse_dependencies(cells[5]),
                task_file=strip_code(cells[6]),
                commit_title=strip_code(cells[7]),
                line=row_line,
            )
        )
    if not rows:
        errors.append("CONTROL.md: status board has no task rows")
    return rows


def parse_task_document(path: Path, errors: list[str]) -> TaskDocument | None:
    try:
        text = read_utf8(path)
    except RoadmapError as exc:
        errors.append(str(exc))
        return None
    heading = TASK_HEADING.search(text)
    if not heading:
        errors.append(f"{path}: missing '# Txx — Title' heading")
        return None
    repository = field_value(text, "Repository")
    required = field_block(text, "Required reads")
    primary = field_block(text, "Primary files")
    if repository is None:
        errors.append(f"{path}: missing Repository field")
    if required is None:
        errors.append(f"{path}: missing Required reads field")
    if primary is None or not primary.strip():
        errors.append(f"{path}: missing or empty Primary files field")
    sections: dict[str, str] = {}
    for name in TASK_SECTIONS:
        content = section(text, name)
        if content is None or not content.strip():
            errors.append(f"{path}: missing or empty '## {name}' section")
        else:
            sections[name] = content
    return TaskDocument(
        path=path,
        text=text,
        task_id=heading.group("id"),
        title=heading.group("title").strip(),
        repository=repository or "",
        required_reads=parse_path_list(required),
        primary_files=primary or "",
        sections=sections,
    )


def safe_roadmap_file(roadmap: Path, relative: str, label: str, errors: list[str]) -> Path | None:
    candidate = (roadmap / relative).resolve()
    try:
        candidate.relative_to(roadmap.resolve())
    except ValueError:
        errors.append(f"{label}: path escapes roadmap directory: {relative}")
        return None
    if not candidate.is_file():
        errors.append(f"{label}: missing file: {relative}")
        return None
    return candidate


def parse_repository_keys(path: Path, errors: list[str]) -> set[str]:
    try:
        text = read_utf8(path)
    except RoadmapError as exc:
        errors.append(str(exc))
        return set()
    body = section(text, "Repositories")
    if body is None:
        errors.append(f"{path}: missing '## Repositories' section")
        return set()
    keys: set[str] = set()
    for match in re.finditer(r"^\|(?P<row>.+)\|\s*$", body, re.MULTILINE):
        cells = [cell.strip() for cell in match.group("row").split("|")]
        if not cells or cells[0] == "Key" or set(cells[0]) <= {"-", ":"}:
            continue
        if len(cells) != 5:
            errors.append(f"{path}: repository row must have 5 cells")
            continue
        key = strip_code(cells[0])
        if key in keys:
            errors.append(f"{path}: duplicate repository key {key}")
        keys.add(key)
    if not keys:
        errors.append(f"{path}: no repository rows found")
    return keys


def find_cycle(rows: dict[str, TaskRow]) -> list[str] | None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(task_id: str) -> list[str] | None:
        if task_id in visiting:
            start = visiting.index(task_id)
            return visiting[start:] + [task_id]
        if task_id in visited:
            return None
        visiting.append(task_id)
        for dependency in rows[task_id].dependencies:
            if dependency in rows:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        visiting.pop()
        visited.add(task_id)
        return None

    for task_id in rows:
        cycle = visit(task_id)
        if cycle:
            return cycle
    return None


def validate_roadmap(path: Path) -> Validation:
    roadmap = path.resolve()
    result = Validation(roadmap=roadmap, control_path=roadmap / "CONTROL.md")
    if not roadmap.is_dir():
        result.errors.append(f"roadmap directory does not exist: {roadmap}")
        return result
    try:
        verify_owner_session(roadmap)
        require_no_transaction(roadmap)
    except RoadmapError as exc:
        result.errors.append(str(exc))
        return result
    if roadmap.parent.name != "roadmaps" or roadmap.parent.parent.name != ".pyxis":
        result.errors.append("roadmap must live at <git-root>/.pyxis/roadmaps/<slug>")
    try:
        result.control_text = read_utf8(result.control_path)
    except RoadmapError as exc:
        result.errors.append(str(exc))
        return result

    for name in CONTROL_SECTIONS:
        if section(result.control_text, name) is None:
            result.errors.append(f"CONTROL.md: missing '## {name}' section")
    for name in CONTROL_FIELDS:
        value = field_value(result.control_text, name)
        if value is None:
            result.errors.append(f"CONTROL.md: missing {name} field")
        else:
            result.control_fields[name] = value
    if result.control_fields.get("Schema") != SCHEMA:
        result.errors.append(
            f"CONTROL.md: Schema must be {SCHEMA!r}, found {result.control_fields.get('Schema')!r}"
        )
    if result.control_fields.get("Roadmap ID") != roadmap.name:
        result.errors.append(
            f"CONTROL.md: Roadmap ID must match directory name {roadmap.name!r}"
        )
    if result.control_fields.get("Runtime inspector") != ".pyxis/scripts/roadmap.py":
        result.errors.append("CONTROL.md: Runtime inspector must be '.pyxis/scripts/roadmap.py'")
    runtime_path = roadmap.parent.parent / "scripts" / "roadmap.py"
    if not runtime_path.is_file():
        result.errors.append(f"missing repository-local inspector: {runtime_path}")
    result.control_reads = parse_path_list(section(result.control_text, "Required reads"))
    if not result.control_reads:
        result.errors.append("CONTROL.md: Required reads must name at least one policy file")
    core_reads = {
        "policies/repositories.md",
        "policies/constraints.md",
        "policies/acceptance.md",
    }
    missing_core_reads = sorted(core_reads - set(result.control_reads))
    if missing_core_reads:
        result.errors.append("CONTROL.md: missing core Required reads: " + ", ".join(missing_core_reads))
    result.rows = parse_status_board(result.control_text, result.errors)

    row_map = {row.task_id: row for row in result.rows}
    order = {row.task_id: index for index, row in enumerate(result.rows)}
    expected_files: set[Path] = set()
    for row in result.rows:
        if row.status not in ALLOWED_STATUSES:
            result.errors.append(f"CONTROL.md:{row.line}: invalid status {row.status!r} for {row.task_id}")
        task_path = safe_roadmap_file(roadmap, row.task_file, f"CONTROL.md:{row.line}", result.errors)
        if task_path is None:
            continue
        try:
            task_relative = task_path.relative_to(roadmap)
        except ValueError:
            task_relative = Path()
        if not task_relative.parts or task_relative.parts[0] != "tasks" or task_relative.suffix != ".md":
            result.errors.append(f"CONTROL.md:{row.line}: task file must be a Markdown file under tasks/")
        expected_files.add(task_path)
        document = parse_task_document(task_path, result.errors)
        if document is None:
            continue
        result.documents[row.task_id] = document
        if document.task_id != row.task_id:
            result.errors.append(f"{task_path}: heading ID {document.task_id} does not match {row.task_id}")
        if document.title != row.title:
            result.errors.append(f"{task_path}: heading title does not match CONTROL.md for {row.task_id}")
        if document.repository != row.repository:
            result.errors.append(f"{task_path}: Repository does not match CONTROL.md for {row.task_id}")

    tasks_dir = roadmap / "tasks"
    actual_files = {file.resolve() for file in tasks_dir.glob("*.md")} if tasks_dir.is_dir() else set()
    for orphan in sorted(actual_files - expected_files):
        result.errors.append(f"orphan task file not listed in CONTROL.md: {orphan.relative_to(roadmap)}")

    for row in result.rows:
        for dependency in row.dependencies:
            if dependency not in row_map:
                result.errors.append(f"CONTROL.md:{row.line}: {row.task_id} references unknown dependency {dependency}")
            elif dependency == row.task_id:
                result.errors.append(f"CONTROL.md:{row.line}: {row.task_id} depends on itself")
            elif order[dependency] >= order[row.task_id]:
                result.errors.append(f"CONTROL.md:{row.line}: dependency {dependency} must appear before {row.task_id}")
        if row.status in {"READY", "DONE"}:
            incomplete = [
                dependency
                for dependency in row.dependencies
                if dependency in row_map and row_map[dependency].status not in TERMINAL_STATUSES
            ]
            if incomplete:
                result.errors.append(
                    f"CONTROL.md:{row.line}: {row.task_id} is {row.status} before dependencies are terminal: "
                    + ", ".join(incomplete)
                )

    cycle = find_cycle(row_map)
    if cycle:
        result.errors.append("dependency cycle: " + " -> ".join(cycle))
    ready = [row for row in result.rows if row.status == "READY"]
    nonterminal = [row for row in result.rows if row.status not in TERMINAL_STATUSES]
    if len(ready) > 1:
        result.errors.append("multiple READY tasks: " + ", ".join(row.task_id for row in ready))
    if ready and nonterminal and ready[0].task_id != nonterminal[0].task_id:
        result.errors.append(
            f"READY task {ready[0].task_id} is not the first nonterminal task ({nonterminal[0].task_id})"
        )
    if nonterminal and not ready and nonterminal[0].status != "BLOCKED":
        result.errors.append(
            f"first nonterminal task {nonterminal[0].task_id} must be READY or BLOCKED, not {nonterminal[0].status}"
        )

    for relative in result.control_reads:
        safe_roadmap_file(roadmap, relative, "CONTROL.md Required reads", result.errors)
    for task_id, document in result.documents.items():
        for relative in document.required_reads:
            safe_roadmap_file(roadmap, relative, f"{task_id} Required reads", result.errors)

    repository_policy = roadmap / "policies" / "repositories.md"
    repository_keys = parse_repository_keys(repository_policy, result.errors) if repository_policy.is_file() else set()
    owner_repository = result.control_fields.get("Owner repository")
    if repository_keys and owner_repository not in repository_keys:
        result.errors.append(f"CONTROL.md: unknown owner repository key {owner_repository!r}")
    for row in result.rows:
        if repository_keys and row.repository not in repository_keys:
            result.errors.append(f"CONTROL.md:{row.line}: unknown repository key {row.repository!r}")

    completion_path = roadmap / "history" / "completion-log.md"
    try:
        completion_text = read_utf8(completion_path)
    except RoadmapError as exc:
        result.errors.append(str(exc))
        completion_text = ""
    receipt_ids = [match.group("id") for match in RECEIPT_HEADING.finditer(completion_text)]
    receipts = set(receipt_ids)
    for receipt in sorted(receipts):
        if receipt_ids.count(receipt) > 1:
            result.errors.append(f"completion log has duplicate receipts for {receipt}")
    for receipt in receipts - set(row_map):
        result.errors.append(f"completion log references unknown task {receipt}")
    for row in result.rows:
        document = result.documents.get(row.task_id)
        if row.status == "DONE":
            if row.task_id not in receipts:
                result.errors.append(f"DONE task {row.task_id} has no completion-log receipt")
            if document and PENDING_EVIDENCE.search(document.sections.get("Evidence", "")):
                result.errors.append(f"DONE task {row.task_id} still has pending evidence")
            if document and any(item == " " for item in CHECKBOX.findall(document.sections.get("Acceptance", ""))):
                result.errors.append(f"DONE task {row.task_id} has unchecked acceptance items")
        if row.status == "BLOCKED" and document:
            blockers = document.sections.get("Blockers", "").strip().lower()
            if blockers in {"- none.", "- none known.", "- pending."}:
                result.errors.append(f"BLOCKED task {row.task_id} does not name an exact blocker")

    for markdown in sorted(roadmap.rglob("*.md")):
        try:
            text = read_utf8(markdown)
        except RoadmapError as exc:
            result.errors.append(str(exc))
            continue
        placeholders = sorted(set(PLACEHOLDER.findall(text)))
        if placeholders:
            result.errors.append(
                f"{markdown.relative_to(roadmap)}: unresolved placeholders: " + ", ".join(placeholders[:8])
            )
        lines = len(text.splitlines())
        relative = markdown.relative_to(roadmap)
        if relative == Path("CONTROL.md") and lines > 300:
            result.warnings.append(f"CONTROL.md is {lines} lines; target <= 300")
        elif relative.parts and relative.parts[0] in {"tasks", "policies"} and lines > 200:
            result.warnings.append(f"{relative} is {lines} lines; target <= 200")

    if result.rows and result.rows[0].task_id != "T00":
        result.warnings.append(f"first task is {result.rows[0].task_id}, not the recommended T00")
    if result.rows and not re.search(r"final|audit", result.rows[-1].title, re.IGNORECASE):
        result.warnings.append(f"last task {result.rows[-1].task_id} does not appear to be a final audit")
    return result


def manifest_hash(result: Validation) -> str:
    files = sorted(result.roadmap.rglob("*.md"))
    owner = result.roadmap / OWNER_FILE
    if owner.is_file():
        files.append(owner)
    runtime = result.roadmap.parent.parent / "scripts" / "roadmap.py"
    if runtime.is_file():
        files.append(runtime)
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        try:
            relative = path.relative_to(result.roadmap.parent.parent).as_posix()
        except ValueError:
            relative = str(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest().upper()


def completion_entries(text: str) -> list[str]:
    matches = list(RECEIPT_HEADING.finditer(text))
    entries: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entries.append(text[match.start() : end].strip())
    return entries


def print_diagnostics(result: Validation) -> None:
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)


def serialized_mutation(function):
    """OS advisory lock is released on process exit, including an interrupted recovery."""
    @wraps(function)
    def locked(roadmap: Path, *args, **kwargs):
        key = owner_digest(os.path.normcase(str(roadmap.resolve())))
        lock_path = Path(tempfile.gettempdir()) / f"pyxis-{key}.lock"
        with lock_path.open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if not lock.tell():
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RoadmapError("another roadmap mutation is running; retry after it exits") from exc
            try:
                return function(roadmap, *args, **kwargs)
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return locked


@serialized_mutation
def check_command(roadmap: Path) -> int:
    result = validate_roadmap(roadmap)
    print_diagnostics(result)
    if result.errors:
        print(f"Roadmap invalid: {len(result.errors)} error(s), {len(result.warnings)} warning(s).", file=sys.stderr)
        return 1
    counts = ", ".join(
        f"{status}={sum(row.status == status for row in result.rows)}"
        for status in ("READY", "PENDING", "BLOCKED", "DONE", "SUPERSEDED")
    )
    current = result.current.task_id if result.current else "none"
    print(f"Roadmap valid: {len(result.rows)} task(s); next={current}; {counts}.")
    return 0


@serialized_mutation
def inspect_command(roadmap: Path, max_lines: int, recent: int, max_bytes: int = 48000) -> int:
    result = validate_roadmap(roadmap)
    print_diagnostics(result)
    if result.errors:
        print("VALIDATION: FAIL", file=sys.stderr)
        return 1

    current = result.current
    component_paths: list[Path] = [result.control_path]
    if current and current.task_id in result.documents:
        component_paths.append(result.documents[current.task_id].path)
    required_relatives = list(result.control_reads)
    if current and current.task_id in result.documents:
        required_relatives.extend(result.documents[current.task_id].required_reads)
    for relative in required_relatives:
        path = (result.roadmap / relative).resolve()
        if path not in component_paths:
            component_paths.append(path)

    completion_path = result.roadmap / "history" / "completion-log.md"
    recent_entries = completion_entries(read_utf8(completion_path))[-recent:] if recent > 0 else []
    dependencies = []
    row_map = {row.task_id: row for row in result.rows}
    if current:
        dependencies = [row_map[task_id] for task_id in current.dependencies if task_id in row_map]

    header = [
        "PYXIS ROADMAP RESUME PACKET",
        "VALIDATION: PASS",
        f"ROADMAP: {result.roadmap}",
        f"ROADMAP_ID: {result.control_fields.get('Roadmap ID', '')}",
        f"MANIFEST_SHA256: {manifest_hash(result)}",
        f"TASKS: {len(result.rows)}",
        f"NEXT_TASK: {current.task_id if current else 'none'}",
        "STATUS_COUNTS: "
        + ", ".join(
            f"{status}={sum(row.status == status for row in result.rows)}"
            for status in ("READY", "PENDING", "BLOCKED", "DONE", "SUPERSEDED")
        ),
        "REQUIRED_PACKET_FILES:",
        *[f"- {path.relative_to(result.roadmap).as_posix()}" for path in component_paths],
        "DIRECT_DEPENDENCIES:",
        *(
            [f"- {row.task_id}: {row.status}; {row.commit_title}" for row in dependencies]
            if dependencies
            else ["- none"]
        ),
    ]
    components: list[str] = []
    for path in component_paths:
        relative = path.relative_to(result.roadmap).as_posix()
        content = read_utf8(path).rstrip()
        components.append(
            f"--- BEGIN {relative} SHA256={sha256_file(path)} ---\n{content}\n--- END {relative} ---"
        )
    if recent_entries:
        components.append(
            "--- BEGIN recent completion receipts ---\n"
            + "\n\n".join(recent_entries)
            + "\n--- END recent completion receipts ---"
        )
    packet = "\n".join(header + [""] + components) + "\n"
    packet_lines = len(packet.splitlines())
    packet_bytes = len(packet.encode("utf-8"))
    if packet_lines > max_lines or packet_bytes > max_bytes:
        print(
            f"ERROR: resume packet would be {packet_lines} lines / {packet_bytes} UTF-8 bytes; "
            f"limits are --max-lines {max_lines} / --max-bytes {max_bytes}.",
            file=sys.stderr,
        )
        for path in component_paths:
            content = read_utf8(path)
            print(f"COMPONENT: {path.relative_to(result.roadmap)} lines={len(content.splitlines())} bytes={len(content.encode('utf-8'))}", file=sys.stderr)
        if recent_entries:
            receipts = "\n\n".join(recent_entries)
            print(f"COMPONENT: recent receipts lines={len(receipts.splitlines())} bytes={len(receipts.encode('utf-8'))}", file=sys.stderr)
        return 1
    print(packet, end="")
    return 0


def git_output(repo: Path, *args: str) -> str:
    try:
        command = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        raise RoadmapError(f"cannot verify Git state: {exc}") from exc
    if command.returncode:
        raise RoadmapError(f"cannot verify Git state: {command.stderr.strip()}")
    return command.stdout.strip()


def transaction_path(roadmap: Path) -> Path:
    if roadmap.parent.name != "roadmaps" or roadmap.parent.parent.name != ".pyxis":
        raise RoadmapError("roadmap must live at <git-root>/.pyxis/roadmaps/<slug>")
    return roadmap.parent.parent / "transactions" / roadmap.name




def require_no_transaction(roadmap: Path) -> None:
    if transaction_path(roadmap).exists():
        raise RoadmapError("unfinished roadmap transaction; run recover --action apply or rollback before continuing")


def snapshot(roadmap: Path) -> dict[str, bytes]:
    files = {}
    for path in [roadmap, *roadmap.rglob("*")]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RoadmapError(f"refuse linked roadmap path: {path}")
        if path.is_file():
            files[path.relative_to(roadmap).as_posix()] = path.read_bytes()
    return files


def atomic_write(path: Path, data: bytes, scratch: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pyxis-write-", dir=scratch)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_changes(roadmap: Path, before: dict[str, bytes], changes: dict[str, bytes]) -> None:
    """Validate first; publish a recovery journal before any multi-file writes."""
    require_no_transaction(roadmap)
    after = before | changes
    with tempfile.TemporaryDirectory(prefix="pyxis-candidate-") as directory:
        candidate = Path(directory) / ".pyxis" / "roadmaps" / roadmap.name
        for relative, data in after.items():
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        runtime = candidate.parent.parent / "scripts" / "roadmap.py"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes((roadmap.parent.parent / "scripts" / "roadmap.py").read_bytes())
        result = validate_roadmap(candidate)
        if result.errors:
            raise RoadmapError("candidate rejected; no changes written:\n" + "\n".join(result.errors))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            packet_result = inspect_command(candidate, 800, 2)
        if packet_result:
            raise RoadmapError("candidate packet rejected; no changes written:\n" + stderr.getvalue())
    if snapshot(roadmap) != before:
        raise RoadmapError("roadmap changed during preparation; retry from a fresh inspect")
    journal = transaction_path(roadmap)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=journal.parent) as directory:
        prepared = Path(directory) / "journal"
        prepared.mkdir()
        (prepared / OWNER_FILE).write_text(owner_marker(current_session_id()), encoding="utf-8")
        entries = {
            name: {"before": base64.b64encode(before[name]).decode("ascii") if name in before else None,
                   "after": base64.b64encode(data).decode("ascii")}
            for name, data in after.items()
        }
        (prepared / "changes.json").write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")
        # A nonempty existing journal cannot be replaced by this rename.
        prepared.rename(journal)
    _recover(roadmap, "apply")


@serialized_mutation
def recover_command(roadmap: Path, action: str) -> int:
    return _recover(roadmap, action)


def _recover(roadmap: Path, action: str) -> int:
    roadmap = roadmap.resolve()
    journal = transaction_path(roadmap)
    verify_owner_session(journal)
    entries = json.loads(read_utf8(journal / "changes.json"))
    decoded = {}
    for relative, values in entries.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
            raise RoadmapError("invalid path in transaction journal")
        if not path.parts or (roadmap / path).resolve().relative_to(roadmap).as_posix() != relative:
            raise RoadmapError("noncanonical path in transaction journal")
        decoded[relative] = {key: base64.b64decode(values[key], validate=True) if values[key] is not None else None
                             for key in ("before", "after")}
    current = snapshot(roadmap)
    if set(current) - set(decoded):
        raise RoadmapError("roadmap has new files since transaction; reconcile them before recovery")
    for name, values in decoded.items():
        if current.get(name) not in (values["before"], values["after"]):
            raise RoadmapError(f"concurrent edit to {name}; recovery will not overwrite it")
    side = "after" if action == "apply" else "before"
    for name, values in decoded.items():
        data = values[side]
        if current.get(name) == data:
            continue
        path = roadmap / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, data, journal)
    # Remove the gate atomically only after every target has reached the chosen side.
    # A process death during subsequent cleanup must not leave an unreadable active journal.
    finished = journal.with_name(f".finished-{journal.name}-{uuid.uuid4().hex}")
    journal.rename(finished)
    shutil.rmtree(finished)
    print(f"TRANSACTION: {action.upper()} COMPLETE")
    return 0


def valid_state(roadmap: Path) -> Validation:
    result = validate_roadmap(roadmap)
    if result.errors:
        raise RoadmapError("\n".join(result.errors))
    return result


def replace_section(text: str, name: str, body: str) -> str:
    pattern = rf"(^##[ \t]+{re.escape(name)}[ \t]*\n).*?(?=^##[ \t]+|\Z)"
    updated, count = re.subn(pattern, lambda m: m[1] + "\n" + body.strip() + "\n\n", text, flags=re.MULTILINE | re.DOTALL)
    if count != 1:
        raise RoadmapError(f"expected exactly one {name} section")
    return updated


def board_row(row: TaskRow, **updates: str) -> str:
    fields = [row.task_id, row.priority, row.repository, row.title, row.status,
              ", ".join(row.dependencies) or "none", row.task_file, row.commit_title]
    for index, name in ((4, "status"), (5, "dependencies")):
        fields[index] = updates.get(name, fields[index])
    if any("|" in value or "\n" in value for value in fields):
        raise RoadmapError("task row values cannot contain pipes or newlines")
    return "| " + " | ".join(fields) + " |"


@serialized_mutation
def transition_command(roadmap: Path, task_id: str, status: str, evidence: Path | None = None,
                       summary: str = "", blocker: str = "", commit: str = "this task's commit",
                       replacement: str = "") -> int:
    roadmap = roadmap.resolve()
    result = valid_state(roadmap)
    row = result.current
    if row is None or row.task_id != task_id:
        raise RoadmapError("transition must target the first nonterminal task")
    allowed = {("READY", "DONE"), ("READY", "BLOCKED"), ("BLOCKED", "READY"),
               ("READY", "SUPERSEDED"), ("BLOCKED", "SUPERSEDED")}
    if (row.status, status) not in allowed:
        raise RoadmapError(f"unsupported transition {row.status} -> {status}")
    before = snapshot(roadmap)
    document = result.documents[task_id]
    task_text = document.text
    changes = {}
    if status in TERMINAL_STATUSES:
        if not summary.strip() or "\n" in summary:
            raise RoadmapError("completion requires a one-line --summary")
        if status == "SUPERSEDED":
            previous = result.rows[:result.rows.index(row)]
            if not any(item.task_id == replacement and item.status == "DONE" for item in previous):
                raise RoadmapError("SUPERSEDED requires --replacement naming an earlier DONE task")
            task_text = replace_section(task_text, "Evidence", f"- Superseded by {replacement}: {summary.strip()}")
        elif evidence is None:
            raise RoadmapError("DONE requires --evidence-file")
        else:
            task_text = replace_section(task_text, "Evidence", read_utf8(evidence))
        if row.repository != result.control_fields["Owner repository"] and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
            raise RoadmapError("external completion requires --commit with the verified full SHA")
        if "\n" in commit:
            raise RoadmapError("commit reference must be one line")
        receipt = (f"\n## {task_id} — {datetime.now(timezone.utc).astimezone().date().isoformat()}\n\n- Repository: {row.repository}\n"
                   f"- Commit: {commit}\n- Verification: {summary.strip()}\n- Evidence: {row.task_file}\n")
        history = "history/completion-log.md"
        changes[history] = (before[history].decode("utf-8").rstrip() + "\n" + receipt).encode("utf-8")
    elif status == "BLOCKED":
        if not blocker.strip():
            raise RoadmapError("BLOCKED requires --blocker naming the unblock condition")
        task_text = replace_section(task_text, "Blockers", "- " + blocker.strip())
    else:
        task_text = replace_section(task_text, "Blockers", "- None.")
    board = section(result.control_text, "Status board")
    lines = board.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^\|\s*`?{re.escape(task_id)}`?\s*\|", line):
            lines[index] = board_row(row, status=status)
    if status in TERMINAL_STATUSES:
        following = next((item for item in result.rows if item.task_id != task_id and item.status not in TERMINAL_STATUSES), None)
        if following and following.status == "PENDING":
            for index, line in enumerate(lines):
                if re.match(rf"^\|\s*`?{re.escape(following.task_id)}`?\s*\|", line):
                    lines[index] = board_row(following, status="READY")
    changes["CONTROL.md"] = replace_section(result.control_text, "Status board", "\n".join(lines)).encode("utf-8")
    changes[document.path.relative_to(roadmap).as_posix()] = task_text.encode("utf-8")
    apply_changes(roadmap, before, changes)
    print(f"TRANSITION: {task_id} {row.status} -> {status}; review and commit exact scope")
    return 0


@serialized_mutation
def insert_command(roadmap: Path, before_id: str, task_file: Path, commit_title: str) -> int:
    roadmap = roadmap.resolve()
    result = valid_state(roadmap)
    target = next((row for row in result.rows if row.task_id == before_id), None)
    if target is None or target.status in TERMINAL_STATUSES:
        raise RoadmapError("insert --before must name a nonterminal task")
    errors = []
    document = parse_task_document(task_file, errors)
    if errors or document is None:
        raise RoadmapError("\n".join(errors))
    if any(row.task_id == document.task_id for row in result.rows):
        raise RoadmapError("inserted task ID already exists")
    if not commit_title.strip():
        raise RoadmapError("insert requires a nonempty commit title")
    new = TaskRow(document.task_id, target.priority, document.repository, document.title,
                  "READY" if result.current == target else "PENDING", target.dependencies,
                  f"tasks/{document.task_id}.md", commit_title, 0)
    board = section(result.control_text, "Status board")
    lines = board.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^\|\s*`?{re.escape(before_id)}`?\s*\|", line):
            lines[index:index + 1] = [board_row(new), board_row(target, status="BLOCKED" if target.status == "BLOCKED" else "PENDING",
                dependencies=", ".join((*target.dependencies, new.task_id)))]
            break
    before = snapshot(roadmap)
    apply_changes(roadmap, before, {
        "CONTROL.md": replace_section(result.control_text, "Status board", "\n".join(lines)).encode("utf-8"),
        new.task_file: document.text.encode("utf-8"),
    })
    print(f"INSERT: {new.task_id} before {before_id}; review and commit control-plane scope")
    return 0


@serialized_mutation
def transfer_command(roadmap: Path, previous_owner: str, confirm: str, reason: str, authorized: bool) -> int:
    roadmap = roadmap.resolve()
    if not authorized or confirm != roadmap.name or not reason.strip() or "\n" in reason:
        raise RoadmapError("transfer requires explicit user authorization, exact --confirm slug and one-line --reason")
    require_no_transaction(roadmap)
    expected = f"{OWNER_SCHEMA}\nSHA256={previous_owner.upper()}\n"
    if not re.fullmatch(r"[0-9a-fA-F]{64}", previous_owner) or read_utf8(roadmap / OWNER_FILE) != expected:
        raise RoadmapError("previous owner digest does not match; no roadmap content was read")
    new_owner = owner_digest(current_session_id())
    if new_owner == previous_owner.upper():
        raise RoadmapError("current task already owns this roadmap")
    repo = roadmap.parents[2]
    relative = roadmap.relative_to(repo).as_posix()
    if Path(git_output(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise RoadmapError("not the owner Git worktree root")
    tracked = git_output(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", relative + "/.owner")
    if tracked != relative + "/.owner" or git_output(repo, "status", "--porcelain", "--untracked-files=all", "--ignored", "--", relative):
        raise RoadmapError("transfer requires a committed, clean roadmap; reconcile original-task changes first")
    before = snapshot(roadmap)
    log_path = "history/ownership-log.md"
    log = before.get(log_path, b"# Ownership transfers\n").decode("utf-8")
    log += (f"\n## {datetime.now(timezone.utc).astimezone().date().isoformat()}\n\n- From SHA256: {previous_owner.upper()}\n"
            f"- To SHA256: {new_owner}\n- Authorization: {reason.strip()}\n")
    apply_changes(roadmap, before, {OWNER_FILE: owner_marker(current_session_id()).encode("utf-8"),
                                   log_path: log.encode("utf-8")})
    print("TRANSFER: COMPLETE; inspect, then commit ownership audit before product work")
    return 0


def verify_close_checkpoint(result: Validation) -> None:
    """Do not delete the only copy of an audit, including ignored/untracked evidence."""
    repo = result.roadmap.parents[2]
    relative = result.roadmap.relative_to(repo).as_posix()

    def git(*args: str) -> str:
        return git_output(repo, *args)

    if Path(git("rev-parse", "--show-toplevel")).resolve() != repo:
        raise RoadmapError("roadmap is not inside its Git worktree root")
    # Reject directory aliases before deletion, including Windows junctions.
    for path in [repo / ".pyxis", result.roadmap.parent, result.roadmap, *result.roadmap.rglob("*")]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RoadmapError(f"cannot close a roadmap containing a linked path: {path}")
    tracked = set(git("ls-tree", "-r", "--name-only", "HEAD", "--", relative).splitlines())
    if not {f"{relative}/.owner", f"{relative}/CONTROL.md"} <= tracked:
        raise RoadmapError("cannot close until the final audit roadmap is committed in HEAD")
    if git("status", "--porcelain=v1", "--untracked-files=all", "--ignored", "--", relative):
        raise RoadmapError("cannot close a roadmap with staged, unstaged, untracked, or ignored changes; commit the final audit first")
    actual_subject = git("log", "-1", "--format=%s", "--", relative)
    if actual_subject != result.rows[-1].commit_title:
        raise RoadmapError(
            f"last roadmap commit must be the final audit {result.rows[-1].commit_title!r}, found {actual_subject!r}"
        )


@serialized_mutation
def close_command(roadmap: Path, confirm: str) -> int:
    result = validate_roadmap(roadmap)
    print_diagnostics(result)
    if result.errors:
        print("CLOSE: REFUSED", file=sys.stderr)
        return 1
    roadmap_id = result.control_fields.get("Roadmap ID", "")
    if confirm != roadmap_id:
        raise RoadmapError(
            f"--confirm must exactly match roadmap ID {roadmap_id!r}; nothing was removed"
        )
    nonterminal = [row.task_id for row in result.rows if row.status not in TERMINAL_STATUSES]
    if nonterminal:
        raise RoadmapError(
            "cannot close a roadmap with nonterminal tasks: " + ", ".join(nonterminal)
        )
    if not result.rows or result.rows[-1].status != "DONE":
        raise RoadmapError("cannot close until the final audit task is DONE")
    acceptance = read_utf8(result.roadmap / "policies" / "acceptance.md")
    checkboxes = CHECKBOX.findall(acceptance)
    if not checkboxes:
        raise RoadmapError("cannot close without a global acceptance checklist")
    if " " in checkboxes:
        raise RoadmapError("cannot close while global acceptance contains unchecked items")

    resolved = result.roadmap.resolve()
    if resolved.parent.name != "roadmaps" or resolved.parent.parent.name != ".pyxis":
        raise RoadmapError("refuse to remove a path outside <git-root>/.pyxis/roadmaps/<slug>")
    verify_close_checkpoint(result)
    shutil.rmtree(resolved)
    print("CLOSE: COMPLETE")
    print(f"ROADMAP_REMOVED: {resolved}")
    print("Do not restore, inspect, or modify this roadmap again. Stage its deletion and finish the final commit.")
    return 0


def asset_root() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "pyxis-roadmap"


def runtime_destination(repo: Path) -> Path:
    return repo / ".pyxis" / "scripts" / "roadmap.py"


def sync_runtime(repo: Path, replace: bool) -> Path:
    source = Path(__file__).resolve()
    destination = runtime_destination(repo)
    if not repo.is_dir() or not (repo / ".git").exists():
        raise RoadmapError(f"not a Git worktree root: {repo}")
    if destination.exists() and source == destination.resolve():
        return destination
    if destination.exists() and destination.read_bytes() != source.read_bytes() and not replace:
        raise RoadmapError(
            f"runtime already exists with different content: {destination}; review it, then rerun with --replace"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_bytes() != source.read_bytes():
        shutil.copy2(source, destination)
    return destination


def init_command(repo: Path, slug: str, project: str) -> int:
    repo = repo.resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise RoadmapError(f"not a Git worktree root: {repo}")
    if not SLUG.fullmatch(slug) or "--" in slug:
        raise RoadmapError("slug must use lowercase letters, digits, and single hyphens (1-64 characters)")
    if not project.strip():
        raise RoadmapError("project title cannot be empty")
    assets = asset_root()
    if not assets.is_dir():
        raise RoadmapError("init must run from the installed/source skill copy because template assets are unavailable")
    destination = repo / ".pyxis" / "roadmaps" / slug
    if destination.exists():
        raise RoadmapError(f"roadmap already exists: {destination}")
    session_id = current_session_id()
    runtime = runtime_destination(repo)
    source = Path(__file__).resolve()
    if runtime.exists() and runtime.read_bytes() != source.read_bytes():
        raise RoadmapError(f"repository runtime differs from this skill: {runtime}; sync it explicitly first")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(assets, destination)
    for markdown in destination.rglob("*.md"):
        text = read_utf8(markdown)
        for name, value in (("ROADMAP_SLUG", slug), ("PROJECT", project)):
            text = text.replace("<" + name + ">", value).replace("{{PYXIS:" + name + "}}", value)
        markdown.write_text(text, encoding="utf-8", newline="\n")
    (destination / OWNER_FILE).write_text(owner_marker(session_id), encoding="utf-8", newline="\n")
    sync_runtime(repo, replace=False)
    print(f"Created roadmap: {destination}")
    print(f"Bound to current Codex task: {session_id}")
    print(f"Installed inspector: {runtime}")
    print("Resolve the remaining placeholders, add all task rows/files, then run check and inspect.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a .pyxis roadmap and repository-local inspector")
    init_parser.add_argument("--repo", type=Path, required=True)
    init_parser.add_argument("--slug", required=True)
    init_parser.add_argument("--project", required=True)

    check_parser = subparsers.add_parser("check", help="validate the complete roadmap without printing its content")
    check_parser.add_argument("roadmap", type=Path)

    inspect_parser = subparsers.add_parser("inspect", help="validate and emit a bounded resume packet")
    inspect_parser.add_argument("roadmap", type=Path)
    inspect_parser.add_argument("--max-lines", type=int, default=800)
    inspect_parser.add_argument("--max-bytes", type=int, default=48000)
    inspect_parser.add_argument("--recent", type=int, default=2)

    close_parser = subparsers.add_parser(
        "close", help="validate completion and permanently remove the current task's roadmap"
    )
    close_parser.add_argument("roadmap", type=Path)
    close_parser.add_argument("--confirm", required=True, help="exact roadmap ID")

    runtime_parser = subparsers.add_parser("sync-runtime", help="copy this inspector into a repository")
    runtime_parser.add_argument("--repo", type=Path, required=True)
    runtime_parser.add_argument("--replace", action="store_true")

    transition_parser = subparsers.add_parser("transition", help="transactionally complete, block, or resume the current task")
    transition_parser.add_argument("roadmap", type=Path)
    transition_parser.add_argument("--task", required=True)
    transition_parser.add_argument("--to", choices=("DONE", "BLOCKED", "READY", "SUPERSEDED"), required=True)
    transition_parser.add_argument("--evidence-file", type=Path)
    transition_parser.add_argument("--summary", default="")
    transition_parser.add_argument("--blocker", default="")
    transition_parser.add_argument("--commit", default="this task's commit")
    transition_parser.add_argument("--replacement", default="")

    insert_parser = subparsers.add_parser("insert", help="insert a remediation task and dependency before a nonterminal task")
    insert_parser.add_argument("roadmap", type=Path)
    insert_parser.add_argument("--before", required=True)
    insert_parser.add_argument("--task-file", type=Path, required=True)
    insert_parser.add_argument("--commit-title", required=True)

    transfer_parser = subparsers.add_parser("transfer", help="record an explicitly user-authorized ownership handoff")
    transfer_parser.add_argument("roadmap", type=Path)
    transfer_parser.add_argument("--from-owner", required=True, help="expected previous owner SHA256, not raw thread ID")
    transfer_parser.add_argument("--confirm", required=True, help="exact roadmap slug")
    transfer_parser.add_argument("--reason", required=True, help="record of the user's explicit handoff authorization")
    transfer_parser.add_argument("--authorized", action="store_true", help="assert explicit user authorization exists in this chat")

    recover_parser = subparsers.add_parser("recover", help="finish or roll back an interrupted roadmap mutation")
    recover_parser.add_argument("roadmap", type=Path)
    recover_parser.add_argument("--action", choices=("apply", "rollback"), required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            return init_command(args.repo, args.slug, args.project)
        if args.command == "check":
            return check_command(args.roadmap)
        if args.command == "inspect":
            if args.max_lines < 100:
                raise RoadmapError("--max-lines must be at least 100")
            if args.recent < 0:
                raise RoadmapError("--recent cannot be negative")
            if args.max_bytes < 1000:
                raise RoadmapError("--max-bytes must be at least 1000")
            return inspect_command(args.roadmap, args.max_lines, args.recent, args.max_bytes)
        if args.command == "close":
            return close_command(args.roadmap, args.confirm)
        if args.command == "sync-runtime":
            destination = sync_runtime(args.repo.resolve(), args.replace)
            print(f"Inspector synchronized: {destination}")
            return 0
        if args.command == "transition":
            return transition_command(args.roadmap, args.task, args.to, args.evidence_file, args.summary, args.blocker, args.commit, args.replacement)
        if args.command == "insert":
            return insert_command(args.roadmap, args.before, args.task_file, args.commit_title)
        if args.command == "transfer":
            return transfer_command(args.roadmap, args.from_owner, args.confirm, args.reason, args.authorized)
        if args.command == "recover":
            return recover_command(args.roadmap, args.action)
    except (RoadmapError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
