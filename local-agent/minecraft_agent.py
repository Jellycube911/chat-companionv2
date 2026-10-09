import asyncio
import json
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
from practice_engine import execute_plan, parse_plan


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
AGENT_BUILD = "self-learning-local-brain-v10-craft-evidence-2026-10-09"
BASE_DIR = Path(__file__).resolve().parent

set_tracing_disabled(True)


PLAYER_CHAT_INSTRUCTIONS = """
You are Chat, Alik's persistent Minecraft companion. Reply like a real player
typing while playing: normally 2-12 words and one sentence. Be casual and
direct. Never invent what your body is doing, what you collected, or what
succeeded. The supplied CURRENT/RECENT evidence is authoritative. If the
inventory has zero of something, never imply you have it. If a recent physical
attempt failed or stalled, say that plainly. Do not use markdown or lists.
/no_think
"""


PRACTICE_PLANNER_INSTRUCTIONS = """
You are the planning part of Chat, the same Minecraft companion identity.
You do NOT execute tools yourself in this mode. Choose exactly ONE safe next
primitive action. The Python host will validate and physically execute it.

Return ONLY one JSON object. No markdown, prose, comments, function-call syntax,
or invented results.

Allowed actions:
- scan_blocks: contains/exact, radius 1-20, limit 1-32, exposed_only
- move_to: x,y,z for a STANDABLE destination only; never target the solid block
  you intend to mine
- move_forward: only for a deliberate short test; prefer move_to for a known
  standable destination
- look_at: x,y,z
- mine: x,y,z, optional tool item id, and expected_block or expected_contains.
  IMPORTANT: mine already makes the body approach, face, reach, hold the mining
  action through real break progress, and physically break the target. Once a
  target block coordinate is known, prefer mine directly instead of
  move_to(target_block). The host verifies the block identity before mining.
- collect
- place: item or slot, optional x,y,z, optional face
- equip: item or slot
- craft: width,height,grid,times. Grid has EXACTLY width*height item-ID
  strings, in row-major order; use "" for empty positions.
  A 1x1 grid contains ONE entry, a 2x2 grid FOUR, and 3x3 NINE.
  A 3x3 recipe requires a PLACED crafting table near your body.
- idle: only if no useful safe action exists

Required fields for every non-idle action:
- intent: a short stable capability name
- hypothesis: what this action is expected to accomplish
- action

Examples of syntax only:
{"intent":"locate logs","hypothesis":"nearby loaded logs can be found by scanning","action":"scan_blocks","contains":["_log"],"radius":20,"limit":12}
{"intent":"harvest known log","hypothesis":"the mining primitive can approach and break the known block","action":"mine","x":100,"y":64,"z":100,"expected_contains":"_log"}
{"intent":"reach a standable spot","hypothesis":"moving to nearby open ground will improve access","action":"move_to","x":98,"y":64,"z":100}
{"intent":"try an observed ingredient in crafting","hypothesis":"the inventory item may craft into a useful ingredient","action":"craft","width":1,"height":1,"grid":["minecraft:oak_log"],"times":1}
{"intent":"place a stored work surface","hypothesis":"a table may allow larger crafting recipes","action":"place","item":"minecraft:crafting_table"}

Vanilla crafting reference for a WOODEN AXE (a hypothesis the world must
verify): requires three matching planks and two sticks, arranged in 3x3:
{"intent":"craft wooden axe","hypothesis":"test standard 3x3 axe recipe","action":"craft","width":3,"height":3,"grid":["minecraft:jungle_planks","minecraft:jungle_planks","","minecraft:jungle_planks","minecraft:stick","","","minecraft:stick",""],"times":1}
Only use that example if the required items are actually carried and a crafting
table is nearby. This example is not proof of success, and it does NOT
authorize imaginary separate "axe heads" or crafting with a pickaxe.

If recent evidence says move_to is repath_pending with no position change, do
NOT repeat the same target. Change the action or target. If a scan found the
desired block within about 4.5 blocks, try mine directly.

EFFICIENCY LEARNING
The host measures actual physical execution time and stores persistent reward
history. Higher average reward means that option has been more effective in
real Minecraft. For mining, break ticks are measured only while actually
breaking the block, so tool comparisons are meaningful. Prefer the best measured
tool for that block type when it is available. If evidence is sparse and the
goal involves repeated harvesting, a safe comparison of plausible inventory
tools can be useful. Do not assume a tool was better unless measured evidence
or a durable user/teacher lesson supports it. When choosing a mining tool, put
its exact inventory item id in the optional "tool" field.

Never claim success. Never output experiment(...), scan_blocks(...), or any
pseudo-call as text. Output JSON only. The host decides whether the action
actually succeeded from Minecraft evidence.
/no_think
"""


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
4. prefer experiment(...) for physical tests because it automatically records
   the action, verified before/after state, and success/failure as a trial;
5. use scan_blocks(...) when you need a coarse loaded-area search for a specific
   block/resource rather than wandering randomly;
6. observe additional state only when needed to interpret the result;
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
- scan_blocks is a coarse nearby resource/map search and is not literal sight;
- experiment wraps one safe primitive action and automatically stores evidence;
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


def _ollama_root():
    base = _local_endpoint()
    if base.endswith("/v1"):
        return base[:-3].rstrip("/")
    return base.rstrip("/")


def _native_local_completion(
    model_name,
    system_prompt,
    user_prompt,
    *,
    json_mode=False,
    max_tokens=80,
):
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {
            "num_predict": int(max_tokens),
            "temperature": 0.2,
        },
    }
    if json_mode:
        payload["format"] = "json"

    response = requests.post(
        f"{_ollama_root()}/api/chat",
        json=payload,
        timeout=35,
    )
    response.raise_for_status()
    data = response.json()
    return str((data.get("message") or {}).get("content") or "").strip()


