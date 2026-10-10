import json
import math
import os
import re
import time

import requests

from action_log import log_event, log_exception
from memory_store import store


MINECRAFT_URL = os.getenv("COMPANION_MINECRAFT_URL", "http://127.0.0.1:8765").rstrip("/")


def _get(path):
    response = requests.get(f"{MINECRAFT_URL}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def _post(path, payload=None):
    kwargs = {"timeout": 8}
    if payload is not None:
        kwargs["json"] = payload
    response = requests.post(f"{MINECRAFT_URL}{path}", **kwargs)
    if response.status_code == 409:
        try:
            data = response.json()
        except Exception:
            data = {"error": response.text or "conflict"}
        if not isinstance(data, dict):
            data = {"error": str(data)}
        data["ok"] = False
        data["status_code"] = 409
        return data
    if not response.ok:
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:350] or "unknown bridge response"}
        detail = body.get("error") if isinstance(body, dict) else str(body)
        return {
            "ok": False,
            "error": str(detail or "Minecraft bridge rejected the action"),
            "status_code": response.status_code,
            "path": path,
        }
    return response.json()


def _failed(result):
    return isinstance(result, dict) and result.get("ok") is False


def _wait_for_job(timeout=12.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        state = _get("/state")
        last = state
        if _failed(state):
            return state
        if not state.get("jobActive", False):
            last_job = state.get("lastJob") or {}
            return {
                "state": last_job.get("state", "IDLE"),
                "reason": last_job.get("reason", "idle"),
                "pos": [
                    round(float(state.get("x", 0)), 2),
                    round(float(state.get("y", 0)), 2),
                    round(float(state.get("z", 0)), 2),
                ],
            }
        if state.get("jobState") != "RUNNING":
            return {
                "state": state.get("jobState"),
                "reason": state.get("jobReason"),
                "pos": [
                    round(float(state.get("x", 0)), 2),
                    round(float(state.get("y", 0)), 2),
                    round(float(state.get("z", 0)), 2),
                ],
            }
        time.sleep(0.2)
    return {
        "state": last.get("jobState") if last else "UNKNOWN",
        "reason": last.get("jobReason") if last else "timeout",
    }


def _inventory():
    result = _get("/inventory")
    if _failed(result):
        return []
    return [
        {
            "slot": int(item["slot"]),
            "item": str(item["item"]),
            "count": int(item["count"]),
        }
        for item in result.get("items", [])
    ]


def _state():
    state = _get("/state")
    owner = state.get("owner", {}) if isinstance(state, dict) else {}
    return {
        "server": state.get("serverState") if isinstance(state, dict) else None,
        "pos": [
            round(float(state.get("x", 0)), 2),
            round(float(state.get("y", 0)), 2),
            round(float(state.get("z", 0)), 2),
        ] if isinstance(state, dict) else [0, 0, 0],
        "facing": state.get("facing") if isinstance(state, dict) else None,
        "owner_distance": round(float(owner.get("distance", 0)), 2),
        "job_active": bool(state.get("jobActive", False)) if isinstance(state, dict) else False,
        "job_type": state.get("jobType") if isinstance(state, dict) else None,
        "job_state": state.get("jobState") if isinstance(state, dict) else None,
        "job_reason": state.get("jobReason") if isinstance(state, dict) else None,
        "job_progress": state.get("jobProgress") if isinstance(state, dict) else None,
    }


def _snapshot():
    return {"state": _state(), "inventory": _inventory()}


def _distance(pos, target):
    return math.sqrt(sum((float(pos[i]) - float(target[i])) ** 2 for i in range(3)))


def _slot_for_item(item_id, inventory):
    wanted = str(item_id or "").strip().lower()
    if not wanted:
        return None
    for item in inventory:
        current = str(item.get("item", "")).lower()
        if current == wanted or current.endswith(":" + wanted):
            return int(item["slot"])
    return None


def _inventory_total(inventory):
    return sum(int(item.get("count", 0)) for item in inventory)


def _choose_mining_tool(block_type, inventory, requested=""):
    """Explore real tools, then reuse the fastest observed tool for this block.

    Tool classes are only an inventory filter, never a block-to-tool recipe.
    """
    candidates = [
        item for item in inventory
        if re.search(
            r"(?:_axe|_pickaxe|_shovel|_hoe|_sword|:shears)$",
            str(item.get("item", "")).lower(),
        )
    ]
    if not candidates:
        return requested or None

    options = {str(item["item"]).lower(): item for item in candidates}
    requested = str(requested or "").lower()
    if requested in options:
        return requested

    history = {
        row["option"]: row
        for row in store.list_efficiency(50, context=f"mine:{block_type}")
    }
    # Sample each available tool before exploiting rewards. A newly crafted
    # tool gets a chance, and all outcomes remain tied to the observed block.
    unexplored = [
        item for item in candidates
        if int(history.get(str(item["item"]).lower(), {}).get("attempts", 0)) == 0
    ]
    if unexplored:
        return str(unexplored[0]["item"]).lower()
    return max(
        options,
        key=lambda option: (
            float(history.get(option, {}).get("avg_reward", 0.0)),
            int(history.get(option, {}).get("successes", 0)),
        ),
    )


def _generalized_procedure(plan):
    action = plan.get("action")
    procedure = {
        "action": action,
        "intent": plan.get("intent"),
    }
    if action == "scan_blocks":
        procedure.update(
            {
                "contains": plan.get("contains") or [],
                "exact": plan.get("exact") or [],
                "radius": plan.get("radius", 16),
                "exposed_only": bool(plan.get("exposed_only", False)),
            }
        )
    elif action == "move_to":
        procedure["target"] = "known standable destination coordinate"
        procedure["verify"] = "distance to destination decreases or arrival completes"
    elif action == "mine":
        procedure["target"] = "known target block coordinate"
        if plan.get("expected_block"):
            procedure["expected_block"] = plan.get("expected_block")
        if plan.get("expected_contains"):
            procedure["expected_contains"] = plan.get("expected_contains")
        if plan.get("tool"):
            procedure["tool"] = plan.get("tool")
        procedure["verify"] = "target identity matches and mining job completes"
    elif action == "look_at":
        procedure["target"] = "known world coordinate"
    elif action == "collect":
        procedure["verify"] = "inventory increases or collection job completes"
    elif action == "place":
        if plan.get("item"):
            procedure["item"] = plan.get("item")
        procedure["target"] = "valid nearby placement or explicit coordinate"
    elif action == "equip":
        if plan.get("item"):
            procedure["item"] = plan.get("item")
    elif action == "craft":
        procedure.update(
            {
                "width": plan.get("width"),
                "height": plan.get("height"),
                "grid": plan.get("grid"),
            }
        )
    return json.dumps(procedure, ensure_ascii=False, separators=(",", ":"))


def _item_count(inventory, item_id):
    wanted = str(item_id or "").strip().lower()
    return sum(
        int(item.get("count", 0))
        for item in inventory
        if str(item.get("item", "")).lower() == wanted
    )


def parse_plan(text):
    text = str(text or "").strip()
    if not text:
        return None

    fenced = re.search(r"\`\`\`(?:json)?\s*([\s\S]*?)\`\`\`", text, re.I)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None

    try:
        plan = json.loads(text[start : end + 1])
    except Exception:
        return None

    if not isinstance(plan, dict):
        return None

    action = str(plan.get("action", "")).strip().lower()
    aliases = {
        "break_block": "mine",
        "mine_block": "mine",
        "turn_to_face": "look_at",
        "find_blocks": "scan_blocks",
        "scan": "scan_blocks",
        "move": "move_to",
    }
    action = aliases.get(action, action)

    allowed = {
        "scan_blocks",
        "move_to",
        "move_forward",
        "look_at",
        "mine",
        "collect",
        "place",
        "equip",
        "craft",
        "idle",
    }
    if action not in allowed:
        return None

    plan["action"] = action
    plan["intent"] = str(plan.get("intent") or action).strip().lower()[:240]
    plan["hypothesis"] = str(
        plan.get("hypothesis") or f"{action} will advance the current goal"
    ).strip()[:1200]

    if action == "mine" and not plan.get("expected_block") and not plan.get("expected_contains"):
        intent = plan["intent"]
        for token, expected in (
            ("log", "_log"),
            ("leaves", "leaves"),
            ("leaf", "leaves"),
            ("stone", "stone"),
            ("dirt", "dirt"),
            ("sand", "sand"),
            ("ore", "_ore"),
        ):
            if token in intent:
                plan["expected_contains"] = expected
                break

    return plan


def _scan_blocks(plan):
    contains = plan.get("contains")
    exact = plan.get("exact")
    if isinstance(contains, str):
        contains = [contains]
    if isinstance(exact, str):
        exact = [exact]
    radius = max(1, min(20, int(plan.get("radius", 16) or 16)))
    limit = max(1, min(32, int(plan.get("limit", 16) or 16)))
    result = _post(
        "/find-blocks",
        {
            "contains": contains or [],
            "exact": exact or [],
            "radius": radius,
            "limit": limit,
            "exposed_only": bool(plan.get("exposed_only", False)),
        },
    )
    if _failed(result):
        return result
    return {
        "ok": True,
        "blocks": [
            {
                "type": block.get("type"),
                "pos": [block.get("x"), block.get("y"), block.get("z")],
                "distance": round(float(block.get("distance", 0)), 2),
            }
            for block in result.get("blocks", [])[:limit]
        ],
    }


def _place_nearby(slot, face="up"):
    """Choose real empty terrain with solid support, retaining failed evidence."""
    state = _get("/state")
    if _failed(state):
        return {"ok": False, "error": "placement_state_unavailable", "details": state}
    bx = math.floor(float(state.get("x", 0)))
    by = math.floor(float(state.get("y", 0)))
    bz = math.floor(float(state.get("z", 0)))
    offsets = [
        (1, 0), (-1, 0), (0, 1), (0, -1),
        (1, 1), (1, -1), (-1, 1), (-1, -1),
        (2, 0), (-2, 0), (0, 2), (0, -2),
    ]
    candidates = []
    failed = []
    for dx, dz in offsets:
        target = {"x": bx + dx, "y": by, "z": bz + dz}
        observed = _post("/block-at", target)
        if _failed(observed):
            failed.append({"pos": [target["x"], by, target["z"]],
                           "reason": observed.get("error", "target_probe_failed")})
            continue
        if not observed.get("replaceable", observed.get("air", False)):
            continue
        # The current automatic placement method works on the upper face of
        # the support below the target. Do not guess that grass or a leaf can
        # support a crafting table.
        support = {"x": target["x"], "y": by - 1, "z": target["z"]}
        floor = _post("/block-at", support)
        if _failed(floor):
            failed.append({"pos": [target["x"], by, target["z"]],
                           "reason": floor.get("error", "support_probe_failed")})
            continue
        if not floor.get("solid_support_up", not floor.get("air", True)):
            continue
        # Pure air first: tall grass is technically replaceable but can
        # occlude clicks aimed at the support block.
        candidates.append((not observed.get("air", False), target))

    candidates.sort(key=lambda item: item[0])
    for _, target in candidates[:8]:
        started = _post(
            "/place-block",
            {**target, "inventory_slot": int(slot), "face": "up"},
        )
        pos = [target["x"], target["y"], target["z"]]
        if _failed(started):
            failed.append({"pos": pos, "reason": started.get("error", "admission_rejected")})
            continue
        result = _wait_for_job(6)
        if result.get("state") == "COMPLETED":
            result["placed_at"] = pos
            return result
        failed.append({
            "pos": pos,
            "reason": result.get("reason") or result.get("error") or result.get("state") or "unknown",
        })
        if result.get("state") == "RUNNING":
            # A pending placement job must not leak into the next candidate.
            _post("/stop-action")
    return {
        "ok": False,
        "error": "no_valid_placement: " + (
            "; ".join(str(a["reason"]) for a in failed[-4:])
            if failed else "no empty target with solid upper support within reach"
        ),
        "candidates": len(candidates),
        "failures": failed[-10:],
    }


def _execute_action(plan, before):
    action = plan["action"]

    if action == "idle":
        return {"ok": True, "state": "IDLE", "reason": str(plan.get("reason", "idle"))}

    if action == "scan_blocks":
        return _scan_blocks(plan)

    x = plan.get("x")
    y = plan.get("y")
    z = plan.get("z")

    if action == "move_to":
        if x is None or y is None or z is None:
            return {"ok": False, "error": "move_to requires x, y, z"}
        target = [float(x), float(y), float(z)]
        current_distance = _distance(before["state"]["pos"], target)
        if current_distance <= 0.8:
            return {"ok": True, "state": "COMPLETED", "reason": "already_at_destination"}
        if current_distance <= 4.25:
            # Proximity alone is not an error. Check whether the coordinate
            # is occupied instead of blocking legitimate short movements.
            observed = _post("/block-at", {
                "x": int(math.floor(float(x))),
                "y": int(math.floor(float(y))),
                "z": int(math.floor(float(z))),
            })
            if _failed(observed):
                return observed
            if not observed.get("air", False):
                return {
                    "ok": False,
                    "error": "destination_occupied",
                    "block": observed.get("type"),
                    "target": target,
                }
        started = _post("/move-to", {"x": float(x), "y": float(y), "z": float(z)})
        return started if _failed(started) else _wait_for_job(12)

    if action == "move_forward":
        started = _post("/move-forward")
        return started if _failed(started) else _wait_for_job(8)

    if action == "look_at":
        if x is None or y is None or z is None:
            return {"ok": False, "error": "look_at requires x, y, z"}
        return _post(
            "/look-at",
            {"x": int(round(float(x))), "y": int(round(float(y))), "z": int(round(float(z)))},
        )

    if action == "collect":
        started = _post("/collect-items")
        return started if _failed(started) else _wait_for_job(12)

    if action == "mine":
        if x is None or y is None or z is None:
            return {"ok": False, "error": "mine requires x, y, z"}

        target = {
            "x": int(round(float(x))),
            "y": int(round(float(y))),
            "z": int(round(float(z))),
        }
        observed = _post("/block-at", target)
        if _failed(observed):
            return observed

        observed_type = str(observed.get("type") or "")
        expected_block = str(plan.get("expected_block") or "").strip()
        expected_contains = str(plan.get("expected_contains") or "").strip()

        if expected_block and observed_type != expected_block:
            return {
                "ok": False,
                "error": "target_block_mismatch",
                "expected_block": expected_block,
                "observed_block": observed_type,
                "target": target,
            }
        if expected_contains and expected_contains not in observed_type:
            return {
                "ok": False,
                "error": "target_block_mismatch",
                "expected_contains": expected_contains,
                "observed_block": observed_type,
                "target": target,
            }
        if observed.get("air"):
            return {
                "ok": False,
                "error": "target_block_is_air",
                "target": target,
            }

        requested_tool = _choose_mining_tool(
            observed_type, before["inventory"], plan.get("tool")
        )
        if requested_tool:
            slot = _slot_for_item(requested_tool, before["inventory"])
            if slot is None:
                return {
                    "ok": False,
                    "error": f"requested mining tool is not in inventory: {requested_tool}",
                }
            equipped = _post("/equip-slot", {"slot": int(slot)})
            if _failed(equipped):
                return equipped

        started = _post("/mine-block", target)
        return started if _failed(started) else _wait_for_job(30)

    if action == "place":
        inventory = before["inventory"]
        slot = plan.get("slot")
        if plan.get("item"):
            slot = _slot_for_item(plan.get("item"), inventory)
        if slot is None:
            return {"ok": False, "error": "place requires a valid item or slot"}
        if x is None and y is None and z is None:
            return _place_nearby(int(slot), str(plan.get("face") or "up"))
        if x is None or y is None or z is None:
            return {"ok": False, "error": "exact placement requires x, y, z"}
        started = _post(
            "/place-block",
            {
                "x": int(round(float(x))),
                "y": int(round(float(y))),
                "z": int(round(float(z))),
                "inventory_slot": int(slot),
                "face": str(plan.get("face") or "up"),
            },
        )
        return started if _failed(started) else _wait_for_job(12)

    if action == "equip":
        slot = plan.get("slot")
        if slot is None and plan.get("item"):
            slot = _slot_for_item(plan.get("item"), before["inventory"])
        if slot is None:
            return {"ok": False, "error": "equip requires item or slot"}
        return _post("/equip-slot", {"slot": int(slot)})

    if action == "craft":
        width = plan.get("width")
        height = plan.get("height")
        grid = plan.get("grid")
        if not isinstance(grid, list):
            return {"ok": False, "error": "craft_grid_missing: grid must be a list"}
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "craft_dimensions_missing: provide integer width and height"}
        if not (1 <= width <= 3 and 1 <= height <= 3):
            return {"ok": False, "error": "craft_dimensions_invalid: width and height must be 1-3"}
        if len(grid) != width * height:
            return {
                "ok": False,
                "error": f"craft_grid_size_mismatch: {width}x{height} requires exactly "
                         f"{width * height} entries, received {len(grid)}. "
                         "Use empty strings for unused cells.",
            }
        if not all(isinstance(item, str) for item in grid):
            return {"ok": False, "error": "craft_grid_items_invalid: use item ID strings or empty strings"}
        normalized = [item.strip() for item in grid]
        required = {}
        for item in normalized:
            if item:
                required[item] = required.get(item, 0) + 1
        if not required:
            return {"ok": False, "error": "craft_grid_empty: supply at least one ingredient"}
        unavailable = [
            f"{item} {quantity} needed, {_item_count(before.get('inventory', []), item)} carried"
            for item, quantity in required.items()
            if _item_count(before.get("inventory", []), item) < quantity
        ]
        if unavailable:
            return {
                "ok": False,
                "error": "craft_ingredients_missing: " + "; ".join(unavailable),
            }
        return _post(
            "/craft",
            {
                "width": width,
                "height": height,
                "grid": normalized,
                "times": max(1, min(64, int(plan.get("times", 1) or 1))),
            },
        )

    return {"ok": False, "error": f"unsupported action: {action}"}


