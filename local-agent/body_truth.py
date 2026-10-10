"""Shared, evidence-grounded status for a Minecraft companion.

Chat and physical work must read the same action outcome. A queued goal is not
a successful craft; a scan is not a mined block. This module is deliberately
independent from model completions so assertions can be tested offline.
"""
import re
import time

ACHIEVEMENT = re.compile(
    r"\b(?:i|we)\s+(?:have\s+|just\s+|already\s+|finally\s+|"
    r"managed\s+to\s+|successfully\s+|)\b"
    r"(?:got|made|crafted|built|placed|mined|chopped|collected|finished)\b",
    re.I,
)
# Support contractions and casual chat ("i've crafted", "i got").
CLAIM = re.compile(
    r"\b(?:i|we)(?:'ve| have| just| already| finally)?\s+"
    r"(?:got|made|crafted|built|placed|mined|chopped|collected|finished)\b",
    re.I,
)
PHYSICAL_VERBS = ("crafted", "made", "got", "built", "placed", "mined", "chopped", "collected")


def target_from_goal(goal):
    if not goal or goal.get("source") != "user":
        return None
    title = str(goal.get("title") or "").lower().strip()
    if title.startswith("craft minecraft:"):
        return title.removeprefix("craft ")
    if title.startswith("craft a "):
        item = title.removeprefix("craft a ").replace(" ", "_")
        return "minecraft:" + item
    if title.startswith("obtain an axe"):
        return "minecraft:wooden_axe"
    return None


def current_user_goal(goals):
    return next((goal for goal in goals if goal.get("source") == "user"), None)


def summarize_action(action):
    if not action:
        return "no verified physical action recorded"
    verb = action.get("action", "unknown")
    result = action.get("result") or {}
    ok = action.get("ok")
    if verb == "scan_blocks":
        return "scanned blocks; observed " + str(len(result.get("blocks") or [])) + " targets (nothing collected)"
    if ok:
        if verb == "craft":
            return "crafted " + str(result.get("item") or "unknown item") + " x" + str(result.get("count") or 1)
        if verb == "mine":
            return "mined block: " + str(result.get("reason") or "verified by Minecraft")
        if verb == "place":
            return "placed a block, verified by Minecraft"
        return "completed " + str(verb)
    return str(verb) + " failed: " + str(result.get("reason") or result.get("error") or "not completed")[:125]


def record_action(runtime, plan, execution):
    """Call once after a real action result, not after a model prediction."""
    if not isinstance(execution, dict):
        return
    result = execution.get("result") or {}
    item = str(result.get("item") or "")
    entry = {
        "at": time.monotonic(),
        "action": plan.get("action"),
        "intent": plan.get("intent"),
        "goal_id": plan.get("goal_id"),
        "ok": bool(execution.get("ok")),
        "result": {
            k: result[k] for k in ("item", "count", "state", "reason", "error", "blocks")
            if k in result
        },
    }
    # Keep only compact scan evidence. The full block list is still in JSONL.
    if "blocks" in entry["result"]:
        entry["result"]["blocks"] = (entry["result"]["blocks"] or [])[:4]
    runtime["body_last_action"] = entry
    if entry["ok"] and entry["action"] in {"craft", "mine", "place", "collect"}:
        runtime["body_last_verified"] = entry
    history = runtime.setdefault("body_recent_actions", [])
    history.append(entry)
    del history[:-18]


def verified_craft_for_goal(runtime, goal):
    target = target_from_goal(goal)
    if not target:
        return False
    done = runtime.get("body_last_verified") or {}
    if (done.get("action") == "craft" and done.get("ok")
            and (done.get("result") or {}).get("item") == target
            and done.get("goal_id") == goal.get("id")):
        return True
    return False


def has_inventory_item(runtime, item):
    inventory = (runtime.get("awareness") or {}).get("inventory") or []
    return any(str(row.get("item") or "") == item and int(row.get("count") or 0) > 0
               for row in inventory)


def grounded_status(runtime, goals):
    goal = current_user_goal(goals)
    if goal:
        label = str(goal.get("title") or "").lower()
        target = target_from_goal(goal)
        if target and not verified_craft_for_goal(runtime, goal):
            return "working on " + target.split(":")[-1].replace("_", " ") + "; not crafted yet"
        if target and verified_craft_for_goal(runtime, goal):
            return "just crafted " + target.split(":")[-1].replace("_", " ")
        return "working on " + label
    body = runtime.get("body_last_action")
    if body and time.monotonic() - float(body.get("at") or 0) < 45:
        if body.get("action") == "scan_blocks":
            return "scanning nearby blocks"
        if body.get("ok"):
            return "just " + summarize_action(body)
        return "stuck: " + summarize_action(body)
    return "no active player task"


def grounded_chat(answer, message, runtime, goals):
    """Return compact truthful chat; reject unsupported achievement narration."""
    text = str(answer or "").strip()
    message = str(message or "").lower()
    goal = current_user_goal(goals)
    target = target_from_goal(goal)
    action = runtime.get("body_last_action") or {}
    recent = runtime.get("body_recent_actions") or []
    if "axe head" in text.lower() or "shape the head" in text.lower():
        return "axes are crafted from planks and sticks, no separate head"
    if any(word in message for word in ("loop", "scanning over", "stuck scanning")):
        scans = sum(1 for row in recent if row.get("action") == "scan_blocks")
        if scans >= 3:
            return "yea, stuck rescanning. no progress yet"
    if target and any(token in message for token in (
        "make", "craft", "already", "finished", "done", "got it", "ur task", "your task"
    )):
        if CLAIM.search(text):
            if not verified_craft_for_goal(runtime, goal):
                return "not yet, still haven't crafted " + target.split(":")[-1].replace("_", " ")
    if CLAIM.search(text):
        # Generic craft claims require a recent world-verified result.
        if re.search(r"\b(?:craft|made|got|finish)", text, re.I):
            done = runtime.get("body_last_verified") or {}
            if (done.get("action") != "craft" or
                    time.monotonic() - float(done.get("at") or 0) > 90):
                return grounded_status(runtime, goals)
            product = str((done.get("result") or {}).get("item") or "")
            stated_item = product.split(":")[-1].replace("_", " ")
            if product and stated_item not in text.lower():
                # "I got the hoe" cannot piggyback on a verified pickaxe
                # craft from another task. Generic "got it" is permitted
                # only for the current active goal's own verified result.
                generic_it = re.search(
                    r"\b(?:got|made|crafted|finished)\s+(?:it|that)\b",
                    text, re.I,
                )
                if not generic_it or (target and target != product):
                    return grounded_status(runtime, goals)
        elif action.get("ok") is not True:
            return grounded_status(runtime, goals)
    return text
