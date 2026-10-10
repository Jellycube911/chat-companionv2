"""Uncoached Stage 1 practical assessment.

Qwen chooses a natural resource and all actions. The examiner checks only
Minecraft-observed targets, physically executed actions and inventory changes.
It never supplies the model with a scripted action sequence or awards virtual
items. This practical exam is distinct from Stage 2 resource autonomy.
"""
import json
import math
import time

import practice_engine as practice

EXAM_MATERIALS = (
    "minecraft:dirt", "minecraft:sand", "minecraft:oak_log",
    "minecraft:birch_log", "minecraft:spruce_log", "minecraft:jungle_log",
    "minecraft:acacia_log", "minecraft:dark_oak_log",
    "minecraft:mangrove_log", "minecraft:cherry_log",
)


def _xyz(block):
    coords = block.get("pos")
    if not isinstance(coords, (tuple, list)) or len(coords) != 3:
        coords = [block.get(k) for k in ("x", "y", "z")]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) and int(v) == v for v in coords):
        return None
    return [int(v) for v in coords]


def _key(block):
    return (block["type"], *block["pos"])


def _exam_proof(steps, target, start_inventory, final_inventory):
    """Evidence audit: discovery -> real approach -> facing -> mine -> pickup."""
    # A scan may discover multiple resources. Give credit when the resource
    # later chosen by Qwen was genuinely present in that scan, not only when
    # it happened to be the last block in the returned list.
    def matched(step):
        if not step.get("verified"):
            return False
        if step.get("action") == "scan_blocks":
            return any(
                block.get("type") == target["type"]
                and _xyz(block) == target["pos"]
                for block in ((step.get("result") or {}).get("blocks") or []))
        return step.get("target") == target
    evidence = [step for step in steps if matched(step)]
    required = ("scan_blocks", "move_to", "look_at", "mine", "collect")
    actions = [s.get("action") for s in evidence]
    sequential = False
    pointer = 0
    for action in actions:
        if action == required[pointer]:
            pointer += 1
            if pointer == len(required):
                sequential = True
                break
    same_item = target["type"]
    count = lambda bag: sum(int(item.get("count", 0)) for item in bag
                             if item.get("item") == same_item)
    return {
        "verified_steps": [x for x in actions],
        "ordered_actions": sequential,
        "inventory_before": count(start_inventory),
        "inventory_after": count(final_inventory),
        "inventory_increased": count(final_inventory) > count(start_inventory),
        "target": dict(target),
        "passed": sequential and count(final_inventory) > count(start_inventory),
    }


def exam_memory_context(memory):
    """Only server-verified experiences, frozen for a fair comparison."""
    skills = []
    for name in ("observe", "navigate", "orient", "mine", "collect"):
        records = memory.recent_skill_trials("training." + name, 4)
        for row in records:
            if row.get("success") and row.get("evidence_status") == "verified":
                try:
                    outcome = json.loads(row.get("outcome") or "{}")
                except (ValueError, TypeError):
                    outcome = {}
                skills.append({
                    "skill": name,
                    "hypothesis": str(row.get("hypothesis") or "")[:160],
                    "outcome": str(outcome.get("outcome") or "")[:180],
                })
                break
    learned = []
    for name in ("observe", "navigate", "orient", "mine", "collect"):
        for row in memory.find_learned_skills("training." + name, 1):
            learned.append({"skill": name, "procedure": row["procedure"][:450]})
    return {"previous_learned_experiences": skills,
            "learned_procedures": learned}


