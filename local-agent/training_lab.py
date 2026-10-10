"""Autonomous Minecraft training lab v1.

Runs controlled curriculum *in the actual Minecraft world*, using the normal
practice executor and persistent SQLite memory. Never spawns items, teleports,
resets a world, or awards imagined results. The curriculum chooses evaluation
objectives; Ollama chooses the physical actions and learns from outcomes.

Training is a separate controller mode. Stop the regular brain first. A file
lock prevents new-version brain/trainer processes controlling the body together.
"""
import argparse
import contextlib
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

import practice_engine as practice
from training_progress import assess_training
from action_log import log_event, start_session
from memory_store import store

BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "data" / "training_reports"
CURRICULUM = ("observe", "navigate", "orient", "mine", "collect")
TRAINING_BUILD = "minecraft-training-lab-v1"
# Safety filter for selecting a training resource, NOT a gameplay strategy.
# No chests, machines, crops, player buildings or precious ores are harvested.
PRACTICE_MATERIALS = ("minecraft:dirt", "minecraft:stone", "minecraft:grass_block",
                      "minecraft:gravel", "minecraft:sand", "minecraft:oak_log",
                      "minecraft:birch_log", "minecraft:spruce_log",
                      "minecraft:jungle_log", "minecraft:acacia_log",
                      "minecraft:dark_oak_log", "minecraft:mangrove_log",
                      "minecraft:cherry_log")
PLANNER_INSTRUCTIONS = """You are Chat's local Minecraft learning brain.
You are in a real Minecraft training session. This is NOT a roleplay.
The host supplies the objective and server-observed world facts.
Think of one hypothesis, return ONE executable JSON primitive.
Minecraft executes it and reports physical evidence. Change your hypothesis
after failure; do not repeat identical actions without new evidence.
Actions: scan_blocks (contains/exact, radius, limit, exposed_only),
look_at (x,y,z), move_to (standable x,y,z), mine (x,y,z,expected_block),
collect, and idle. Never invent target positions.
The supplied allowed actions and observed candidate coordinates are binding.
Use JSON with action, intent, hypothesis, and action-specific fields.
Respond with ONE JSON object only. No markdown or imaginary successes.
/no_think
"""


@contextlib.contextmanager
def exclusive_controller():
    """Cross-platform OS lock; do not delete lock files belonging to a run."""
    path = BASE_DIR / "data" / "body_controller.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                "Another companion controller is active. Stop run.bat before training."
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _position(state):
    return [float(state[k]) for k in ("x", "y", "z")]


def _distance(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def _item_count(inventory, item):
    return sum(int(row.get("count", 0)) for row in inventory
               if row.get("item") == item)


@dataclass
class Challenge:
    name: str
    objective: str
    target: dict
    allowed: tuple
    dimension: str


class RealWorld:
    def snapshot(self, allow_busy=False):
        state = practice._get("/state")
        if state.get("serverState") != "running":
            raise RuntimeError("Minecraft is not open with a loaded companion")
        if state.get("jobActive") and not allow_busy:
            raise RuntimeError(
                "A physical job is already running. Stop the normal brain and its task."
            )
        return {
            "state": state,
            "inventory": practice._inventory(),
            "blocks": (practice._get("/nearby-blocks").get("blocks") or []),
            "entities": (practice._get("/nearby-entities").get("entities") or []),
        }

    def at(self, x, y, z):
        return practice._post("/block-at", {"x": int(x), "y": int(y), "z": int(z)})

    def find(self, block_id, radius=7):
        ids = ([block_id] if isinstance(block_id, str) else list(block_id))
        return practice._post("/find-blocks", {
            "exact": ids, "radius": radius,
            "limit": 64, "exposed_only": True,
        }).get("blocks", [])

    def execute(self, plan):
        return practice.execute_plan(plan)

    def job_state(self):
        return practice._get("/state")

    def stop_job(self):
        return practice._post("/stop-action")


class OllamaPlanner:
    """Uses only the native local Ollama API, never OpenAI or an auto fallback."""
    def __init__(self):
        base = os.getenv("COMPANION_LOCAL_BASE_URL",
                         "http://127.0.0.1:11434/v1").rstrip("/")
        self.root = base[:-3].rstrip("/") if base.endswith("/v1") else base
        self.model = os.getenv("COMPANION_LOCAL_MODEL", "qwen3:8b")
        models = requests.get(base + "/models", timeout=4)
        models.raise_for_status()
        available = {row.get("id") for row in models.json().get("data", [])}
        if self.model not in available:
            raise RuntimeError(
                "Ollama model %s unavailable. Available: %s" %
                (self.model, ", ".join(sorted(str(v) for v in available)))
            )

    def propose(self, prompt):
        response = requests.post(
            self.root + "/api/chat",
            json={"model": self.model, "stream": False, "think": False,
                  "format": "json",
                  "messages": [
                      {"role": "system", "content": PLANNER_INSTRUCTIONS},
                      {"role": "user", "content": prompt},
                  ],
                  "options": {"num_predict": 240, "temperature": 0.2}},
            timeout=50,
        )
        response.raise_for_status()
        return practice.parse_plan(
            (response.json().get("message") or {}).get("content")
        )


def _walkable(world, xyz):
    x, y, z = xyz
    feet = world.at(x, y, z)
    head = world.at(x, y + 1, z)
    support = world.at(x, y - 1, z)
    if any(a.get("ok") is False for a in (feet, head, support)):
        return False
    # The normal move_to primitive rejects occupied feet coordinates, even
    # for replaceable plants. Match that executor contract exactly.
    return (bool(feet.get("air")) and bool(head.get("air"))
            and bool(support.get("solid_support_up")))


def _resource_candidates(world, origin):
    """Observe actual exposed resources and find walkable mining approaches.

    This only chooses safe test scenarios. The local model still decides what
    action to try and the game verifies whether it worked.
    """
    logs = tuple(v for v in PRACTICE_MATERIALS if v.endswith("_log"))
    terrain = tuple(v for v in PRACTICE_MATERIALS if not v.endswith("_log"))
    seen = world.find(logs, radius=10) + world.find(terrain, radius=10)
    usable = []
    for block in seen:
        pos = [block.get(k) for k in ("x", "y", "z")]
        if block.get("type") not in PRACTICE_MATERIALS:
            continue
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in pos):
            continue
        if abs(pos[1] - origin[1]) > 3.0:
            continue
        distance = _distance(origin, [v + 0.5 for v in pos])
        if 1.0 <= distance <= 9.5:
            usable.append((distance, block["type"], pos))
    usable.sort(key=lambda sample: (not sample[1].endswith("_log"), sample[0]))
    candidates = []
    for _, block_type, pos in usable[:24]:
        standable = []
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbour = [pos[0] + dx, pos[1], pos[2] + dz]
            if _walkable(world, neighbour):
                standable.append(neighbour)
        if standable:
            candidates.append({"type": block_type, "pos": pos,
                               "standable_candidates": standable})
    return candidates


