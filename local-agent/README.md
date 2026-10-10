# Chat Companion local brain

## Autonomous physical training lab v1

The first training laboratory lives in local-agent/training_lab.py. It tests
five progressively useful physical capabilities in the ACTUAL loaded Minecraft
world: observe nearby blocks, turn to an observed block, walk to a server-checked
standable location, mine an observed natural block, and collect an existing
dropped item. No fake game success, item grants, teleports, world resets, or
pre-programmed gameplay solutions. Qwen proposes an action; the existing body
executor actually performs it. The host separately evaluates outcome evidence.

**Windows, single command:** With Ollama and your Minecraft world open, stop
the normal Chat brain (close run.bat) and double-click train.bat in the brain
folder. It now asks **How many training cycles? (1-100, Enter for 30)**.
Choose once; the training launcher runs up to that many full curriculum
rounds, with a maximum 120-minute session rather than stopping at two cycles.
It may still stop early if three consecutive rounds have no executed actions,
a physical job remains pending, or the session time runs out. The normal brain
and trainer both use an OS lock to prevent concurrent body control. Minecraft
itself does NOT need to be restarted.

When starting directly with Python rather than train.bat, the default is
**12 curriculum rounds**, limited to **20 minutes**
and four proposals per exercise. The runner stops early if three consecutive
rounds cannot execute even one action, preventing endless parser loops.
You start it once, not after every curriculum round.

You can adjust the session:

    python training_lab.py --rounds 12 --steps 4 --minutes 20 --seed 7

If a report says `physical_actions: 0` and `rejected_proposals` is high,
no Minecraft training took place. Keep the report for debugging rather than
assuming Chat learned a skill. The v2 action parser accepts Qwen's common
nested `scan_blocks`, `look_at`, and `move_to` shapes as well as
`target`/`target_pos` coordinate lists. Rejected format-only attempts are
recorded as diagnostics, not failed gameplay memories.

Requirements: Python dependencies from requirements.txt; local Ollama reachable
at 127.0.0.1:11434 and the loaded NeoForge Chat bridge at 127.0.0.1:8765.
The default local model is qwen3:8b, overridable using COMPANION_LOCAL_MODEL.
The training runner forces the cloud teacher OFF and never uses an OpenAI model.

Training changes natural terrain. Use a dedicated spare survival world/area.
It does not create a new world or prepare ideal test scenarios automatically.
A trial is marked unavailable when there is no real suitable block, standable
location or dropped item. Those are not counted as successes. The collection
stage cannot run until a dropped item actually exists.

**Automatic graduation and next stage:** Every report includes a `graduation`
section calculated directly from `data/companion_memory.sqlite3`. For each of
observe, orient, navigate, mine and collect, the latest ten genuine resolved
trials must include at least nine verified successes across at least three
successful sites separated by eight blocks. Rejected JSON-only proposals,
unavailable scenarios and unresolved body actions are NOT graduation successes.
Readiness recalculates each round, including after restarting the brain.
A later loss of reliability returns the curriculum to Stage 1 automatically.

When all five pass, Chat unlocks an additional **gather_wood** scenario: it
must choose and physically execute the appropriate sequence to mine a natural
world-observed tree log and acquire that wood in its inventory. Both a matching
real mining receipt and increased matching inventory are required. Stage 2
does not automatically imply mastery; future crafting/building stages are not
implemented. Stage 1 continues running, so competence can be reassessed.

**Idle behavior:** The NeoForge and Fabric companion no longer performs
unrequested random head turns or player-gazing; deliberate `look_at` and
movement actions still work. The idle change is in the mod JAR, so updating
that part requires installing a verified build and restarting Minecraft.
Python curriculum changes alone do not require a Minecraft restart.

Reports are saved under data/training_reports/training-*.json, including each
attempt, the validated hypothesis, real outcome evidence, timing, counts,
failure reasons, and a basic reward. Observed episodes and skill trials are
stored in the SAME existing data/companion_memory.sqlite3 for reuse across
sessions, without erasing or replacing old memory. Skills are eligible for
promotion only after multiple successful trials at different positions.

Training waits for live physical jobs to settle and only stops if a physical job remains pending so it cannot
start another one on top of it. Tests with fake worlds exist ONLY for unit and
regression checks; they must not be confused with actual verified Minecraft
practice. A training run is not a guarantee of 90% task competence, neural-weight
fine-tuning, headless gameplay, or automatically provisioned/reset scenarios.
Those remain future milestones. Only Minecraft observations count as real
training successes.


This folder contains the local MCP brain used by the Minecraft companion.

## Negative search evidence (v16)

An empty block scan now records its query, origin, dimension and zero-result
outcome in SQLite. It is an observation, not a successful learned skill or
proof of gathering. The planner receives this evidence explicitly.

For two minutes, repeating that query inside the already-searched cube is
rejected, including smaller radii and different result limits. The local model
gets one chance to choose a different action or wider search. If it repeats
the exhausted search, the host pauses for 30 seconds and gives a rate-limited,
truthful stall message. Existing user-goal teacher escalation still applies.
No fallback mining target or movement coordinate is invented by this guard.

Movement beyond the searched area, a different dimension, expiry, or a verified
mining/placement change permits a fresh search. Failed requests do not establish
resource absence. Spatial guards are session-local; stored observations remain
historical evidence after restart, since another world could use the same
coordinates. This addresses repeated empty scans; it does not establish general
autonomous problem-solving or guarantee that the local model's next plan works.

To update from v15, stop the brain, replace `minecraft_agent.py` and
`practice_engine.py` together, and restart it. This change needs no new mod JAR.
Keep `data/` and the existing SQLite database.

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
