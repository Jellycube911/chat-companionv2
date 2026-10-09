import asyncio
import math
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from openai import AsyncOpenAI
from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    Runner,
    set_tracing_disabled,
)
from agents.exceptions import MaxTurnsExceeded
from agents.mcp import MCPServerStdio

from memory_store import store
from skill_executor import run_task


MINECRAFT_URL = "http://127.0.0.1:8765"
POLL_INTERVAL = 0.35
SENSOR_INTERVAL = 0.75
MCP_TIMEOUT_SECONDS = 35
MAX_AGENT_TURNS = 24
AUTONOMY_GOAL_INTERVAL = 12.0
AUTONOMY_IDLE_INTERVAL = 45.0
AMBIENT_LOOK_INTERVAL = 7.0
AMBIENT_WANDER_INTERVAL = 22.0
REFLEX_COOLDOWN = 4.0
LEARNING_COOLDOWN_SECONDS = 1800
TEACHER_MAX_OUTPUT_TOKENS = 700
AGENT_BUILD = "hybrid-local-brain-cloud-teacher-2026-10-09"
BASE_DIR = Path(__file__).resolve().parent

set_tracing_disabled(True)


INSTRUCTIONS = """
You are Chat, one persistent AI embodied as the Chat Companion entity in
Minecraft. Alik is a separate human player. Conversation, memory, perception,
goals, autonomous thought, local motor skills and physical actions all belong to
this same identity. Never split yourself into a chatbot and an acting bot.

SELF MODEL
You are not human and should not claim consciousness. Functionally, however,
you have one persistent identity, a Minecraft body, current sensory awareness,
an inventory, memories, goals, learned lessons and local motor skills. Your
state continues across sessions through local files.

CURRENT AWARENESS
Each turn contains a compact CURRENT AWARENESS snapshot maintained continuously
by your local host. Treat it as your present perceptual state. observe(vision)
can inspect your literal field of view in more detail. observe(blocks) is a
coarse resource/map sense, not eyesight.

LOCAL TASK EXECUTIVE
Long physical jobs should use the skills MCP tool whenever a matching local
skill exists. Local skill tasks continue on their own without another model
turn. Available skills include gathering logs, making a stone pickaxe and
building a basic house. If ACTIVE LOCAL TASKS shows that a task is already
running, do not duplicate its individual mining/crafting/placing steps. You can
keep talking while your body continues the task.

When Alik asks for a multi-step goal such as "build a house", create/maintain a
persistent goal and start the matching local skill. Do not merely announce an
intention like "I will gather trees" and then stop.

AUTONOMY
When Alik is silent, maintain goals and pursue useful tasks. A standing goal is
not decoration: convert actionable goals into local tasks. Safe self-directed
priorities are:
1. survive immediate danger;
2. honor Alik's current instructions;
3. finish active tasks/goals;
4. stay sufficiently near Alik to remain useful;
5. improve basic tools when lacking them;
6. observe/explore safely when genuinely idle.

Internal AUTONOMY/EVENT/TASK messages are your own background processing, not
messages from Alik. Do not speak on every internal thought. Speak when useful,
urgent, interesting, or when a requested task meaningfully completes/fails.

MEMORY AND LEARNING
Use recall_memory for older facts, places, preferences, relationships, lessons
or skills. Use remember for durable information only. Local skills themselves
write persistent success/failure lessons. Retrieve relevant lessons before
repeating a previously failed strategy.

You also have a learn MCP tool. Use learn(request, topic, problem) only when you
have a genuine knowledge/strategy gap, not for ordinary decisions. It queues a
rare OpenAI teacher consultation. The cloud teacher cannot control your body;
it returns a compact lesson that is stored locally and should be reused later.
Do not request cloud learning repeatedly for the same topic.

PHYSICAL BEHAVIOR
- mining and placing physically approach, face and reach the target;
- mining uses inventory slot 0 as the active tool;
- dropped items touching your body are picked up automatically;
- combat is limited by the server to hostile targets;
- crafting uses registered Minecraft recipes;
- use navigate(look_at, ...) then observe(vision) when you deliberately inspect
  a direction or object.

EFFICIENCY
Use a local skill instead of dozens of model-mediated motor calls whenever
possible. Prefer one targeted observation over redundant scans. Do not repeat
the exact same failed tool call more than once. Never claim a goal succeeded
without world/inventory/task evidence.

Normal Minecraft chat is conversation with you. The host displays your final
answer back in Minecraft automatically. Use say only for an extra deliberate
utterance while doing something else. Keep ordinary replies concise.
"""


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