async def _local_fast_completion(
    runtime,
    system_prompt,
    user_prompt,
    *,
    json_mode=False,
    max_tokens=80,
):
    label = str(runtime.get("model_label") or "")
    if not label.startswith("local:"):
        return None
    model_name = label.split(":", 1)[1]
    try:
        return await asyncio.to_thread(
            _native_local_completion,
            model_name,
            system_prompt,
            user_prompt,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
    except Exception as error:
        log_exception(
            "agent",
            "native_local_fallback",
            error,
            json_mode=json_mode,
        )
        return None


async def _send_ingame(message, *, reason="reply", retry=True):
    message = _format_ingame_reply(message)
    if not message:
        return False

    attempts = 2 if retry else 1
    last_result = None
    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.to_thread(
                _post,
                "/say",
                {"message": message},
            )
            last_result = result
            ok = not (
                isinstance(result, dict)
                and result.get("ok") is False
            )
            log_event(
                "chat",
                "send_result",
                reason=reason,
                message=message,
                attempt=attempt,
                ok=ok,
                result=result,
            )
            if ok:
                return True
        except Exception as error:
            log_exception(
                "chat",
                "send_error",
                error,
                reason=reason,
                message=message,
                attempt=attempt,
            )
        if attempt < attempts:
            await asyncio.sleep(0.15)

    store.record_event(
        "chat_delivery_failed",
        f"Could not send in-game chat ({reason}): {last_result}",
    )
    return False


async def _delayed_chat_ack(delay=1.2):
    await asyncio.sleep(delay)
    await _send_ingame("sec", reason="slow_reply_ack", retry=False)


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


def _chat_awareness_text(runtime):
    awareness = runtime.get("awareness") or {}
    state = awareness.get("state") or {}
    owner = state.get("owner") or {}
    inventory = awareness.get("inventory") or []
    items = [
        f"{item.get('item')}x{item.get('count')}"
        for item in inventory[:36]
    ]
    try:
        pos = (
            f"({float(state.get('x', 0)):.1f},"
            f"{float(state.get('y', 0)):.1f},"
            f"{float(state.get('z', 0)):.1f})"
        )
        owner_distance = f"{float(owner.get('distance', 0)):.1f}"
    except Exception:
        pos = "unknown"
        owner_distance = "unknown"
    recent_events = store.recent_events(3)
    evidence = [
        f"[{event['kind']}] {_trim(event['summary'], 260)}"
        for event in recent_events
        if event.get("kind") in {
            "practice_result",
            "practice_observation",
            "practice_error",
            "tool_failure",
            "user_feedback",
            "learning_complete",
        }
    ]
    job = (
        f"{state.get('jobType')} {state.get('jobState')} {state.get('jobReason')}"
        if state.get("jobActive")
        else "none"
    )
    return (
        "QUICK CHAT CONTEXT:\n"
        f"- pos={pos}\n"
        f"- Alik distance={owner_distance}\n"
        f"- physical job={job}\n"
        f"- activity={_current_activity_text(runtime)}\n"
        f"- inventory={items or ['empty']}\n"
        f"- recent physical evidence={evidence or ['none']}\n"
        "- Never claim progress not supported by this evidence."
    )


def build_practice_plan_input(runtime, directive=None):
    goals = store.list_goals("active", 4)
    trials = store.recent_trials(4)
    events = store.recent_events(5)

    learning_query = " ".join(
        [directive or ""]
        + [f"{goal['title']} {goal['description']}" for goal in goals[:3]]
    )
    learned = [
        skill for skill in store.find_learned_skills(learning_query, 8)
        if float(skill.get("confidence", 0)) >= 0.6
        and int(skill.get("successes", 0)) >= 2
        and int(skill.get("successes", 0)) >= int(skill.get("failures", 0))
    ][:3]
    relevant_memory = store.recall(learning_query, 4)
    efficiency = store.list_efficiency(12)

    parts = [_awareness_text(runtime)]
    if directive:
        parts.append(f"CURRENT USER DIRECTIVE: {directive}")
    recent_subgoal = _current_user_subgoal(runtime)
    if recent_subgoal:
        parts.append(f"RECENT USER INSTRUCTION (temporary subgoal): {recent_subgoal}")

    if goals:
        parts.append("ACTIVE GOALS:")
        for goal in goals:
            parts.append(
                f"- #{goal['id']} p{goal['priority']} {goal['title']}: "
                f"{_trim(goal['description'], 220)}"
            )

    if learned:
        parts.append("RELEVANT LEARNED PROCEDURES:")
        for skill in learned:
            parts.append(
                f"- {skill['name']} confidence={skill['confidence']}: "
                f"{_trim(skill['procedure'], 420)}"
            )

    if relevant_memory:
        parts.append("RELEVANT DURABLE LESSONS / USER HINTS:")
        for memory in relevant_memory:
            parts.append(
                f"- [{memory['kind']}] {_trim(memory['content'], 500)}"
            )

    if efficiency:
        parts.append("PERSISTENT EFFICIENCY REWARD MEMORY (higher reward is better):")
        for item in efficiency:
            parts.append(
                f"- {item['context']} option={item['option']} "
                f"attempts={item['attempts']} success={item['successes']}/{item['attempts']} "
                f"avg={item['avg_seconds']:.2f}s best={item['best_seconds']:.2f}s "
                f"reward={item['avg_reward']:.3f}"
            )

    if trials:
        parts.append("RECENT EXPERIMENT EVIDENCE:")
        for trial in trials:
            parts.append(
                f"- intent={trial['intent']} success={trial['success']} "
                f"hypothesis={_trim(trial['hypothesis'], 180)} "
                f"outcome={_trim(trial['outcome'], 280)}"
            )

    blocked = [
        (key, value) for key, value in runtime.get("invalid_targets", {}).items()
        if time.monotonic() - value[0] < _invalid_target_ttl(key)
    ]
    if blocked:
        parts.append("REJECTED ACTIONS: do not repeat exact coordinates or crafting grids:")
        for key, (_, reason) in blocked[-8:]:
            parts.append(f"- {_trim(key, 280)}: {_trim(reason, 180)}")

    if events:
        parts.append("RECENT EVENTS/OBSERVATIONS:")
        for event in events:
            parts.append(
                f"- [{event['kind']}] {_trim(event['summary'], 360)}"
            )

    goal = next((g for g in goals if g.get("source") == "user"), None)
    if goal:
        budget = max(0, 2 - _goal_navigation_count(
            runtime, goal, (runtime.get("awareness") or {}).get("inventory") or []
        ))
        parts.append(
            f"Navigation trials left before a material experiment: {budget}. "
            "When zero, DO NOT move_to/move_forward/look_at. Use craft, "
            "place, equip, collect, mine, scan_blocks or idle. "
            "Inventoried resources should be used before gathering more."
        )
    parts.append(
        "Choose ONE next primitive that best advances the current user directive "
        "or, if there is none, the highest-priority active goal. "
        "For every action supporting the active USER goal, include numeric goal_id "
        "and a short goal_reason saying WHY this experiment is a prerequisite. "
        "An action like crafting an intermediate material can support a goal "
        "without naming the final item in its action intent. "
        "If a resource "
        "location is unknown, scan first. IMPORTANT API SEMANTICS: mine already "
        "approaches/faces/reaches the target block. Never move_to the coordinate "
        "of a solid block you intend to mine. move_to is only for a standable "
        "destination. Respect Alik's corrections and teacher hints above. Do not "
        "repeat a failed target unchanged. A user-assigned crafting goal takes "
        "priority over self-practice. If resources are in the inventory, "
        "experiment with craft/place before searching for more resources. "
        "craft takes width,height,grid (use empty strings for empty slots),times. "
        "For 3x3, array length MUST be 9, not 1 or 2; a placed table must "
        "be close enough. Match ingredient counts to the actual inventory. "
        "Do not put a pickaxe in the crafting grid to make an axe or invent "
        "a separate axe-head recipe. Do not invent recipe outcomes. Output JSON only."
    )
    return "\n".join(parts)


def _inventory_count(inventory, predicate):
    return sum(
        int(item.get("count", 0))
        for item in (inventory or [])
        if predicate(str(item.get("item", "")))
    )


def _reconcile_user_goals(execution):
    after = (execution or {}).get("after") or {}
    inventory = after.get("inventory") or []
    messages = []

    for goal in store.list_goals("active", 20):
        if goal.get("source") != "user":
            continue
        title = str(goal.get("title") or "")
        normalized = title.lower().strip()

        match = re.fullmatch(r"gather\s+(\d+)\s+logs?", normalized)
        if match:
            target = int(match.group(1))
            count = _inventory_count(
                inventory,
                lambda item: item.startswith("minecraft:") and item.endswith("_log"),
            )
            if count >= target:
                store.update_goal(goal["id"], status="completed")
                messages.append(f"got {count} logs, task done")
                continue

        if normalized in {"obtain a stone pickaxe", "get a stone pickaxe"}:
            has_pickaxe = _inventory_count(
                inventory,
                lambda item: item == "minecraft:stone_pickaxe",
            ) > 0
            if has_pickaxe:
                store.update_goal(goal["id"], status="completed")
                messages.append("got the stone pickaxe, task done")
                continue

        if normalized in {"obtain an axe", "get an axe"}:
            has_axe = _inventory_count(
                inventory,
                lambda item: item.startswith("minecraft:") and item.endswith("_axe"),
            ) > 0
            if has_axe:
                store.update_goal(goal["id"], status="completed")
                messages.append("got an axe, task done")
                continue

        if normalized in {"obtain a crafting table", "get a crafting table"}:
            table_count = _inventory_count(
                inventory,
                lambda item: item == "minecraft:crafting_table",
            )
            if table_count > 0:
                store.update_goal(goal["id"], status="completed")
                messages.append(
                    f"already got {table_count} crafting table"
                    + ("s, task done" if table_count != 1 else ", task done")
                )

    return messages


def _execution_failure_reason(execution):
    if not execution:
        return "no result"
    result = execution.get("result") or {}
    return str(
        result.get("error")
        or result.get("reason")
        or execution.get("error")
        or "unknown issue"
    ).strip()


async def _notify_once(runtime, key, message, cooldown=30.0):
    now = time.monotonic()
    notices = runtime.setdefault("notices", {})
    last = float(notices.get(key, 0.0) or 0.0)
    if now - last < cooldown:
        return False
    # Routine background failures should not flood the user's chat. Explicit
    # user-command failures and completed goals always get priority.
    urgent = key.startswith(("goal_complete:", "command_fail:"))
    if not urgent and now - float(notices.get("_last_routine", 0.0)) < 90.0:
        return False
    notices[key] = now
    if not urgent:
        notices["_last_routine"] = now
    await _send_ingame(message, reason="proactive_status")
    log_event(
        "chat",
        "proactive_status",
        key=key,
        message=message,
    )
    return True


async def _report_planned_outcome(runtime, execution, source):
    if not execution:
        if source == "command":
            await _notify_once(
                runtime,
                "command:no_result",
                "couldn't get a usable action plan",
                cooldown=5.0,
            )
        return

    for message in _reconcile_user_goals(execution):
        await _notify_once(
            runtime,
            "goal_complete:" + message,
            message,
            cooldown=2.0,
        )

    learning = execution.get("learning") or {}
    promoted = learning.get("promoted_skill") if isinstance(learning, dict) else None
    teacher = learning.get("teacher_request") if isinstance(learning, dict) else None

    if promoted:
        name = str(promoted.get("name") or promoted.get("intent") or "that")
        await _notify_once(
            runtime,
            f"learned:{promoted.get('id', name)}",
            _format_ingame_reply(f"figured out a reliable way to {name}"),
            cooldown=120.0,
        )

    if teacher and teacher.get("ok") and not teacher.get("reused"):
        intent = str((execution.get("plan") or {}).get("intent") or "this")
        await _notify_once(
            runtime,
            f"teacher:{intent}",
            _format_ingame_reply(f"I'm stuck on {intent}, trying to learn another way"),
            cooldown=90.0,
        )

    plan = execution.get("plan") or {}
    streaks = runtime.setdefault("failure_streaks", {})
    streak_key = f"{plan.get('intent', '')}:{plan.get('action', '')}"

    if execution.get("status") in {
        "manual_control_hold",
        "goal_drift",
        "planner_backoff",
        "planner_stuck_backoff",
        "physical_job_in_progress",
        "pending",
        "post_goal_pause",
        "goal_completed",
        "skipped_repeat",
    }:
        return

    if execution.get("ok"):
        streaks.pop(streak_key, None)
        return

    streaks[streak_key] = int(streaks.get(streak_key, 0)) + 1
    reason = _execution_failure_reason(execution)
    if reason.startswith("mining_blocked|"):
        parts = dict(
            field.split("=", 1) for field in reason.split("|")[1:] if "=" in field
        )
        block = str(parts.get("block") or "something").removeprefix("minecraft:")
        reason = f"{block} blocks my view"
    short_reasons = {
        "target_block_mismatch": "wrong block, rechecking",
        "target_block_is_air": "block is already gone",
        "destination_occupied": "that spot is blocked",
    }
    reason = short_reasons.get(reason, reason.split(".")[0][:60])

    if source == "command":
        await _notify_once(
            runtime,
            f"command_fail:{streak_key}:{reason}",
            _format_ingame_reply(f"couldn't do that: {reason}"),
            cooldown=5.0,
        )
    elif streaks[streak_key] >= 2:
        intent = str(plan.get("intent") or "that")
        await _notify_once(
            runtime,
            f"blocked:{streak_key}:{reason}",
            _format_ingame_reply(f"stuck on {intent}: {reason}"),
            cooldown=90.0,
        )


def _plan_matches_user_goal(plan, goal, subgoal=""):
    """Guard against objective drift without prescribing Minecraft recipes."""
    if not goal:
        return True
    # An explicit goal id may not claim a different user objective.
    if goal.get("id") is not None and plan.get("goal_id") is not None:
        try:
            if int(plan["goal_id"]) != int(goal["id"]):
                return False
        except (TypeError, ValueError):
            return False
    if plan.get("action") in {"idle", "scan_blocks", "craft", "place"}:
        return True
    # A tool-building prerequisite does not have to name the finished item.
    # Require an explicit link to the real active goal, not a Minecraft recipe.
    if goal.get("id") is not None:
        try:
            linked = int(plan.get("goal_id")) == int(goal["id"])
        except (TypeError, ValueError):
            linked = False
        if linked and len(str(plan.get("goal_reason") or "").strip()) >= 8:
            return True
    keywords = set(re.findall(r"[a-z]{3,}", str(goal.get("title") or "").lower())) - {
        "obtain", "make", "craft", "gather", "build", "some", "with", "from", "your"
    }
    if not keywords:
        return True
    rationale = " ".join(
        str(plan.get(key) or "").lower() for key in ("intent", "hypothesis")
    )
    if any(word in rationale for word in keywords):
        return True
    if subgoal:
        subgoal_words = set(re.findall(r"[a-z]{3,}", subgoal.lower())) - {
            "which", "what", "where", "spot", "need", "should", "you",
            "craft", "make", "will", "and", "the", "with", "that", "this",
            "said", "please", "have", "your"
        }
        return bool(subgoal_words & set(re.findall(r"[a-z]{3,}", rationale)))
    return False


def _goal_navigation_count(runtime, goal, inventory):
    if not goal:
        return 0
    signature = tuple(sorted(
        (str(item.get("item") or ""), int(item.get("count") or 0))
        for item in (inventory or [])
    ))
    marker = (str(goal.get("id", goal.get("title"))), signature)
    saved = runtime.get("goal_navigation") or {}
    if saved.get("marker") != marker:
        saved = {"marker": marker, "count": 0}
        runtime["goal_navigation"] = saved
    return int(saved.get("count", 0))


def _record_goal_navigation(runtime, goal, inventory, plan):
    if goal and plan.get("action") in {"move_to", "move_forward", "look_at"}:
        _goal_navigation_count(runtime, goal, inventory)
        runtime["goal_navigation"]["count"] += 1


def _autonomy_job_blocks_practice(state, runtime, has_goals):
    """A continuous follow job is a stance, not a completed work action."""
    if not state.get("jobActive"):
        return False
    return not (
        has_goals
        and str(state.get("jobType") or "").upper() == "FOLLOW"
        and time.monotonic() >= float(runtime.get("manual_control_until", 0.0) or 0.0)
    )


def _current_user_subgoal(runtime):
    subgoal = runtime.get("user_subgoal") or {}
    if time.monotonic() - float(subgoal.get("at", 0.0)) >= 120.0:
        return ""
    return str(subgoal.get("text") or "")


def _target_failure_needs_replan(execution):
    """Quarantine invalid targets and rejected recipe hypotheses."""
    result = (execution or {}).get("result") or {}
    plan = (execution or {}).get("plan") or {}
    if plan.get("action") == "craft":
        return (execution or {}).get("ok") is False
    reason = str(result.get("error") or result.get("reason") or "")
    return reason in {
        "target_block_mismatch", "target_block_is_air", "destination_occupied",
        "mining_alignment_timeout",
    } or reason.startswith("mining_blocked|")


def _invalid_target_ttl(key):
    return 1800.0 if str(key).startswith("craft@") else 180.0


def _handle_craft_learning(runtime, plan, execution, goal, inventory):
    """Ask for sparse guidance only after repeated real crafting failures."""
    if plan.get("action") != "craft":
        return
    if execution.get("ok"):
        runtime["failed_craft_attempts"] = 0
        return
    count = int(runtime.get("failed_craft_attempts", 0)) + 1
    runtime["failed_craft_attempts"] = count
    if count < 3 or not goal or not _cloud_teacher_enabled():
        return
    runtime["failed_craft_attempts"] = 0
    result = execution.get("result") or {}
    observed_error = (
        result.get("error") or execution.get("error")
        or "crafting did not produce an item"
    )
    hint = store.request_learning(
        "crafting:" + str(goal.get("title") or "unknown").lower(),
        "User goal: " + str(goal.get("title"))
        + ". Local Minecraft server rejected crafting attempts. "
        + "Latest attempted dimensions/grid: "
        + json.dumps({
            "width": plan.get("width"), "height": plan.get("height"),
            "grid": plan.get("grid"),
        }, ensure_ascii=False)
        + ". Observed error: " + str(observed_error)
        + ". Actual inventory: " + json.dumps(inventory, ensure_ascii=False)
        + ". Propose an UNTESTED valid Minecraft crafting sequence with "
        "correct width*height grid entries, needed placement/crafting table, "
        "and a world-verified success criterion. Never invent an axe head item.",
        cooldown_seconds=1800,
    )
    log_event(
        "practice_host", "craft_teacher_escalation",
        failures=count, teacher_request=hint,
    )


def _plan_target_key(plan):
    action = str(plan.get("action") or "")
    if action == "craft":
        return "craft@" + json.dumps(
            [plan.get("width"), plan.get("height"), plan.get("grid")],
            ensure_ascii=False, separators=(",", ":")
        )[:550]
    if action not in {"mine", "move_to", "place"}:
        return None
    try:
        xyz = ",".join(str(int(math.floor(float(plan[axis])))) for axis in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return f"{action}@{xyz}"


async def run_planned_action(planner_agent, runtime, directive=None, source="practice"):
    started = time.monotonic()

    try:
        await update_awareness(runtime)
    except Exception:
        pass

    awareness = runtime.get("awareness") or {}
    state = awareness.get("state") or {}
    awareness_inventory = awareness.get("inventory") or []

    completed = _reconcile_user_goals(
        {"after": {"inventory": awareness_inventory}}
    )
    for message in completed:
        await _notify_once(
            runtime,
            "goal_complete:" + message,
            message,
            cooldown=2.0,
        )

    if completed and source == "practice":
        runtime["post_goal_pause_until"] = time.monotonic() + 20.0
        log_event(
            "practice_host",
            "goal_satisfied_before_planning",
            completed=completed,
        )
        return {
            "ok": True,
            "status": "goal_completed",
            "completed": completed,
        }

    if (
        source == "practice"
        and time.monotonic() < float(runtime.get("post_goal_pause_until", 0.0) or 0.0)
        and not any(
            goal.get("source") == "user"
            for goal in store.list_goals("active", 20)
        )
    ):
        log_event("practice_host", "post_goal_pause")
        return {
            "ok": True,
            "status": "post_goal_pause",
        }

    if (
        source == "practice"
        and time.monotonic() < float(runtime.get("manual_control_until", 0.0) or 0.0)
    ):
        log_event("practice_host", "manual_control_hold")
        return {"ok": True, "status": "manual_control_hold"}

    active_goal = bool(store.list_goals("active", 4))
    preempt_follow = (
        source == "practice"
        and state.get("jobActive")
        and not _autonomy_job_blocks_practice(state, runtime, active_goal)
    )
    if state.get("jobActive"):
        if source == "command" or preempt_follow:
            try:
                stopped = await asyncio.to_thread(_post, "/stop-action")
                if isinstance(stopped, dict) and stopped.get("ok") is False:
                    raise RuntimeError(f"stop action rejected: {stopped}")
                await asyncio.sleep(0.1)
                await update_awareness(runtime)
                state = (runtime.get("awareness") or {}).get("state") or {}
                log_event(
                    "practice_host", "preempt_follow_for_goal" if preempt_follow else "command_preemption",
                    accepted=not state.get("jobActive", False),
                    job_type=state.get("jobType"),
                )
                if state.get("jobActive"):
                    return {"ok": False, "status": "preemption_failed"}
            except Exception as error:
                log_exception(
                    "practice_host",
                    "command_preemption_error",
                    error,
                    directive=directive,
                )
                return {"ok": False, "status": "preemption_failed"}
        else:
            log_event(
                "practice_host",
                "physical_job_in_progress",
                job_type=state.get("jobType"),
                job_state=state.get("jobState"),
                job_reason=state.get("jobReason"),
                job_progress=state.get("jobProgress"),
            )
            return {
                "ok": True,
                "status": "physical_job_in_progress",
                "job": {
                    "type": state.get("jobType"),
                    "state": state.get("jobState"),
                    "reason": state.get("jobReason"),
                    "progress": state.get("jobProgress"),
                },
            }

    if source == "practice" and time.monotonic() < float(runtime.get("planner_backoff_until", 0.0) or 0.0):
        log_event("practice_host", "planner_backoff", until=runtime["planner_backoff_until"])
        return {"ok": True, "status": "planner_backoff"}

    prompt = build_practice_plan_input(runtime, directive=directive)
    log_event(
        "practice_host",
        "planner_start",
        source=source,
        directive=directive,
        active_goals=store.list_goals("active", 4),
    )

    try:
        raw = await _local_fast_completion(
            runtime,
            PRACTICE_PLANNER_INSTRUCTIONS,
            prompt,
            json_mode=True,
            max_tokens=180,
        )
        if not raw:
            result = await asyncio.wait_for(
                Runner.run(planner_agent, prompt, max_turns=1),
                timeout=30.0,
            )
            raw = str(result.final_output or "").strip()
    except asyncio.TimeoutError as error:
        log_exception(
            "practice_host",
            "planner_timeout",
            error,
            source=source,
            directive=directive,
        )
        store.record_event(
            "practice_error",
            "Local planner timed out before proposing an action.",
        )
        return None

    plan = parse_plan(raw)
    if plan is None:
        log_event(
            "practice_host",
            "invalid_plan",
            source=source,
            directive=directive,
            raw=raw,
        )
        store.record_event(
            "practice_error",
            f"Planner returned invalid action JSON: {_trim(raw, 500)}",
        )
        return None

    user_goal = next(
        (goal for goal in store.list_goals("active", 20) if goal.get("source") == "user"),
        None,
    )
    exhausted = (
        source == "practice"
        and user_goal is not None
        and plan.get("action") in {"move_to", "move_forward", "look_at"}
        and _goal_navigation_count(runtime, user_goal, awareness_inventory) >= 2
    )
    drift = not _plan_matches_user_goal(
        plan, user_goal, directive or _current_user_subgoal(runtime)
    )
    if drift or exhausted:
        cause = "navigation_budget_exhausted" if exhausted else "plan_unrelated_to_user_goal"
        log_event("practice_host", "planner_plan_rejected", plan=plan, cause=cause)
        restriction = (
            "DO NOT propose move_to, move_forward, or look_at. "
            "Use a craft/place/inventory experiment or scan instead."
            if exhausted else
            "Propose an action that directly prepares materials or explain its "
            "relation using goal_id and goal_reason."
        )
        raw_revision = await _local_fast_completion(
            runtime, PRACTICE_PLANNER_INSTRUCTIONS,
            prompt + "\nREJECTED: " + json.dumps(plan, ensure_ascii=False)
            + "\n" + restriction + "\nReturn a DIFFERENT JSON action.",
            json_mode=True, max_tokens=180,
        )
        revision = parse_plan(raw_revision)
        invalid = (
            revision is None
            or not _plan_matches_user_goal(
                revision, user_goal, directive or _current_user_subgoal(runtime)
            )
            or (exhausted and revision.get("action") in {"move_to", "move_forward", "look_at"})
        )
        if invalid:
            runtime["planner_backoff_until"] = time.monotonic() + 30.0
            log_event("practice_host", "planner_stuck_backoff", cause=cause, revision=revision)
            if exhausted and user_goal and _cloud_teacher_enabled():
                # A repeated failed reasoning strategy is a legitimate reason
                # for a rare cloud lesson, not another motion experiment.
                lesson = store.request_learning(
                    "goal_navigation:" + str(user_goal["title"]).lower(),
                    "Local planner repeatedly proposes navigation rather than "
                    "material actions for user goal: " + str(user_goal["title"])
                    + ". Available inventory: "
                    + json.dumps(awareness_inventory, ensure_ascii=False)
                    + ". Give a concise untested next crafting/placement "
                    "hypothesis, with an example grid and verification step. "
                    "Do not claim any physical action succeeded.",
                    cooldown_seconds=1800,
                )
                log_event("practice_host", "goal_teacher_escalation", request=lesson)
            return {"ok": False, "status": "planner_stuck_backoff", "plan": plan, "error": cause}
        log_event("practice_host", "goal_drift_replanned", plan=revision)
        plan = revision

    if source == "practice":
        _record_goal_navigation(runtime, user_goal, awareness_inventory, plan)

    target_key = _plan_target_key(plan)
    invalid = runtime.setdefault("invalid_targets", {})
    if (
        target_key in invalid
        and time.monotonic() - invalid[target_key][0] < _invalid_target_ttl(target_key)
    ):
        why = str(invalid[target_key][1])
        log_event(
            "practice_host", "repeat_invalid_target_skipped",
            target=target_key, reason=why,
        )
        # Don't burn another HTTP request on an identical rejected recipe.
        # Give the planner one chance to form a new, testable hypothesis.
        alternate = await _local_fast_completion(
            runtime, PRACTICE_PLANNER_INSTRUCTIONS,
            prompt + "\nYOUR LAST ACTION IS ALREADY KNOWN TO FAIL: "
            + _trim(target_key, 450) + "\nObserved error: " + _trim(why, 250)
            + "\nChoose DIFFERENT valid crafting dimensions/grid or a "
            "different physical action. Repeating the same grid is forbidden. "
            "Return ONE JSON object.",
            json_mode=True, max_tokens=220,
        )
        revised = parse_plan(alternate)
        revised_key = _plan_target_key(revised) if revised is not None else None
        if (
            revised is None or revised_key == target_key
            or not _plan_matches_user_goal(
                revised, user_goal, directive or _current_user_subgoal(runtime)
            )
            or (
                revised_key in invalid
                and time.monotonic() - invalid[revised_key][0] < _invalid_target_ttl(revised_key)
            )
        ):
            runtime["planner_backoff_until"] = time.monotonic() + 25.0
            log_event("practice_host", "repeated_recipe_backoff", rejected=target_key, revision=revised)
            return {"ok": False, "status": "skipped_repeat", "plan": plan}
        log_event("practice_host", "invalid_target_replanned", old=plan, new=revised)
        plan = revised
        target_key = revised_key

    runtime["physical_action_active"] = True
    try:
        execution = await asyncio.to_thread(execute_plan, plan)
    finally:
        runtime["physical_action_active"] = False
    result = execution.get("result") or {}
    reason = str(
        result.get("error") or result.get("reason")
        or execution.get("error") or ""
    )
    if target_key and _target_failure_needs_replan(execution):
        invalid[target_key] = (time.monotonic(), reason)
        if plan.get("action") == "craft":
            log_event(
                "practice_host", "craft_recipe_rejected",
                recipe_key=target_key, reason=reason,
            )
    elif target_key and execution.get("ok"):
        invalid.pop(target_key, None)
        if plan.get("action") == "mine":
            # Mining changes the physical scene. Previously occluded targets
            # may now be reachable, so let the brain revisit them.
            for key, (_, why) in list(invalid.items()):
                if why.startswith("mining_blocked|"):
                    invalid.pop(key, None)
    _handle_craft_learning(runtime, plan, execution, user_goal, awareness_inventory)
    await update_awareness(runtime)
    log_event(
        "practice_host",
        "planner_end",
        source=source,
        directive=directive,
        elapsed_ms=round((time.monotonic() - started) * 1000),
        plan=plan,
        execution=execution,
    )
    if execution.get("ok") and (execution.get("result") or {}).get("state") == "COMPLETED":
        runtime["last_verified_action"] = {
            "at": time.monotonic(),
            "action": plan.get("action"),
            "result": execution.get("result"),
        }
    await _report_planned_outcome(runtime, execution, source)
    return execution


def build_turn_input(message, source, runtime):
    if source in {"minecraft", "console"}:
        parts = [_chat_awareness_text(runtime)]
        recent = store.recent_episodes(2)
        if recent:
            parts.append("RECENT CHAT:")
            for episode in recent:
                parts.append(f"Alik: {_trim(episode['user'], 160)}")
                parts.append(
                    f"Chat: {_trim(_format_ingame_reply(episode['assistant']), 120)}"
                )

        relevant = store.recall(message, 2)
        if relevant:
            parts.append("RELEVANT MEMORY:")
            for memory in relevant:
                parts.append(
                    f"- {_trim(memory['content'], 220)}"
                )

        parts.append("CURRENT MESSAGE FROM ALIK:")
        parts.append(message)
        parts.append(
            "Reply like an in-game player. One short natural sentence unless "
            "Alik explicitly asks for detail."
        )
        parts.append("/no_think")
        return "\n".join(parts)

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
            "INTERNAL PRACTICE TURN: not a message from Alik. Never chat, ask "
            "questions, or explain observations. Advance the highest-priority "
            "active goal through ONE small evidence-producing action. Prefer "
            "experiment(...) because it performs the physical action and records "
            "verified learning evidence automatically. Use scan_blocks(...) when "
            "you need to locate a resource. IMPORTANT: never output tool arguments "
            "or an action object as JSON/text. If you want an action to happen, "
            "CALL the MCP tool. A text description of an action does nothing. "
            "If no safe useful physical action is possible, inspect once and stop. "
            "When the goal is visibly achieved, update the goal to completed. "
            "After the tool result, end with at most 8 words of internal status."
        )
        parts.append("/no_think")
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
    max_turns = (
        6
        if source in {"autonomy", "event", "task", "learning", "practice"}
        else 8
        if source == "command"
        else 1
    )
    turn_started = time.monotonic()
    ack_task = None
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
        answer = None
        if source in {"minecraft", "console"}:
            answer = await _local_fast_completion(
                runtime,
                PLAYER_CHAT_INSTRUCTIONS,
                prepared,
                json_mode=False,
                max_tokens=48,
            )
        if not answer:
            result = await Runner.run(agent, prepared, max_turns=max_turns)
            answer = str(result.final_output or "").strip()
        if source in {"minecraft", "console"}:
            answer = _format_ingame_reply(_ground_chat_reply(answer, runtime))
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
            else "got stuck thinking, one sec"
        )
    except asyncio.CancelledError:
        if ack_task is not None and not ack_task.done():
            ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)
        raise

    if answer and source in {"minecraft", "console"}:
        print(f"\nAI: {answer}")
    elif answer and source == "task":
        print(f"\n[TASK] {_format_ingame_reply(answer)}")
    elif answer and source == "learning":
        print(f"\n[LEARNING] {_trim(answer, 240)}")
    elif answer:
        # Internal autonomy/practice/command output belongs in the action log.
        # Printing raw model output here caused tool arguments to look like
        # actions even when the local model had merely emitted JSON as text.
        log_event(
            "agent",
            "internal_output",
            source=source,
            reply=answer,
        )

    if ack_task is not None and not ack_task.done():
        ack_task.cancel()
        await asyncio.gather(ack_task, return_exceptions=True)

    if answer and reply_in_game:
        sent = await _send_ingame(
            answer,
            reason=f"{source}_final_reply",
        )
        if not sent:
            print("\n[CHAT ERROR] Could not deliver in-game reply.")

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