def _select_challenge(name, world, snapshot, rng, preferred_resource=None):
    state, blocks = snapshot["state"], snapshot["blocks"]
    origin = _position(state)
    dim = str(state.get("dimension") or "unknown")
    candidates = [
        b for b in blocks
        if b.get("type") and b.get("nearestX") is not None
        and 1.0 <= float(b.get("nearestDistance") or 0) <= 7.0
    ]
    if name == "observe":
        if not candidates:
            return None
        b = rng.choice(candidates[:8])
        return Challenge(name, "Locate the observed block type with a scan",
                         {"type": b["type"]}, ("scan_blocks",), dim)
    if name == "orient":
        # Look at the SAME physical resource reached by the immediately
        # preceding navigation exercise, if a fresh scan still sees it.
        if preferred_resource:
            kind = preferred_resource.get("type")
            xyz = preferred_resource.get("pos")
            if kind and isinstance(xyz, list) and len(xyz) == 3:
                fresh = world.find(kind, radius=9)
                if any(b.get("type") == kind and
                       [b.get(k) for k in ("x", "y", "z")] == xyz
                       for b in fresh):
                    return Challenge(
                        name, "Face the observed resource just approached",
                        {"type": kind, "pos": xyz}, ("look_at",), dim)
        facing = [b for b in candidates if
                  math.hypot(float(b["nearestX"]) - origin[0],
                             float(b["nearestZ"]) - origin[2]) > 0.75]
        if not facing:
            return None
        b = rng.choice(facing[:8])
        return Challenge(name, "Turn horizontally toward the observed target block",
                         {"type": b["type"],
                          "pos": [int(b["nearestX"]), int(b["nearestY"]),
                                  int(b["nearestZ"])]},
                         ("look_at",), dim)
    if name in {"navigate", "mine", "gather_wood"}:
        resources = _resource_candidates(world, origin)
        if name == "navigate":
            # Navigate toward a *resource*, not an unrelated random waypoint.
            # Choose an actual, walkable standing spot next to the target.
            # Near-enough resources are for mining practice instead.
            for resource in resources:
                center = [v + 0.5 for v in resource["pos"]]
                before_resource = _distance(origin, center)
                if before_resource < 3.5:
                    continue
                choices = []
                for standing in resource["standable_candidates"]:
                    approach = [standing[0] + 0.5, standing[1],
                                standing[2] + 0.5]
                    if (_distance(origin, approach) >= 2.0 and
                        before_resource - _distance(approach, center) >= 0.6):
                        choices.append(( _distance(origin, approach), standing))
                if choices:
                    choices.sort(key=lambda row: row[0])
                    return Challenge(
                        name, "Approach the observed " + resource["type"] +
                        " by walking to a world-verified standing position beside it",
                        {"pos": choices[0][1], "resource_pos": resource["pos"],
                         "resource_type": resource["type"],
                         "standable_candidates": resource["standable_candidates"]},
                        ("move_to",), dim)
            return None
        if name == "mine":
            if not resources:
                return None
            selected = next((
                r for r in resources if preferred_resource
                and r["pos"] == preferred_resource.get("pos")
                and r["type"] == preferred_resource.get("type")
            ), resources[0])
            return Challenge(
                name, "Mine a confirmed exposed natural block physically",
                selected, ("mine", "scan_blocks", "look_at", "move_to"), dim)
        # Stage 2 must choose an actual existing log, not whatever terrain
        # happens to be nearest. It must prove the item was really acquired.
        wood = next((r for r in resources if r["type"].endswith("_log")), None)
        if wood is None:
            return None
        return Challenge(
            name, "Independently gather one naturally occurring log, from finding it "
            "to physical mining and possession in companion inventory",
            wood, ("scan_blocks", "look_at", "move_to", "mine", "collect"), dim)
    if name == "collect":
        entities = [e for e in snapshot["entities"]
                    if e.get("droppedItem")
                    and float(e.get("distance") or 99) <= 5.5]
        if not entities:
            return None
        e = rng.choice(entities[:5])
        return Challenge(name, "Physically pick up the observed dropped item",
                         {"item": e["droppedItem"],
                          "pos": [e["x"], e["y"], e["z"]]},
                         ("collect",), dim)
    raise ValueError("unknown curriculum task: " + name)