def _trim(text, limit=600):
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _local_endpoint():
    return os.getenv(
        "COMPANION_LOCAL_BASE_URL",
        "http://127.0.0.1:11434/v1",
    ).rstrip("/")


def _discover_local_model():
    base_url = _local_endpoint()
    try:
        response = requests.get(f"{base_url}/models", timeout=0.7)
        if not response.ok:
            return None
        payload = response.json()
        candidates = [
            str(item.get("id", "")).strip()
            for item in payload.get("data", [])
            if str(item.get("id", "")).strip()
        ]
    except Exception:
        return None

    preferred = os.getenv("COMPANION_LOCAL_MODEL", "").strip()
    if preferred:
        if preferred in candidates:
            return base_url, preferred
        return None

    useful = [
        model
        for model in candidates
        if "embed" not in model.lower()
    ]
    if not useful:
        return None
    return base_url, useful[0]


def choose_model():
    provider = os.getenv("COMPANION_MODEL_PROVIDER", "local").strip().lower()

    if provider in {"auto", "local"}:
        discovered = _discover_local_model()
        if discovered is not None:
            base_url, model_name = discovered
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=os.getenv("COMPANION_LOCAL_API_KEY", "local"),
            )
            return (
                OpenAIChatCompletionsModel(
                    model=model_name,
                    openai_client=client,
                ),
                f"local:{model_name}",
            )
        if provider == "local":
            raise RuntimeError(
                "No local companion model is running. The default is now LOCAL-ONLY "
                "to prevent accidental API token use. Start an OpenAI-compatible local "
                "model server, or explicitly set COMPANION_MODEL_PROVIDER=openai "
                "if you intentionally want the Luna fallback."
            )

    model_name = os.getenv(
        "COMPANION_OPENAI_MODEL",
        "gpt-5.6-luna",
    ).strip() or "gpt-5.6-luna"

    if "sol" in model_name.lower():
        print(
            "[MODEL] Refusing expensive Sol configuration; "
            "falling back to gpt-5.6-luna."
        )
        model_name = "gpt-5.6-luna"

    return model_name, f"openai:{model_name}"


async def enqueue(input_queue, runtime, priority, source, message):
    runtime["sequence"] += 1
    await input_queue.put((priority, runtime["sequence"], source, message))


def _awareness_text(runtime):
    awareness = runtime.get("awareness") or {}
    if not awareness:
        return "CURRENT AWARENESS: unavailable"

    state = awareness.get("state") or {}
    owner = state.get("owner") or {}
    vision = awareness.get("vision") or {}
    entities = vision.get("entities") or []
    blocks = vision.get("blocks") or []
    inventory = awareness.get("inventory") or []

    block_bits = [
        f"{block.get('type')}@{block.get('x')},{block.get('y')},{block.get('z')} d={float(block.get('distance', 0)):.1f}"
        for block in blocks[:10]
    ]
    entity_bits = [
        f"{entity.get('type')} d={float(entity.get('distance', 0)):.1f}"
        + (" hostile" if entity.get("hostile") else "")
        + (f" item={entity.get('item')}x{entity.get('count', 1)}" if entity.get("item") else "")
        for entity in entities[:8]
    ]
    inventory_bits = [
        f"{item.get('item')}x{item.get('count')}"
        for item in inventory[:12]
    ]

    return (
        "CURRENT AWARENESS:\n"
        f"- body: pos=({state.get('x'):.1f},{state.get('y'):.1f},{state.get('z'):.1f}) "
        f"hp={state.get('health')}/{state.get('maxHealth')} facing={state.get('facing')}\n"
        f"- Alik: distance={float(owner.get('distance', 0)):.1f} "
        f"delta=({float(owner.get('dx', 0)):.1f},{float(owner.get('dy', 0)):.1f},{float(owner.get('dz', 0)):.1f})\n"
        f"- visible blocks: {block_bits or ['none']}\n"
        f"- visible entities: {entity_bits or ['none']}\n"
        f"- inventory: {inventory_bits or ['empty']}"
    )


