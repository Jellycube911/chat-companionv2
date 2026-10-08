import functools
import time
import traceback
from typing import Literal

import requests
from mcp.server import MCPServer

from memory_store import DATA_DIR, store


MINECRAFT_URL = "http://127.0.0.1:8765"
ERROR_LOG = DATA_DIR / "mcp_errors.log"
mcp = MCPServer("minecraft-companion")


def _tool_guard(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                with ERROR_LOG.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"{fn.__name__}: {type(error).__name__}: {error}\n"
                    )
                    handle.write(traceback.format_exc())
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"{fn.__name__} failed: {type(error).__name__}: {error}",
            }

    return wrapped


def _request(method, path, payload=None):
    try:
        kwargs = {"timeout": 5}
        if payload is not None:
            kwargs["json"] = payload
        response = requests.request(
            method,
            f"{MINECRAFT_URL}{path}",
            **kwargs,
        )
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:500]}

        if response.ok:
            return body

        message = (
            body.get("error")
            if isinstance(body, dict)
            else None
        ) or f"Minecraft bridge returned HTTP {response.status_code}"

        return {
            "ok": False,
            "error": message,
            "status": response.status_code,
            "path": path,
        }
    except requests.RequestException as error:
        return {
            "ok": False,
            "error": f"Minecraft bridge unavailable: {error}",
            "path": path,
        }


def _get(path):
    return _request("GET", path)


def _post(path, payload=None):
    return _request("POST", path, payload)


def _failed(value):
    return isinstance(value, dict) and value.get("ok") is False


def _wait_for_job(timeout=10.0):
    deadline = time.time() + timeout
    last = None

    while time.time() < deadline:
        state = _get("/state")
        if _failed(state):
            return state

        last = state
        if not state.get("jobActive", False):
            last_job = state.get("lastJob") or {}
            return {
                "state": last_job.get("state", "IDLE"),
                "reason": last_job.get("reason", "idle"),
                "pos": [
                    round(state.get("x", 0), 2),
                    round(state.get("y", 0), 2),
                    round(state.get("z", 0), 2),
                ],
            }

        if state.get("jobState") != "RUNNING":
            return {
                "state": state.get("jobState"),
                "reason": state.get("jobReason"),
                "pos": [
                    round(state.get("x", 0), 2),
                    round(state.get("y", 0), 2),
                    round(state.get("z", 0), 2),
                ],
            }
        time.sleep(0.25)

    return {
        "state": last.get("jobState") if last else "UNKNOWN",
        "reason": last.get("jobReason") if last else "timeout",
    }


def _observe(view: Literal["state", "entities", "blocks", "inventory", "vision"]):
    """Read one compact Minecraft observation. Prefer the narrowest useful view."""
    if view == "state":
        state = _get("/state")
        if _failed(state):
            return state

        owner = state.get("owner", {})
        result = {
            "server": state.get("serverState", "running"),
            "pos": [
                round(state.get("x", 0), 2),
                round(state.get("y", 0), 2),
                round(state.get("z", 0), 2),
            ],
            "hp": [state.get("health"), state.get("maxHealth")],
            "facing": state.get("facing"),
            "owner": {
                "distance": round(owner.get("distance", 0), 2),
                "delta": [
                    round(owner.get("dx", 0), 2),
                    round(owner.get("dy", 0), 2),
                    round(owner.get("dz", 0), 2),
                ],
            },
        }

        if state.get("jobActive"):
            result["job"] = [
                state.get("jobType"),
                state.get("jobState"),
                state.get("jobReason"),
            ]
        return result

    if view == "vision":
        result = _get("/vision")
        if _failed(result):
            return result
        return {
            "facing": [round(result.get("yaw", 0), 1), round(result.get("pitch", 0), 1)],
            "fov": [result.get("horizontalFov"), result.get("verticalFov")],
            "blocks": [
                {
                    "type": block.get("type"),
                    "d": round(block.get("distance", 0), 2),
                    "pos": [block.get("x"), block.get("y"), block.get("z")],
                    "screen": [block.get("yawOffset"), block.get("pitchOffset")],
                }
                for block in result.get("blocks", [])
            ],
            "entities": [
                {
                    "id": entity.get("uuid"),
                    "type": entity.get("type"),
                    "d": round(entity.get("distance", 0), 2),
                    "pos": [
                        round(entity.get("x", 0), 1),
                        round(entity.get("y", 0), 1),
                        round(entity.get("z", 0), 1),
                    ],
                    **({"owner": True} if entity.get("owner") else {}),
                    **({"hostile": True} if entity.get("hostile") else {}),
                    **({"item": entity.get("item"), "count": entity.get("count", 1)}
                       if entity.get("item") else {}),
                }
                for entity in result.get("entities", [])
            ],
        }

    if view == "entities":
        result = _get("/nearby-entities")
        if _failed(result):
            return result
        compact = []
        for entity in result.get("entities", []):
            item = {
                "id": entity.get("uuid"),
                "type": entity.get("type"),
                "d": round(entity.get("distance", 0), 2),
                "pos": [
                    round(entity.get("x", 0), 1),
                    round(entity.get("y", 0), 1),
                    round(entity.get("z", 0), 1),
                ],
            }
            if entity.get("isOwner"):
                item["owner"] = True
            if entity.get("hostile"):
                item["hostile"] = True
            if "health" in entity:
                item["hp"] = round(entity["health"], 1)
            if "droppedItem" in entity:
                item["item"] = entity["droppedItem"]
                item["count"] = entity.get("count", 1)
            compact.append(item)
        return compact

    if view == "blocks":
        result = _get("/nearby-blocks")
        if _failed(result):
            return result
        return [
            {
                "type": block["type"],
                "count": block["count"],
                "d": round(block["nearestDistance"], 2),
                "pos": [
                    block["nearestX"],
                    block["nearestY"],
                    block["nearestZ"],
                ],
            }
            for block in result.get("blocks", [])
        ]

    result = _get("/inventory")
    if _failed(result):
        return result
    return [
        {
            "slot": item["slot"],
            "item": item["item"],
            "count": item["count"],
        }
        for item in result.get("items", [])
    ]




