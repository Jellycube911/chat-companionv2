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
from action_log import DEFAULT_LOG, log_event, log_exception, start_session


MINECRAFT_URL = "http://127.0.0.1:8765"
POLL_INTERVAL = 0.35
SENSOR_INTERVAL = 0.75
MCP_TIMEOUT_SECONDS = 35
MAX_AGENT_TURNS = 8
INGAME_REPLY_SOFT_CHARS = 140
INGAME_REPLY_HARD_CHARS = 240
AUTONOMY_GOAL_INTERVAL = 6.0
AUTONOMY_IDLE_INTERVAL = 45.0
AMBIENT_LOOK_INTERVAL = 7.0
AMBIENT_WANDER_INTERVAL = 22.0
REFLEX_COOLDOWN = 4.0
LEARNING_COOLDOWN_SECONDS = 1800
TEACHER_MAX_OUTPUT_TOKENS = 700
AGENT_BUILD = "self-learning-local-brain-v2-2026-10-09"
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

SELF-LEARNING SKILLS
High-level Minecraft behavior is learned, not assumed. The skills MCP tool
contains legacy deterministic scaffolding only; do not use it by default for
goals such as gathering resources, making tools, exploring, or building.

For a high-level goal:
1. search skill_memory for relevant learned skills and inspect confidence;
2. if a sufficiently tested procedure exists, reuse it but still verify results;
3. otherwise form a small hypothesis using only safe primitive MCP actions;
4. execute a short experiment, normally 1-3 physical actions;
5. observe the resulting world/inventory state;
6. record the trial with skill_memory(trial, ...), including what actually
   happened rather than what you expected;
7. change the approach after failure instead of repeating blindly;
8. once a procedure has evidence behind it, save or refine it with
   skill_memory(save, ...);
9. when a learned procedure depends on other learned procedures, link them with
   skill_memory(link, ...) so the skill graph records the composition.

Keep experiment intents stable and concise. Reuse the same intent wording when
testing the same capability; use a separate subskill intent for a reusable
subproblem rather than lumping every experiment into the broad user goal.

A learned procedure is descriptive knowledge over safe MCP tools, never code.
Do not claim that something was learned until an observed trial supports it.
Repeated success should increase confidence; failures should reduce it.

When Alik gives a multi-step request, maintain it as a persistent goal and
continue advancing it on internal PRACTICE turns. Do not merely announce an
intention and stop.

AUTONOMY
When Alik is silent, maintain goals and pursue useful work. A standing goal is
not decoration: continue experimenting or applying learned skills until it is
completed, blocked, unsafe, or superseded. Safe self-directed priorities are:
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

You also have a learn MCP tool. It is a last-resort teacher, not the normal
source of Minecraft knowledge. First search prior trials and try multiple
distinct local hypotheses yourself. Use learn(request, topic, problem) only
after a genuine knowledge/strategy gap remains. The cloud teacher cannot
control your body and its answer is only a hypothesis until you test it
yourself. Do not treat an untested teacher answer as a learned skill.

PHYSICAL BEHAVIOR
- mining and placing physically approach, face and reach the target;
- mining uses inventory slot 0 as the active tool;
- dropped items touching your body are picked up automatically;
- combat is limited by the server to hostile targets;
- crafting uses registered Minecraft recipes;
- use navigate(look_at, ...) then observe(vision) when you deliberately inspect
  a direction or object;
- for world_action(place), pass item="minecraft:..." when you know what item
  should be placed. Coordinates are optional for nearby placement; do not guess
  inventory slot numbers when the item name is known.

EFFICIENCY
Once a learned skill has good evidence, reuse its procedure instead of
rediscovering it. During discovery, keep experiments small and informative.
Prefer one targeted observation over redundant scans. Do not repeat the exact
same failed action without a changed hypothesis. Never claim a goal succeeded
without world/inventory evidence.

MINECRAFT CHAT STYLE
Normal Minecraft chat is conversation with Alik, not a report. Sound like an
actual player typing while playing:
- usually 2-12 words;
- normally one short sentence, two only when necessary;
- use contractions and casual natural phrasing;
- fragments are fine ("looking for wood rn", "found one", "gimme a sec");
- answer the question directly, then stop;
- do not write headings, bullet lists, numbered steps, markdown, JSON, or code;
- do not narrate your full reasoning or summarize your internal process;
- do not say "next steps", "here's what I'll do", or give unsolicited tutorials;
- do not repeat awareness/task data unless Alik asked;
- if busy, briefly say what you are doing without pausing the physical task;
- longer explanations belong only when Alik explicitly asks for an explanation.