def _ground_confirmed_coordinates(plan, challenge):
    """Canonicalize Qwen's textual coordinate mention, never invent a target.

    When Qwen says "the target position [54, 87, 180]" but omits the numeric
    JSON fields, accept that *exact* triple only if Minecraft already supplied
    those coordinates as the current challenge's target. Any other triple
    is rejected normally. This is protocol translation, not gameplay policy.
    """
    if not isinstance(plan, dict):
        return plan
    if plan.get("action") not in {"move_to", "look_at", "mine"}:
        return plan
    if all(plan.get(k) is not None for k in ("x", "y", "z")):
        return plan
    target = challenge.target.get("pos")
    if not isinstance(target, (list, tuple)) or len(target) != 3:
        return plan
    text = str(plan.get("hypothesis") or "")
    matches = re.findall(
        r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]",
        text,
    )
    for match in matches:
        observed = [int(v) for v in match]
        if observed == list(target):
            fixed = dict(plan)
            for axis, value in zip(("x", "y", "z"), observed):
                if fixed.get(axis) is None:
                    fixed[axis] = value
            fixed["_schema_binding"] = "exact_triple_confirmed_in_world_objective"
            return fixed
    return plan


def _bind_observed_mining_identity(plan, challenge, observed_extra=()):
    """Translate an incomplete mining *schema*, never choose a game action.

    The model must independently request mine at exact world-confirmed XYZ.
    If it only supplied a fuzzy identity such as expected_contains="_log",
    bind that declared identity to the exact ID observed there. Explicit
    contradictory IDs and unobserved positions are never repaired.
    """
    if not isinstance(plan, dict) or plan.get("action") != "mine":
        return plan
    if plan.get("expected_block"):
        return plan
    try:
        xyz = [float(plan[k]) for k in ("x", "y", "z")]
        if not all(math.isfinite(v) for v in xyz):
            return plan
    except (ValueError, TypeError, KeyError, OverflowError):
        return plan
    known = [challenge.target] + list(observed_extra)
    matched = next((
        b for b in known if isinstance(b, dict)
        and isinstance(b.get("pos"), (list, tuple))
        and len(b["pos"]) == 3 and b.get("type") in PRACTICE_MATERIALS
        and all(abs(float(b["pos"][i]) - xyz[i]) <= 0.05
                for i in range(3))
    ), None)
    if matched is None:
        return plan
    kind = matched["type"]
    asserted = plan.get("expected_contains")
    if asserted:
        if isinstance(asserted, list):
            matches = any(str(a).lower() in kind.lower() for a in asserted)
        else:
            matches = str(asserted).lower() in kind.lower()
        if not matches:
            return plan
    fixed = dict(plan)
    fixed["expected_block"] = kind
    fixed["_schema_binding"] = "exact_observed_block_identity"
    return fixed


def _interface_feedback(reason, challenge):
    """Schema instructions only; does not give the model a mining plan."""
    feedback = {
        "unsupported_action": "Choose an action from permitted_actions.",
        "missing_xyz": "Supply finite numeric x, y, z fields for the selected action.",
        "unobserved_coordinates": "Coordinates must match a Minecraft-observed target or standable approach.",
        "block_identity_mismatch": "For mine, expected_block must exactly identify the observed block at those coordinates.",
        "repeat_without_new_evidence": "The identical action failed or was rejected. Revise the hypothesis, query, or coordinates using new world evidence.",
    }
    note = feedback.get(reason, "Review the rejection and choose a different supported experiment.")
    if reason == "block_identity_mismatch":
        note += " The observed target's exact type is in target_confirmed_by_minecraft."
    return note