def _normalize_request_text(text):
    normalized = str(text or "").lower().strip().rstrip(".!?")
    prefixes = (
        "yeah so ",
        "yea so ",
        "yep so ",
        "ok so ",
        "okay so ",
        "alright so ",
        "right so ",
        "well ",
        "so ",
        "come on ",
        "cmon ",
        "c'mon ",
        "mate ",
        "hey ",
        "sorry ",
        "actually ",
    )
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):].lstrip(" ,")
                changed = True
                break
    return normalized


def _looks_like_direct_request(text):
    normalized = _normalize_request_text(text)
    if any(
        phrase in normalized
        for phrase in (
            "didnt ",
            "didn't ",
            "did not ",
            "havent ",
            "haven't ",
            "hasnt ",
            "hasn't ",
            "you didnt",
            "you didn't",
            "you did not",
        )
    ):
        return False
    return normalized.startswith(
        (
            "can you ",
            "can u ",
            "could you ",
            "could u ",
            "would you ",
            "will you ",
            "please ",
            "gather ",
            "collect ",
            "get ",
            "chop ",
            "cut ",
            "build ",
            "make ",
            "craft ",
        )
    )


async def fast_task_intent(text):
    normalized = _normalize_request_text(text)
    if normalized in {"make it", "craft it", "get it"}:
        user_goals = [
            goal for goal in store.list_goals("active", 20)
            if goal["source"] == "user"
        ]
        return max(user_goals, key=lambda goal: int(goal["id"]), default=None)
    if not _looks_like_direct_request(text):
        return None

    if (
        re.search(r"\b(?:make|craft|get|obtain)\s+(?:an?\s+)?(?:wooden|stone|iron|golden|diamond|netherite)?\s*axe\b", normalized)
        and "pickaxe" not in normalized
    ):
        return _ensure_user_goal("Obtain an axe", text, 9)

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

    if (
        "crafting table" in normalized
        and any(word in normalized for word in ("make", "craft", "get", "build"))
    ):
        return _ensure_user_goal(
            "Obtain a crafting table",
            text,
            9,
        )

    return None

