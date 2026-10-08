import math
import time
from collections import Counter

import requests

from memory_store import store


MINECRAFT_URL = "http://127.0.0.1:8765"

LOG_PATTERNS = ["_log", "_stem", "_hyphae"]
PLANK_SUFFIX = "_planks"


class SkillCancelled(Exception):
    pass


class SkillFailure(Exception):
    pass


def _request(method, path, payload=None, timeout=8):
    try:
        kwargs = {"timeout": timeout}
        if payload is not None:
            kwargs["json"] = payload
        response = requests.request(method, f"{MINECRAFT_URL}{path}", **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:500]}
        if response.ok:
            return body
        message = body.get("error") if isinstance(body, dict) else None
        return {
            "ok": False,
            "error": message or f"HTTP {response.status_code}",
            "status": response.status_code,
            "path": path,
        }
    except requests.RequestException as error:
        return {"ok": False, "error": str(error), "path": path}


def _get(path):
    return _request("GET", path)


def _post(path, payload=None):
    return _request("POST", path, payload)


def _require(result, what):
    if isinstance(result, dict) and result.get("ok") is False:
        raise SkillFailure(f"{what}: {result.get('error', 'failed')}")
    return result


def _task(task_id):
    current = store.task(task_id)
    if current is None:
        raise SkillCancelled()
    if current["status"] == "cancelled":
        raise SkillCancelled()
    return current


def _progress(task_id, message):
    _task(task_id)
    store.update_task(task_id, progress=message)
    store.record_event("skill_progress", f"Task #{task_id}: {message}")


def _wait_job(task_id, timeout=25):
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        _task(task_id)
        state = _require(_get("/state"), "read state")
        last_state = state
        if not state.get("jobActive", False):
            last = state.get("lastJob") or {}
            status = last.get("state", "UNKNOWN")
            reason = last.get("reason", "idle")
            if status == "COMPLETED":
                return {"ok": True, "state": status, "reason": reason}
            if status in {"FAILED", "CANCELLED"}:
                return {"ok": False, "state": status, "reason": reason}
            return {"ok": True, "state": status, "reason": reason}
        if state.get("jobState") not in {None, "RUNNING"}:
            return {
                "ok": state.get("jobState") == "COMPLETED",
                "state": state.get("jobState"),
                "reason": state.get("jobReason"),
            }
        time.sleep(0.2)

    return {
        "ok": False,
        "state": last_state.get("jobState") if last_state else "UNKNOWN",
        "reason": "timeout",
    }


def _inventory():
    payload = _require(_get("/inventory"), "read inventory")
    return payload.get("items", [])


def _item_counts():
    counts = Counter()
    for item in _inventory():
        counts[item["item"]] += int(item["count"])
    return counts


def _count_matching(predicate):
    total = 0
    for item, count in _item_counts().items():
        if predicate(item):
            total += count
    return total


def _slot_matching(predicate):
    for item in _inventory():
        if predicate(item["item"]):
            return int(item["slot"]), item["item"], int(item["count"])
    return None


def _find_blocks(*, exact=None, contains=None, radius=20, limit=64):
    payload = _require(
        _post(
            "/find-blocks",
            {
                "exact": exact or [],
                "contains": contains or [],
                "radius": radius,
                "limit": limit,
            },
        ),
        "find blocks",
    )
    return payload.get("blocks", [])


def _start_and_wait(task_id, path, payload=None, timeout=25):
    started = _require(_post(path, payload), f"start {path}")
    result = _wait_job(task_id, timeout)
    if not result.get("ok"):
        raise SkillFailure(f"{path}: {result.get('reason', 'failed')}")
    return started, result


def _collect(task_id):
    started = _post("/collect-items")
    if isinstance(started, dict) and started.get("ok") is False:
        return started
    return _wait_job(task_id, 18)


def _mine(task_id, block):
    _progress(
        task_id,
        f"mining {block['type']} at {block['x']} {block['y']} {block['z']}",
    )
    _start_and_wait(
        task_id,
        "/mine-block",
        {"x": block["x"], "y": block["y"], "z": block["z"]},
        30,
    )


def _equip(task_id, predicate, label):
    found = _slot_matching(predicate)
    if found is None:
        raise SkillFailure(f"no {label} in inventory")
    slot, item_id, _ = found
    _progress(task_id, f"equipping {item_id}")
    _require(_post("/equip-slot", {"slot": slot}), f"equip {item_id}")
    return item_id