def _plan_allowed(plan, challenge, observed_extra=()):
    """Reject invented coordinates; no automatic hard-coded recovery moves."""
    if not isinstance(plan, dict) or plan.get("action") not in challenge.allowed:
        return False, "unsupported_action"
    action = plan["action"]
    if action == "scan_blocks":
        try:
            q = practice.scan_query(plan)
        except (TypeError, ValueError, OverflowError):
            return False, "invalid_scan"
        return (bool(q["contains"] or q["exact"]),
                "ok" if q["contains"] or q["exact"] else "empty_scan_query")
    if action == "collect":
        return True, "ok"
    try:
        pos = [float(plan[k]) for k in ("x", "y", "z")]
        if not all(math.isfinite(v) for v in pos):
            return False, "nonfinite_coordinates"
    except (ValueError, TypeError, KeyError, OverflowError):
        return False, "missing_xyz"
    known = [challenge.target.get("pos")] + [
        b.get("pos") if isinstance(b, dict) else b for b in observed_extra
    ]
    if action == "move_to":
        # Only standable positions checked by block-at are valid destinations.
        known = list(challenge.target.get("standable_candidates") or [])
        if challenge.name == "navigate":
            known = [challenge.target.get("pos")]
    matched = any(
        isinstance(p, (tuple, list)) and len(p) == 3
        and all(abs(pos[i] - float(p[i])) <= 0.05 for i in range(3))
        for p in known
    )
    if not matched:
        return False, "unobserved_coordinates"
    if action == "mine":
        expected = challenge.target.get("type")
        matching_blocker = next(
            (b for b in observed_extra if isinstance(b, dict)
             and isinstance(b.get("pos"), list)
             and all(abs(pos[i] - b["pos"][i]) <= 0.05 for i in range(3))), None
        )
        if matching_blocker:
            expected = matching_blocker["type"]
            if expected not in PRACTICE_MATERIALS:
                return False, "unsafe_obstruction_material"
        if plan.get("expected_block") != expected:
            return False, "block_identity_mismatch"
    return True, "ok"


def _observed_obstruction(execution):
    """Extract a blocker only when the Minecraft raycast identified it."""
    reason = str((execution.get("result") or {}).get("reason") or "")
    if not reason.startswith("mining_blocked|"):
        return None
    fields = dict(part.split("=", 1) for part in reason.split("|")[1:]
                  if "=" in part)
    try:
        xyz = [int(v) for v in fields["at"].split(",")]
    except (KeyError, TypeError, ValueError):
        return None
    if len(xyz) != 3 or not fields.get("block"):
        return None
    return {"type": fields["block"], "pos": xyz}


def _bearing_error(state, pos):
    dx = float(pos[0]) + 0.5 - float(state["x"])
    dz = float(pos[2]) + 0.5 - float(state["z"])
    if math.hypot(dx, dz) < 0.1:
        return 0.0
    wanted = math.degrees(math.atan2(-dx, dz))
    actual = float(state.get("yaw", 0))
    return abs((actual - wanted + 180) % 360 - 180)


# Match the Minecraft server's physical job type, not merely a terminal
# response received sometime later. The existing controller lock prevents
# simultaneous local brains issuing body commands.
PHYSICAL_JOB_TYPES = {"move_to": "MOVE", "move_forward": "MOVE",
                      "mine": "MINE", "collect": "COLLECT", "place": "PLACE"}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}


def _settle_pending_job(world, execution, timeout_seconds=35.0, poll_seconds=0.5):
    """Resolve a started Minecraft job before advancing the curriculum.

    A RUNNING/repath_pending result is not failure or success. Poll the live
    server; continue if it finishes. After a bounded deadline cancel only the
    active job matching our own action. If cancellation cannot be confirmed,
    keep the status pending and halt the session to avoid parallel commands.
    """
    if execution.get("status") != "pending":
        return execution
    action = (execution.get("plan") or {}).get("action")
    expected_type = PHYSICAL_JOB_TYPES.get(action)
    if not expected_type:
        return execution

    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    def resolved(state):
        if not isinstance(state, dict) or state.get("ok") is False:
            return None
        if state.get("jobActive"):
            return None
        last = state.get("lastJob") or {}
        if (str(last.get("type") or "").upper() != expected_type
                or str(last.get("state") or "").upper() not in TERMINAL_STATES):
            return None
        terminal = str(last["state"]).upper()
        return {
            **execution,
            "status": "settled",
            "ok": terminal == "COMPLETED",
            "result": {"state": terminal,
                       "reason": str(last.get("reason") or "unknown"),
                       "pos": [state.get(v) for v in ("x", "y", "z")]},
        }

    while True:
        state = world.job_state()
        final = resolved(state)
        if final is not None:
            return final
        if not isinstance(state, dict) or state.get("ok") is False:
            return execution
        if not state.get("jobActive") or str(state.get("jobType") or "").upper() != expected_type:
            return execution
        if time.monotonic() >= deadline:
            break
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    # Timeouts are ordinary failed *physical* trials once Minecraft confirms
    # the job was cancelled. No additional action is launched until then.
    log_event("training", "job_deadline", action=action,
              last_reason=state.get("jobReason"), timeout_seconds=timeout_seconds)
    try:
        stop = world.stop_job()
        if isinstance(stop, dict) and stop.get("ok") is False:
            return execution
        for _ in range(6):
            state = world.job_state()
            final = resolved(state)
            if final is not None:
                final["result"]["training_timeout"] = True
                log_event("training", "job_timeout_cancelled",
                          action=action, terminal=final["result"])
                return final
            if not state.get("jobActive"):
                return execution
            time.sleep(0.25)
    except (KeyError, TypeError, ValueError, requests.RequestException) as error:
        log_event("training", "job_cancel_check_failed",
                  action=action, error=str(error)[:180])
    return execution


