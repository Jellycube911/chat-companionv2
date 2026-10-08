# Chat Companion local brain

This folder contains the local MCP brain used by the Minecraft companion.

## Brain model selection

The agent is **local-first**.

By default:

1. It checks for an OpenAI-compatible local endpoint at:
   `http://127.0.0.1:11434/v1`
2. If one is available, it uses a local model from that endpoint.
3. If no local endpoint is available, it falls back to `gpt-5.6-luna`.
4. The agent refuses model names containing `sol` and falls back to Luna.

### Force local inference

PowerShell:

```powershell
$env:COMPANION_MODEL_PROVIDER = "local"
$env:COMPANION_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
$env:COMPANION_LOCAL_MODEL = "YOUR_TOOL_CAPABLE_LOCAL_MODEL"
.\.venv\Scripts\python.exe minecraft_agent.py
```

The local model should support OpenAI-style chat completions and function/tool calls.

### Automatic local-or-Luna mode

```powershell
$env:COMPANION_MODEL_PROVIDER = "auto"
.\.venv\Scripts\python.exe minecraft_agent.py
```

### Force the cheap OpenAI fallback

```powershell
$env:COMPANION_MODEL_PROVIDER = "openai"
$env:COMPANION_OPENAI_MODEL = "gpt-5.6-luna"
.\.venv\Scripts\python.exe minecraft_agent.py
```

## Persistent local brain state

Stored under:

```text
data/companion_memory.sqlite3
```

The database stores:

- durable memories
- conversation episodes
- persistent goals
- significant body/brain events
- persistent local skill tasks

Running tasks that were interrupted by a Python restart are returned to the queue.

## Local skills

The task executive currently has deterministic skills for:

- gathering logs
- making a stone pickaxe
- building a basic wooden house

These continue locally without an LLM call for each movement, mined block, craft, or placed block.

## Awareness

The host continuously maintains a compact current world model from:

- body state
- ray-based field-of-view vision
- nearby entities
- inventory
- active goals/tasks

This awareness is injected into brain turns without storing every sensor tick in the prompt.