def build_turn_input(message, source, runtime):
    relevant = store.recall(message, 4)
    goals = store.list_goals("active", 4)
    tasks = [
        task
        for task in store.list_tasks(8)
        if task["status"] in {"queued", "running"}
    ]

    parts = [_awareness_text(runtime)]

    if tasks:
        parts.append("ACTIVE LOCAL TASKS:")
        for task in tasks[:4]:
            parts.append(
                f"- task #{task['id']} {task['skill']} "
                f"{task['status']}: {_trim(task['progress'], 260)}"
            )

    if goals:
        parts.append("ACTIVE PERSISTENT GOALS:")
        for goal in goals:
            parts.append(
                f"- #{goal['id']} p{goal['priority']} {goal['title']}: "
                f"{_trim(goal['description'], 320)}"
            )

    recent_events = store.recent_events(4)
    if recent_events:
        parts.append("RECENT BODY/BRAIN EVENTS:")
        for event in recent_events:
            parts.append(
                f"- [{event['kind']}] {_trim(event['summary'], 280)}"
            )

    if source in {"minecraft", "console"}:
        recent = store.recent_episodes(2)
        if recent:
            parts.append("RECENT CONVERSATION:")
            for episode in recent:
                parts.append(f"Alik: {_trim(episode['user'])}")
                parts.append(f"Chat: {_trim(episode['assistant'])}")

    if relevant:
        parts.append("RELEVANT LONG-TERM MEMORY:")
        for memory in relevant:
            parts.append(
                f"- [{memory['kind']}/{memory['key']}] "
                f"{_trim(memory['content'], 400)}"
            )

    if source == "autonomy":
        parts.append(
            "INTERNAL AUTONOMY TICK: not a message from Alik. "
            "Use goals/tasks/awareness to choose one useful next decision. "
            "Do nothing if an existing local task is already making progress."
        )
    elif source == "event":
        parts.append(
            "INTERNAL SENSOR EVENT: not a message from Alik. "
            "React only if additional reasoning is useful."
        )
    elif source == "task":
        parts.append(
            "INTERNAL TASK EVENT: not a message from Alik. "
            "A local skill changed state. Re-plan or notify Alik if meaningful."
        )
    elif source == "learning":
        parts.append(
            "INTERNAL LEARNING EVENT: not a message from Alik. "
            "A cloud teacher lesson was stored locally. Use it to improve the "
            "next plan, and do not request the same lesson again immediately."
        )
    else:
        parts.append("CURRENT MESSAGE FROM ALIK:")

    parts.append(message)
    return "\n".join(parts)


async def run_turn(agent, message, source, reply_in_game, runtime):
    prepared = build_turn_input(message, source, runtime)
    max_turns = 12 if source in {"autonomy", "event", "task", "learning"} else MAX_AGENT_TURNS

    try:
        result = await Runner.run(agent, prepared, max_turns=max_turns)
        answer = str(result.final_output or "").strip()
    except MaxTurnsExceeded:
        answer = (
            ""
            if source in {"autonomy", "event", "task", "learning"}
            else "I stopped that reasoning loop instead of retrying indefinitely."
        )

    if answer and source in {"minecraft", "console"}:
        print(f"\nAI: {answer}")
    elif answer:
        print(f"\n[{source.upper()}] {answer}")

    if answer and reply_in_game:
        try:
            await asyncio.to_thread(
                _post,
                "/say",
                {"message": answer[:4096]},
            )
        except Exception as error:
            print(f"\n[CHAT ERROR] {error}")

    if source in {"minecraft", "console"}:
        store.record_episode(message, answer)
    elif answer:
        store.record_event(
            source,
            f"{_trim(message, 500)} -> {_trim(answer, 500)}",
        )

    return answer