def _gather_logs(task_id, target_total):
    while _count_matching(
        lambda item: any(pattern in item for pattern in LOG_PATTERNS)
    ) < target_total:
        _task(task_id)
        state = _require(_get("/state"), "read state")
        blocks = _find_blocks(contains=LOG_PATTERNS, radius=20, limit=64)
        max_y = float(state.get("y", 0)) + 2.5
        blocks = [block for block in blocks if float(block["y"]) <= max_y]

        if not blocks:
            raise SkillFailure(
                "no reachable logs found within 20 blocks; move closer to trees and resume"
            )

        before = _count_matching(
            lambda item: any(pattern in item for pattern in LOG_PATTERNS)
        )
        progress_made = False

        for block in blocks[:8]:
            if _count_matching(
                lambda item: any(pattern in item for pattern in LOG_PATTERNS)
            ) >= target_total:
                break
            try:
                _mine(task_id, block)
                _collect(task_id)
                after = _count_matching(
                    lambda item: any(pattern in item for pattern in LOG_PATTERNS)
                )
                if after > before:
                    before = after
                    progress_made = True
            except SkillFailure:
                continue

        if not progress_made:
            raise SkillFailure("found logs but could not harvest any reachable log blocks")


def _craft(task_id, width, height, grid, times=1, description="crafting"):
    _progress(task_id, description)
    return _require(
        _post(
            "/craft",
            {
                "width": width,
                "height": height,
                "grid": grid,
                "times": times,
            },
        ),
        description,
    )


def _ensure_planks(task_id, target_count):
    current = _count_matching(lambda item: item.endswith(PLANK_SUFFIX))
    if current >= target_count:
        return

    needed_planks = target_count - current
    needed_logs = math.ceil(needed_planks / 4)
    current_logs = _count_matching(
        lambda item: any(pattern in item for pattern in LOG_PATTERNS)
    )
    if current_logs < needed_logs:
        _gather_logs(task_id, needed_logs)

    while _count_matching(lambda item: item.endswith(PLANK_SUFFIX)) < target_count:
        counts = _item_counts()
        log_entry = next(
            (
                (item, count)
                for item, count in counts.items()
                if any(pattern in item for pattern in LOG_PATTERNS) and count > 0
            ),
            None,
        )
        if log_entry is None:
            raise SkillFailure("ran out of logs while crafting planks")

        log_id, count = log_entry
        remaining = target_count - _count_matching(
            lambda item: item.endswith(PLANK_SUFFIX)
        )
        crafts = min(count, max(1, math.ceil(remaining / 4)))
        _craft(
            task_id,
            1,
            1,
            [log_id],
            crafts,
            f"crafting {crafts} log(s) into planks",
        )


def _take_plank_ids(count):
    ids = []
    for item in _inventory():
        if item["item"].endswith(PLANK_SUFFIX):
            ids.extend([item["item"]] * int(item["count"]))
            if len(ids) >= count:
                return ids[:count]
    return ids


def _ensure_sticks(task_id, target_count):
    if _item_counts().get("minecraft:stick", 0) >= target_count:
        return
    _ensure_planks(task_id, 2)
    planks = _take_plank_ids(2)
    if len(planks) < 2:
        raise SkillFailure("not enough planks to craft sticks")
    _craft(
        task_id,
        1,
        2,
        [planks[0], planks[1]],
        1,
        "crafting sticks",
    )


def _find_nearby_table():
    return _find_blocks(
        exact=["minecraft:crafting_table"],
        radius=4,
        limit=8,
    )


