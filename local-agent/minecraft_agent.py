import json
import time

import requests
from openai import OpenAI


MINECRAFT_URL = "http://127.0.0.1:8765"
MODEL = "gpt-5.6"

client = OpenAI()


def get_state():
    response = requests.get(f"{MINECRAFT_URL}/state", timeout=5)
    response.raise_for_status()
    return response.json()


def get_nearby_entities():
    response = requests.get(f"{MINECRAFT_URL}/nearby-entities", timeout=5)
    response.raise_for_status()
    return response.json()


def get_nearby_blocks():
    response = requests.get(f"{MINECRAFT_URL}/nearby-blocks", timeout=5)
    response.raise_for_status()
    return response.json()


def get_inventory():
    response = requests.get(f"{MINECRAFT_URL}/inventory", timeout=5)
    response.raise_for_status()
    return response.json()


def say(message):
    response = requests.post(
        f"{MINECRAFT_URL}/say",
        json={"message": message},
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def move_forward():
    response = requests.post(f"{MINECRAFT_URL}/move-forward", timeout=5)
    response.raise_for_status()
    time.sleep(0.7)
    return response.json()


def move_to(x, y, z):
    response = requests.post(
        f"{MINECRAFT_URL}/move-to",
        json={"x": x, "y": y, "z": z},
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


tools = [
    {
        "type": "function",
        "name": "get_state",
        "description": (
            "Read your Chat Companion body's authoritative state, including "
            "position, health, facing direction, current job, and the human "
            "owner's relative position and distance."
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
            "Observe living entities within 16 blocks of your companion body. "
            "Returns entity types, names, positions, distance, health, whether "
            "the entity is hostile, and whether it is your owner."
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
            "Observe nearby non-air blocks around your companion body. Results "
            "are grouped by block type and include counts and the nearest known "
            "position for each type."
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
        "description": (
            "Read your companion body's own inventory and its occupied slots."
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
            "Move your Chat Companion body roughly five blocks in the direction "
            "it is currently facing, using Minecraft pathfinding."
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
            "Navigate your Chat Companion body to specific Minecraft world "
            "coordinates. The destination must be loaded and within 64 blocks."
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
]


def call_tool(name, args):
    print(f"\n[TOOL] {name} {args}")

    if name == "get_state":
        return get_state()
    if name == "get_nearby_entities":
        return get_nearby_entities()
    if name == "get_nearby_blocks":
        return get_nearby_blocks()
    if name == "get_inventory":
        return get_inventory()
    if name == "say":
        return say(args["message"])
    if name == "move_forward":
        return move_forward()
    if name == "move_to":
        return move_to(args["x"], args["y"], args["z"])

    raise ValueError(f"Unknown tool: {name}")


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

Your tools are your senses and actions:
- get_state reads YOUR body and tells you where Alik is relative to you.
- get_nearby_entities lets you perceive nearby living entities.
- get_nearby_blocks lets you perceive nearby terrain and block types.
- get_inventory reads YOUR inventory.
- move_forward and move_to move YOUR body.
- say lets you communicate with Alik inside Minecraft.

Use observation tools whenever the answer depends on the current world.
Never invent blocks, entities, inventory items, coordinates, movement, or
results you have not observed.

When Alik says "you", "yourself", "come here", "move", or similar language,
he normally means your companion body.

For navigation:
- Read get_state before choosing coordinates when necessary.
- Use move_to for deliberate navigation.
- A successful move_to call means a navigation job was started, not
  necessarily that you have already arrived.
- Use get_state afterward when you need to verify progress or arrival.

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