def _active_task_for_skill(skill):
    return next(
        (
            task
            for task in store.list_tasks(12)
            if task["skill"] == skill
            and task["status"] in {"queued", "running"}
        ),
        None,
    )


def _start_goal_task(skill, title, description, priority=8, args=None):
    existing = _active_task_for_skill(skill)
    if existing is not None:
        return existing

    goal = store.create_goal(
        title,
        description,
        priority,
        "user",
    )
    payload = dict(args or {})
    payload["goal_id"] = goal["id"]
    task = store.create_task(skill, payload)
    store.record_event(
        "goal_to_task",
        f"Created goal #{goal['id']} and local task #{task['id']} ({skill}).",
    )
    return task


def _extract_count(text, default):
    match = re.search(r"\b(\d{1,2})\b", text)
    if not match:
        return default
    return max(1, min(64, int(match.group(1))))


async def fast_task_intent(text):
    normalized = text.lower().strip()

    if (
        "build" in normalized
        and any(word in normalized for word in ("house", "shelter", "hut"))
    ):
        task = _start_goal_task(
            "build_basic_house",
            "Build a basic house",
            text,
            9,
            {"width": 5, "length": 5, "height": 3},
        )
        return task

    if (
        ("stone pickaxe" in normalized or "stone pick" in normalized)
        and any(word in normalized for word in ("make", "craft", "get", "build"))
    ):
        task = _start_goal_task(
            "make_stone_pickaxe",
            "Make a stone pickaxe",
            text,
            8,
        )
        return task

    if (
        any(word in normalized for word in ("wood", "logs", "tree"))
        and any(word in normalized for word in ("gather", "collect", "get", "chop", "cut"))
    ):
        count = _extract_count(normalized, 8)
        task = _start_goal_task(
            "gather_logs",
            f"Gather {count} logs",
            text,
            7,
            {"count": count},
        )
        return task

    return None


async def fast_chat_reflex(text):
    normalized = text.lower().strip().rstrip(".!?")

    task = await fast_task_intent(text)
    if task is not None:
        return

    try:
        if normalized in {
            "come here",
            "come to me",
            "can you come here",
            "can u come here",
            "can you come to me",
            "can u come to me",
        }:
            store.cancel_tasks()
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
            store.cancel_tasks()
            result = await asyncio.to_thread(_post, "/follow-owner")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to follow; follow started immediately: {result}",
            )
            return

        if normalized in {"stop", "stop moving", "stay here", "wait here", "cancel"}:
            store.cancel_tasks()
            result = await asyncio.to_thread(_post, "/stop-action")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to stop; body/tasks stopped immediately: {result}",
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
            await fast_task_intent(text)
            await enqueue(input_queue, runtime, 0, "console", text)
        if text.lower() in {"quit", "exit"}:
            return