def _place_crafting_table(task_id):
    tables = _find_nearby_table()
    if tables:
        return tables[0]

    _ensure_planks(task_id, 4)
    planks = _take_plank_ids(4)
    if len(planks) < 4:
        raise SkillFailure("not enough planks for a crafting table")

    _craft(
        task_id,
        2,
        2,
        planks[:4],
        1,
        "crafting a crafting table",
    )

    table_slot = _slot_matching(lambda item: item == "minecraft:crafting_table")
    if table_slot is None:
        raise SkillFailure("crafted a table but cannot find it in inventory")

    slot = table_slot[0]
    state = _require(_get("/state"), "read state")
    bx = math.floor(float(state["x"]))
    by = math.floor(float(state["y"]))
    bz = math.floor(float(state["z"]))

    candidates = [
        (bx + 1, by, bz),
        (bx - 1, by, bz),
        (bx, by, bz + 1),
        (bx, by, bz - 1),
        (bx + 1, by, bz + 1),
        (bx - 1, by, bz - 1),
    ]

    for x, y, z in candidates:
        _task(task_id)
        started = _post(
            "/place-block",
            {
                "x": x,
                "y": y,
                "z": z,
                "inventory_slot": slot,
                "face": "up",
            },
        )
        if isinstance(started, dict) and started.get("ok") is False:
            continue
        result = _wait_job(task_id, 15)
        if result.get("ok"):
            tables = _find_nearby_table()
            if tables:
                return tables[0]

    raise SkillFailure("could not place crafting table on nearby ground")


def _craft_pickaxe(task_id, head_item, output_label):
    _ensure_sticks(task_id, 2)
    planks = _take_plank_ids(3) if head_item == "planks" else None

    if head_item == "planks":
        _ensure_planks(task_id, 3)
        planks = _take_plank_ids(3)
        if len(planks) < 3:
            raise SkillFailure("not enough planks for wooden pickaxe")
        heads = planks
    else:
        counts = _item_counts()
        if counts.get(head_item, 0) < 3:
            raise SkillFailure(f"not enough {head_item}")
        heads = [head_item, head_item, head_item]

    grid = [
        heads[0], heads[1], heads[2],
        "", "minecraft:stick", "",
        "", "minecraft:stick", "",
    ]
    return _craft(
        task_id,
        3,
        3,
        grid,
        1,
        f"crafting {output_label}",
    )


def gather_logs(task_id, args):
    count = max(1, min(64, int(args.get("count", 8))))
    _progress(task_id, f"gathering {count} logs")
    _gather_logs(task_id, count)
    _collect(task_id)
    total = _count_matching(
        lambda item: any(pattern in item for pattern in LOG_PATTERNS)
    )
    return f"gathered logs; inventory now contains {total} log/stem blocks"


def make_stone_pickaxe(task_id, args):
    if _item_counts().get("minecraft:stone_pickaxe", 0) > 0:
        return "stone pickaxe already in inventory"

    _progress(task_id, "preparing wood for tool progression")
    _ensure_planks(task_id, 9)
    _ensure_sticks(task_id, 4)
    _place_crafting_table(task_id)

    if _item_counts().get("minecraft:wooden_pickaxe", 0) == 0:
        _craft_pickaxe(task_id, "planks", "wooden pickaxe")

    _equip(
        task_id,
        lambda item: item == "minecraft:wooden_pickaxe",
        "wooden pickaxe",
    )

    while _item_counts().get("minecraft:cobblestone", 0) < 3:
        blocks = _find_blocks(exact=["minecraft:stone"], radius=20, limit=32)
        if not blocks:
            raise SkillFailure("no stone found within 20 blocks")

        before = _item_counts().get("minecraft:cobblestone", 0)
        progress_made = False
        for block in blocks[:6]:
            if _item_counts().get("minecraft:cobblestone", 0) >= 3:
                break
            try:
                _mine(task_id, block)
                _collect(task_id)
                after = _item_counts().get("minecraft:cobblestone", 0)
                if after > before:
                    before = after
                    progress_made = True
            except SkillFailure:
                continue

        if not progress_made:
            raise SkillFailure("stone was found but could not be harvested")

    _place_crafting_table(task_id)
    _craft_pickaxe(task_id, "minecraft:cobblestone", "stone pickaxe")

    if _item_counts().get("minecraft:stone_pickaxe", 0) <= 0:
        raise SkillFailure("stone pickaxe recipe finished but output is missing")

    return "crafted and verified a stone pickaxe"


def _plank_slot():
    found = _slot_matching(lambda item: item.endswith(PLANK_SUFFIX))
    if found is None:
        raise SkillFailure("no planks available for building")
    return found[0]


def _place_build_block(task_id, x, y, z, face="up"):
    for attempt in range(2):
        _task(task_id)
        slot = _plank_slot()
        started = _post(
            "/place-block",
            {
                "x": x,
                "y": y,
                "z": z,
                "inventory_slot": slot,
                "face": face,
            },
        )
        if isinstance(started, dict) and started.get("ok") is False:
            if attempt == 0 and "empty_inventory_slot" in str(started):
                continue
            return False
        result = _wait_job(task_id, 18)
        if result.get("ok"):
            return True
    return False


