import asyncio
import random
import sys
import time
from pathlib import Path

import requests
from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.mcp import MCPServerStdio

from memory_store import store


MINECRAFT_URL = "http://127.0.0.1:8765"
MODEL = "gpt-5.6"
POLL_INTERVAL = 0.5
SENSOR_INTERVAL = 0.75
MCP_TIMEOUT_SECONDS = 35
MAX_AGENT_TURNS = 24
AUTONOMY_GOAL_INTERVAL = 20.0
AUTONOMY_IDLE_INTERVAL = 60.0
AMBIENT_LOOK_INTERVAL = 8.0
AMBIENT_WANDER_INTERVAL = 24.0
REFLEX_COOLDOWN = 4.0
AGENT_BUILD = "unified-autonomy-vision-memory-2026-10-08"
BASE_DIR = Path(__file__).resolve().parent


INSTRUCTIONS = """
You are Chat, one persistent AI embodied as the Chat Companion entity in
Minecraft. Alik is a separate human player. Conversation, autonomous thought,
memory, goals, perception, and physical actions are all parts of this same
identity. Never describe an acting bot and a chatbot as separate agents.

You have a functional self-model, not human consciousness. You know you have a
Minecraft body, inventory, current sensory state, persistent memories and
persistent goals. Use those consistently across sessions.

PERCEPTION
- observe(vision) is your primary visual sense. It is field-of-view and
  line-of-sight based, so it reflects what your body can actually see.
- observe(entities) is a broader proximity sense useful for nearby creatures
  and dropped items.
- observe(blocks) is a coarse local resource/map scan, not literal eyesight.
- observe(state) is proprioception: body position, health, job state and Alik's
  relative position.
Never claim to see something that your current senses did not report.

AUTONOMY AND GOALS
- Maintain persistent goals with the goals MCP tool.
- Convert meaningful multi-step user requests into goals when useful.
- Mark goals completed only after verifying the result.
- Internal AUTONOMY messages are your own background thought opportunities,
  not messages from Alik.
- On an autonomy turn, pursue an active goal, inspect the environment, make a
  small safe useful decision, or create a modest self-directed goal if idle.
- Do not speak every time you think. Speak autonomously only when something is
  genuinely useful, urgent, interesting, or relevant to Alik.
- Avoid destructive or irreversible autonomous behavior unless it clearly
  serves an active goal or immediate safety.

FAST REFLEXES
The local host may perform simple reflexes without asking the model first:
defending against a close hostile, approaching Alik if extremely far away, and
collecting a dropped item already very close. Treat those as actions your own
body performed automatically, like reflexes, not as a different agent.

MEMORY
- use recall_memory when older facts, preferences, places, lessons or skills may
  matter;
- use remember for durable information worth future retrieval;
- important failures should become lessons rather than being retried forever;
- do not store transient coordinates, health ticks, or routine chatter as
  durable memories.

PHYSICAL BEHAVIOR
- mining and placing require approach, reach, facing and line of sight;
- mining uses inventory slot 0 as the active tool, so equip appropriately;
- dropped items touching your body are picked up automatically;
- combat is limited by the server to hostile targets;
- crafting uses real registered recipes; up to 2x2 works anywhere and larger
  grids require a crafting table within usable reach.

EFFICIENCY
- prefer one targeted sense/tool call over redundant scans;
- reuse current-turn observations;
- do not repeat the exact same failed tool call more than once;
- if a tool returns {"ok": false, ...}, inspect once and choose a different
  recovery or stop;
- do not announce success until world/inventory evidence verifies it.

Normal Minecraft chat is conversation with you. When a turn came from Minecraft
the host displays your final answer automatically, so use say only for an extra
deliberate in-world utterance during another task.

Do not narrate protocol details such as queued jobs or MCP plumbing. Keep
ordinary replies concise.
"""


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


def _trim(text, limit=600):
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def enqueue(input_queue, runtime, priority, source, message):
    runtime["sequence"] += 1
    await input_queue.put((priority, runtime["sequence"], source, message))