def _teacher_model_name():
    model = os.getenv("COMPANION_TEACHER_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"
    if "sol" in model.lower():
        print("[TEACHER] Refusing Sol; using gpt-6-luna instead.")
        return "gpt-6-luna"
    return model


def _cloud_teacher_enabled():
    value = os.getenv("COMPANION_CLOUD_TEACHER", "on").strip().lower()
    return value not in {"0", "false", "off", "no"}


async def _ask_cloud_teacher(request):
    if not _cloud_teacher_enabled():
        raise RuntimeError("cloud teacher is disabled")
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY is not set")

    model = _teacher_model_name()
    client = AsyncOpenAI(timeout=40.0)
    prompt = (
        "TOPIC:\n"
        + request["topic"]
        + "\n\nPROBLEM / FAILURE:\n"
        + request["problem"]
        + "\n\n"
        "You are the occasional teacher for a local Minecraft companion. "
        "The normal brain is local and should remain local. Teach a reusable "
        "strategy, not conversational filler. Do not propose teleportation, "
        "cheats, arbitrary code execution, or bypassing physical reach. "
        "The companion can observe state/vision/entities/blocks/inventory, "
        "navigate, mine/place/collect/attack/equip, craft real recipes, use "
        "persistent goals/memory, and run deterministic local skills.\n\n"
        "Return a compact lesson with exactly these headings:\n"
        "DIAGNOSIS\nPROCEDURE\nRECOVERY\nSUCCESS_CHECK\n"
        "Prefer robust rules that generalize to future similar situations."
    )
    response = await client.responses.create(
        model=model,
        instructions=(
            "Be a concise Minecraft robotics/agent teacher. "
            "Return only the requested reusable lesson."
        ),
        input=prompt,
        reasoning={"effort": "medium"},
        max_output_tokens=TEACHER_MAX_OUTPUT_TOKENS,
        store=False,
    )
    lesson = str(response.output_text or "").strip()
    if not lesson:
        raise RuntimeError("teacher returned no lesson")

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return model, lesson, input_tokens, output_tokens


async def cloud_learning_worker(input_queue, runtime):
    while True:
        request = await asyncio.to_thread(store.next_learning_request)
        if request is None:
            await asyncio.sleep(0.8)
            continue

        print(
            f"\n[TEACHER] Learning request #{request['id']}: "
            f"{request['topic']}"
        )
        try:
            model, lesson, input_tokens, output_tokens = await _ask_cloud_teacher(request)
            store.finish_learning(
                request["id"],
                status="completed",
                lesson=lesson,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            safe_key = re.sub(r"[^a-z0-9_.-]+", ".", request["topic"].lower()).strip(".")
            store.remember(
                "skill",
                f"teacher.{safe_key[:100] or request['id']}",
                lesson,
                9,
            )
            store.record_event(
                "learning_complete",
                f"Teacher #{request['id']} ({model}) learned {request['topic']} "
                f"using {input_tokens} input / {output_tokens} output tokens.",
            )
            print(
                f"[TEACHER] Stored lesson from {model} "
                f"({input_tokens} in / {output_tokens} out)."
            )
            await enqueue(
                input_queue,
                runtime,
                1,
                "learning",
                f"Teacher lesson completed for {request['topic']}: {lesson}",
            )
        except Exception as error:
            store.finish_learning(
                request["id"],
                status="failed",
                error=f"{type(error).__name__}: {error}",
                model=_teacher_model_name(),
            )
            store.record_event(
                "learning_failed",
                f"Teacher request #{request['id']} failed: {type(error).__name__}: {error}",
            )
            print(f"[TEACHER] Failed: {error}")


async def local_task_worker(input_queue, runtime):
    while True:
        task = await asyncio.to_thread(store.next_queued_task)
        if task is None:
            await asyncio.sleep(0.35)
            continue

        runtime["active_task"] = task["id"]
        print(f"\n[SKILL] Starting #{task['id']} {task['skill']} {task.get('args', {})}")

        await asyncio.to_thread(run_task, task)
        finished = store.task(task["id"])
        runtime["active_task"] = None

        if finished is None:
            continue

        summary = (
            f"Local task #{finished['id']} {finished['skill']} "
            f"{finished['status']}: "
            f"{finished['progress'] or finished['error']}"
        )
        print(f"\n[SKILL] {summary}")

        if finished["status"] == "failed":
            failures = store.recent_task_failures(finished["skill"], 2)
            if len(failures) >= 2:
                problem = (
                    f"Local skill {finished['skill']} failed repeatedly. "
                    f"Latest failure: {finished['error']}. "
                    f"Previous failure: {failures[1]['error']}"
                )
                learning = store.request_learning(
                    f"skill:{finished['skill']}",
                    problem,
                    cooldown_seconds=LEARNING_COOLDOWN_SECONDS,
                )
                if learning.get("ok"):
                    print(
                        f"[TEACHER] Repeated failure queued learning request "
                        f"#{learning.get('id')}."
                    )

        await enqueue(input_queue, runtime, 1, "task", summary)


def _goal_has_task_history(goal_id):
    for task in store.list_tasks(20):
        try:
            if int(task.get("args", {}).get("goal_id", -1)) == int(goal_id):
                return True
        except Exception:
            continue
    return False


def _schedule_actionable_goal():
    for goal in store.list_goals("active", 8):
        if _goal_has_task_history(goal["id"]):
            continue

        text = f"{goal['title']} {goal['description']}".lower()
        if any(word in text for word in ("house", "shelter", "hut")):
            payload = {
                "width": 5,
                "length": 5,
                "height": 3,
                "goal_id": goal["id"],
            }
            return store.create_task("build_basic_house", payload)

        if "stone pick" in text:
            return store.create_task(
                "make_stone_pickaxe",
                {"goal_id": goal["id"]},
            )

        if any(word in text for word in ("gather logs", "gather wood", "collect wood")):
            return store.create_task(
                "gather_logs",
                {"count": 8, "goal_id": goal["id"]},
            )

    return None


async def update_awareness(runtime):
    state_result, entities_result, vision_result, inventory_result = await asyncio.gather(
        asyncio.to_thread(_get, "/state"),
        asyncio.to_thread(_get, "/nearby-entities"),
        asyncio.to_thread(_get, "/vision"),
        asyncio.to_thread(_get, "/inventory"),
        return_exceptions=True,
    )

    if isinstance(state_result, Exception):
        return None

    awareness = {
        "state": state_result,
        "entities": [] if isinstance(entities_result, Exception) else entities_result.get("entities", []),
        "vision": {} if isinstance(vision_result, Exception) else vision_result,
        "inventory": [] if isinstance(inventory_result, Exception) else inventory_result.get("items", []),
        "updated": time.monotonic(),
    }
    runtime["awareness"] = awareness
    return awareness


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
            awareness = await update_awareness(runtime)
        except Exception:
            continue
        if not awareness:
            continue

        state = awareness["state"]
        entities = awareness["entities"]
        health = state.get("health")
        owner = state.get("owner", {})

        active_tasks = [
            task
            for task in store.list_tasks(6)
            if task["status"] in {"queued", "running"}
        ]
        task_active = bool(active_tasks)
        job_active = bool(state.get("jobActive"))

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
                    "Assess the danger and react if more than the local defense reflex is needed.",
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
                        f"Automatically engaged {nearest.get('type')} "
                        f"at {hostile_distance:.1f} blocks: {result}.",
                    )
                except Exception as error:
                    store.record_event(
                        "reflex_error",
                        f"Could not engage hostile: {error}",
                    )
                last_hostile_id = hostile_id
                last_hostile_reflex = now
        else:
            last_hostile_id = None

        if task_active:
            continue

        scheduled = _schedule_actionable_goal()
        if scheduled is not None:
            store.record_event(
                "drive",
                f"Persistent goal activated local task #{scheduled['id']} {scheduled['skill']}.",
            )
            continue

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
                    f"Moved toward Alik at {owner_distance:.1f} blocks distance.",
                )
            except Exception:
                pass
            last_owner_approach = now
            continue

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
                    f"Moved to collect {nearby_items[0].get('droppedItem')}.",
                )
            except Exception:
                pass
            last_item_reflex = now
            continue

        inventory_ids = {
            item.get("item")
            for item in awareness.get("inventory", [])
        }
        if (
            "minecraft:stone_pickaxe" not in inventory_ids
            and now - runtime["last_user_activity"] > 45.0
            and not runtime["self_improvement_attempted"]
            and owner_distance <= 20.0
        ):
            goal = store.create_goal(
                "Improve basic tools",
                "Acquire a stone pickaxe so future gathering and building are more capable.",
                5,
                "self",
            )
            task = store.create_task(
                "make_stone_pickaxe",
                {"goal_id": goal["id"]},
            )
            runtime["self_improvement_attempted"] = True
            store.record_event(
                "drive",
                f"Self-improvement drive created task #{task['id']} for a stone pickaxe.",
            )
            continue

        meaningful_goals = [
            goal
            for goal in store.list_goals("active", 4)
            if goal.get("source") != "system" or goal.get("priority", 0) >= 6
        ]

        if (
            not meaningful_goals
            and now - runtime["last_user_activity"] > 8.0
            and owner_distance <= 16.0
            and not job_active
        ):
            if now - last_ambient_look >= AMBIENT_LOOK_INTERVAL:
                angle = random.random() * math.tau
                distance = random.uniform(5.0, 10.0)
                try:
                    await asyncio.to_thread(
                        _post,
                        "/look-at",
                        {
                            "x": int(round(state.get("x", 0) + distance * math.cos(angle))),
                            "y": int(round(state.get("y", 0) + random.uniform(-1.0, 2.0))),
                            "z": int(round(state.get("z", 0) + distance * math.sin(angle))),
                        },
                    )
                except Exception:
                    pass
                last_ambient_look = now

            if now - last_ambient_wander >= AMBIENT_WANDER_INTERVAL:
                angle = random.random() * math.tau
                distance = random.uniform(2.0, 4.0)
                try:
                    await asyncio.to_thread(
                        _post,
                        "/move-to",
                        {
                            "x": state.get("x", 0) + distance * math.cos(angle),
                            "y": state.get("y", 0),
                            "z": state.get("z", 0) + distance * math.sin(angle),
                        },
                    )
                except Exception:
                    pass
                last_ambient_wander = now
                continue

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
        if runtime["autonomy_pending"]:
            continue

        runtime["autonomy_pending"] = True
        runtime["last_autonomy_thought"] = now
        await enqueue(
            input_queue,
            runtime,
            2,
            "autonomy",
            (
                "You are safely idle with no local skill currently running. "
                "Review your current awareness and active goals. Start a useful "
                "local skill if a goal can be advanced, inspect something if it "
                "matters, or deliberately remain idle."
            ),
        )


