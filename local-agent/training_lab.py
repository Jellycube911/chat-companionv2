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
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

import practice_engine as practice
from action_log import log_event, start_session
from memory_store import store

BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "data" / "training_reports"
CURRICULUM = ("observe", "orient", "navigate", "mine", "collect")
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
        return practice._post("/find-blocks", {
            "exact": [block_id], "radius": radius,
            "limit": 24, "exposed_only": True,
        }).get("blocks", [])

    def execute(self, plan):
        return practice.execute_plan(plan)

    def job_state(self):
        return practice._get("/state")


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
    return (bool(feet.get("air") or feet.get("replaceable"))
            and bool(head.get("air") or head.get("replaceable"))
            and bool(support.get("solid_support_up")))


def _select_challenge(name, world, snapshot, rng):
    state, blocks = snapshot["state"], snapshot["blocks"]
    origin = _position(state)
    dim = str(state.get("dimension") or "unknown")
    # The curriculum picks scenarios from live observations, NOT action plans.
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
        facing = [b for b in candidates if
                  math.hypot(float(b["nearestX"]) - origin[0],
                             float(b["nearestZ"]) - origin[2]) > 0.75]
        if not facing:
            return None
        b = rng.choice(facing[:8])
        return Challenge(name, "Physically turn to face the observed target block",
                         {"type": b["type"],
                          "pos": [int(b["nearestX"]), int(b["nearestY"]),
                                  int(b["nearestZ"])]},
                         ("look_at",), dim)
    if name == "navigate":
        bx, by, bz = (math.floor(v) for v in origin)
        offsets = [(dx, dz) for dx in (-4, -3, 3, 4)
                   for dz in (-4, -3, 3, 4)]
        rng.shuffle(offsets)
        for dx, dz in offsets:
            point = [bx + dx, by, bz + dz]
            if _walkable(world, point):
                return Challenge(name,
                                 "Walk to this world-confirmed standable location",
                                 {"pos": point}, ("move_to",), dim)
        return None
    if name == "mine":
        usable = [b for b in candidates if b.get("type") in PRACTICE_MATERIALS]
        usable.sort(key=lambda b: float(b.get("nearestDistance") or 99))
        for b in usable[:8]:
            for found in world.find(b["type"], radius=7):
                pos = [found.get(k) for k in ("x", "y", "z")]
                if (all(isinstance(v, int) for v in pos)
                        and _distance(origin, pos) <= 5.2
                        and abs(pos[1] - origin[1]) <= 2.0):
                    return Challenge(
                        name, "Mine this observed natural block through real breaking",
                        {"type": b["type"], "pos": pos},
                        ("mine", "scan_blocks", "look_at", "move_to"), dim)
        return None
    if name == "collect":
        entities = [e for e in snapshot["entities"]
                    if e.get("droppedItem")
                    and float(e.get("distance") or 99) <= 5.5]
        if not entities:
            return None  # Cannot create a drop out of nothing.
        e = rng.choice(entities[:5])
        return Challenge(name, "Physically pick up the observed dropped item",
                         {"item": e["droppedItem"],
                          "pos": [e["x"], e["y"], e["z"]]},
                         ("collect",), dim)
    raise ValueError("unknown curriculum task: " + name)


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


def _verify(challenge, before, after, execution):
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
        return (action == "move_to" and current <= 2.5
                and prior - current > 0.5,
                "distance_before=%.2f after=%.2f" % (prior, current))
    if challenge.name == "mine":
        result = execution.get("result") or {}
        mined = str(result.get("reason") or "").startswith("block_mined|")
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


def _record_episode(challenge, attempts, passed, elapsed, initial, reason):
    intent = "training." + challenge.name
    actions = json.dumps({
        "dimension": challenge.dimension,
        "start": [round(v, 1) for v in _position(initial["state"])],
        "target_kind": challenge.target.get("type") or challenge.target.get("item"),
        "experiments": attempts[-8:],
    }, ensure_ascii=False)[:4700]
    hypothesis = (attempts[-1].get("hypothesis") if attempts else None) or \
                 "Test world-grounded " + challenge.name
    rejections = sum(a.get("status") == "rejected" for a in attempts)
    physical = sum(a.get("status") in {"completed", "failed"} for a in attempts)
    reward = (1.0 if passed else -1.0) - (0.1 * rejections) - (
        0.02 * elapsed) - (0.05 * max(0, physical - 1))
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
    history = store.recent_skill_trials(intent, 12)
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
        "previous_episodes": [
            {"success": h["success"], "hypothesis": h["hypothesis"],
             "outcome": h["outcome"][:250]} for h in history[-4:]
        ],
        "rule": "Use only observed coordinates and actual block IDs. "
                "If failed, form a different hypothesis. Never claim success.",
    }, ensure_ascii=False)