def _verify(challenge, before, after, execution, previous_attempts=(),
            initial_inventory=None):
    """Training success must come from actual world state, not plan text."""
    plan = execution.get("plan") or {}
    action = plan.get("action")
    if execution.get("status") == "pending":
        return False, "pending_job_unverified"
    if not execution.get("ok"):
        return False, str((execution.get("result") or {}).get("reason")
                          or (execution.get("result") or {}).get("error")
                          or "minecraft_action_failed")
    if before["state"].get("dimension") != after["state"].get("dimension"):
        return False, "dimension_changed"
    target = challenge.target
    if challenge.name == "gather_wood":
        # Stage 2 is a compound goal. Never credit merely describing a
        # solution, reaching a tree, or collecting unrelated items.
        def mined_target(a):
            p = a.get("plan") or {}
            m = practice._parse_mining_metrics(a.get("result") or {})
            return (p.get("action") == "mine" and m is not None
                    and m.get("block") == target["type"]
                    and (a.get("result") or {}).get("state") == "COMPLETED"
                    and all(p.get(k) == v for k, v in
                            zip(("x", "y", "z"), target["pos"])))
        previously_mined = any(mined_target(a) for a in previous_attempts
                               if a.get("status") in {"completed", "progress"})
        current_mined = mined_target({
            "plan": plan, "result": execution.get("result") or {}
        }) if execution.get("ok") else False
        acquired = _item_count(after["inventory"], target["type"]) > _item_count(
            initial_inventory if initial_inventory is not None
            else before["inventory"], target["type"])
        if acquired and (previously_mined or current_mined):
            return True, "wood_gathered_from_verified_mine_and_inventory"
        return False, "wood_gathering_in_progress"
    if challenge.name == "observe":
        blocks = (execution.get("result") or {}).get("blocks") or []
        return (action == "scan_blocks" and
                any(b.get("type") == target["type"] for b in blocks),
                "observed_matching_world_block" if blocks else "no_matching_block")
    if challenge.name == "orient":
        error = _bearing_error(after["state"], target["pos"])
        return (action == "look_at" and error <= 22.5,
                "yaw_error_degrees=%.1f" % error)
    if challenge.name == "navigate":
        prior = _distance(_position(before["state"]), target["pos"])
        current = _distance(_position(after["state"]), target["pos"])
        resource_pos = target.get("resource_pos")
        if resource_pos is None:
            # Preserve evaluation of old single-waypoint examples in tests;
            # new challenges always have an observed resource as well.
            return (action == "move_to" and current <= 2.5
                    and prior - current > 0.5,
                    "distance_before=%.2f after=%.2f" % (prior, current))
        center = [v + 0.5 for v in resource_pos]
        resource_before = _distance(_position(before["state"]), center)
        resource_after = _distance(_position(after["state"]), center)
        passed = (action == "move_to" and current <= 2.5
                  and prior - current > 0.5
                  and resource_after <= 4.5
                  and resource_before - resource_after > 0.5)
        return passed, (
            "waypoint_before=%.2f after=%.2f|resource=%s|"
            "resource_before=%.2f after=%.2f" %
            (prior, current, target["resource_type"],
             resource_before, resource_after))
    if challenge.name == "mine":
        result = execution.get("result") or {}
        metrics = practice._parse_mining_metrics(result)
        mined = bool(metrics and metrics.get("block") == target["type"])
        target_match = (action == "mine" and
                        [int(plan[k]) for k in ("x", "y", "z")] == target["pos"])
        return (mined and target_match and
                result.get("state") == "COMPLETED",
                str(result.get("reason") or "mining_not_verified"))
    if challenge.name == "collect":
        previous = _item_count(before["inventory"], target["item"])
        current = _item_count(after["inventory"], target["item"])
        return (action == "collect" and current > previous,
                "target_item_before=%d after=%d" % (previous, current))
    return False, "unknown_task"


def _verified_history(intent, limit=12):
    """Exclude legacy interface-only rejections without erasing user memory."""
    records = store.recent_skill_trials(intent, limit)
    genuine = []
    for entry in records:
        try:
            experiments = json.loads(entry.get("actions") or "{}")["experiments"]
            if any(e.get("status") in {"completed", "failed"}
                   for e in experiments if isinstance(e, dict)):
                genuine.append(entry)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return genuine


