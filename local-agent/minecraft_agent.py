import json
import queue
import threading
import time
from collections import deque

import requests
from openai import OpenAI


MINECRAFT_URL = "http://127.0.0.1:8765"
MODEL = "gpt-5.6"
POLL_INTERVAL = 0.5

client = OpenAI()
agent_lock = threading.Lock()
conversation = deque(maxlen=10)
chat_queue = queue.Queue()


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


def _compact_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def get_state():
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


def get_nearby_entities():
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


def get_nearby_blocks():
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


def get_inventory():
    result = _get("/inventory")
    return [
        {
            "slot": item["slot"],
            "item": item["item"],
            "count": item["count"],
        }
        for item in result.get("items", [])
    ]


def say(message):
    return _post("/say", {"message": message[:4096]})


def _wait_for_job(timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        raw = _get("/state")
        last = raw
        if raw.get("jobState") != "RUNNING":
            return {
                "state": raw.get("jobState"),
                "reason": raw.get("jobReason"),
                "pos": [
                    round(raw.get("x", 0), 2),
                    round(raw.get("y", 0), 2),
                    round(raw.get("z", 0), 2),
                ],
            }
        time.sleep(0.25)

    return {
        "state": last.get("jobState") if last else "UNKNOWN",
        "reason": last.get("jobReason") if last else "timeout",
    }


def move_forward():
    _post("/move-forward")
    return _wait_for_job(8)


def move_to(x, y, z):
    _post("/move-to", {"x": x, "y": y, "z": z})
    return _wait_for_job(12)


def follow_owner():
    return _post("/follow-owner")


def stop_action():
    return _post("/stop-action")


def resume_action():
    _post("/resume-action")
    return _wait_for_job(8)


def collect_items():
    _post("/collect-items")
    result = _wait_for_job(12)
    result["inventory"] = get_inventory()
    return result


def mine_block(x, y, z):
    _post("/mine-block", {"x": x, "y": y, "z": z})
    result = _wait_for_job(15)
    result["inventory"] = get_inventory()
    return result


def place_block(x, y, z, inventory_slot, face):
    _post(
        "/place-block",
        {
            "x": x,
            "y": y,
            "z": z,
            "inventory_slot": inventory_slot,
            "face": face,
        },
    )
    return _wait_for_job(12)


def attack_entity(entity_id):
    _post("/attack-entity", {"entity_id": entity_id})
    return _wait_for_job(15)


def take_held_item():
    _post("/take-held-item")
    return {"ok": True, "inventory": get_inventory()}


def craft(width, height, grid, times):
    return _post(
        "/craft",
        {
            "width": width,
            "height": height,
            "grid": grid,
            "times": times,
        },
    )


def equip_slot(slot):
    return _post("/equip-slot", {"slot": slot})


tools = [
    {
        "type": "function",
        "name": "get_state",
        "description": "Your position, health, facing, current job, and Alik's relative position.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_nearby_entities",
        "description": "Nearby mobs, players, and dropped items with positions, distance and useful flags.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_nearby_blocks",
        "description": "Nearby block types with nearest observed position and distance.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_inventory",
        "description": "Your inventory slots, item IDs and counts.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "say",
        "description": "Say something in Minecraft chat when you deliberately want an in-world utterance.",
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "move_forward",
        "description": "Walk roughly five blocks forward using pathfinding.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "move_to",
        "description": "Navigate to loaded world coordinates within 64 blocks.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "follow_owner",
        "description": "Follow Alik continuously.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "stop_action",
        "description": "Stop your current movement or physical job.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "resume_action",
        "description": "Resume a suspended movement job.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "collect_items",
        "description": "Actively walk to and collect nearby dropped items. Items at your feet are picked up automatically.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "mine_block",
        "description": "Walk into player-like reach, face an observed block, then mine it. Tool slot is inventory slot 0.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "z": {"type": "integer"},
            },
            "required": ["x", "y", "z"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "place_block",
        "description": "Walk into player-like reach, face the target, then place an item from your inventory.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "z": {"type": "integer"},
                "inventory_slot": {"type": "integer", "minimum": 0, "maximum": 35},
                "face": {
                    "type": "string",
                    "enum": ["up", "down", "north", "south", "east", "west"],
                },
            },
            "required": ["x", "y", "z", "inventory_slot", "face"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "attack_entity",
        "description": "Move into combat range and attack a hostile mob by UUID. Non-hostile targets are rejected.",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "take_held_item",
        "description": "Accept the item Alik is deliberately holding out to you while nearby.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "craft",
        "description": (
            "Craft from your inventory using an exact row-major crafting grid. "
            "Use item IDs such as minecraft:oak_planks and an empty string for an empty cell. "
            "1x1 through 2x2 works anywhere. A grid wider or taller than 2 requires a crafting "
            "table within player-like reach. The server validates the real Minecraft recipe."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 1, "maximum": 3},
                "height": {"type": "integer", "minimum": 1, "maximum": 3},
                "grid": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 9,
                    "items": {"type": "string"},
                },
                "times": {"type": "integer", "minimum": 1, "maximum": 64},
            },
            "required": ["width", "height", "grid", "times"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "equip_slot",
        "description": "Swap one of your inventory slots into tool slot 0 before mining.",
        "parameters": {
            "type": "object",
            "properties": {"slot": {"type": "integer", "minimum": 0, "maximum": 35}},
            "required": ["slot"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def call_tool(name, args):
    print(f"\n[TOOL] {name} {args}")

    functions = {
        "get_state": get_state,
        "get_nearby_entities": get_nearby_entities,
        "get_nearby_blocks": get_nearby_blocks,
        "get_inventory": get_inventory,
        "say": lambda: say(args["message"]),
        "move_forward": move_forward,
        "move_to": lambda: move_to(args["x"], args["y"], args["z"]),
        "follow_owner": follow_owner,
        "stop_action": stop_action,
        "resume_action": resume_action,
        "collect_items": collect_items,
        "mine_block": lambda: mine_block(args["x"], args["y"], args["z"]),
        "place_block": lambda: place_block(
            args["x"], args["y"], args["z"], args["inventory_slot"], args["face"]
        ),
        "attack_entity": lambda: attack_entity(args["entity_id"]),
        "take_held_item": take_held_item,
        "craft": lambda: craft(
            args["width"], args["height"], args["grid"], args["times"]
        ),
        "equip_slot": lambda: equip_slot(args["slot"]),
    }

    function = functions.get(name)
    if function is None:
        raise ValueError(f"Unknown tool: {name}")

    result = function()
    print("[RESULT]", result)
    return result


INSTRUCTIONS = """
You are an AI embodied as the Chat Companion entity in Minecraft. Alik is a
separate human player in the same world.

Observe before acting when current world facts matter. Never invent positions,
blocks, entities, inventory, recipes, or action results.

Movement and physical interactions should behave like a player:
- mining and placing walk into reach, face the target and require line of sight;
- mining uses inventory slot 0 as the active tool; equip_slot can swap the correct tool into it;
- dropped items touching your body are picked up automatically;
- collect_items is for deliberately seeking nearby drops;
- hostile combat is allowed, non-hostile targets are rejected.

Crafting:
- inspect your inventory first;
- craft uses an exact row-major recipe grid and real server recipe validation;
- use "" for empty cells;
- 2x2 or smaller can be crafted anywhere;
- larger recipes require a crafting table close enough to use;
- if a table is visible but too far away, move to it first.

Do not narrate that a job was merely accepted or submitted. Physical tool calls
already wait locally for completion when practical. Report the meaningful
result, not transport/protocol details.

When a user message came from Minecraft chat, your final answer is displayed
back in Minecraft automatically, so do not call say merely to answer it.
Use say only when you intentionally want an extra in-world utterance while
performing some other task.

Be concise. Prefer one targeted observation tool over several broad redundant
observations. Reuse facts from the current turn rather than requesting them
again.
"""


def run_agent(user_message, reply_in_game=False):
    with agent_lock:
        turn_input = list(conversation)
        turn_input.append({"role": "user", "content": user_message})

        while True:
            response = client.responses.create(
                model=MODEL,
                instructions=INSTRUCTIONS,
                tools=tools,
                input=turn_input,
                max_output_tokens=350,
            )

            tool_calls = [
                item for item in response.output if item.type == "function_call"
            ]

            if not tool_calls:
                answer = response.output_text or ""
                if answer:
                    print("\nAI:", answer)
                    if reply_in_game:
                        try:
                            say(answer)
                        except Exception as error:
                            print("\n[CHAT ERROR]", error)

                conversation.append({"role": "user", "content": user_message})
                if answer:
                    conversation.append({"role": "assistant", "content": answer})
                return answer

            turn_input.extend(response.output)

            for tool_call in tool_calls:
                try:
                    args = json.loads(tool_call.arguments)
                    result = call_tool(tool_call.name, args)
                except Exception as error:
                    result = {"error": str(error)}
                    print("[RESULT]", result)

                turn_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call.call_id,
                        "output": _compact_json(result),
                    }
                )


def poll_minecraft_chat():
    while True:
        try:
            payload = _get("/chat-inbox")
            for message in payload.get("messages", []):
                chat_queue.put(message["text"])
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


def process_minecraft_chat():
    while True:
        message = chat_queue.get()
        print(f"\n[MINECRAFT] Alik: {message}")
        try:
            run_agent(message, reply_in_game=True)
        except Exception as error:
            print("\nERROR:", error)
            try:
                say(f"I hit an error: {error}")
            except Exception:
                pass


threading.Thread(target=poll_minecraft_chat, daemon=True).start()
threading.Thread(target=process_minecraft_chat, daemon=True).start()

print()
print("Minecraft companion AI connected.")
print("In Minecraft chat, address it with: chat: <message>")
print("Items dropped at its feet are picked up automatically.")
print("Type 'quit' here to stop.")
print()

while True:
    user_input = input("You: ").strip()

    if user_input.lower() in {"quit", "exit"}:
        break
    if not user_input:
        continue

    try:
        run_agent(user_input)
    except Exception as error:
        print("\nERROR:", error)