def build_basic_house(task_id, args):
    width = max(4, min(7, int(args.get("width", 5))))
    length = max(4, min(7, int(args.get("length", 5))))
    height = max(3, min(4, int(args.get("height", 3))))

    perimeter = 2 * width + 2 * max(0, length - 2)
    wall_blocks = perimeter * height - 2
    roof_blocks = width * length
    required_planks = wall_blocks + roof_blocks + 6

    _progress(
        task_id,
        f"preparing {required_planks} planks for a {width}x{length} house",
    )
    _ensure_planks(task_id, required_planks)

    state = _require(_get("/state"), "read state")
    base_y = math.floor(float(state["y"]))
    start_x = math.floor(float(state["x"])) + 3
    start_z = math.floor(float(state["z"])) - length // 2
    end_x = start_x + width - 1
    end_z = start_z + length - 1
    door_z = start_z + length // 2

    placed = 0

    for layer in range(height):
        y = base_y + layer
        ring = []
        for x in range(start_x, end_x + 1):
            ring.append((x, y, start_z))
            if end_z != start_z:
                ring.append((x, y, end_z))
        for z in range(start_z + 1, end_z):
            ring.append((start_x, y, z))
            if end_x != start_x:
                ring.append((end_x, y, z))

        seen = set()
        for x, py, z in ring:
            if (x, py, z) in seen:
                continue
            seen.add((x, py, z))
            if x == start_x and z == door_z and layer in {0, 1}:
                continue

            _progress(
                task_id,
                f"building walls: {placed}/{wall_blocks + roof_blocks} blocks",
            )
            if _place_build_block(task_id, x, py, z, "up"):
                placed += 1

    roof_y = base_y + height
    for z in range(start_z, end_z + 1):
        for index, x in enumerate(range(start_x, end_x + 1)):
            face = "up" if index == 0 else "east"
            _progress(
                task_id,
                f"building roof: {placed}/{wall_blocks + roof_blocks} blocks",
            )
            if _place_build_block(task_id, x, roof_y, z, face):
                placed += 1

    if placed < int((wall_blocks + roof_blocks) * 0.75):
        raise SkillFailure(
            f"house build made insufficient progress ({placed} blocks placed)"
        )

    return (
        f"built a basic {width}x{length} wooden shelter "
        f"with {placed} placed blocks"
    )


SKILLS = {
    "gather_logs": gather_logs,
    "make_stone_pickaxe": make_stone_pickaxe,
    "build_basic_house": build_basic_house,
}


def execute_task(task):
    task_id = int(task["id"])
    skill = task["skill"]
    args = task.get("args") or {}

    fn = SKILLS.get(skill)
    if fn is None:
        raise SkillFailure(f"unknown skill: {skill}")

    store.record_event("skill_start", f"Task #{task_id} started: {skill} {args}")
    _progress(task_id, f"running {skill}")
    result = fn(task_id, args)
    store.update_task(
        task_id,
        status="completed",
        progress=result,
        error="",
    )
    store.remember(
        "skill",
        f"skill.{skill}",
        f"Local executor successfully completed {skill}. Latest result: {result}",
        6,
    )
    goal_id = args.get("goal_id")
    if goal_id is not None:
        try:
            store.update_goal(int(goal_id), status="completed")
        except Exception:
            pass
    store.record_event("skill_complete", f"Task #{task_id}: {result}")
    return result


def run_task(task):
    try:
        return execute_task(task)
    except SkillCancelled:
        store.update_task(
            task["id"],
            status="cancelled",
            progress="cancelled",
        )
        store.record_event("skill_cancel", f"Task #{task['id']} cancelled")
        return None
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        store.update_task(
            task["id"],
            status="failed",
            progress="failed",
            error=message,
        )
        store.remember(
            "lesson",
            f"failure.{task['skill']}",
            f"Most recent {task['skill']} failure: {message}. Do not blindly repeat the same approach; change the environment, prerequisites, or plan first.",
            7,
        )
        store.record_event(
            "skill_failed",
            f"Task #{task['id']} {task['skill']} failed: {message}",
        )
        return None
