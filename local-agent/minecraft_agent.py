import json
import time

import requests
from openai import OpenAI


MINECRAFT_URL = "http://127.0.0.1:8765"
MODEL = "gpt-5.6"

client = OpenAI()


def _get(path):
    response = requests.get(f"{MINECRAFT_URL}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def _post(path, payload=None, settle=0.0):
    kwargs = {"timeout": 5}
    if payload is not None:
        kwargs["json"] = payload
    response = requests.post(f"{MINECRAFT_URL}{path}", **kwargs)
    response.raise_for_status()
    if settle:
        time.sleep(settle)
    return response.json()


def get_state():
    return _get("/state")


def get_nearby_entities():
    return _get("/nearby-entities")


def get_nearby_blocks():
    return _get("/nearby-blocks")


def get_inventory():
    return _get("/inventory")


def say(message):
    return _post("/say", {"message": message})


def move_forward():
    return _post("/move-forward", settle=0.7)


def move_to(x, y, z):
    return _post("/move-to", {"x": x, "y": y, "z": z}, settle=0.4)


def follow_owner():
    return _post("/follow-owner", settle=0.4)


def stop_action():
    return _post("/stop-action", settle=0.2)


def resume_action():
    return _post("/resume-action", settle=0.3)


def collect_items():
    return _post("/collect-items", settle=0.4)


def mine_block(x, y, z):
    return _post("/mine-block", {"x": x, "y": y, "z": z}, settle=0.4)


def place_block(x, y, z, inventory_slot, face):
    return _post(
        "/place-block",
        {
            "x": x,
            "y": y,
            "z": z,
            "inventory_slot": inventory_slot,
            "face": face,
        },
        settle=0.4,
    )


def attack_entity(entity_id):
    return _post("/attack-entity", {"entity_id": entity_id}, settle=0.4)


def take_held_item():
    return _post("/take-held-item", settle=0.2)


tools = [
    {
        "type": "function",
        "name": "get_state",
        "description": (
            "Read your Chat Companion body's authoritative state, including "
            "position, health, facing direction, current job, whether world "
            "actions are enabled, and Alik's relative position and distance."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_nearby_entities",
        "description": (
            "Observe nearby entities within 16 blocks. Dropped items include "
            "their actual item ID, display name, and count. Living entities "
            "include health and hostile/owner information."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_nearby_blocks",
        "description": (
            "Observe nearby non-air blocks. Results are grouped by block type "
            "and include counts and the nearest observed coordinates."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_inventory",
        "description": "Read your companion body's own 36-slot inventory.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "say",
        "description": "Display a message from you inside Minecraft chat.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to display in Minecraft.",
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "move_forward",
        "description": (
            "Move your body roughly five blocks in the direction you are "
            "currently facing, using Minecraft pathfinding."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "move_to",
        "description": (
            "Navigate your body to specific loaded Minecraft coordinates "
            "within 64 blocks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "Target X coordinate."},
                "y": {"type": "number", "description": "Target Y coordinate."},
                "z": {"type": "number", "description": "Target Z coordinate."},
            },
            "required": ["x", "y", "z"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "follow_owner",
        "description": (
            "Start following Alik and maintain roughly three blocks of distance."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "stop_action",
        "description": "Stop your current movement or physical job.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "resume_action",
        "description": (
            "Resume a safely suspended movement job if one is available."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "collect_items",
        "description": (
            "Collect nearby dropped item entities into your inventory. This "
            "searches within eight blocks and collects up to sixteen item "
            "entities. Requires world actions to be enabled by Alik."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "mine_block",
        "description": (
            "Mine a block at exact integer coordinates. You must be within "
            "reach and have an appropriate tool in inventory slot 0. Requires "
            "world actions to be enabled by Alik."
        ),
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
        "description": (
            "Place the item from one of your inventory slots at exact integer "
            "coordinates. Requires world actions to be enabled by Alik."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "z": {"type": "integer"},
                "inventory_slot": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 35,
                },
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
        "description": (
            "Attack a nearby hostile mob by UUID. The server refuses non-hostile "
            "targets and allies. Requires world actions to be enabled by Alik."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "UUID returned by get_nearby_entities.",
                }
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "take_held_item",
        "description": (
            "Transfer the item Alik is currently holding into your inventory. "
            "Only use this when Alik explicitly asks you to take or accept the "
            "item he is holding, and only while you are within four blocks."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def call_tool(name, args):
    print(f"\n[TOOL] {name} {args}")

    functions = {
        "get_state": lambda: get_state(),
        "get_nearby_entities": lambda: get_nearby_entities(),
        "get_nearby_blocks": lambda: get_nearby_blocks(),
        "get_inventory": lambda: get_inventory(),
        "say": lambda: say(args["message"]),
        "move_forward": lambda: move_forward(),
        "move_to": lambda: move_to(args["x"], args["y"], args["z"]),
        "follow_owner": lambda: follow_owner(),
        "stop_action": lambda: stop_action(),
        "resume_action": lambda: resume_action(),
        "collect_items": lambda: collect_items(),
        "mine_block": lambda: mine_block(args["x"], args["y"], args["z"]),
        "place_block": lambda: place_block(
            args["x"],
            args["y"],
            args["z"],
            args["inventory_slot"],
            args["face"],
        ),
        "attack_entity": lambda: attack_entity(args["entity_id"]),
        "take_held_item": lambda: take_held_item(),
    }

    function = functions.get(name)
    if function is None:
        raise ValueError(f"Unknown tool: {name}")
    return function()


history = []


def run_agent(user_message):
    history.append({"role": "user", "content": user_message})

    while True:
        response = client.responses.create(
            model=MODEL,
            instructions="""
You are an AI embodied as the Chat Companion entity inside Minecraft.

The human player is Alik. You are NOT Alik's player character. You and Alik
are two separate entities sharing the same Minecraft world.

Your observation tools are:
- get_state: your body, job state, action permissions, and Alik's relative location.
- get_nearby_entities: nearby mobs, players, and dropped items.
- get_nearby_blocks: nearby terrain and block types.
- get_inventory: your own inventory.

Your physical tools are:
- move_forward / move_to: movement.
- follow_owner / stop_action / resume_action: navigation control.
- collect_items: collect dropped items.
- mine_block: mine a nearby block.
- place_block: place an inventory item as a block.
- attack_entity: defend against hostile mobs only.
- take_held_item: accept the item Alik is explicitly offering you.
- say: communicate with Alik inside Minecraft.

Use observation tools whenever your answer or action depends on the current
world. Never invent blocks, entities, inventory items, coordinates, movement,
job results, or item transfers you have not observed.

World-changing actions (collect, mine, place, attack) are deliberately gated by
Alik. If get_state reports worldActionsAllowed=false or an action returns that
world actions are disabled, tell Alik to run /chat actions on. Never try to
bypass this control.

For navigation, inspect state before choosing coordinates when needed.
A successful movement or physical action request may only mean the job was
submitted. Inspect get_state, get_inventory, get_nearby_entities, or
get_nearby_blocks afterward when you need to verify the result.

When asked to collect a particular dropped item, first inspect nearby entities,
move closer if necessary, call collect_items, then verify with get_inventory.

When asked to mine a block, inspect nearby blocks first, navigate within reach
if necessary, mine the exact observed coordinates, then verify the world or
inventory.

Only call take_held_item when Alik explicitly asks you to take/accept what he
is holding. Do not take held items merely because they might be useful.

When Alik says "you", "yourself", "come here", "move", or similar language, he
normally means your companion body.

Do not confuse Alik's health, location, movement, or inventory with your own.
""",
            tools=tools,
            input=history,
        )

        history.extend(response.output)

        tool_calls = [
            item for item in response.output if item.type == "function_call"
        ]

        if not tool_calls:
            if response.output_text:
                print("\nAI:", response.output_text)
            return

        for tool_call in tool_calls:
            try:
                args = json.loads(tool_call.arguments)
                result = call_tool(tool_call.name, args)
            except Exception as error:
                result = {"error": str(error)}

            print("[RESULT]", result)
            history.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": json.dumps(result),
                }
            )


print()
print("Minecraft companion AI connected.")
print("Keep Minecraft running in a singleplayer world with /chat spawn.")
print("World-changing actions require /chat actions on.")
print("Type 'quit' to stop.")
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