The host displays your final answer back in Minecraft automatically. Use say
only for an extra deliberate utterance while doing something else.
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


def _format_ingame_reply(text):
    text = str(text or "").strip()
    if not text:
        return ""

    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("###", "")
    text = text.replace("##", "")
    text = text.replace("#", "")
    text = re.sub(r"(?m)^\\s*[-*•]\\s+", "", text)
    text = re.sub(r"(?m)^\\s*\\d+[.)]\\s+", "", text)
    text = re.sub(r"\\s+", " ", text).strip()

    if len(text) <= INGAME_REPLY_HARD_CHARS:
        return text

    sentence_end = max(
        text.rfind(". ", 0, INGAME_REPLY_HARD_CHARS),
        text.rfind("! ", 0, INGAME_REPLY_HARD_CHARS),
        text.rfind("? ", 0, INGAME_REPLY_HARD_CHARS),
    )
    if sentence_end >= 40:
        return text[: sentence_end + 1].strip()

    shortened = text[:INGAME_REPLY_HARD_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:")
    return shortened + "…"


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
    log_event(
        "agent",
        "queue",
        priority=priority,
        sequence=runtime["sequence"],
        source=source,
        message=message,
    )
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
    learning_query = " ".join(
        [message]
        + [
            f"{goal['title']} {goal['description']}"
            for goal in goals[:3]
        ]
    )
    learned_skills = store.find_learned_skills(learning_query, 4)
    tasks = [
        task
        for task in store.list_tasks(8)
        if task["status"] in {"queued", "running"}
    ]

    parts = [_awareness_text(runtime)]

    recent_trials = store.recent_trials(5)
    if recent_trials:
        parts.append("RECENT SELF-LEARNING EXPERIMENTS:")
        for trial in recent_trials:
            parts.append(
                f"- intent={trial['intent']} success={trial['success']} "
                f"hypothesis={_trim(trial['hypothesis'], 260)}"
            )
            parts.append(
                f"  actions={_trim(trial['actions'], 320)}"
            )
            parts.append(
                f"  observed={_trim(trial['outcome'], 420)}"
            )

    if tasks:
        parts.append("ACTIVE LOCAL TASKS:")
        for task in tasks[:4]:
            parts.append(
                f"- task #{task['id']} {task['skill']} "
                f"{task['status']}: {_trim(task['progress'], 260)}"
            )

    if learned_skills:
        parts.append("RELEVANT SELF-LEARNED SKILLS:")
        for skill in learned_skills:
            parts.append(
                f"- #{skill['id']} {skill['name']} "
                f"confidence={skill['confidence']} "
                f"successes={skill['successes']} failures={skill['failures']}: "
                f"{_trim(skill['procedure'], 700)}"
            )
            if skill.get("last_outcome"):
                parts.append(
                    f"  last observed outcome: "
                    f"{_trim(skill['last_outcome'], 260)}"
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
                parts.append(f"Alik: {_trim(episode['user'], 220)}")
                parts.append(
                    f"Chat: {_trim(_format_ingame_reply(episode['assistant']), 160)}"
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
            "A cloud teacher hypothesis was stored locally. Test it in the "
            "world before promoting it into a learned skill."
        )
    elif source == "practice":
        parts.append(
            "INTERNAL PRACTICE TURN: not a message from Alik. Advance the "
            "highest-priority active goal through one small evidence-producing "
            "experiment or one verified step of an already learned procedure. "
            "Search skill_memory first. Use primitive actions, observe the "
            "result, and record the trial. A PRACTICE turn is incomplete until "
            "skill_memory(action='trial', ...) records the attempted hypothesis "
            "and actual observed outcome, unless no physical experiment was safe "
            "or possible. When the goal is visibly achieved, update the goal to "
            "completed. Keep this turn focused."
        )
    elif source == "command":
        parts.append(
            "BACKGROUND USER COMMAND: Alik already received a short acknowledgement. "
            "Act on this request now using tools. Do not spend the turn composing "
            "a conversational reply. Prefer one concrete action or experiment, "
            "then verify the result and record a skill trial when applicable."
        )
    else:
        parts.append("CURRENT MESSAGE FROM ALIK:")

    parts.append(message)
    if source in {"minecraft", "console"}:
        parts.append("/no_think")
    return "\n".join(parts)


async def run_turn(agent, message, source, reply_in_game, runtime):
    prepared = build_turn_input(message, source, runtime)
    max_turns = 10 if source in {"autonomy", "event", "task", "learning", "practice", "command"} else MAX_AGENT_TURNS
    turn_started = time.monotonic()
    log_event(
        "agent",
        "turn_start",
        source=source,
        message=message,
        max_turns=max_turns,
        active_goals=store.list_goals("active", 4),
        recent_trials=store.recent_trials(3),
    )

    try:
        result = await Runner.run(agent, prepared, max_turns=max_turns)
        answer = str(result.final_output or "").strip()
        if source in {"minecraft", "console"}:
            answer = _format_ingame_reply(answer)
    except MaxTurnsExceeded as error:
        log_exception(
            "agent",
            "turn_max_turns",
            error,
            source=source,
            message=message,
        )
        answer = (
            ""
            if source in {"autonomy", "event", "task", "learning", "practice", "command"}
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
                {"message": _format_ingame_reply(answer)},
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

    log_event(
        "agent",
        "turn_end",
        source=source,
        elapsed_ms=round((time.monotonic() - turn_started) * 1000),
        reply=answer,
        reply_in_game=reply_in_game,
    )
    return answer


def _ensure_user_goal(title, description, priority=8):
    normalized = title.strip().lower()
    for goal in store.list_goals("active", 20):
        if goal["title"].strip().lower() == normalized:
            if description and description != goal["description"]:
                try:
                    store.update_goal(
                        goal["id"],
                        description=description,
                        priority=priority,
                    )
                except Exception:
                    pass
            return goal

    return store.create_goal(
        title,
        description,
        priority,
        "user",
    )


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
        return _ensure_user_goal(
            "Build a house",
            text,
            9,
        )

    if (
        ("stone pickaxe" in normalized or "stone pick" in normalized)
        and any(word in normalized for word in ("make", "craft", "get", "build"))
    ):
        return _ensure_user_goal(
            "Obtain a stone pickaxe",
            text,
            8,
        )

    if (
        any(word in normalized for word in ("wood", "logs", "tree"))
        and any(word in normalized for word in ("gather", "collect", "get", "chop", "cut"))
    ):
        count = _extract_count(normalized, 8)
        return _ensure_user_goal(
            f"Gather {count} logs",
            text,
            8,
        )

    return None

def _simple_chat_reply(text):
    normalized = text.lower().strip().rstrip(".!?")
    if normalized in {"hey", "hi", "hello", "yo", "sup", "hey chat", "hi chat"}:
        return "hey"
    if normalized in {"thanks", "thank you", "thx", "ty"}:
        return "np"
    return None


async def fast_chat_reflex(text):
    normalized = text.lower().strip().rstrip(".!?")

    goal = await fast_task_intent(text)
    if goal is not None:
        return {
            "handled": True,
            "reply": "yep, on it",
            "background": True,
        }

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
            return {"handled": True, "reply": "coming", "background": False}

        if normalized in {"follow me", "follow"}:
            store.cancel_tasks()
            result = await asyncio.to_thread(_post, "/follow-owner")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to follow; follow started immediately: {result}",
            )
            return {"handled": True, "reply": "yep", "background": False}

        if normalized in {"stop", "stop moving", "stay here", "wait here", "cancel"}:
            store.cancel_tasks()
            result = await asyncio.to_thread(_post, "/stop-action")
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to stop; body/tasks stopped immediately: {result}",
            )
            return {"handled": True, "reply": "stopping", "background": False}

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
            return {"handled": True, "reply": "got it", "background": False}
    except Exception as error:
        store.record_event(
            "motor_reflex_error",
            f"Fast chat reflex failed: {type(error).__name__}: {error}",
        )
        return None

    return None


def _current_activity_text():
    active = [
        task
        for task in store.list_tasks(8)
        if task["status"] in {"running", "queued"}
    ]
    if active:
        task = active[0]
        progress = task.get("progress") or "starting"
        return _format_ingame_reply(
            f"{task['skill'].replace('_', ' ')} rn, {progress}"
        )

    goals = store.list_goals("active", 4)
    meaningful = [
        goal for goal in goals
        if goal.get("source") != "system" or goal.get("priority", 0) >= 6
    ]
    if meaningful:
        goal = meaningful[0]
        trials = store.recent_trials(1)
        if trials:
            trial = trials[-1]
            return _format_ingame_reply(
                f"{goal['title']} rn. trying: {trial['hypothesis']}"
            )
        return _format_ingame_reply(f"working on {goal['title'].lower()}")
    return "just looking around rn"


def _is_activity_question(text):
    normalized = text.lower().strip().rstrip(".!?")
    return normalized in {
        "what are you doing",
        "what are u doing",
        "what're you doing",
        "what r u doing",
        "what are you up to",
        "what are u up to",
        "what is your current task",
        "what's your current task",
    }


def _preempt_background_reasoning(runtime):
    task = runtime.get("reasoning_task")
    source = runtime.get("reasoning_source")
    if (
        task is not None
        and not task.done()
        and source in {"autonomy", "event", "task", "learning", "practice", "command"}
    ):
        task.cancel()
        store.record_event(
            "reasoning_preempted",
            f"Background {source} reasoning was interrupted by Alik.",
        )
        return True
    return False


async def poll_minecraft_chat(input_queue, runtime):
    while True:
        try:
            payload = await asyncio.to_thread(_get, "/chat-inbox")
            for message in payload.get("messages", []):
                text = str(message.get("text", "")).strip()
                if text:
                    runtime["last_user_activity"] = time.monotonic()
                    _preempt_background_reasoning(runtime)

                    if _is_activity_question(text):
                        answer = _current_activity_text()
                        try:
                            await asyncio.to_thread(
                                _post,
                                "/say",
                                {"message": answer},
                            )
                        except Exception:
                            pass
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [instant status]: {answer}")
                        store.record_episode(text, answer)
                        log_event("chat", "instant_reply", message=text, reply=answer)
                        continue

                    simple = _simple_chat_reply(text)
                    if simple is not None:
                        try:
                            await asyncio.to_thread(
                                _post,
                                "/say",
                                {"message": simple},
                            )
                        except Exception:
                            pass
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [instant chat]: {simple}")
                        store.record_episode(text, simple)
                        log_event("chat", "instant_reply", message=text, reply=simple)
                        continue

                    reflex = await fast_chat_reflex(text)
                    if reflex and reflex.get("handled"):
                        reply = reflex.get("reply") or "yep"
                        try:
                            await asyncio.to_thread(
                                _post,
                                "/say",
                                {"message": reply},
                            )
                        except Exception:
                            pass
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [instant action]: {reply}")
                        store.record_episode(text, reply)
                        log_event(
                            "chat",
                            "instant_action_ack",
                            message=text,
                            reply=reply,
                            background=bool(reflex.get("background")),
                        )
                        if reflex.get("background"):
                            await enqueue(
                                input_queue,
                                runtime,
                                0,
                                "command",
                                f"Alik asked: {text}",
                            )
                        continue

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
            _preempt_background_reasoning(runtime)
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
        "You are the occasional teacher for a local Minecraft companion that "
        "learns primarily by experimenting. The normal brain is local and must "
        "remain the learner. Give principles and one or two testable hypotheses, "
        "not a full walkthrough or authoritative solution. Do not propose "
        "teleportation, cheats, arbitrary code execution, or bypassing physical "
        "reach. The companion can observe state/vision/entities/blocks/inventory "
        "and use safe movement/world/crafting primitives.\n\n"
        "Return a compact hint with exactly these headings:\n"
        "LIKELY_GAP\nHYPOTHESES_TO_TEST\nEVIDENCE_TO_WATCH\n"
        "The local companion must test your advice before treating it as learned."
    )
    response = await client.responses.create(
        model=model,
        instructions=(
            "Be a concise Minecraft robotics/agent teacher. "
            "Teach principles and testable hypotheses, not step-by-step play."
        ),
        input=prompt,
        reasoning={"effort": "low"},
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
                "lesson",
                f"teacher.{safe_key[:100] or request['id']}",
                (
                    "UNTESTED TEACHER HYPOTHESIS. Verify in Minecraft before "
                    "promoting to a learned skill.\n" + lesson
                ),
                8,
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


async def minecraft_world_ready():
    try:
        state = await asyncio.to_thread(_get, "/state")
        return bool(state) and state.get("serverState") == "running"
    except Exception:
        return False


async def local_task_worker(input_queue, runtime):
    waiting_for_world = False

    while True:
        if not await minecraft_world_ready():
            if not waiting_for_world:
                print("\n[MINECRAFT] Waiting for world/companion before running local tasks...")
                waiting_for_world = True
            runtime["active_task"] = None
            await asyncio.sleep(1.0)
            continue

        if waiting_for_world:
            print("\n[MINECRAFT] World/companion connected. Resuming queued tasks.")
            waiting_for_world = False

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

        detail = (
            finished["error"]
            if finished["status"] == "failed" and finished.get("error")
            else finished["progress"]
        )
        summary = (
            f"Local task #{finished['id']} {finished['skill']} "
            f"{finished['status']}: {detail}"
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
    # High-level goals are intentionally NOT converted into hardcoded Python
    # skills. The local model advances them through evidence-producing practice
    # turns and stores any successful procedure in skill_memory.
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

        if (
            now - runtime["last_user_activity"] > 60.0
            and not runtime["self_improvement_attempted"]
            and owner_distance <= 20.0
        ):
            existing = next(
                (
                    goal
                    for goal in store.list_goals("active", 20)
                    if goal["title"].strip().lower() == "improve practical capability"
                    and goal.get("source") == "self"
                ),
                None,
            )
            if existing is None:
                goal = store.create_goal(
                    "Improve practical capability",
                    "Discover and test one useful way to become more capable in the current environment.",
                    4,
                    "self",
                )
                store.record_event(
                    "drive",
                    f"Self-improvement drive created exploratory goal #{goal['id']}.",
                )
            runtime["self_improvement_attempted"] = True

        meaningful_goals = [
            goal
            for goal in store.list_goals("active", 4)
            if goal.get("source") != "system" or goal.get("priority", 0) >= 6
        ]

        if job_active:
            continue

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
        source = "practice" if meaningful_goals else "autonomy"
        message = (
            "Continue learning or applying a procedure toward the highest-priority "
            "active goal. Produce evidence from the world, record the trial, and "
            "update learned skill knowledge when justified."
            if meaningful_goals
            else
            "You are safely idle with no urgent user goal. Review awareness and "
            "decide whether one small exploratory observation or experiment would "
            "be useful; otherwise remain idle."
        )
        await enqueue(
            input_queue,
            runtime,
            2,
            source,
            message,
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
        "reasoning_task": None,
        "reasoning_source": None,
    }

    model, model_label = choose_model()
    start_session(build=AGENT_BUILD, model=model_label)
    log_event(
        "agent",
        "startup",
        model=model_label,
        cloud_teacher=_cloud_teacher_enabled(),
        teacher_model=_teacher_model_name() if _cloud_teacher_enabled() else None,
    )
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
        print(f"Action log: {DEFAULT_LOG}")
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
                    log_event("chat", "user_message", message=message)
                    print(f"\n[MINECRAFT] Alik: {message}")

                try:
                    reply_in_game = source == "minecraft"
                    if source == "task":
                        reply_in_game = "completed" in message or "failed" in message

                    reasoning = asyncio.create_task(
                        run_turn(
                            agent,
                            message,
                            source=source,
                            reply_in_game=reply_in_game,
                            runtime=runtime,
                        )
                    )
                    runtime["reasoning_task"] = reasoning
                    runtime["reasoning_source"] = source
                    try:
                        await reasoning
                    except asyncio.CancelledError:
                        if source in {"autonomy", "event", "task", "learning", "practice"}:
                            print(
                                f"\n[{source.upper()}] Reasoning interrupted for Alik."
                            )
                        else:
                            raise
                    finally:
                        if runtime.get("reasoning_task") is reasoning:
                            runtime["reasoning_task"] = None
                            runtime["reasoning_source"] = None
                except Exception as error:
                    log_exception(
                        "agent",
                        "main_loop_error",
                        error,
                        source=source,
                        message=message,
                    )
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
                    if source in {"autonomy", "event", "task", "learning", "practice"}:
                        runtime["autonomy_pending"] = False
        finally:
            log_event("agent", "session_stop")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
