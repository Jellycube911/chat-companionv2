import time
from typing import Literal

import requests
from mcp.server import MCPServer

from memory_store import store


MINECRAFT_URL = "http://127.0.0.1:8765"
mcp = MCPServer("minecraft-companion")


def _get(path):
    response = requests.get(f"{MINECRAFT_URL}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def _post(path, payload=None):
    kwargs = {"timeout": 5}
    if payload is not None:
        kwargs["json"] = payload
    response = requests.post(f"{MINECRAFT_URL}{path}", **kwargs)
    response.raise_for_status()
    return response.json()


def _wait_for_job(timeout=10.0):
    deadline = time.time() + timeout
    last = None

    while time.time() < deadline:
        state = _get("/state")
        last = state
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


@mcp.tool()
def observe(view: Literal["state", "entities", "blocks", "inventory"]):
    """Read one compact Minecraft observation. Prefer the narrowest useful view."""
    if view == "state":
        state = _get("/state")
        owner = state.get("owner", {})
        return {
            "pos": [
                round(state.get("x", 0), 2),
                round(state.get("y", 0), 2),
                round(state.get("z", 0), 2),
            ],
            "hp": [state.get("health"), state.get("maxHealth")],
            "facing": state.get("facing"),
            "job": [
                state.get("jobType"),
                state.get("jobState"),
                state.get("jobReason"),
            ],
            "owner": {
                "distance": round(owner.get("distance", 0), 2),
                "delta": [
                    round(owner.get("dx", 0), 2),
                    round(owner.get("dy", 0), 2),
                    round(owner.get("dz", 0), 2),
                ],
            },
        }

    if view == "entities":
        result = _get("/nearby-entities")
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
    return [
        {
            "slot": item["slot"],
            "item": item["item"],
            "count": item["count"],
        }
        for item in result.get("items", [])
    ]


@mcp.tool()
def navigate(
    action: Literal["move_to", "move_forward", "follow", "stop", "resume"],
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
):
    """Move the companion body or control its current navigation job."""
    if action == "move_to":
        if x is None or y is None or z is None:
            raise ValueError("move_to requires x, y and z")
        _post("/move-to", {"x": x, "y": y, "z": z})
        return _wait_for_job(12)

    if action == "move_forward":
        _post("/move-forward")
        return _wait_for_job(8)

    if action == "follow":
        return _post("/follow-owner")

    if action == "stop":
        return _post("/stop-action")

    _post("/resume-action")
    return _wait_for_job(8)


@mcp.tool()
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
        _post("/collect-items")
        result = _wait_for_job(12)
        result["inventory"] = observe("inventory")
        return result

    if action == "mine":
        if x is None or y is None or z is None:
            raise ValueError("mine requires x, y and z")
        _post("/mine-block", {"x": x, "y": y, "z": z})
        result = _wait_for_job(15)
        result["inventory"] = observe("inventory")
        return result

    if action == "place":
        if x is None or y is None or z is None or slot is None:
            raise ValueError("place requires x, y, z and slot")
        _post(
            "/place-block",
            {
                "x": x,
                "y": y,
                "z": z,
                "inventory_slot": slot,
                "face": face or "up",
            },
        )
        return _wait_for_job(12)

    if action == "attack":
        if not entity_id:
            raise ValueError("attack requires entity_id")
        _post("/attack-entity", {"entity_id": entity_id})
        return _wait_for_job(15)

    if action == "take_held":
        _post("/take-held-item")
        return {"ok": True, "inventory": observe("inventory")}

    if slot is None:
        raise ValueError("equip requires slot")
    return _post("/equip-slot", {"slot": slot})


@mcp.tool()
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
def say(message: str):
    """Say a deliberate extra message in Minecraft chat."""
    return _post("/say", {"message": message[:4096]})


@mcp.tool()
def remember(
    kind: Literal["fact", "preference", "location", "lesson", "plan", "relationship", "skill"],
    key: str,
    content: str,
    importance: int = 5,
):
    """Persist a durable memory locally. Use only for information worth recalling in future sessions."""
    return store.remember(kind, key, content, importance)


@mcp.tool()
def recall_memory(query: str, limit: int = 6):
    """Retrieve a few locally stored memories relevant to the current situation."""
    return store.recall(query, limit)


@mcp.tool()
def memory_status():
    """Return counts and local database location without dumping memory contents."""
    return store.stats()


if __name__ == "__main__":
    mcp.run()
