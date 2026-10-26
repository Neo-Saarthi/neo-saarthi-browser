import hashlib
import ipaddress
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from db.encryption import decrypt_text, encrypt_text
from db.local_store.workspace_db import DEFAULT_DB_PATH, connect, initialize_database


class BrowserWorkflowError(ValueError):
    pass


class BrowserWorkflowStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def _database(self) -> sqlite3.Connection:
        initialize_database(self.db_path)
        return connect(self.db_path)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _steps(self, value: str) -> List[Dict[str, Any]]:
        return json.loads(decrypt_text(value))

    def _run_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        steps = self._steps(str(row["steps_json"]))
        index = int(row["next_step"])
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "name": decrypt_text(str(row["name"])),
            "status": row["status"],
            "next_step": index,
            "step": steps[index] if index < len(steps) else None,
            "total_steps": len(steps),
            "tab_id": row["tab_id"],
            "client_id": row["client_id"],
            "updated_at": row["updated_at"],
        }

    def start_recording(self, distributor_id: str, name: str) -> Dict[str, Any]:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 100:
            raise BrowserWorkflowError("Workflow name must contain 1 to 100 characters.")
        database = self._database()
        try:
            database.execute("BEGIN IMMEDIATE")
            active = database.execute(
                "SELECT id FROM browser_use_workflows WHERE distributor_id = ? AND status = 'recording'",
                (distributor_id,),
            ).fetchone()
            if active:
                raise BrowserWorkflowError("Finish the current browser recording first.")
            running = database.execute(
                "SELECT id FROM browser_use_workflow_runs WHERE distributor_id = ? AND status IN ('active', 'pending', 'interrupted') LIMIT 1",
                (distributor_id,),
            ).fetchone()
            if running:
                raise BrowserWorkflowError("Finish or cancel the active browser workflow before recording another.")
            workflow_id = str(uuid4())
            now = self._now()
            try:
                database.execute(
                    "INSERT INTO browser_use_workflows (id, distributor_id, name, status, steps_json, created_at, updated_at) VALUES (?, ?, ?, 'recording', '[]', ?, ?)",
                    (workflow_id, distributor_id, encrypt_text(clean_name), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise BrowserWorkflowError("Finish the current browser recording first.") from exc
            database.commit()
            return {"id": workflow_id, "name": clean_name, "status": "recording"}
        finally:
            database.close()

    def finish_recording(self, distributor_id: str) -> Dict[str, Any]:
        database = self._database()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT id, name, steps_json, skipped_actions, pending_actions FROM browser_use_workflows WHERE distributor_id = ? AND status = 'recording'",
                (distributor_id,),
            ).fetchone()
            if row is None:
                raise BrowserWorkflowError("No browser recording is active.")
            if int(row["pending_actions"]) > 0:
                raise BrowserWorkflowError("Wait for the browser actions in progress before saving this recording.")
            steps = self._steps(str(row["steps_json"]))
            if int(row["skipped_actions"]) > 0:
                raise BrowserWorkflowError("This recording contains unsupported or unsafe actions. Cancel it and record a simpler workflow.")
            if not steps or steps[0]["action"] != "open":
                raise BrowserWorkflowError("Open a page in a new managed tab before saving this recording.")
            database.execute(
                "UPDATE browser_use_workflows SET status = 'saved', updated_at = ? WHERE id = ?",
                (self._now(), row["id"]),
            )
            database.commit()
            return {"id": row["id"], "name": decrypt_text(str(row["name"])), "status": "saved", "steps": steps, "skipped_actions": row["skipped_actions"]}
        finally:
            database.close()

    def begin_action(self, distributor_id: str, client_id: str, action: str) -> Optional[str]:
        if action in {"snapshot", "browser_info", "get_permissions", "list_tabs", "list_windows", "download_status", "wait"}:
            return None
        database = self._database()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT id, client_id FROM browser_use_workflows WHERE distributor_id = ? AND status = 'recording'",
                (distributor_id,),
            ).fetchone()
            if row is None:
                return None
            if row["client_id"] is not None and row["client_id"] != client_id:
                database.execute(
                    "UPDATE browser_use_workflows SET skipped_actions = skipped_actions + 1, updated_at = ? WHERE id = ?",
                    (self._now(), row["id"]),
                )
                database.commit()
                return None
            database.execute(
                "UPDATE browser_use_workflows SET client_id = ?, pending_actions = pending_actions + 1, updated_at = ? WHERE id = ?",
                (client_id, self._now(), row["id"]),
            )
            database.commit()
            return str(row["id"])
        finally:
            database.close()

    def finish_action(self, recording_id: Optional[str], failed: bool) -> None:
        if recording_id is None:
            return
        database = self._database()
        try:
            database.execute(
                """
                UPDATE browser_use_workflows
                SET pending_actions = MAX(0, pending_actions - 1),
                    skipped_actions = skipped_actions + ?, updated_at = ?
                WHERE id = ? AND status = 'recording'
                """,
                (1 if failed else 0, self._now(), recording_id),
            )
            database.commit()
        finally:
            database.close()

    def preview_recording(self, distributor_id: str) -> Dict[str, Any]:
        database = self._database()
        try:
            row = database.execute(
                "SELECT id, name, client_id, steps_json, skipped_actions, pending_actions FROM browser_use_workflows WHERE distributor_id = ? AND status = 'recording'",
                (distributor_id,),
            ).fetchone()
            if row is None:
                raise BrowserWorkflowError("No browser recording is active.")
            return {"id": row["id"], "name": decrypt_text(str(row["name"])), "client_id": row["client_id"], "steps": self._steps(str(row["steps_json"])), "skipped_actions": row["skipped_actions"], "pending_actions": row["pending_actions"]}
        finally:
            database.close()

    def capture(
        self,
        distributor_id: str,
        client_id: str,
        action: str,
        arguments: Dict[str, Any],
        before_page: Optional[Dict[str, Any]],
        result: Dict[str, Any],
        recording_id: Optional[str] = None,
    ) -> None:
        if action in {"snapshot", "browser_info", "get_permissions", "list_tabs", "list_windows", "download_status", "wait"}:
            return
        database = self._database()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT id, client_id, steps_json FROM browser_use_workflows WHERE distributor_id = ? AND status = 'recording' AND (? IS NULL OR id = ?)",
                (distributor_id, recording_id, recording_id),
            ).fetchone()
            if row is None:
                return
            if row["client_id"] is not None and row["client_id"] != client_id:
                database.execute("UPDATE browser_use_workflows SET skipped_actions = skipped_actions + 1, updated_at = ? WHERE id = ?", (self._now(), row["id"]))
                database.commit()
                return
            if row["client_id"] is None:
                database.execute("UPDATE browser_use_workflows SET client_id = ? WHERE id = ?", (client_id, row["id"]))
            steps = self._steps(str(row["steps_json"]))
            step = self._safe_step(action, arguments, before_page, result) if action in {"open", "click", "fill"} else None
            if step is None or len(steps) >= 30:
                database.execute(
                    "UPDATE browser_use_workflows SET skipped_actions = skipped_actions + 1, updated_at = ? WHERE id = ?",
                    (self._now(), row["id"]),
                )
            else:
                if action == "open" and steps:
                    database.execute("UPDATE browser_use_workflows SET skipped_actions = skipped_actions + 1, updated_at = ? WHERE id = ?", (self._now(), row["id"]))
                    database.commit()
                    return
                steps.append(step)
                database.execute(
                    "UPDATE browser_use_workflows SET steps_json = ?, updated_at = ? WHERE id = ?",
                    (encrypt_text(json.dumps(steps, separators=(",", ":"))), self._now(), row["id"]),
                )
            database.commit()
        finally:
            database.close()

    def _safe_step(
        self,
        action: str,
        arguments: Dict[str, Any],
        before_page: Optional[Dict[str, Any]],
        result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if action == "open":
            url = str(arguments.get("url", ""))
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password or not re.fullmatch(r"/[A-Za-z/_-]*", parsed.path or "/"):
                return None
            hostname = parsed.hostname.casefold()
            if hostname == "localhost" or hostname.endswith((".local", ".internal", ".lan")):
                return None
            try:
                if not ipaddress.ip_address(hostname).is_global:
                    return None
            except ValueError:
                pass
            return {"action": "open", "url": url}
        if not before_page or not result.get("page"):
            return None
        element_id = str(arguments.get("element_id", ""))
        if action == "click" and result.get("changed") is not True:
            return None
        if action == "fill" and (result.get("filled") != element_id or result.get("submitted") is not bool(arguments.get("submit", False))):
            return None
        candidates = [item for item in before_page.get("interactive", []) if item.get("id") == element_id]
        if len(candidates) != 1:
            return None
        element = candidates[0]
        label = str(element.get("label", "")).strip()
        if not label or "[redacted" in label.casefold():
            return None
        href = element.get("href")
        target = {
            "label": label,
            "tag": element.get("tag"),
            "type": element.get("type"),
            "href_digest": hashlib.sha256(str(href).encode()).hexdigest() if href else None,
        }
        page_url = urlparse(str(before_page.get("url", "")))
        if page_url.scheme != "https" or not page_url.netloc:
            return None
        origin = f"{page_url.scheme}://{page_url.netloc}"
        path_digest = hashlib.sha256(page_url.path.encode()).hexdigest()
        if action == "fill":
            return {"action": "fill", "origin": origin, "path_digest": path_digest, "target": target, "needs_input": True, "submit": bool(arguments.get("submit", False))}
        return {"action": "click", "origin": origin, "path_digest": path_digest, "target": target, "expect_dialog": bool(arguments.get("expect_dialog", False))}

    def list_workflows(self, distributor_id: str) -> List[Dict[str, Any]]:
        database = self._database()
        try:
            rows = database.execute(
                "SELECT id, name, status, steps_json, skipped_actions, updated_at FROM browser_use_workflows WHERE distributor_id = ? ORDER BY updated_at DESC",
                (distributor_id,),
            ).fetchall()
            return [{"id": row["id"], "name": decrypt_text(str(row["name"])), "status": row["status"], "step_count": len(self._steps(str(row["steps_json"]))), "skipped_actions": row["skipped_actions"], "updated_at": row["updated_at"]} for row in rows]
        finally:
            database.close()

    def list_runs(self, distributor_id: str) -> List[Dict[str, Any]]:
        database = self._database()
        try:
            rows = database.execute(
                """
                SELECT run.id, run.workflow_id, run.status, run.next_step,
                       run.tab_id, run.client_id, run.updated_at,
                       workflow.name, workflow.steps_json
                FROM browser_use_workflow_runs AS run
                JOIN browser_use_workflows AS workflow ON workflow.id = run.workflow_id
                WHERE run.distributor_id = ? AND workflow.distributor_id = ?
                ORDER BY run.updated_at DESC LIMIT 30
                """,
                (distributor_id, distributor_id),
            ).fetchall()
            return [self._run_payload(row) for row in rows]
        finally:
            database.close()

    def cancel_recording(self, distributor_id: str) -> Dict[str, Any]:
        database = self._database()
        try:
            cursor = database.execute(
                "DELETE FROM browser_use_workflows WHERE distributor_id = ? AND status IN ('recording', 'interrupted')",
                (distributor_id,),
            )
            database.commit()
            return {"cancelled": cursor.rowcount > 0}
        finally:
            database.close()

    def delete_workflow(self, distributor_id: str, workflow_id: str) -> Dict[str, Any]:
        database = self._database()
        try:
            active = database.execute(
                "SELECT id FROM browser_use_workflow_runs WHERE distributor_id = ? AND workflow_id = ? AND status IN ('active', 'pending', 'interrupted') LIMIT 1",
                (distributor_id, workflow_id),
            ).fetchone()
            if active:
                raise BrowserWorkflowError("Finish or cancel the workflow run before deleting its template.")
            cursor = database.execute(
                "DELETE FROM browser_use_workflows WHERE distributor_id = ? AND id = ? AND status = 'saved'",
                (distributor_id, workflow_id),
            )
            database.commit()
            return {"deleted": cursor.rowcount > 0}
        finally:
            database.close()

    def workflow(self, distributor_id: str, workflow_id: str) -> Dict[str, Any]:
        database = self._database()
        try:
            row = database.execute(
                "SELECT id, name, client_id, status, steps_json, skipped_actions FROM browser_use_workflows WHERE distributor_id = ? AND id = ?",
                (distributor_id, workflow_id),
            ).fetchone()
            if row is None:
                raise BrowserWorkflowError("Browser workflow was not found.")
            return {"id": row["id"], "name": decrypt_text(str(row["name"])), "client_id": row["client_id"], "status": row["status"], "steps": self._steps(str(row["steps_json"])), "skipped_actions": row["skipped_actions"]}
        finally:
            database.close()

    def start_run(self, distributor_id: str, workflow_id: str, client_id: Optional[str]) -> Dict[str, Any]:
        workflow = self.workflow(distributor_id, workflow_id)
        if workflow["status"] != "saved":
            raise BrowserWorkflowError("Finish recording before running this workflow.")
        if workflow["client_id"] != client_id:
            raise BrowserWorkflowError("Run this workflow in the browser profile where it was recorded.")
        run_id = str(uuid4())
        database = self._database()
        try:
            database.execute("BEGIN IMMEDIATE")
            recording = database.execute(
                "SELECT id FROM browser_use_workflows WHERE distributor_id = ? AND status = 'recording' LIMIT 1",
                (distributor_id,),
            ).fetchone()
            if recording:
                raise BrowserWorkflowError("Finish or cancel the current recording before running a workflow.")
            running = database.execute(
                "SELECT id FROM browser_use_workflow_runs WHERE distributor_id = ? AND client_id = ? AND status IN ('active', 'pending', 'interrupted') LIMIT 1",
                (distributor_id, client_id),
            ).fetchone()
            if running:
                raise BrowserWorkflowError("Finish or cancel the existing workflow in this browser profile first.")
            try:
                database.execute(
                    "INSERT INTO browser_use_workflow_runs (id, distributor_id, workflow_id, status, next_step, client_id, updated_at) VALUES (?, ?, ?, 'active', 0, ?, ?)",
                    (run_id, distributor_id, workflow_id, client_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise BrowserWorkflowError("Finish or cancel the existing workflow in this browser profile first.") from exc
            database.commit()
        finally:
            database.close()
        return self.run(distributor_id, run_id)

    def run(self, distributor_id: str, run_id: str) -> Dict[str, Any]:
        database = self._database()
        try:
            row = database.execute(
                """
                SELECT run.id, run.workflow_id, run.status, run.next_step,
                       run.tab_id, run.client_id, run.updated_at,
                       workflow.name, workflow.steps_json
                FROM browser_use_workflow_runs AS run
                JOIN browser_use_workflows AS workflow ON workflow.id = run.workflow_id
                WHERE run.distributor_id = ? AND workflow.distributor_id = ? AND run.id = ?
                """,
                (distributor_id, distributor_id, run_id),
            ).fetchone()
            if row is None:
                raise BrowserWorkflowError("Browser workflow run was not found.")
            return self._run_payload(row)
        finally:
            database.close()

    def mark_pending(self, distributor_id: str, run_id: str, step_index: int) -> None:
        database = self._database()
        try:
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = 'pending', updated_at = ? WHERE distributor_id = ? AND id = ? AND status = 'active' AND next_step = ?",
                (self._now(), distributor_id, run_id, step_index),
            )
            if cursor.rowcount != 1:
                raise BrowserWorkflowError("Workflow step changed; read the run again.")
            database.commit()
        finally:
            database.close()

    def finish_step(self, distributor_id: str, run_id: str, step_index: int, tab_id: Optional[str], total_steps: int) -> None:
        database = self._database()
        try:
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = ?, next_step = ?, tab_id = COALESCE(?, tab_id), updated_at = ? WHERE distributor_id = ? AND id = ? AND status = 'pending' AND next_step = ?",
                ("completed" if step_index + 1 >= total_steps else "active", step_index + 1, tab_id, self._now(), distributor_id, run_id, step_index),
            )
            if cursor.rowcount != 1:
                raise BrowserWorkflowError("The workflow changed before this step could be saved.")
            database.commit()
        finally:
            database.close()

    def interrupt(self, distributor_id: str, run_id: str) -> None:
        database = self._database()
        try:
            database.execute(
                "UPDATE browser_use_workflow_runs SET status = 'interrupted', updated_at = ? WHERE distributor_id = ? AND id = ? AND status = 'pending'",
                (self._now(), distributor_id, run_id),
            )
            database.commit()
        finally:
            database.close()

    def cancel_run(self, distributor_id: str, run_id: str) -> Dict[str, Any]:
        run = self.run(distributor_id, run_id)
        if run["status"] == "pending":
            raise BrowserWorkflowError("An action is in progress; wait for its result before cancelling.")
        if run["status"] not in {"active", "interrupted"}:
            return run
        database = self._database()
        try:
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = 'cancelled', updated_at = ? WHERE distributor_id = ? AND id = ? AND status = ?",
                (self._now(), distributor_id, run_id, run["status"]),
            )
            if cursor.rowcount != 1:
                raise BrowserWorkflowError("The workflow changed while cancellation was requested.")
            database.commit()
        finally:
            database.close()
        return self.run(distributor_id, run_id)

    def begin_takeover(self, distributor_id: str, run_id: str) -> Dict[str, Any]:
        run = self.run(distributor_id, run_id)
        if run["status"] not in {"active", "interrupted"} or not run["tab_id"]:
            raise BrowserWorkflowError("Only a browser workflow with an open tab can be taken over.")
        database = self._database()
        try:
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = 'pending', updated_at = ? WHERE distributor_id = ? AND id = ? AND status = ?",
                (self._now(), distributor_id, run_id, run["status"]),
            )
            if cursor.rowcount != 1:
                raise BrowserWorkflowError("The workflow changed while takeover was requested.")
            database.commit()
        finally:
            database.close()
        return run

    def finish_takeover(self, distributor_id: str, run_id: str, released: bool) -> Dict[str, Any]:
        database = self._database()
        try:
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = ?, updated_at = ? WHERE distributor_id = ? AND id = ? AND status = 'pending'",
                ("cancelled" if released else "interrupted", self._now(), distributor_id, run_id),
            )
            if cursor.rowcount != 1:
                raise BrowserWorkflowError("The workflow changed before takeover finished.")
            database.commit()
        finally:
            database.close()
        return self.run(distributor_id, run_id)

    def recover_interrupted(self) -> int:
        database = self._database()
        try:
            database.execute(
                "UPDATE browser_use_workflows SET status = 'interrupted', updated_at = ? WHERE status = 'recording'",
                (self._now(),),
            )
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = 'interrupted', updated_at = ? WHERE status IN ('pending', 'active')",
                (self._now(),),
            )
            database.commit()
            return cursor.rowcount
        finally:
            database.close()

    def review_interrupted(self, distributor_id: str, run_id: str, completed: bool, tab_id: Optional[str] = None) -> Dict[str, Any]:
        run = self.run(distributor_id, run_id)
        if run["status"] != "interrupted":
            raise BrowserWorkflowError("Only an interrupted step can be reviewed.")
        if completed and run["step"] and run["step"]["action"] == "open" and not (run["tab_id"] or tab_id):
            raise BrowserWorkflowError("Provide the opened tab ID after inspecting browser tabs.")
        database = self._database()
        try:
            next_step = run["next_step"] + (1 if completed else 0)
            status = "completed" if next_step >= run["total_steps"] else "active"
            cursor = database.execute(
                "UPDATE browser_use_workflow_runs SET status = ?, next_step = ?, tab_id = COALESCE(?, tab_id), updated_at = ? WHERE distributor_id = ? AND id = ? AND status = 'interrupted'",
                (status, next_step, tab_id, self._now(), distributor_id, run_id),
            )
            if cursor.rowcount != 1:
                raise BrowserWorkflowError("The workflow changed before this review could be saved.")
            database.commit()
        finally:
            database.close()
        return self.run(distributor_id, run_id)


browser_workflow_store = BrowserWorkflowStore()