@mcp.tool()
@_tool_guard
def observe(view: Literal["state", "entities", "blocks", "inventory", "vision"]):
    """Read one compact Minecraft sense. vision is view-dependent ray-based sight."""
    return _observe(view)


@mcp.tool()
@_tool_guard
def navigate(
    action: Literal["move_to", "move_forward", "follow", "stop", "resume"],
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
):
    """Move the companion body or control its current navigation job."""
    if action == "move_to":
        if x is None or y is None or z is None:
            return {"ok": False, "error": "move_to requires x, y and z"}
        started = _post("/move-to", {"x": x, "y": y, "z": z})
        if _failed(started):
            return started
        return _wait_for_job(12)

    if action == "move_forward":
        started = _post("/move-forward")
        if _failed(started):
            return started
        return _wait_for_job(8)

    if action == "follow":
        return _post("/follow-owner")

    if action == "stop":
        return _post("/stop-action")

    started = _post("/resume-action")
    if _failed(started):
        return started
    return _wait_for_job(8)


@mcp.tool()
@_tool_guard
def world_action(
    action: Literal[
        "collect",
        "mine",
        "place",
        "attack",
        "take_held",
        "equip",
    ],
    x: int | None = None,
    y: int | None = None,
    z: int | None = None,
    slot: int | None = None,
    face: Literal["up", "down", "north", "south", "east", "west"] | None = None,
    entity_id: str | None = None,
):
    """Perform one physical inventory/world action using the companion body."""
    if action == "collect":
        started = _post("/collect-items")
        if _failed(started):
            return started
        result = _wait_for_job(12)
        result["inventory"] = _observe("inventory")
        return result

    if action == "mine":
        if x is None or y is None or z is None:
            return {"ok": False, "error": "mine requires x, y and z"}
        started = _post("/mine-block", {"x": x, "y": y, "z": z})
        if _failed(started):
            return started
        result = _wait_for_job(15)
        result["inventory"] = _observe("inventory")
        return result

    if action == "place":
        if x is None or y is None or z is None or slot is None:
            return {"ok": False, "error": "place requires x, y, z and slot"}
        started = _post(
            "/place-block",
            {
                "x": x,
                "y": y,
                "z": z,
                "inventory_slot": slot,
                "face": face or "up",
            },
        )
        if _failed(started):
            return started
        return _wait_for_job(12)

    if action == "attack":
        if not entity_id:
            return {"ok": False, "error": "attack requires entity_id"}
        started = _post("/attack-entity", {"entity_id": entity_id})
        if _failed(started):
            return started
        return _wait_for_job(15)

    if action == "take_held":
        result = _post("/take-held-item")
        if _failed(result):
            return result
        return {"ok": True, "inventory": _observe("inventory")}

    if slot is None:
        return {"ok": False, "error": "equip requires slot"}
    return _post("/equip-slot", {"slot": slot})


@mcp.tool()
@_tool_guard
def craft(
    width: int,
    height: int,
    grid: list[str],
    times: int = 1,
):
    """Craft a real registered recipe from companion inventory. Grids above 2x2 require a nearby crafting table."""
    return _post(
        "/craft",
        {
            "width": width,
            "height": height,
            "grid": grid,
            "times": times,
        },
    )


@mcp.tool()
@_tool_guard
def say(message: str):
    """Say a deliberate extra message in Minecraft chat."""
    return _post("/say", {"message": message[:4096]})


@mcp.tool()
@_tool_guard
def remember(
    kind: Literal["fact", "preference", "location", "lesson", "plan", "relationship", "skill"],
    key: str,
    content: str,
    importance: int = 5,
):
    """Persist a durable memory locally. Use only for information worth recalling in future sessions."""
    return store.remember(kind, key, content, importance)


@mcp.tool()
@_tool_guard
def recall_memory(query: str, limit: int = 6):
    """Retrieve a few locally stored memories relevant to the current situation."""
    return store.recall(query, limit)


@mcp.tool()
@_tool_guard
def goals(
    action: Literal["create", "list", "update"],
    title: str | None = None,
    description: str | None = None,
    goal_id: int | None = None,
    status: Literal["active", "paused", "completed", "cancelled", "all"] | None = None,
    priority: int | None = None,
):
    """Create, inspect, or update persistent self-directed goals."""
    if action == "create":
        if not title:
            return {"ok": False, "error": "create requires title"}
        return store.create_goal(
            title,
            description or "",
            5 if priority is None else priority,
            "self",
        )
    if action == "list":
        return store.list_goals(status or "active", 8)
    if goal_id is None:
        return {"ok": False, "error": "update requires goal_id"}
    return store.update_goal(
        goal_id,
        status=None if status in {None, "all"} else status,
        description=description,
        priority=priority,
    )


@mcp.tool()
@_tool_guard
def memory_status():
    """Return counts and local database location without dumping memory contents."""
    return store.stats()


if __name__ == "__main__":
    mcp.run()