def run_exam(world, planner, memory, *, max_steps=18, checkpoint=None,
             memory_context=None, record_trial=True):
    """Attempt a complete uncoached resource acquisition in the real world.

    The model sees its own observations and candidate standing ground, but no
    hand-picked target block or forced next action. Physical outcomes decide
    success. Returns a report episode and stores only completed evidence.
    """
    from training_lab import (
        _position, _distance, _bearing_error, _walkable,
        _settle_pending_job, _observed_obstruction,
    )
    initial = world.snapshot()
    origin = _position(initial["state"])
    candidates = {}
    attempts = []
    proofs = []
    mined = None
    approached = None
    faced = None
    last_reason = "exam_incomplete"
    pending = False
    started = time.monotonic()
    # None means normal exam with verified SQLite experience. An explicit
    # empty context is a genuine memory-withheld baseline: the same model,
    # tool rules, world observations and within-trial feedback still apply.
    context = (exam_memory_context(memory) if memory_context is None
               else memory_context)
    skills = context.get("previous_learned_experiences", [])
    learned = context.get("learned_procedures", [])

    def record(action, plan, result, verified, reason, target=None):
        nonlocal last_reason
        last_reason = reason
        step = {
            "action": action, "plan": plan,
            "verified": bool(verified), "reason": reason,
            "target": target,
            "result": result.get("result") if isinstance(result, dict) else None,
        }
        attempts.append(step)
        if checkpoint:
            checkpoint(attempts)

    for _ in range(max(1, int(max_steps))):
        before = world.snapshot()
        current = _position(before["state"])
        if before["state"]["dimension"] != initial["state"]["dimension"]:
            last_reason = "dimension_changed"
            break
        if before["state"].get("jobActive"):
            last_reason = "world_busy"
            break
        prompt = json.dumps({
            "task": "stage1_exam", "mode": "uncoached_practical",
            "goal": ("Independently find a naturally occurring log, dirt or sand "
                     "block, physically approach it, face it, break it and "
                     "pick up its dropped item. Choose your own resource and "
                     "each action. You must demonstrate ALL FIVE abilities."),
            "permitted_actions": [
                "scan_blocks", "move_to", "look_at", "mine", "collect"],
            "safe_resource_types": EXAM_MATERIALS,
            "world_state": {
                "position": current, "yaw": before["state"].get("yaw"),
                "dimension": before["state"].get("dimension"),
                "nearby_blocks": before["blocks"][:10],
                "nearby_dropped_items": [e for e in before["entities"]
                                         if e.get("droppedItem")][:5],
            },
            "own_observed_resources": list(candidates.values())[:15],
            "confirmed_progress": {
                "approached": approached,
                "faced": faced, "mined": mined,
            },
            "recent_attempts": [
                {"action": a["action"], "reason": a["reason"],
                 "verified": a["verified"], "target": a["target"]}
                for a in attempts[-5:]],
            "previous_learned_experiences": skills,
            "learned_procedures": learned,
            "rule": ("Choose one JSON primitive. Do not invent coordinates. "
                     "Scan before approaching. Move only to provided verified "
                     "standable positions. Mining requires expected_block "
                     "matching an observed resource. Physical Minecraft "
                     "outcomes, not claims, decide success."),
        }, ensure_ascii=False)
        raw = planner.propose(prompt)
        plan = practice.parse_plan(json.dumps(raw)) if isinstance(raw, dict) else None
        if not isinstance(plan, dict):
            record("invalid", raw, {}, False, "invalid_json")
            continue
        action = plan.get("action")
        if action not in {"scan_blocks", "move_to", "look_at", "mine", "collect"}:
            record(action, plan, {}, False, "unsupported_action")
            continue
        selected = None
        if action == "scan_blocks":
            try:
                query = practice.scan_query(plan)
            except (TypeError, ValueError, OverflowError):
                record(action, plan, {}, False, "invalid_scan")
                continue
            if not query["contains"] and not query["exact"]:
                record(action, plan, {}, False, "empty_scan_query")
                continue
            plan = dict(plan, radius=min(10, max(1, int(query["radius"]))),
                        limit=min(32, max(1, int(query["limit"]))),
                        exposed_only=True)
        else:
            if action != "collect":
                try:
                    xyz = [float(plan[k]) for k in ("x", "y", "z")]
                    if not all(math.isfinite(v) for v in xyz):
                        raise ValueError()
                except (KeyError, TypeError, ValueError, OverflowError):
                    record(action, plan, {}, False, "missing_xyz")
                    continue
                if action == "move_to":
                    selected = next((
                        c for c in candidates.values() if any(
                            all(abs(xyz[i] - p[i]) <= 0.05 for i in range(3))
                            for p in c["standable_candidates"])
                    ), None)
                else:
                    selected = next((
                        c for c in candidates.values()
                        if all(abs(xyz[i] - c["pos"][i]) <= 0.05
                               for i in range(3))
                    ), None)
                if selected is None:
                    record(action, plan, {}, False, "unobserved_coordinates")
                    continue
            if action == "look_at" and (
                    approached is None or approached != _key(selected)):
                record(action, plan, {}, False, "approach_before_orient")
                continue
            if action == "mine":
                if not (approached == _key(selected) and
                        faced == _key(selected)):
                    record(action, plan, {}, False, "observe_approach_face_first")
                    continue
                expected = plan.get("expected_block")
                if expected and expected != selected["type"]:
                    record(action, plan, {}, False, "block_identity_mismatch")
                    continue
                asserted = plan.get("expected_contains")
                if asserted and str(asserted).lower() not in selected["type"].lower():
                    record(action, plan, {}, False, "block_identity_mismatch")
                    continue
                plan["expected_block"] = selected["type"]
            if action == "collect" and mined is None:
                record(action, plan, {}, False, "mine_before_collect")
                continue
        # Persist an unverified in-flight command BEFORE issuing it. This
        # remains honest if Minecraft/Python is terminated during the exam.
        attempts.append({
            "action": action, "plan": plan, "verified": False,
            "reason": "issued_unverified", "status": "in_flight",
            "target": selected, "result": None,
        })
        if checkpoint:
            checkpoint(attempts)
        # Every world mutation is made by the real Minecraft bridge.
        result = world.execute(plan)
        attempts.pop()
        if result.get("status") == "pending":
            result = _settle_pending_job(world, result)
        after = world.snapshot(allow_busy=True)
        if result.get("status") == "pending":
            pending = True
            record(action, plan, result, False, "physical_job_pending")
            break
        receipt = result.get("result") or {}
        successful = bool(result.get("ok"))
        found_target = None
        verified = False
        reason = str(receipt.get("reason") or receipt.get("error") or "unverified")
        if action == "scan_blocks" and successful:
            for block in receipt.get("blocks") or []:
                pos = _xyz(block)
                kind = block.get("type")
                if pos is None or kind not in EXAM_MATERIALS:
                    continue
                center = [v + 0.5 for v in pos]
                if not (3.0 <= _distance(origin, center) <= 10.0):
                    continue
                if abs(pos[1] - origin[1]) > 3:
                    continue
                standable = []
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1),
                               (1, 1), (1, -1), (-1, 1), (-1, -1)):
                    for y in (int(current[1]), pos[1], pos[1] - 1,
                              pos[1] + 1):
                        point = [pos[0] + dx, y, pos[2] + dz]
                        if _walkable(world, point):
                            standable.append(point)
                            break
                if standable:
                    candidate = {"type": kind, "pos": pos,
                                 "standable_candidates": standable}
                    candidates[_key(candidate)] = candidate
                    found_target = candidate
                    if len(candidates) >= 15:
                        break
            verified = found_target is not None
            selected = found_target
            reason = ("exam_observed_usable_resource" if verified
                      else "no_exam_resource_in_scan")
        elif action == "move_to" and successful and selected:
            previous = _distance(current, [v + .5 for v in selected["pos"]])
            current_dist = _distance(_position(after["state"]),
                                     [v + .5 for v in selected["pos"]])
            verified = (
                receipt.get("state") == "COMPLETED"
                and _distance(current, _position(after["state"])) >= .75
                and previous - current_dist >= .4 and current_dist <= 4.5)
            reason = ("approached_observed_resource" if verified else
                      "movement_did_not_approach_resource")
            if verified:
                approached = _key(selected)
                faced = None
        elif action == "look_at" and successful and selected:
            verified = _bearing_error(after["state"], selected["pos"]) <= 22.5
            reason = "faced_observed_resource" if verified else "orientation_failed"
            if verified:
                faced = _key(selected)
        elif action == "mine" and successful and selected:
            metrics = practice._parse_mining_metrics(receipt)
            verified = (receipt.get("state") == "COMPLETED"
                        and metrics is not None
                        and metrics.get("block") == selected["type"])
            reason = str(receipt.get("reason") or "mining_not_verified")
            if verified:
                mined = _key(selected)
        elif action == "collect" and successful and mined is not None:
            typ = mined[0]
            count = lambda bag: sum(int(v.get("count", 0)) for v in bag
                                    if v.get("item") == typ)
            # The pickup must happen *during this collect action*. A prior
            # accidental pickup, or a successful-looking tool receipt alone,
            # is not proof that the tested collection step worked.
            verified = (receipt.get("state") == "COMPLETED"
                        and count(after["inventory"]) > count(before["inventory"])
                        and count(after["inventory"]) > count(initial["inventory"]))
            reason = "collected_mined_resource" if verified else "no_inventory_gain"
            if verified:
                selected = candidates.get(mined)
        record(action, plan, result, verified, reason, selected)
        if action == "collect" and verified:
            break
    target = candidates.get(mined) if mined is not None else None
    proof = (_exam_proof(attempts, target, initial["inventory"],
                         world.snapshot(allow_busy=True)["inventory"])
             if target is not None else {"passed": False})
    passed = bool(proof["passed"]) and not pending
    # Preserve a compact but auditable record; no pass from proposal text alone.
    status = ("pending" if pending else "passed" if passed else
              "unavailable" if not candidates else "failed")
    evidence = {"verified": passed, "proof": proof, "steps": attempts,
                "target": target, "dimension": initial["state"]["dimension"],
                "start": origin}
    if record_trial and status in {"passed", "failed"}:
        memory.record_skill_trial(
            "training.stage1_exam", "uncoached independent resource acquisition",
            json.dumps(evidence, ensure_ascii=False),
            json.dumps({"verified": passed, "outcome": last_reason,
                        "proof": proof}, ensure_ascii=False),
            passed)
    return {
        "task": "stage1_exam", "status": status, "reason": last_reason,
        "attempts": len(attempts), "seconds": round(time.monotonic() - started, 2),
        "experiments": attempts, "proof": proof,
        "memory_exposure": {
            "verified_experiences": len(skills),
            "learned_procedures": len(learned),
        },
    }