def run_training(world, planner, *, rounds=2, max_steps=3, seed=7,
                 report_dir=REPORT_DIR):
    rng = random.Random(seed)
    rounds = max(1, min(20, int(rounds)))
    max_steps = max(1, min(8, int(max_steps)))
    started = time.monotonic()
    report = {"version": TRAINING_BUILD,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "rounds": rounds, "max_steps": max_steps,
              "mode": "live_minecraft_no_world_reset",
              "episodes": [], "summary": {}}
    for repetition in range(rounds):
        for task in CURRICULUM:
            initial = world.snapshot()
            challenge = _select_challenge(task, world, initial, rng)
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
            for step in range(max_steps):
                observed = world.snapshot()
                if observed["state"].get("dimension") != challenge.dimension:
                    outcome = "dimension_changed"
                    break
                if observed["state"].get("jobActive"):
                    outcome = "world_busy"
                    break
                history = store.recent_skill_trials("training." + task, 6)
                plan = planner.propose(_prompt(
                    challenge, observed, attempts, history, observed_extra))
                valid, why = _plan_allowed(plan, challenge, observed_extra)
                fingerprint = json.dumps({
                    k: (plan or {}).get(k)
                    for k in ("action", "x", "y", "z", "expected_block", "contains",
                              "exact", "radius")
                }, sort_keys=True)
                if fingerprint in used:
                    valid, why = False, "repeat_without_new_evidence"
                used.add(fingerprint)
                if not valid:
                    attempts.append({"step": step + 1, "status": "rejected",
                                     "reason": why, "plan": plan})
                    outcome = why
                    log_event("training", "proposal_rejected", task=task,
                              reason=why, plan=plan)
                    continue
                # All game actions run through the same physical practice engine.
                result = world.execute(plan)
                after = world.snapshot(allow_busy=True)
                ok, evidence = _verify(challenge, observed, after, result)
                attempts.append({
                    "step": step + 1, "status": "completed" if ok else "failed",
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
            _record_episode(challenge, attempts, verified, elapsed, initial, outcome)
            record = {
                "round": repetition + 1, "task": task,
                "status": "passed" if verified else "failed",
                "attempts": len(attempts), "seconds": round(elapsed, 2),
                "reason": outcome, "experiments": attempts,
            }
            report["episodes"].append(record)
            print("[TRAIN] %-8s %-11s %d action(s)  %s" %
                  (task, record["status"], record["attempts"], outcome[:90]),
                  flush=True)

    episodes = report["episodes"]
    eligible = [e for e in episodes if e["status"] != "unavailable"]
    total_physical = sum(sum(
        1 for a in e.get("experiments", [])
        if a.get("status") in {"completed", "failed"}
    ) for e in eligible)
    successes = sum(e["status"] == "passed" for e in eligible)
    report["summary"] = {
        "eligible": len(eligible), "passed": successes,
        "failed": sum(e["status"] == "failed" for e in eligible),
        "unavailable": len(episodes) - len(eligible),
        "completion_rate": round(successes / len(eligible), 3) if eligible else None,
        "physical_actions": total_physical,
        "rejected_proposals": sum(sum(a.get("status") == "rejected"
                for a in e.get("experiments", [])) for e in eligible),
        "duration_seconds": round(time.monotonic() - started, 2),
        "teacher_calls": 0,
    }
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
    log_event("training", "session_complete", **report["summary"],
              report_path=str(output))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Physical Minecraft learning lab")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--steps", type=int, default=3)
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
                              max_steps=args.steps, seed=args.seed)
        print("[TRAIN] Summary: " + json.dumps(result["summary"]))
        print("[TRAIN] Report: " + result["report_path"])
        print("[TRAIN] Existing SQLite memories preserved.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException) as error:
        print("[TRAIN] ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
