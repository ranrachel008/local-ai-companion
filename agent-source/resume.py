"""Determine whether a stopped Product Core execution can safely resume."""
from __future__ import annotations

from typing import Any


def resume_status(plan: dict[str, Any], execution_state: dict[str, Any] | None,
                  audit_tail: list[dict[str, Any]]) -> dict[str, Any]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or plan.get("status") != "READY_FOR_APPROVAL":
        return {"status": "INVALID_STATE", "last_completed_step": None,
                "stopped_step": None, "reason": "invalid or non-executable plan", "next_executable_step": None}
    ids = [step.get("step_id") for step in steps]
    if None in ids or len(ids) != len(set(ids)):
        return {"status": "INVALID_STATE", "last_completed_step": None,
                "stopped_step": None, "reason": "missing or duplicate step id", "next_executable_step": None}
    last = next((step for step in reversed(steps) if step.get("status") == "SUCCESS"), None)
    stopped = next((step for step in steps if step.get("status") in ("STOPPED", "FAILED", "WAITING_FOR_USER")), None)
    pending = next((step for step in steps if step.get("status") == "PENDING"), None)
    if all(step.get("status") == "SUCCESS" for step in steps):
        state = "ALREADY_COMPLETE"
    elif stopped:
        deviation = stopped.get("deviation_id") or stopped.get("reason")
        approvals = set((execution_state or {}).get("approved_deviations", []))
        state = "CAN_RESUME" if deviation in approvals else "WAITING_FOR_APPROVAL"
    elif pending and pending.get("approved") is True:
        state = "CAN_RESUME"
    else:
        state = "INVALID_STATE"
    audit_reason = next((row.get("metadata", {}).get("reason") for row in reversed(audit_tail)
                         if row.get("result") in ("STOPPED", "FAILED")), None)
    return {
        "status": state, "last_completed_step": last.get("step_id") if last else None,
        "stopped_step": stopped.get("step_id") if stopped else None,
        "reason": (stopped or {}).get("reason") or audit_reason,
        "user_approval_exists": state == "CAN_RESUME" and stopped is not None,
        "next_executable_step": (stopped or pending or {}).get("step_id") if state == "CAN_RESUME" else None,
    }