def _inventory_summary(runtime):
    inventory = ((runtime or {}).get("awareness") or {}).get("inventory") or []
    counts = {}
    for entry in inventory:
        item = str(entry.get("item") or "")
        counts[item] = counts.get(item, 0) + int(entry.get("count", 0) or 0)
    return inventory, counts


def _inventory_fact_reply(text, runtime):
    normalized = str(text or "").lower().strip().rstrip(".!?")
    inventory, counts = _inventory_summary(runtime)

    log_count = sum(
        count for item, count in counts.items()
        if item.startswith("minecraft:") and item.endswith("_log")
    )
    plank_count = sum(
        count for item, count in counts.items()
        if item.startswith("minecraft:") and item.endswith("_planks")
    )
    table_count = counts.get("minecraft:crafting_table", 0)

    asks_have = any(
        phrase in normalized
        for phrase in (
            "do you have",
            "do u have",
            "you do have",
            "u do have",
            "you have",
            "u have",
            "got any",
            "got some",
            "you got",
            "u got",
            "do you got",
            "do u got",
            "dont u",
            "don't you",
        )
    )

    if asks_have and "wood" in normalized:
        if log_count or plank_count:
            parts = []
            if log_count:
                parts.append(f"{log_count} logs")
            if plank_count:
                parts.append(f"{plank_count} planks")
            return "yea, i've got " + " and ".join(parts)
        return "nah, no logs or planks rn"

    if asks_have and ("log" in normalized or "logs" in normalized):
        return f"yea, {log_count} logs" if log_count else "nah, no logs rn"

    if asks_have and "crafting table" in normalized:
        return (
            f"yea, i've got {table_count}"
            if table_count
            else "nah, no crafting table rn"
        )

    return None