def build_turn_input(message, source):
    relevant = store.recall(message, 4)
    goals = store.list_goals("active", 4)

    parts = []

    if source in {"minecraft", "console"}:
        recent = store.recent_episodes(2)
        if recent:
            parts.append("RECENT CONVERSATION (local cache, reference only):")
            for episode in recent:
                parts.append(f"Alik: {_trim(episode['user'])}")
                parts.append(f"Chat: {_trim(episode['assistant'])}")

    recent_events = store.recent_events(3)
    if recent_events:
        parts.append("RECENT BODY/BRAIN EVENTS:")
        for event in recent_events:
            parts.append(
                f"- [{event['kind']}] {_trim(event['summary'], 260)}"
            )

    if goals:
        parts.append("ACTIVE PERSISTENT GOALS:")
        for goal in goals:
            parts.append(
                f"- #{goal['id']} p{goal['priority']} {goal['title']}: "
                f"{_trim(goal['description'], 320)}"
            )

    if relevant:
        parts.append("RELEVANT LONG-TERM MEMORY:")
        for memory in relevant:
            parts.append(
                f"- [{memory['kind']}/{memory['key']}] "
                f"{_trim(memory['content'], 400)}"
            )

    if source == "autonomy":
        parts.append(
            "INTERNAL AUTONOMY TICK: This is not a message from Alik. "
            "Choose whether any action is worth taking. Silence is allowed."
        )
    elif source == "event":
        parts.append(
            "INTERNAL EVENT: This event was detected by your local body/sensors. "
            "React only if further reasoning or action is useful."
        )
    else:
        parts.append("CURRENT MESSAGE FROM ALIK:")

    parts.append(message)
    return "\n".join(parts)


async def run_turn(agent, message, source, reply_in_game):
    prepared = build_turn_input(message, source)
    max_turns = 12 if source in {"autonomy", "event"} else MAX_AGENT_TURNS

    try:
        result = await Runner.run(agent, prepared, max_turns=max_turns)
        answer = str(result.final_output or "").strip()
    except MaxTurnsExceeded:
        answer = (
            "I stopped this line of reasoning instead of retrying indefinitely."
            if source in {"autonomy", "event"}
            else (
                "I stopped because this task used too many reasoning/tool steps. "
                "I won't keep retrying blindly."
            )
        )

    if answer and source in {"minecraft", "console"}:
        print(f"\nAI: {answer}")
    elif answer and source in {"autonomy", "event"}:
        print(f"\n[AUTONOMY] {answer}")

    if answer and reply_in_game:
        try:
            await asyncio.to_thread(_post, "/say", {"message": answer[:4096]})
        except Exception as error:
            print(f"\n[CHAT ERROR] {error}")

    if source in {"minecraft", "console"}:
        store.record_episode(message, answer)
    else:
        store.record_event(source, f"{_trim(message, 500)} -> {_trim(answer, 500)}")

    return answer


async def fast_chat_reflex(text):
    normalized = text.lower().strip().rstrip(".!?")

    try:
        if normalized in {
            "come here",
            "come to me",
            "can you come here",
            "can u come here",
            "can you come to me",
            "can u come to me",
        }:
            state = await asyncio.to_thread(_get, "/state")
            owner = state.get("owner", {})
            result = await asyncio.to_thread(
                _post,
                "/move-to",
                {
                    "x": owner.get("x"),
                    "y": owner.get("y"),
                    "z": owner.get("z"),
                },
            )
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to come to him; movement started immediately: {result}",
            )
            return

        if normalized in {"follow me", "follow"}:
            result = await asyncio.to_thread(_post, "/follow-owner")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to follow; follow started immediately: {result}",
            )
            return

        if normalized in {"stop", "stop moving", "stay here", "wait here"}:
            result = await asyncio.to_thread(_post, "/stop-action")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to stop; body stopped immediately: {result}",
            )
            return

        if normalized in {
            "pick that up",
            "pick it up",
            "pick up the items",
            "collect that",
            "collect the items",
        }:
            result = await asyncio.to_thread(_post, "/collect-items")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to collect nearby items; collection started immediately: {result}",
            )
    except Exception as error:
        store.record_event(
            "motor_reflex_error",
            f"Fast chat reflex failed: {type(error).__name__}: {error}",
        )


async def poll_minecraft_chat(input_queue, runtime):
    while True:
        try:
            payload = await asyncio.to_thread(_get, "/chat-inbox")
            for message in payload.get("messages", []):
                text = str(message.get("text", "")).strip()
                if text:
                    runtime["last_user_activity"] = time.monotonic()
                    await fast_chat_reflex(text)
                    await enqueue(input_queue, runtime, 0, "minecraft", text)
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)


async def console_input(input_queue, runtime):
    while True:
        text = await asyncio.to_thread(input, "You: ")
        text = text.strip()
        if text:
            runtime["last_user_activity"] = time.monotonic()
            await enqueue(input_queue, runtime, 0, "console", text)
        if text.lower() in {"quit", "exit"}:
            return