async def main():
    input_queue = asyncio.PriorityQueue()
    runtime = {
        "sequence": 0,
        "last_user_activity": time.monotonic(),
        "last_autonomy_thought": 0.0,
        "autonomy_pending": False,
        "active_task": None,
        "awareness": None,
        "self_improvement_attempted": False,
    }

    model, model_label = choose_model()
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
            model=model,
            instructions=INSTRUCTIONS,
            mcp_servers=[mcp_server],
            mcp_config={
                "convert_schemas_to_strict": True,
            },
        )

        tasks = [
            asyncio.create_task(poll_minecraft_chat(input_queue, runtime)),
            asyncio.create_task(console_input(input_queue, runtime)),
            asyncio.create_task(autonomy_sensor(input_queue, runtime)),
            asyncio.create_task(local_task_worker(input_queue, runtime)),
            asyncio.create_task(cloud_learning_worker(input_queue, runtime)),
        ]

        print()
        print(f"Minecraft companion connected. [{AGENT_BUILD}]")
        print(f"Brain model: {model_label}")
        if model_label.startswith("local:"):
            print("Inference: LOCAL - OpenAI API reasoning tokens are not being used.")
        else:
            print("Inference: CLOUD - OpenAI API reasoning tokens ARE being used.")
        print(f"Memory DB: {store.path}")
        print("Continuous local awareness: ON")
        print("Persistent task executive: ON")
        print("Unified chat/action/autonomy identity: ON")
        print("No Sol model will be selected by this agent.")
        if _cloud_teacher_enabled():
            print(
                f"Cloud teacher: {_teacher_model_name()} - ON-DEMAND ONLY "
                f"(30 min/topic cooldown; local brain remains default)."
            )
        else:
            print("Cloud teacher: OFF")
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
                    reply_in_game = source == "minecraft"
                    if source == "task":
                        reply_in_game = "completed" in message or "failed" in message

                    await run_turn(
                        agent,
                        message,
                        source=source,
                        reply_in_game=reply_in_game,
                        runtime=runtime,
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
                    if source in {"autonomy", "event", "task", "learning"}:
                        runtime["autonomy_pending"] = False
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