def _simple_chat_reply(text):
    normalized = text.lower().strip().rstrip(".!?")
    if normalized in {"hey", "hi", "hello", "yo", "sup", "hey chat", "hi chat"}:
        return "hey"
    if any(
        phrase in normalized
        for phrase in (
            "how are you",
            "how you doing",
            "how u doing",
            "how are u",
        )
    ):
        return "good lol, you?"
    if normalized in {"thanks", "thank you", "thx", "ty"}:
        return "np"
    if normalized in {
        "whats up",
        "what's up",
        "wassup",
        "wats up",
        "wyd",
        "what you doing",
        "what u doing",
    }:
        return _current_activity_text()
    return None


def _feedback_reply(text):
    normalized = text.lower().strip().rstrip(".!?")

    negative = (
        "you didnt ",
        "you didn't ",
        "you did not ",
        "you havent ",
        "you haven't ",
        "you have not ",
    )
    if normalized.startswith(negative):
        return "yea, you're right", True

    if any(
        phrase in normalized
        for phrase in (
            "you are just ",
            "you're just ",
            "ur just ",
            "not actually ",
            "there is no ",
            "there are no ",
            "not an issue",
            "isn't an issue",
            "is not an issue",
            "no ur not",
            "no you're not",
            "no you are not",
            "thats wrong",
            "that's wrong",
            "right tool",
            "correct tool",
            "should use ",
        )
    ):
        return "got it", True

    if any(
        phrase in normalized
        for phrase in (
            "you are close enough",
            "you're close enough",
            "ur close enough",
            "close enough",
        )
    ):
        return "got it", True

    if (
        any(word in normalized for word in ("tree", "trees", "log", "logs"))
        and any(
            phrase in normalized
            for phrase in (
                "near you",
                "nearby",
                "right near",
                "around you",
                "above you",
                "behind you",
                "in front of you",
                "standing near",
            )
        )
    ):
        return "oh nice, got it", True

    affirmative = normalized.startswith(
        ("yes", "yeah", "yep", "yea", "correct", "right")
    )
    if affirmative and any(
        word in normalized for word in ("there", "near", "tree", "trees", "log", "logs")
    ):
        return "got it", True

    return None