def _record_episode(challenge, attempts, passed, elapsed, initial, reason):
    intent = "training." + challenge.name
    # Condense huge scan responses instead of truncating JSON in mid-object.
    # Graduation needs durable, parseable proof of physical execution.
    reduced = []
    for a in attempts[-8:]:
        compact = dict(a)
        result = compact.get("result")
        if isinstance(result, dict):
            compact["result"] = {
                k: v for k, v in result.items()
                if k in {"state", "reason", "ok", "error"}
            }
            if "blocks" in result:
                compact["result"]["observed_count"] = len(result["blocks"])
        if isinstance(compact.get("plan"), dict):
            compact["plan"] = {
                k: v for k, v in compact["plan"].items()
                if k in {"action", "intent", "x", "y", "z",
                         "expected_block", "exact", "contains"}
            }
        compact.pop("hypothesis", None)
        reduced.append(compact)
    actions = json.dumps({
        "dimension": challenge.dimension,
        "start": [round(v, 1) for v in _position(initial["state"])],
        "target_kind": challenge.target.get("type") or challenge.target.get("item"),
        "experiments": reduced,
    }, ensure_ascii=False)
    hypothesis = (attempts[-1].get("hypothesis") if attempts else None) or \
                 "Test world-grounded " + challenge.name
    rejections = sum(a.get("status") == "rejected" for a in attempts)
    executed = sum(a.get("status") in {"completed", "failed", "progress"}
                   for a in attempts)
    if executed == 0:
        # A malformed model JSON or forbidden action never touched Minecraft.
        # Keep the diagnosis, but do not poison skill memory with fictitious
        # "failed gameplay" experiences.
        store.record_event(
            "training_interface_failure",
            json.dumps({"task": challenge.name, "rejections": rejections,
                        "reason": reason, "seconds": round(elapsed, 2)})[:1300],
        )
        return
    reward = (1.0 if passed else -1.0) - (0.1 * rejections) - (
        0.02 * elapsed) - (0.05 * max(0, executed - 1))
    store.record_skill_trial(
        intent, hypothesis, actions,
        json.dumps({"verified": bool(passed), "outcome": reason,
                    "seconds": round(elapsed, 2),
                    "reward": round(reward, 3)}),
        bool(passed),
    )
    store.record_event(
        "training_episode",
        json.dumps({"task": challenge.name, "success": passed,
                    "reason": reason, "seconds": round(elapsed, 2)})[:1300],
    )
    # Promote only after 3 verified successes in genuinely different starting
    # locations. Do not trust a memorized coordinate or a single lucky trial.
    history = _verified_history(intent, 12)
    successes = [h for h in history if h.get("success")]
    origins = set()
    for h in successes:
        try:
            start = json.loads(h["actions"])["start"]
            origins.add(tuple(round(float(x)) for x in start))
        except (TypeError, ValueError, KeyError):
            continue
    if (len(successes) >= 3 and len(origins) >= 3
            and len(successes) / max(1, len(history)) >= .75
            and not store.find_learned_skills(intent, 2)):
        verified_plan = next(
            (a.get("plan") for a in reversed(attempts)
             if a.get("verified") and a.get("plan")), None
        )
        if verified_plan:
            procedure = practice._generalized_procedure(verified_plan)
            store.save_learned_skill(
                "Learned " + challenge.name + " from physical trials",
                intent, procedure, source="training",
            )
            log_event("training", "skill_promoted", intent=intent)


def _prompt(challenge, snapshot, attempts, history, observed_extra=()):
    target = challenge.target
    # Do not hand model an arbitrary plan. It must choose a primitive.
    return json.dumps({
        "objective": challenge.objective,
        "task": challenge.name,
        "target_confirmed_by_minecraft": target,
        "other_minecraft_confirmed_obstructions": list(observed_extra)[-3:],
        "permitted_actions": challenge.allowed,
        "minecraft_state": {
            "pos": _position(snapshot["state"]),
            "yaw": snapshot["state"].get("yaw"),
            "dimension": snapshot["state"].get("dimension"),
        },
        "inventory": snapshot["inventory"][:15],
        "previous_attempts_this_trial": attempts[-3:],
        "interface_feedback": [
            {"reason": a["reason"], "guidance": _interface_feedback(a["reason"], challenge)}
            for a in attempts[-3:] if a.get("status") == "rejected"
        ],
        "previous_episodes": [
            {"success": h["success"], "hypothesis": h["hypothesis"],
             "outcome": h["outcome"][:250]} for h in history[-4:]
        ],
        "rule": "Use only observed coordinates and actual block IDs. "
                "If failed, form a different hypothesis. Never claim success.",
    }, ensure_ascii=False)