def _objective_success(plan, before, after, result):
    if _failed(result):
        return False

    action = plan["action"]
    if action == "scan_blocks":
        return bool(result.get("blocks"))
    if action == "idle":
        return True

    terminal = result.get("state") if isinstance(result, dict) else None
    if terminal in {"FAILED", "CANCELLED", "UNKNOWN"}:
        return False

    if action == "move_to":
        target = [plan.get("x"), plan.get("y"), plan.get("z")]
        if any(value is None for value in target):
            return False
        before_d = _distance(before["state"]["pos"], target)
        after_d = _distance(after["state"]["pos"], target)
        return after_d <= 2.5 or after_d < before_d - 0.35

    if action == "move_forward":
        target = plan.get("target")
        if isinstance(target, list) and len(target) == 3:
            before_d = _distance(before["state"]["pos"], target)
            after_d = _distance(after["state"]["pos"], target)
            return after_d < before_d - 0.25
        return terminal == "COMPLETED"

    if action == "look_at":
        return bool(result.get("ok", True))

    if action == "mine":
        return terminal == "COMPLETED" and str(result.get("reason", "")).startswith("block_mined")

    if action == "collect":
        wanted = plan.get("item")
        if wanted:
            return _item_count(after["inventory"], wanted) > _item_count(before["inventory"], wanted)
        return _inventory_total(after["inventory"]) > _inventory_total(before["inventory"]) or terminal == "COMPLETED"

    if action == "place":
        wanted = plan.get("item")
        if wanted:
            return _item_count(after["inventory"], wanted) < _item_count(before["inventory"], wanted) or terminal == "COMPLETED"
        return terminal == "COMPLETED"

    if action == "equip":
        return bool(result.get("ok", True))

    if action == "craft":
        return bool(result.get("ok", True))

    return False