def _looks_like_action_request(text):
    normalized = _normalize_request_text(text)
    if any(
        phrase in normalized
        for phrase in ("explain", "tell me how", "how do", "how can", "what is", "why ")
    ):
        return False

    verbs = (
        "place",
        "put",
        "mine",
        "break",
        "craft",
        "gather",
        "collect",
        "build",
        "make",
        "chop",
        "cut",
        "attack",
        "pick up",
        "move",
        "go ",
        "look at",
        "equip",
    )
    if any(normalized.startswith(verb) for verb in verbs):
        return True
    if normalized.startswith(("can you ", "can u ", "please ")):
        return any(verb in normalized for verb in verbs)
    return bool(re.search(
        r"\b(?:u|you)\s+(?:need|should)\s+to\s+(?:place|put|mine|break|craft|gather|collect|build|make|chop|cut|move|equip)\b",
        normalized,
    ))


async def fast_chat_reflex(text, runtime=None):
    normalized = _normalize_request_text(text)
    if (
        ("didnt say follow" in normalized or "didn't say follow" in normalized)
        and "said come" in normalized
    ):
        normalized = "come to me"

    goal = await fast_task_intent(text)
    if goal is not None:
        if str(goal.get("title", "")).lower() == "obtain a crafting table":
            _, counts = _inventory_summary(runtime)
            table_count = counts.get("minecraft:crafting_table", 0)
            if table_count > 0:
                store.update_goal(goal["id"], status="completed")
                return {
                    "handled": True,
                    "reply": f"already got {table_count} crafting table"
                    + ("s" if table_count != 1 else ""),
                    "background": False,
                }
        return {
            "handled": True,
            "reply": "yep, on it",
            "background": True,
        }

    try:
        if "look up" in normalized:
            state = await asyncio.to_thread(_get, "/state")
            result = await asyncio.to_thread(
                _post,
                "/look-at",
                {
                    "x": int(round(state.get("x", 0))),
                    "y": int(round(state.get("y", 0) + 8)),
                    "z": int(round(state.get("z", 0))),
                },
            )
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to look up: {result}",
            )
            return {"handled": True, "reply": "looking", "background": False}

        if "look down" in normalized:
            state = await asyncio.to_thread(_get, "/state")
            result = await asyncio.to_thread(
                _post,
                "/look-at",
                {
                    "x": int(round(state.get("x", 0))),
                    "y": int(round(state.get("y", 0) - 6)),
                    "z": int(round(state.get("z", 0))),
                },
            )
            store.record_event(
                "motor_reflex",
                f"Alik asked Chat to look down: {result}",
            )
            return {"handled": True, "reply": "looking", "background": False}

        if (
            normalized == "come"
            or "come to me" in normalized
            or normalized.startswith("come here")
            or normalized.startswith("can you come here")
            or normalized.startswith("can u come here")
            or normalized.startswith("can you come to me")
            or normalized.startswith("can u come to me")
        ):
            store.cancel_tasks()
            state = await asyncio.to_thread(_get, "/state")
            owner = state.get("owner") or {}
            if float(owner.get("distance", 999) or 999) <= 2.3:
                return {"handled": True, "reply": "i'm here", "background": False}
            if not all(owner.get(axis) is not None for axis in ("x", "y", "z")):
                return {"handled": True, "reply": "can't locate you", "background": False}
            result = await asyncio.to_thread(_post, "/move-to", {
                "x": owner["x"], "y": owner["y"], "z": owner["z"],
                "stop_distance": 2.0,
            })
            accepted = isinstance(result, dict) and result.get("ok") is not False
            if runtime is not None:
                runtime["last_move_command"] = {
                    "at": time.monotonic(), "accepted": accepted, "kind": "approach_once"
                }
                if accepted:
                    runtime["manual_control_until"] = time.monotonic() + 20.0
            store.record_event("motor_reflex", f"One-time approach result: {result}")
            return {"handled": True, "reply": "coming" if accepted else "couldn't move", "background": False}

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

    feedback = _feedback_reply(text)
    if feedback is not None:
        reply, background = feedback
        store.record_event(
            "user_feedback",
            f"Alik said: {text}",
        )
        normalized_feedback = text.lower()
        if any(
            phrase in normalized_feedback
            for phrase in (
                "right tool",
                "correct tool",
                "should use ",
                "use an axe",
                "use a ",
            )
        ):
            safe_key = re.sub(r"[^a-z0-9]+", ".", normalized_feedback).strip(".")[:90]
            store.remember(
                "lesson",
                f"user_hint.{safe_key or 'correction'}",
                f"Alik taught/corrected: {text}",
                8,
            )
        return {
            "handled": True,
            "reply": reply,
            "background": background,
        }

    if _looks_like_action_request(text):
        if runtime is not None:
            runtime["user_subgoal"] = {"at": time.monotonic(), "text": text}
        store.record_event(
            "user_instruction",
            f"Alik asked: {text}",
        )
        return {
            "handled": True,
            "reply": "yep, on it",
            "background": True,
        }

    return None