def run_training(world, planner, *, rounds=12, max_steps=4, seed=7,
                 report_dir=REPORT_DIR, duration_minutes=20):
    rng = random.Random(seed)
    rounds = max(1, min(100, int(rounds)))
    max_steps = max(1, min(8, int(max_steps)))
    duration_seconds = max(1.0, min(120.0, float(duration_minutes))) * 60.0
    started = time.monotonic()
    report = {"version": TRAINING_BUILD,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "rounds": rounds, "max_steps": max_steps,
              "duration_limit_minutes": float(duration_minutes),
              "mode": "live_minecraft_no_world_reset",
              "episodes": [], "summary": {}}
    halted_on_pending = False
    finished_due_to_time = False
    unproductive_rounds = 0
    for repetition in range(rounds):
        preferred_resource = None
        round_actions_start = sum(
            len([a for a in episode.get("experiments", [])
                 if a.get("status") in {"completed", "failed", "progress"}])
            for episode in report["episodes"])
        # Recalculate from durable gameplay evidence on every round.
        # When old skills deteriorate, Stage 2 becomes locked again.
        readiness = assess_training(store, report["episodes"])
        tasks = CURRICULUM + (("gather_wood",)
                              if readiness["stage_2_unlocked"] else ())
        for task in tasks:
            if time.monotonic() - started >= duration_seconds:
                finished_due_to_time = True
                break
            initial = world.snapshot()
            challenge = _select_challenge(
                task, world, initial, rng,
                preferred_resource if task in {"orient", "mine"} else None)
            if challenge is None:
                record = {"round": repetition + 1, "task": task,
                          "status": "unavailable", "reason": "no_natural_valid_scenario",
                          "attempts": 0, "seconds": 0}
                report["episodes"].append(record)
                print("[TRAIN] %-8s unavailable (no suitable world observation)" % task,
                      flush=True)
                continue

            attempts = []
            observed_extra = []
            used = set()
            outcome = "no_verified_completion"
            verified = False
            episode_start = time.monotonic()
            for step in range(max(8, max_steps) if task == "gather_wood" else max_steps):
                observed = world.snapshot()
                if observed["state"].get("dimension") != challenge.dimension:
                    outcome = "dimension_changed"
                    break
                if observed["state"].get("jobActive"):
                    outcome = "world_busy"
                    break
                history = _verified_history("training." + task, 12)[-6:]
                plan = planner.propose(_prompt(
                    challenge, observed, attempts, history, observed_extra))
                plan = _ground_confirmed_coordinates(plan, challenge)
                plan = _bind_observed_mining_identity(plan, challenge, observed_extra)
                if isinstance(plan, dict) and plan.get("_schema_binding"):
                    log_event("training", "schema_arguments_grounded",
                              task=task, action=plan.get("action"),
                              target=challenge.target.get("pos"))
                valid, why = _plan_allowed(plan, challenge, observed_extra)
                fingerprint = json.dumps({
                    k: (plan or {}).get(k)
                    for k in ("action", "x", "y", "z", "expected_block",
                              "expected_contains", "contains", "exact", "radius")
                }, sort_keys=True)
                # A repeated proposal is only a repeat if the observations
                # have not changed. Real location/blocker changes permit a
                # new experiment without pretending the action improved.
                evidence_key = json.dumps({
                    "position": [round(v, 1) for v in _position(observed["state"])],
                    "blockers": observed_extra,
                }, sort_keys=True)
                attempt_key = (fingerprint, evidence_key)
                repeated = attempt_key in used
                if repeated:
                    valid, why = False, "repeat_without_new_evidence"
                used.add(attempt_key)
                if not valid:
                    attempts.append({"step": step + 1, "status": "rejected",
                                     "reason": why, "plan": plan,
                                     "feedback": _interface_feedback(why, challenge)})
                    outcome = why
                    log_event("training", "proposal_rejected", task=task,
                              reason=why, plan=plan)
                    # An identical rejected command has no new information.
                    # Stop this exercise rather than burn model calls and
                    # mislabel the tool-interface failure as Minecraft.
                    if repeated:
                        break
                    continue
                # All game actions run through the same physical practice engine.
                result = world.execute(plan)
                if result.get("status") == "pending":
                    print("[TRAIN] %-8s waiting for Minecraft physical job..." % task,
                          flush=True)
                    result = _settle_pending_job(world, result)
                after = world.snapshot(allow_busy=True)
                ok, evidence = _verify(
                    challenge, observed, after, result, attempts, initial["inventory"])
                progress = task == "gather_wood" and result.get("ok") and not ok
                attempts.append({
                    "step": step + 1,
                    "status": ("completed" if ok else
                               "progress" if progress else "failed"),
                    "verified": ok, "hypothesis": plan.get("hypothesis"),
                    "reason": evidence, "plan": plan,
                    "result": result.get("result"),
                })
                outcome = evidence
                blocker = _observed_obstruction(result)
                if blocker and blocker not in observed_extra:
                    observed_extra.append(blocker)
                    log_event("training", "occlusion_observed",
                              task=task, blocker=blocker)
                log_event("training", "physical_trial",
                          task=task, verified=ok, evidence=evidence,
                          action=plan.get("action"))
                if ok:
                    verified = True
                    break
                # A still-running job may be acting physically; do not launch
                # another action on top of it or label it a failed skill.
                if result.get("status") == "pending":
                    outcome = "physical_job_pending;no_parallel_actions"
                    break

            elapsed = time.monotonic() - episode_start
            if outcome == "physical_job_pending;no_parallel_actions":
                halted_on_pending = True
                report["episodes"].append({
                    "round": repetition + 1, "task": task, "status": "pending",
                    "reason": outcome, "attempts": len(attempts),
                    "seconds": round(elapsed, 2), "experiments": attempts,
                })
                print("[TRAIN] %-8s still physically running; halting safely" % task,
                      flush=True)
                break
            real_actions = any(a.get("status") in {"completed", "failed", "progress"}
                               for a in attempts)
            _record_episode(challenge, attempts, verified, elapsed, initial, outcome)
            record = {
                "round": repetition + 1, "task": task,
                "status": ("passed" if verified else
                           "failed" if real_actions else "interface_error"),
                "attempts": len(attempts), "seconds": round(elapsed, 2),
                "reason": outcome, "experiments": attempts,
            }
            report["episodes"].append(record)
            if task == "navigate" and verified:
                preferred_resource = {
                    "type": challenge.target["resource_type"],
                    "pos": challenge.target["resource_pos"],
                }
            print("[TRAIN] %-8s %-11s %d action(s)  %s" %
                  (task, record["status"], record["attempts"], outcome[:90]),
                  flush=True)
        if halted_on_pending or finished_due_to_time:
            break
        round_actions_end = sum(
            len([a for a in episode.get("experiments", [])
                 if a.get("status") in {"completed", "failed", "progress"}])
            for episode in report["episodes"])
        if round_actions_end == round_actions_start:
            unproductive_rounds += 1
        else:
            unproductive_rounds = 0
        # Stop rather than making dozens of LLM calls while the action
        # interface or local environment cannot support any real trial.
        if unproductive_rounds >= 3:
            print("[TRAIN] No executed actions in 3 rounds; stopping to report the blocker.",
                  flush=True)
            break

    episodes = report["episodes"]
    eligible = [e for e in episodes if e["status"] in {"passed", "failed"}]
    executed = [
        a for e in eligible for a in e.get("experiments", [])
        if a.get("status") in {"completed", "failed", "progress"}
    ]
    total_physical = sum(
        a.get("plan", {}).get("action") in {
            "move_to", "move_forward", "look_at", "mine", "collect", "place",
            "craft", "equip"
        } for a in executed
    )
    successes = sum(e["status"] == "passed" for e in eligible)
    report["summary"] = {
        "eligible": len(eligible), "passed": successes,
        "failed": sum(e["status"] == "failed" for e in eligible),
        "unavailable": sum(e["status"] == "unavailable" for e in episodes),
        "interface_errors": sum(e["status"] == "interface_error" for e in episodes),
        "pending": sum(e["status"] == "pending" for e in episodes),
        "completion_rate": round(successes / len(eligible), 3) if eligible else None,
        "physical_actions": total_physical,
        "observation_actions": sum(
            a.get("plan", {}).get("action") == "scan_blocks" for a in executed),
        "executed_primitives": len(executed),
        "rejected_proposals": sum(sum(a.get("status") == "rejected"
                for a in e.get("experiments", [])) for e in episodes),
        "duration_seconds": round(time.monotonic() - started, 2),
        "teacher_calls": 0,
        "stopped_due_to_time": finished_due_to_time,
        "stopped_due_to_no_actions": unproductive_rounds >= 3,
    }
    progress = assess_training(store, report["episodes"])
    report["graduation"] = progress
    report["summary"]["stage"] = progress["stage"]
    report["summary"]["basic_skills_graduated"] = progress["graduated_count"]
    report["summary"]["stage_2_unlocked"] = progress["stage_2_unlocked"]
    report["summary"]["stage_2_wood_gather_successes"] = sum(
        e["task"] == "gather_wood" and e["status"] == "passed"
        for e in report["episodes"])
    report["ended_at"] = datetime.now(timezone.utc).isoformat()
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / ("training-" + datetime.now(timezone.utc).strftime(
        "%Y%m%d-%H%M%S") + ".json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    report["report_path"] = str(output)
    store.record_event("training_session",
                       json.dumps(report["summary"])[:1500])
    store.record_event("training_graduation",
                       json.dumps({k: v for k, v in progress.items()
                                   if k != "skills"})[:1300])
    log_event("training", "session_complete", **report["summary"],
              report_path=str(output))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Physical Minecraft learning lab")
    parser.add_argument("--rounds", type=int, default=12,
                        help="Maximum repeated curriculum rounds (default 12)")
    parser.add_argument("--steps", type=int, default=4,
                        help="Maximum hypotheses per objective (default 4)")
    parser.add_argument("--minutes", type=float, default=20,
                        help="Maximum training session length (default 20 minutes)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    # Training has no cloud worker and never selects the OpenAI provider.
    os.environ["COMPANION_MODEL_PROVIDER"] = "local"
    os.environ["COMPANION_CLOUD_TEACHER"] = "off"
    os.environ["COMPANION_TRAINING_MODE"] = "on"
    print("[TRAIN] Real Minecraft practice. No teleporting or item spawning.")
    print("[TRAIN] Stop the normal Chat brain first; keep Minecraft/Ollama running.")
    with exclusive_controller():
        world = RealWorld()
        world.snapshot()  # fail fast if Minecraft is not connected
        planner = OllamaPlanner()
        start_session(build=TRAINING_BUILD, model="local:" + planner.model)
        result = run_training(world, planner, rounds=args.rounds,
                              max_steps=args.steps, seed=args.seed,
                              duration_minutes=args.minutes)
        print("[TRAIN] Summary: " + json.dumps(result["summary"]))
        for name, score in result["graduation"]["skills"].items():
            print("[TRAIN] %-8s %2d/%d successes, %d sites, %s" % (
                name, score["successes"], score["resolved_trials"],
                score["distinct_success_sites"],
                "GRADUATED" if score["graduated"] else "NEEDS PRACTICE"))
        print("[TRAIN] Stage %d: %s" % (
            result["graduation"]["stage"], result["graduation"]["stage_name"]))
        print("[TRAIN] Report: " + result["report_path"])
        print("[TRAIN] Existing SQLite memories preserved.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException) as error:
        print("[TRAIN] ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