def _parse_mining_metrics(result):
    reason = str((result or {}).get("reason") or "")
    if not reason.startswith("block_mined|"):
        return None

    values = {}
    for part in reason.split("|")[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        values[key.strip()] = value.strip()

    try:
        ticks = max(1, int(values.get("break_ticks", "0")))
    except ValueError:
        return None

    block = values.get("block") or "unknown"
    tool = values.get("tool") or "minecraft:air"
    seconds = ticks / 20.0
    reward = 1.0 + (20.0 / ticks)
    return {
        "context": f"mine:{block}",
        "option": tool,
        "break_ticks": ticks,
        "seconds": seconds,
        "reward": reward,
        "block": block,
        "tool": tool,
    }


def _record_efficiency(plan, result, success):
    if plan.get("action") != "mine":
        return None

    metrics = _parse_mining_metrics(result)
    if metrics is None:
        return None

    row = store.record_efficiency(
        metrics["context"],
        metrics["option"],
        metrics["seconds"],
        success,
        metrics["reward"] if success else 0.0,
        metadata=json.dumps(
            {
                "block": metrics["block"],
                "tool": metrics["tool"],
                "break_ticks": metrics["break_ticks"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    return {
        **metrics,
        "memory": row,
    }


def _record_learning(plan, result, before, after, success, promotable=True):
    intent = plan["intent"]
    hypothesis = plan["hypothesis"]
    actions = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    reusable_procedure = _generalized_procedure(plan)
    outcome = json.dumps(
        {"result": result, "before": before, "after": after},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    trial = store.record_skill_trial(
        intent,
        hypothesis,
        actions,
        outcome,
        success,
        skill_id=plan.get("skill_id"),
    )

    history = store.recent_skill_trials(intent, 8)
    successes = [item for item in history if item["success"]]
    failures = [item for item in history if not item["success"]]

    promoted = None
    # Two arbitrary successful steps are not proof of a reusable procedure.
    # Require repeatable success on the same mined block + actual tool, and
    # reject histories dominated by errors (such as stale target coordinates).
    def evidence_key(item):
        try:
            action_data = json.loads(item.get("actions") or "{}")
            if action_data.get("action") == "mine":
                outcome_data = json.loads(item.get("outcome") or "{}")
                metrics = _parse_mining_metrics(outcome_data.get("result"))
                if not metrics:
                    return None
                return ("mine", metrics["block"], metrics["tool"])
            return ("other", _generalized_procedure(action_data))
        except (TypeError, ValueError, AttributeError):
            return None

    current = {
        "actions": actions,
        "outcome": outcome,
    }
    matching = sum(
        1 for item in successes
        if evidence_key(item) == evidence_key(current)
    )
    reliable = (
        len(history) >= 3
        and len(successes) / len(history) >= 0.75
        and matching >= 3
        and evidence_key(current) is not None
    )
    if success and promotable and trial.get("skill") is None and reliable:
        promoted = store.save_learned_skill(
            str(plan.get("name") or intent)[:120],
            intent,
            reusable_procedure,
            source="self",
        )

    distinct_failures = {
        item["hypothesis"].strip().lower()
        for item in failures
        if item.get("hypothesis", "").strip()
    }
    teacher = None
    if len(failures) >= 3 and len(distinct_failures) >= 2:
        teacher = store.request_learning(
            f"intent:{intent}",
            "Local experiments remain blocked. Recent failures: "
            + " | ".join(
                f"{item['hypothesis']} => {item['outcome'][:350]}"
                for item in failures[:4]
            ),
            cooldown_seconds=1800,
        )

    return {
        "trial": trial,
        "promoted_skill": promoted,
        "teacher_request": teacher,
    }


def execute_plan(plan):
    started = time.monotonic()
    log_event("practice_host", "plan", plan=plan)

    try:
        before = _snapshot()
        result = _execute_action(plan, before)

        if plan["action"] == "scan_blocks":
            observation = {
                "action": "scan_blocks",
                "result": result,
            }
            store.record_event(
                "practice_observation",
                json.dumps(observation, ensure_ascii=False)[:1800],
            )
            log_event(
                "practice_host",
                "scan_result",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                plan=plan,
                result=result,
            )
            return {
                "ok": not _failed(result),
                "plan": plan,
                "result": result,
                "learning": None,
            }

        after = _snapshot()
        terminal = result.get("state") if isinstance(result, dict) else None
        if terminal == "RUNNING":
            summary = {
                "ok": None,
                "status": "pending",
                "plan": plan,
                "result": result,
                "before": before,
                "after": after,
                "learning": None,
                "efficiency": None,
            }
            store.record_event(
                "practice_pending",
                (
                    f"intent={plan['intent']} action={plan['action']} "
                    f"reason={result.get('reason', 'running')}"
                )[:1200],
            )
            log_event(
                "practice_host",
                "action_pending",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                **summary,
            )
            return summary

        success = _objective_success(plan, before, after, result)
        learning = None
        efficiency = None
        if plan["action"] != "idle":
            promotable = not (
                plan["action"] in {"move_to", "mine"}
                and terminal == "RUNNING"
            )
            learning = _record_learning(
                plan,
                result,
                before,
                after,
                success,
                promotable=promotable,
            )
            efficiency = _record_efficiency(plan, result, success)

        summary = {
            "ok": success,
            "plan": plan,
            "result": result,
            "before": before,
            "after": after,
            "learning": learning,
            "efficiency": efficiency,
        }
        store.record_event(
            "practice_result",
            (
                f"intent={plan['intent']} action={plan['action']} "
                f"success={success} result={result}"
            )[:1600],
        )
        log_event(
            "practice_host",
            "action_result",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            **summary,
        )
        return summary
    except Exception as error:
        log_exception("practice_host", "execute_error", error, plan=plan)
        store.record_event(
            "practice_error",
            f"{type(error).__name__}: {error}",
        )
        return {
            "ok": False,
            "plan": plan,
            "error": f"{type(error).__name__}: {error}",
        }