def _current_activity_text(runtime=None):
    awareness = (runtime or {}).get("awareness") or {}
    state = awareness.get("state") or {}
    if state.get("jobActive"):
        job = str(state.get("jobType") or "task").lower()
        reason = str(state.get("jobReason") or "")
        if job == "follow":
            return "following you" if reason != "holding" else "staying near you"
        return _format_ingame_reply(f"{job} rn, {reason or 'in progress'}")
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
        return _format_ingame_reply(f"planning {goal['title'].lower()}; haven't finished it")
    return "just looking around rn"


def _instant_failure_explanation(text):
    normalized = text.lower().strip().rstrip(".!?")
    if not (
        "why" in normalized
        and any(
            phrase in normalized
            for phrase in ("cant ", "can't ", "cannot ", "couldnt ", "couldn't ")
        )
    ):
        return None

    trials = store.recent_trials(6)
    failed = next(
        (trial for trial in reversed(trials) if not trial.get("success")),
        None,
    )
    if failed is None:
        return None

    try:
        outcome = json.loads(failed.get("outcome") or "{}")
        result = outcome.get("result") or {}
        reason = str(result.get("reason") or result.get("error") or "").strip()
    except Exception:
        reason = ""

    lowered = reason.lower()
    if "repath_pending" in lowered:
        return "pathing's stuck trying to get into reach"
    if lowered == "moving" or "moving" in lowered:
        return "still getting into reach, haven't mined it yet"
    if "invalid_mining_target" in lowered:
        return "that target wasn't actually mineable"
    if "line" in lowered and "sight" in lowered:
        return "can't get a clear line on it yet"
    if "reach" in lowered:
        return "can't get into reach yet"
    if reason:
        return _format_ingame_reply(f"last try failed: {reason}")
    return "last mining attempt failed; trying another approach"


def _is_learning_status_question(text):
    normalized = text.lower().strip().rstrip(".!?")
    return any(
        phrase in normalized
        for phrase in (
            "did u finish learning",
            "did you finish learning",
            "are u done learning",
            "are you done learning",
            "did u learn it",
            "did you learn it",
        )
    )


def _learning_status_text():
    requests_ = store.list_learning(6)
    recent = requests_[0] if requests_ else None
    trials = store.recent_trials(4)

    if recent and recent.get("status") in {"queued", "running"}:
        return "still learning it"
    if recent and recent.get("status") == "completed":
        if trials:
            return "got a hint, still testing it"
        return "got the hint, haven't tested it yet"
    if trials:
        return "still testing it"
    return "haven't learned it yet"


def _is_movement_feedback(text):
    normalized = _normalize_request_text(text)
    return (
        bool(re.search(r"\b(?:u|you)\s+(?:didnt|didn't|did not|havent|haven't)\s+(?:\w+\s+){0,3}mov\w*", normalized))
        or normalized in {"stuck on what", "why are you stuck", "why are u stuck"}
    )


def _motion_evidence_reply(runtime):
    state = ((runtime or {}).get("awareness") or {}).get("state") or {}
    move = (runtime or {}).get("last_move_command") or {}
    reason = str(state.get("jobReason") or "")
    job = str(state.get("jobType") or "")
    if state.get("jobActive") and job in {"FOLLOW", "MOVE"}:
        if reason == "holding":
            return "i'm holding near you, not moving"
        return "movement is active; haven't confirmed arrival"
    if move and not move.get("accepted"):
        return "my movement command was rejected"
    if move and move.get("accepted"):
        return "you're right, movement wasn't confirmed"
    return "you're right, no movement attempt recorded"


def _ground_chat_reply(answer, runtime):
    """Strip unsupported physical claims, not conversational personality."""
    answer = str(answer or "")
    # No recipe lookup was performed by a chat-only turn. Never fabricate
    # quantitative requirements from the agent's inventory counts.
    if re.search(
        r"\b(?:need|requires?|takes?)\s+\d+[^.!?]{0,100}\b(?:logs?|sticks?|planks?)\b",
        answer, re.I,
    ):
        return "haven't checked the recipe yet"
    attempted_movement = bool(re.search(
        r"\bi\s+(?:tried\s+(?:to\s+)?mov\w*|moved|walked)\b", answer, re.I,
    ))
    recent = (runtime or {}).get("last_move_command") or {}
    if attempted_movement and not recent:
        return "haven't confirmed any movement yet"
    return answer


def _is_activity_question(text):
    normalized = _normalize_request_text(text)
    return normalized in {
        "what are you doing",
        "what are u doing",
        "what're you doing",
        "what r u doing",
        "what are you up to",
        "what are u up to",
        "what is your current task",
        "what's your current task",
        "what is next on your task",
    }


def _preempt_background_reasoning(runtime):
    task = runtime.get("reasoning_task")
    source = runtime.get("reasoning_source")
    # Host-planned PRACTICE/COMMAND turns are intentionally serialized. A
    # blocking local-model HTTP request cannot actually be cancelled once
    # dispatched; pretending otherwise creates overlapping Qwen generations.
    if (
        task is not None
        and not task.done()
        and source in {"autonomy", "event", "task", "learning"}
        and not runtime.get("physical_action_active", False)
    ):
        task.cancel()
        store.record_event(
            "reasoning_preempted",
            f"Background {source} reasoning was interrupted by Alik.",
        )
        return True
    return False


