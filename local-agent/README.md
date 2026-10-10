# Chat Companion local brain

This folder contains the local MCP brain used by the Minecraft companion.

## Brain model selection

The agent is **local-only by default**.

By default:

1. It checks for an OpenAI-compatible local endpoint at:
   `http://127.0.0.1:11434/v1`
2. If one is available, it uses a local model from that endpoint.
3. If no local endpoint is available, the brain refuses to start rather than silently using API tokens.
4. Cloud use is opt-in with `COMPANION_MODEL_PROVIDER=openai`.
5. The agent refuses model names containing `sol` and uses Luna instead.

### Force local inference

PowerShell:

```powershell
$env:COMPANION_MODEL_PROVIDER = "local"
$env:COMPANION_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
$env:COMPANION_LOCAL_MODEL = "YOUR_TOOL_CAPABLE_LOCAL_MODEL"
.\.venv\Scripts\python.exe minecraft_agent.py
```

The local model should support OpenAI-style chat completions and function/tool calls.

### Optional automatic local-or-Luna mode

This explicitly allows a cloud fallback:

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


## Self-service Minecraft recipe knowledge (v14)

The NeoForge localhost bridge exposes `POST /recipe-knowledge` to query the
**loaded server RecipeManager**, including datapack and mod-added shaped /
shapeless recipes. The MCP tool `recipe_knowledge(output="minecraft:torch")`
returns actual recipe IDs, dimensions, ingredient alternatives and outputs.
It is an observation API, not an item grant or recipe bypass.

`knowledge_engine.py` uses these recipes to plan intermediate materials from
the companion's actual inventory. It re-queries the world after each physical
step. Successful craft results are recorded by the existing practice engine.
This does not automatically implement furnaces, machines, trading, combat,
building or general game navigation: those need their own supported primitives
and planning/verification loop. Dynamic/special recipes without static grids
are intentionally not reported as simple crafting recipes.

**Installation (v14):** Replace the old NeoForge companion JAR with the
matching new JAR and restart Minecraft **once** because a new HTTP route was
added. Stop `run.bat`, update `minecraft_agent.py`, `mcp_server.py` and
`knowledge_engine.py` in `C:\\MinecraftAgent\\brain\\`, then restart
`run.bat`. Leave `data/` and the SQLite database untouched.

For cloud teaching, `COMPANION_CLOUD_TEACHER=on` does **not** itself authorize
API access. Set a valid `OPENAI_API_KEY` securely in your environment and
select a model available to your API project with `COMPANION_TEACHER_MODEL`.
The terminal reports missing keys, and cloud responses/errors appear with
`[TEACHER]`. Repeated unproductive goals can queue one short teacher lesson;
per-topic cooldown prevents constant requests.

To check the key without printing it on Windows CMD:
```bat
if defined OPENAI_API_KEY (echo Teacher API key found) else (echo Teacher API key MISSING)
```

## Hybrid local brain + OpenAI teacher

Normal conversation, planning, awareness, goals and Minecraft behavior use the
local model by default.

OpenAI is reserved for rare learning/escalation events. The local brain can
queue a learning request with the MCP `learn` tool, but it cannot directly
spend cloud tokens. The host processes the queue under a per-topic cooldown.

Automatic escalation currently occurs after repeated failures of the same local
skill. A successful teacher answer is saved into the local SQLite database as a
high-importance skill memory and is available to future local reasoning.

Default teacher model:

```text
gpt-6-luna
```

Sol models are refused for both the normal brain and the teacher.

Environment controls:

```powershell
# Normal brain remains local.
$env:COMPANION_MODEL_PROVIDER = "local"

# Enable or disable the cloud teacher.
$env:COMPANION_CLOUD_TEACHER = "on"   # default
# $env:COMPANION_CLOUD_TEACHER = "off"

# Optional teacher model override. Sol names are rejected.
$env:COMPANION_TEACHER_MODEL = "gpt-6-luna"
```

The teacher requires `OPENAI_API_KEY`. If the key is absent, the local brain
and local skills continue to work; only teacher requests fail.

Teacher calls use a 30-minute cooldown per topic by default. They return a
compact reusable lesson rather than controlling the Minecraft body directly.


## Self-learning skill system

High-level Minecraft goals are no longer automatically mapped to the legacy
`gather_logs`, `make_stone_pickaxe`, or `build_basic_house` Python routines.
Those routines remain fallback scaffolding only.

The normal path is now:

```text
goal
 -> search learned skill memory
 -> form a local hypothesis
 -> execute a short experiment with primitive MCP actions
 -> observe the resulting world/inventory state
 -> record the trial and verified after-state
 -> vary the hypothesis after failure
 -> promote repeated successes into a learned skill
 -> compose learned skills through parent/subskill links
 -> reuse and update confidence on future trials
```

Persistent SQLite tables now include:

- `learned_skills`
- `skill_trials`
- `skill_edges`

A learned skill stores a natural-language procedure over safe MCP primitives,
not executable code. Confidence is based on observed successes and failures.

The MCP `skill_memory` tool supports:

- `search`
- `save`
- `trial`
- `history`
- `list`
- `link`
- `graph`

Two successful experiments can promote a new low-confidence procedure. Several
distinct failed hypotheses can queue the cloud teacher. Teacher output is a hint
to test, not trusted knowledge by itself.

User chat preempts background PRACTICE reasoning. Physical Minecraft jobs are
allowed to finish their current action before another practice turn starts.


## Shareable recent-action diagnostics

Each brain startup creates a fresh diagnostic log at:

```text
data/recent_actions.jsonl
```

The previous run is preserved at:

```text
data/recent_actions.previous.jsonl
```

Send `recent_actions.jsonl` when a session behaves strangely. It is structured
JSON Lines and records meaningful behavior rather than every sensor poll:

- user messages and agent replies
- queued autonomy/practice events
- MCP tool calls and results
- navigation, mining, placing, combat and crafting
- compact state/inventory snapshots after physical MCP actions
- learned-skill experiments and memory-tool calls
- legacy fallback-skill HTTP actions
- teacher activity, task failures and timing/errors

Fields whose names look like API keys, tokens, passwords, authorization headers
or secrets are automatically replaced with `<redacted>`.

The logger is best-effort: a logging failure never stops the companion.
Set `COMPANION_ACTION_LOG` to override the current-log path.
