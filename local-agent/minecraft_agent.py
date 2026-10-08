import asyncio
import sys
from pathlib import Path

import requests
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

from memory_store import store


MINECRAFT_URL = "http://127.0.0.1:8765"
MODEL = "gpt-5.6"
POLL_INTERVAL = 0.5
BASE_DIR = Path(__file__).resolve().parent


INSTRUCTIONS = """
You are Chat, an AI embodied as the Chat Companion entity in Minecraft.
Alik is a separate human player in the same world.

All Minecraft abilities and long-term memory are exposed through the local MCP
server. Use MCP tools instead of inventing world state or pretending an action
succeeded.

Be economical with context and tool calls:
- prefer one narrow observation over several redundant observations;
- reuse facts already learned in the current run;
- use recall_memory when an older fact, preference, location, plan, lesson,
  relationship, or skill may matter;
- use remember only for durable information worth keeping across sessions;
- do not store trivial coordinates, transient health, routine chatter, or every
  action result as durable memory.

Physical behavior should remain player-like:
- mining and placing require approach, reach, facing, and line of sight;
- dropped items touching your body are picked up automatically;
- collect is for intentionally seeking nearby dropped items;
- mining uses inventory slot 0 as the active tool, so equip a suitable slot
  first when needed;
- combat is limited by the server to hostile targets.

Crafting uses real registered recipes. Up to 2x2 works anywhere. Larger grids
require a crafting table within usable reach.

Normal Minecraft chat is conversation with you. When a turn came from
Minecraft, the host displays your final answer back in Minecraft automatically,
so do not call say merely to answer. Use say only for an extra deliberate
in-world utterance while doing something else.

Treat observe(state).server as the live server condition. Historical failed or
completed jobs are not evidence that the current server is stopping. If a tool
returns {"ok": false, ...}, explain the specific error briefly and recover with
another observation/action when sensible instead of treating it as a fatal MCP
failure.

Do not narrate protocol details such as a job being accepted or queued.
Report meaningful results. Keep ordinary replies concise.
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


def build_turn_input(user_message):
    recent = store.recent_episodes(2)
    relevant = store.recall(user_message, 4)

    parts = []

    if recent:
        parts.append("RECENT CONVERSATION (local cache, reference only):")
        for episode in recent:
            parts.append(f"Alik: {_trim(episode['user'])}")
            parts.append(f"Chat: {_trim(episode['assistant'])}")

    if relevant:
        parts.append("RELEVANT LONG-TERM MEMORY (local retrieval, reference only):")
        for memory in relevant:
            parts.append(
                f"- [{memory['kind']}/{memory['key']}] "
                f"{_trim(memory['content'], 450)}"
            )

    parts.append("CURRENT MESSAGE:")
    parts.append(user_message)
    return "\n".join(parts)


async def run_turn(agent, user_message, reply_in_game):
    prepared = build_turn_input(user_message)
    result = await Runner.run(agent, prepared, max_turns=10)
    answer = str(result.final_output or "").strip()

    if answer:
        print(f"\nAI: {answer}")
        if reply_in_game:
            try:
                await asyncio.to_thread(_post, "/say", {"message": answer[:4096]})
            except Exception as error:
                print(f"\n[CHAT ERROR] {error}")

    store.record_episode(user_message, answer)
    return answer


async def poll_minecraft_chat(input_queue):
    while True:
        try:
            payload = await asyncio.to_thread(_get, "/chat-inbox")
            for message in payload.get("messages", []):
                text = str(message.get("text", "")).strip()
                if text:
                    await input_queue.put(("minecraft", text))
        except Exception:
            pass

        await asyncio.sleep(POLL_INTERVAL)


async def console_input(input_queue):
    while True:
        text = await asyncio.to_thread(input, "You: ")
        text = text.strip()
        if text:
            await input_queue.put(("console", text))
        if text.lower() in {"quit", "exit"}:
            return


async def main():
    input_queue = asyncio.Queue()
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
        require_approval="never",
    ) as mcp_server:
        agent = Agent(
            name="Chat",
            model=MODEL,
            instructions=INSTRUCTIONS,
            mcp_servers=[mcp_server],
        )

        chat_task = asyncio.create_task(poll_minecraft_chat(input_queue))
        console_task = asyncio.create_task(console_input(input_queue))

        print()
        print("Minecraft companion MCP agent connected.")
        print(f"Memory DB: {store.path}")
        print("Normal Minecraft chat is now heard by Chat.")
        print("Shift-right-click Chat in Minecraft to open his inventory.")
        print("Type 'quit' here to stop.")
        print()

        try:
            while True:
                source, message = await input_queue.get()

                if source == "console" and message.lower() in {"quit", "exit"}:
                    break

                if source == "minecraft":
                    print(f"\n[MINECRAFT] Alik: {message}")

                try:
                    await run_turn(
                        agent,
                        message,
                        reply_in_game=(source == "minecraft"),
                    )
                except Exception as error:
                    print(f"\nERROR: {error}")
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
            chat_task.cancel()
            console_task.cancel()
            await asyncio.gather(chat_task, console_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