async def _background_player_chat(chat_agent, text, runtime):
    try:
        await run_turn(
            chat_agent,
            text,
            source="minecraft",
            reply_in_game=True,
            runtime=runtime,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log_exception(
            "chat",
            "background_chat_error",
            error,
            message=text,
        )
        await _send_ingame(
            "something broke, gimme a sec",
            reason="background_chat_error",
        )


def _spawn_player_chat(chat_agent, text, runtime):
    task = asyncio.create_task(
        _background_player_chat(chat_agent, text, runtime)
    )
    runtime["chat_tasks"].add(task)

    def _done(finished):
        runtime["chat_tasks"].discard(finished)

    task.add_done_callback(_done)


async def poll_minecraft_chat(input_queue, runtime, chat_agent):
    while True:
        try:
            payload = await asyncio.to_thread(_get, "/chat-inbox")
            for message in payload.get("messages", []):
                text = str(message.get("text", "")).strip()
                if text:
                    runtime["last_user_activity"] = time.monotonic()
                    log_event("chat", "user_message", message=text)
                    try:
                        await update_awareness(runtime)
                    except Exception:
                        pass
                    _preempt_background_reasoning(runtime)

                    failure_answer = _instant_failure_explanation(text)
                    if failure_answer is not None:
                        await _send_ingame(
                            failure_answer,
                            reason="instant_failure_explanation",
                        )
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [failure status]: {failure_answer}")
                        store.record_episode(text, failure_answer)
                        log_event(
                            "chat",
                            "instant_reply",
                            message=text,
                            reply=failure_answer,
                        )
                        continue

                    if _is_movement_feedback(text):
                        answer = _motion_evidence_reply(runtime)
                        await _send_ingame(answer, reason="instant_movement_evidence")
                        store.record_episode(text, answer)
                        log_event("chat", "instant_reply", message=text, reply=answer)
                        continue

                    if _is_learning_status_question(text):
                        answer = _learning_status_text()
                        await _send_ingame(
                            answer,
                            reason="instant_learning_status",
                        )
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [learning status]: {answer}")
                        store.record_episode(text, answer)
                        log_event("chat", "instant_reply", message=text, reply=answer)
                        continue

                    if _is_activity_question(text):
                        answer = _current_activity_text()
                        await _send_ingame(
                            answer,
                            reason="instant_status",
                        )
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [instant status]: {answer}")
                        store.record_episode(text, answer)
                        log_event("chat", "instant_reply", message=text, reply=answer)
                        continue

                    inventory_reply = _inventory_fact_reply(text, runtime)
                    if inventory_reply is not None:
                        await _send_ingame(
                            inventory_reply,
                            reason="instant_inventory_fact",
                        )
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [inventory]: {inventory_reply}")
                        store.record_episode(text, inventory_reply)
                        log_event(
                            "chat",
                            "instant_reply",
                            message=text,
                            reply=inventory_reply,
                        )
                        continue

                    simple = _simple_chat_reply(text)
                    if simple is not None:
                        await _send_ingame(
                            simple,
                            reason="instant_chat",
                        )
                        print(f"\n[MINECRAFT] Alik: {text}")
                        print(f"\nAI [instant chat]: {simple}")
                        store.record_episode(text, simple)
                        log_event("chat", "instant_reply", message=text, reply=simple)
                        continue

                    reflex = await fast_chat_reflex(text, runtime)
                    if reflex and reflex.get("handled"):
                        reply = reflex.get("reply") or "yep"
                        await _send_ingame(
                            reply,
                            reason="instant_action_ack",
                        )
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
                                f"Alik said: {text}",
                            )
                        continue

                    # Qwen's fast chat lane typically answers within a second.
                    # Do not spam 'sec' before every normal message.
                    _spawn_player_chat(
                        chat_agent,
                        text,
                        runtime,
                    )
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
                (
                    f"Teacher #{request['id']} ({model}) hint for {request['topic']}: "
                    f"{_trim(lesson, 700)}"
                ),
            )
            print(
                f"[TEACHER] Stored lesson from {model} "
                f"({input_tokens} in / {output_tokens} out)."
            )
            # The lesson is persisted and will be injected into the next planner
            # turn. Do not waste a second LLM turn paraphrasing the teacher.
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
        # A queued/stale legacy task does not prove a task is executing.
        # Only the actual worker holding a task may suspend practice.
        task_active = runtime.get("active_task") is not None
        job_active = bool(state.get("jobActive"))
        if now - float(runtime.get("last_sensor_trace", 0.0) or 0.0) >= 15.0:
            runtime["last_sensor_trace"] = now
            log_event(
                "sensor", "autonomy_heartbeat",
                job_active=job_active,
                job_type=state.get("jobType"),
                job_reason=state.get("jobReason"),
                active_worker=runtime.get("active_task"),
                legacy_tasks=[
                    {"id": task.get("id"), "status": task.get("status"), "skill": task.get("skill")}
                    for task in active_tasks[:4]
                ],
                goals=[
                    {"id": goal.get("id"), "source": goal.get("source"), "title": goal.get("title")}
                    for goal in store.list_goals("active", 4)
                ],
                owner_distance=(state.get("owner") or {}).get("distance"),
                manual_control=now < float(runtime.get("manual_control_until", 0.0) or 0.0),
                autonomy_pending=bool(runtime.get("autonomy_pending")),
            )

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

        if _autonomy_job_blocks_practice(
            state, runtime, bool(meaningful_goals)
        ):
            continue

        if (
            now < float(runtime.get("post_goal_pause_until", 0.0) or 0.0)
            and not any(goal.get("source") == "user" for goal in meaningful_goals)
        ):
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
        # Even curiosity must use the host-executed experiment lane.
        # A free-form autonomy text turn cannot perform physical actions.
        source = "practice"
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


async def resilient_autonomy_sensor(input_queue, runtime):
    """Restart sensing after an unexpected exception instead of going silent."""
    while True:
        try:
            await autonomy_sensor(input_queue, runtime)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_exception("sensor", "sensor_crashed", error)
            runtime["autonomy_pending"] = False
            await asyncio.sleep(2.0)


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
        "physical_action_active": False,
        "model_label": None,
        "chat_tasks": set(),
        "notices": {},
        "failure_streaks": {},
        "post_goal_pause_until": 0.0,
    }

    model, model_label = choose_model()
    runtime["model_label"] = model_label
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
        chat_agent = Agent(
            name="Chat",
            model=model,
            instructions=PLAYER_CHAT_INSTRUCTIONS,
        )
        practice_planner = Agent(
            name="Chat",
            model=model,
            instructions=PRACTICE_PLANNER_INSTRUCTIONS,
        )

        tasks = [
            asyncio.create_task(
                poll_minecraft_chat(input_queue, runtime, chat_agent)
            ),
            asyncio.create_task(console_input(input_queue, runtime)),
            asyncio.create_task(resilient_autonomy_sensor(input_queue, runtime)),
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
                    print(f"\n[MINECRAFT] Alik: {message}")

                try:
                    reply_in_game = source == "minecraft"
                    if source == "task":
                        reply_in_game = "completed" in message or "failed" in message

                    if source in {"practice", "command"}:
                        directive = message if source == "command" else None
                        reasoning = asyncio.create_task(
                            run_planned_action(
                                practice_planner,
                                runtime,
                                directive=directive,
                                source=source,
                            )
                        )
                    else:
                        turn_agent = (
                            chat_agent
                            if source in {"minecraft", "console"}
                            else agent
                        )
                        reasoning = asyncio.create_task(
                            run_turn(
                                turn_agent,
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
                        if source in {"autonomy", "event", "task", "learning", "practice", "command"}:
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
                        await _send_ingame(
                            "something broke, gimme a sec",
                            reason="agent_error",
                        )
                finally:
                    if source in {"autonomy", "event", "task", "learning", "practice"}:
                        runtime["autonomy_pending"] = False
        finally:
            log_event("agent", "session_stop")
            for task in tasks:
                task.cancel()
            for task in list(runtime.get("chat_tasks", ())):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if runtime.get("chat_tasks"):
                await asyncio.gather(
                    *list(runtime["chat_tasks"]),
                    return_exceptions=True,
                )


if __name__ == "__main__":
    asyncio.run(main())
