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
    response.raise_for_status()
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
        if plan.get("tool"):
            procedure["tool"] = plan.get("tool")
        procedure["verify"] = "mining job completes and resulting world/inventory evidence changes"
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
    state = _get("/state")
    bx = math.floor(float(state.get("x", 0)))
    by = math.floor(float(state.get("y", 0)))
    bz = math.floor(float(state.get("z", 0)))
    for x, y, z in [
        (bx + 1, by, bz),
        (bx - 1, by, bz),
        (bx, by, bz + 1),
        (bx, by, bz - 1),
        (bx + 1, by, bz + 1),
        (bx + 1, by, bz - 1),
        (bx - 1, by, bz + 1),
        (bx - 1, by, bz - 1),
    ]:
        started = _post(
            "/place-block",
            {
                "x": x,
                "y": y,
                "z": z,
                "inventory_slot": int(slot),
                "face": face or "up",
            },
        )
        if _failed(started):
            continue
        result = _wait_for_job(10)
        if result.get("state") == "COMPLETED":
            result["placed_at"] = [x, y, z]
            return result
    return {"ok": False, "error": "no nearby placement candidate succeeded"}


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
        if current_distance <= 4.25:
            return {
                "ok": False,
                "error": (
                    "move_to target is already within interaction range. "
                    "If this coordinate is a block to mine/use, call that physical "
                    "action directly instead of trying to stand inside the block."
                ),
                "distance": round(current_distance, 2),
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

        requested_tool = str(plan.get("tool") or "").strip()
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

        started = _post(
            "/mine-block",
            {"x": int(round(float(x))), "y": int(round(float(y))), "z": int(round(float(z)))},
        )
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
        if width is None or height is None or not isinstance(grid, list):
            return {"ok": False, "error": "craft requires width, height and grid"}
        return _post(
            "/craft",
            {
                "width": int(width),
                "height": int(height),
                "grid": [str(item) for item in grid],
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
    if success and promotable and trial.get("skill") is None and len(successes) >= 2:
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
        success = _objective_success(plan, before, after, result)
        learning = None
        efficiency = None
        if plan["action"] != "idle":
            terminal = result.get("state") if isinstance(result, dict) else None
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