async def autonomy_sensor(input_queue, runtime):
    last_health = None
    last_hostile_id = None
    last_hostile_reflex = 0.0
    last_owner_approach = 0.0
    last_item_reflex = 0.0
    last_ambient_look = 0.0
    last_ambient_wander = 0.0

    while True:
        await asyncio.sleep(SENSOR_INTERVAL)
        now = time.monotonic()

        try:
            state = await asyncio.to_thread(_get, "/state")
            entities_payload = await asyncio.to_thread(_get, "/nearby-entities")
        except Exception:
            continue

        health = state.get("health")
        owner = state.get("owner", {})
        job_active = bool(state.get("jobActive"))
        entities = entities_payload.get("entities", [])

        hostiles = sorted(
            (
                entity for entity in entities
                if entity.get("hostile") and entity.get("uuid")
            ),
            key=lambda entity: entity.get("distance", 999),
        )
        nearby_items = sorted(
            (
                entity for entity in entities
                if entity.get("droppedItem")
            ),
            key=lambda entity: entity.get("distance", 999),
        )

        if last_health is not None and health is not None and health < last_health:
            store.record_event(
                "damage",
                f"Chat took damage: health {last_health} -> {health}.",
            )
            if not runtime["autonomy_pending"]:
                runtime["autonomy_pending"] = True
                await enqueue(
                    input_queue,
                    runtime,
                    1,
                    "event",
                    f"Your health dropped from {last_health} to {health}. "
                    "Assess the danger and react if needed.",
                )
        last_health = health

        if hostiles:
            nearest = hostiles[0]
            hostile_id = nearest.get("uuid")
            hostile_distance = float(nearest.get("distance", 999))

            if (
                hostile_distance <= 5.5
                and state.get("jobType") != "DEFEND"
                and (
                    hostile_id != last_hostile_id
                    or now - last_hostile_reflex >= REFLEX_COOLDOWN
                )
            ):
                try:
                    result = await asyncio.to_thread(
                        _post,
                        "/attack-entity",
                        {"entity_id": hostile_id},
                    )
                    store.record_event(
                        "reflex",
                        f"Automatically engaged hostile {nearest.get('type')} "
                        f"at {hostile_distance:.1f} blocks: {result}.",
                    )
                except Exception as error:
                    store.record_event(
                        "reflex_error",
                        f"Could not engage nearby hostile: {error}",
                    )
                last_hostile_id = hostile_id
                last_hostile_reflex = now
        else:
            last_hostile_id = None

        owner_distance = float(owner.get("distance", 0) or 0)
        if (
            owner_distance > 18.0
            and not job_active
            and now - last_owner_approach > 12.0
        ):
            try:
                await asyncio.to_thread(
                    _post,
                    "/move-to",
                    {
                        "x": owner.get("x"),
                        "y": owner.get("y"),
                        "z": owner.get("z"),
                    },
                )
                store.record_event(
                    "reflex",
                    f"Moved toward Alik after distance reached {owner_distance:.1f} blocks.",
                )
            except Exception:
                pass
            last_owner_approach = now

        if (
            nearby_items
            and float(nearby_items[0].get("distance", 999)) <= 3.5
            and not job_active
            and now - last_item_reflex > 8.0
        ):
            try:
                await asyncio.to_thread(_post, "/collect-items")
                store.record_event(
                    "reflex",
                    f"Moved to collect nearby dropped {nearby_items[0].get('droppedItem')}.",
                )
            except Exception:
                pass
            last_item_reflex = now

        if job_active or runtime["autonomy_pending"]:
            continue

        goals = store.list_goals("active", 4)
        meaningful_goals = [
            goal for goal in goals
            if goal.get("source") != "system" or goal.get("priority", 0) >= 6
        ]

        if (
            not meaningful_goals
            and now - runtime["last_user_activity"] > 8.0
            and owner_distance <= 16.0
        ):
            if now - last_ambient_look >= AMBIENT_LOOK_INTERVAL:
                angle = random.random() * 6.283185307179586
                distance = random.uniform(5.0, 10.0)
                try:
                    await asyncio.to_thread(
                        _post,
                        "/look-at",
                        {
                            "x": int(round(state.get("x", 0) + distance * __import__("math").cos(angle))),
                            "y": int(round(state.get("y", 0) + random.uniform(-1.0, 2.0))),
                            "z": int(round(state.get("z", 0) + distance * __import__("math").sin(angle))),
                        },
                    )
                except Exception:
                    pass
                last_ambient_look = now

            if now - last_ambient_wander >= AMBIENT_WANDER_INTERVAL:
                angle = random.random() * 6.283185307179586
                distance = random.uniform(2.0, 4.0)
                try:
                    await asyncio.to_thread(
                        _post,
                        "/move-to",
                        {
                            "x": state.get("x", 0) + distance * __import__("math").cos(angle),
                            "y": state.get("y", 0),
                            "z": state.get("z", 0) + distance * __import__("math").sin(angle),
                        },
                    )
                except Exception:
                    pass
                last_ambient_wander = now

        interval = (
            AUTONOMY_GOAL_INTERVAL
            if meaningful_goals
            else AUTONOMY_IDLE_INTERVAL
        )

        if now - runtime["last_autonomy_thought"] < interval:
            continue

        if (
            not meaningful_goals
            and now - runtime["last_user_activity"] < 20.0
        ):
            continue

        try:
            vision = await asyncio.to_thread(_get, "/vision")
            visible_blocks = [
                block.get("type")
                for block in vision.get("blocks", [])[:8]
            ]
            visible_entities = [
                entity.get("type")
                for entity in vision.get("entities", [])[:6]
            ]
        except Exception:
            visible_blocks = []
            visible_entities = []

        goal_summary = "; ".join(
            f"#{goal['id']} {goal['title']}" for goal in goals[:3]
        ) or "none"

        runtime["autonomy_pending"] = True
        runtime["last_autonomy_thought"] = now
        await enqueue(
            input_queue,
            runtime,
            2,
            "autonomy",
            (
                f"You are currently idle. Active goals: {goal_summary}. "
                f"Alik is {owner_distance:.1f} blocks away. "
                f"Visible blocks: {visible_blocks}. "
                f"Visible entities: {visible_entities}. "
                "Decide on one small useful next action, pursue a goal, observe "
                "more closely, or deliberately do nothing if that is wiser."
            ),
        )


async def main():
    input_queue = asyncio.PriorityQueue()
    runtime = {
        "sequence": 0,
        "last_user_activity": time.monotonic(),
        "last_autonomy_thought": 0.0,
        "autonomy_pending": False,
    }

    python_executable = sys.executable
    server_file = BASE_DIR / "mcp_server.py"

    async with MCPServerStdio(
        name="Minecraft Companion MCP",
        params={
            "command": python_executable,
            "args": [str(server_file)],
            "cwd": str(BASE_DIR),
        },
        cache_tools_list=True,
        client_session_timeout_seconds=MCP_TIMEOUT_SECONDS,
        require_approval="never",
    ) as mcp_server:
        agent = Agent(
            name="Chat",
            model=MODEL,
            instructions=INSTRUCTIONS,
            mcp_servers=[mcp_server],
            mcp_config={
                "convert_schemas_to_strict": True,
            },
        )

        chat_task = asyncio.create_task(
            poll_minecraft_chat(input_queue, runtime)
        )
        console_task = asyncio.create_task(
            console_input(input_queue, runtime)
        )
        autonomy_task = asyncio.create_task(
            autonomy_sensor(input_queue, runtime)
        )

        print()
        print(f"Minecraft companion MCP agent connected. [{AGENT_BUILD}]")
        print(f"Memory DB: {store.path}")
        print("Unified chat/action/autonomy brain: ON")
        print("Local reflexes: hostile defense, nearby pickup, owner-distance recovery")
        print("Normal Minecraft chat is heard by Chat.")
        print("Shift-right-click Chat in Minecraft to open his inventory.")
        print("Type 'quit' here to stop.")
        print()

        try:
            while True:
                _, _, source, message = await input_queue.get()

                if source == "console" and message.lower() in {"quit", "exit"}:
                    break

                if source == "minecraft":
                    print(f"\n[MINECRAFT] Alik: {message}")

                try:
                    await run_turn(
                        agent,
                        message,
                        source=source,
                        reply_in_game=(source == "minecraft"),
                    )
                except Exception as error:
                    print(f"\nERROR: {error}")
                    store.record_event(
                        "agent_error",
                        f"{type(error).__name__}: {error}",
                    )
                    if source == "minecraft":
                        try:
                            await asyncio.to_thread(
                                _post,
                                "/say",
                                {"message": f"I hit an error: {error}"[:4096]},
                            )
                        except Exception:
                            pass
                finally:
                    if source in {"autonomy", "event"}:
                        runtime["autonomy_pending"] = False
        finally:
            chat_task.cancel()
            console_task.cancel()
            autonomy_task.cancel()
            await asyncio.gather(
                chat_task,
                console_task,
                autonomy_task,
                return_exceptions=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
