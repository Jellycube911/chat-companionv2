# Chat Companion replacement: engineering specification

**Specification revision:** 1.0
**Contract review date:** 7 October 2026
**Target:** Minecraft 1.21.1; Java 21; NeoForge first, Fabric second
**Status:** Implementation baseline; no implementation or runtime validation is claimed
**Input:** The supplied technical research report and replacement architecture

This is the archived stable-release target. See [the project README](../README.md) and [verification record](verification.md) for the implemented alpha and its limitations.

This specification defines a new source-controlled companion implementation. It converts the report into requirements, subsystem contracts, recovery rules and release gates. The acceptance suite is in [acceptance-tests.md](acceptance-tests.md); dependency-ordered delivery packages are in [implementation-backlog.csv](implementation-backlog.csv).

`MUST` identifies a release requirement. `SHOULD` identifies a recommended implementation choice whose alternative requires a recorded design decision. Defaults below are initial engineering settings, not performance measurements.

## 1. Product boundary and delivery scope

The companion is a server-owned Minecraft entity with local navigation and task execution. OpenAI selects goals, supplies conversation and requests allowlisted operations. A model response does not itself execute an operation or establish its success.

The NeoForge release MUST provide persistent addressed conversation, deterministic follow/stop commands, durable tool processing, bounded world actions, recovery, operational diagnostics and optional client speech. Fabric MUST implement the same observable contracts before it is advertised as supported. NeoForge internal builds do not wait for the Fabric port.

| Installation | Supported capability |
|---|---|
| Client plus integrated server | Full companion, subject to owner permissions and remote-processing consent |
| Compatible mod on client and dedicated server | Full companion; credentials stay on server or trusted gateway |
| Client only, remote server without this mod | Optional local chat/speech surface and cosmetics; no authoritative world-changing NPC |
| Dedicated server without compatible clients | Headless Agent and local jobs; only presentation explicitly compatible with unmodified clients |

Capability negotiation MUST determine which of these features is available. A custom entity cannot be promised to an unmodified client merely because the server has the mod. Server-only use MUST avoid spawning/sending custom entity data to clients that cannot decode it, unless a separately tested compatible presentation adapter exists.

Excluded from the initial release: binary patching of 0.19.x, microphone recording, arbitrary shell/command execution, unrestricted machine interactions, cross-dimensional following, automatic teleportation, permanent chunk tickets, cloud webhooks, embedded speech models and support for other Minecraft versions. Autonomous behavior is optional, off by default and constrained by configured capability and cost budgets.

### 1.1 Requirements

| ID | Normative requirement | Release evidence |
|---|---|---|
| R-01 | Addressed chat and `/chat say <text>` MUST use the same input router. Direct control commands MUST produce a local receipt and visible outcome. | Router and loader chat tests |
| R-02 | Every durably accepted input MUST have a unique identity and be processed, remain visibly queued, or reach an explicit terminal disposition. Identical text MUST remain separate inputs. Emergency control can apply before durability, but its receipt MUST distinguish applied from durably recorded. | Restart, saturation and lost-response tests |
| R-03 | Session recovery MUST reconcile the intended turn, current required actions and saved items. `idle` MUST NOT imply success. | Fake-service lifecycle and reconnect tests |
| R-04 | Each remote call key MUST have at most one execution admission. Result retries MUST reuse the recorded outcome. Uncertain execution MUST NOT automatically replay. | Duplicate-call, crash and acknowledgement tests |
| R-05 | World state MUST be read/mutated on the logical server through validated adapters; blocking remote or filesystem I/O MUST NOT occur on tick threads. | Thread assertions, GameTests and delayed-I/O tests |
| R-06 | Jobs MUST have stable IDs and observable states. Follow MUST continue holding/following until a defined termination; local work MUST continue during remote inference failure. | Navigation, restart and offline tests |
| R-07 | Local stop MUST fence queued and in-flight physical work by the next applicable tick. Remote cancellation MUST be tracked separately. | Delayed-dispatch and late-callback tests |
| R-08 | Final assistant output MUST be recoverable from saved items and deduplicated by item ID per recipient. Chat and audio MUST track separate acknowledgements. | Output loss and reconnect tests |
| R-09 | Speech MUST play on the owning client, respect audio controls and use stable utterance IDs. Playback completion MUST be reported only after the audio finishes. | Client audio and dedicated-server isolation tests |
| R-10 | Server-side authorization MUST validate every action at execution and throughout continuing jobs. Remote chat and remote speech MUST have separate consent controls. | Rejection, revocation, packet and privacy tests |
| R-11 | Both loader artifacts MUST isolate physical client classes and negotiate protocol capabilities. Shared core MUST not depend on mapped Minecraft classes. | Dedicated-server startup, artifact audits and adapter contracts |
| R-12 | Queues, observations, retries, workers and caches MUST be bounded. Diagnostics MUST distinguish AI state, execution, navigation and playback. | Load, retry-budget and status tests |
| R-13 | Storage, tool, instruction and network revisions MUST be explicit. Unsupported schemas MUST enter recovery without modifying state. | Corruption, migration and rollback tests |

The accompanying test plan is the acceptance authority for these requirements. Release claims require passing evidence on the target loader/version, not merely compilation or a successful model turn.

## 2. Ownership, modules and execution domains

Use a Gradle multi-project repository:

```text
chat-companion/
  core/                 # Plain Java: actors, transport DTOs, queues, ledger, recovery
  minecraft-common/     # Plain Java game contracts, jobs and immutable observations
  neoforge/             # Mapped Minecraft implementation, loader hooks and client adapters
  fabric/               # Separately compiled mapped implementation and loader hooks
  test-support/         # Fake Agents service, deterministic clock, crash harness
  integration-tests/    # Contract, loader launch and game-world tests
  documentation/
```

`minecraft-common` is a boundary module, not an assumption that NeoForge and Fabric compile identical mapped entity source. The loaders may use different mappings, development plugins and packaging rules. Each adapter owns entity registration, navigation integration, world interaction, networking, rendering and audio. A shared mapped source layer or Architectury MAY be introduced after a compile/package spike proves compatibility for both target builds; it is not required by this design.

Pin the 64-bit Java toolchain to 21 and compile with `--release 21`. Start from the official NeoForge 1.21.1 ModDevGradle MDK; use Fabric Loom with a compatible Fabric API for 1.21.1. Commit the wrapper, dependency versions, locking and verification metadata. Exact loader/plugin pins MUST be established in the foundation work package from a successful client/server build; this document does not invent an untested version combination. Produce separate NeoForge and remapped Fabric artifacts. Mixed-loader multiplayer is not a v1 support claim. [NeoForge setup](https://docs.neoforged.net/docs/1.21.1/gettingstarted/)

| Domain | Owns | Rules |
|---|---|---|
| Server main thread | Entities, world snapshots, inventories, active navigation and task ticks | No blocking HTTP, synthesis, journal flush or waiting on worker futures |
| Client main/audio domain | UI, Minecraft rendering/sound integration and playback acknowledgements | Decoder/synthesis workers hand off immutable data through the client scheduler |
| Per-session actor mailbox | Remote session state, input queue, ledger decisions and reconciliation | Serial logical execution; no live Minecraft objects and no direct off-thread world access |
| Bounded I/O pools | HTTP/SSE, storage, decoding and optional synthesis | Completion callbacks only enqueue mailbox/main-thread messages |

The actor MAY use a shared bounded executor with serial mailboxes; one operating-system thread per player is unnecessary. It MUST fence every callback by actor identity and server-runtime epoch. No HTTP completion handler may directly mutate the ledger, queue or job state.

Entity ticks and local jobs MUST keep running independently of the actor's remote state. The server constructs bounded immutable observations on its thread. Background code receives UUIDs, dimension IDs, positions and snapshots, never a `Level`, entity, block entity or inventory reference.

### 2.1 Port interfaces

```java
interface AgentsTransport {
    CompletionStage<SessionSnapshot> create(SessionCreateRequest request);
    CompletionStage<SessionSnapshot> retrieve(String sessionId);
    CompletionStage<EventAcceptance> submit(String sessionId, InputEnvelope input);
    EventSubscription subscribe(String sessionId, EventReceiver receiver);
    CompletionStage<ItemPage> items(String sessionId, ItemPageRequest page);
    CompletionStage<TurnSnapshot> turn(String sessionId, String turnId);
}

interface DurableStore {
    CompletionStage<DurableAck> append(JournalEvent event);
    CompletionStage<RecoveredState> load(StorageLocation location);
    CompletionStage<DurableAck> snapshot(StateSnapshot state);
}

interface GameExecutor {
    // Adapter schedules onto the current logical server; execution is fenced again there.
    CompletionStage<ObservedOutcome> execute(AdmittedAction action);
    CompletionStage<WorldEvidence> reconcile(UncertainAction action);
}

interface JobScheduler {
    JobReceipt start(ValidatedJobSpec job);  // Server thread only
    CancellationReceipt cancel(JobId id, ControlGeneration generation);
    JobSnapshot inspect(JobId id);
    void tick();                            // Server thread only
}
```

These are signature sketches, not SDK methods or drop-in Minecraft classes. Transport DTOs MUST preserve raw supported lifecycle data and discriminate unknown event/action types. Codec and contract tests MUST establish the actual request envelope before real API integration.

## 3. Identity, persistence and state ownership

### 3.1 Stable identities

| Record | Required fields |
|---|---|
| `SessionKey` | `playerUuid`, `serverContextId`, `worldId`, `companionUuid` |
| `SessionRecord` | Remote session/agent IDs, instruction/tool revisions, state, intended turn ID, last reconcile time |
| `InputRecord` | `messageId`, sequence, intent ID if any, source, priority, text, idempotency key, immutable payload hash, state |
| `ToolCallKey` | `sessionId`, `turnId`, `callId` |
| `ToolLedgerEntry` | Key, tool name, preserved arguments, canonical arguments hash, execution state, result-delivery state, intent ID, control generation, immutable result, evidence, timestamps |
| `JobRecord` | Stable job ID, owner/session context, type, configuration, control generation, state/substatus, reason, progress evidence |
| `DeliveryRecord` | `sessionId`, item ID, recipient UUID, chat state, speech state, last acknowledgement |
| `SpeechRecord` | Utterance ID, playback-attempt ID, source item ID, voice/provider/settings revision, synthesis state, playback state |
| `VersionRecord` | Mod version, storage schema, instruction revision, tool schema, network protocol |

The server creates/persists context identities before enabling remote processing. A world clone MUST not accidentally become a concurrent writer to the original session. Cloning or importing a world requires an explicit new-context operation or a verified exclusive move. Entity unload/reload MUST preserve companion UUID and job identity; it MUST not create another actor with simultaneous ownership of the same `SessionKey`.

`serverRuntimeEpoch` is unique to each server start. `controlGeneration` is a persisted monotonic generation for each companion. All admitted physical work and turn capabilities carry both. Owner stop and superseding explicit movement commands increment the generation before cancelling old work. A callback/action with an obsolete runtime epoch or generation MUST be discarded or recorded as stale without mutating the world.

### 3.2 Journal contract

Use versioned append-only journal segments plus atomic snapshots. A journal event MUST contain schema, monotonically increasing sequence, unique event ID, aggregate ID, event type, bounded payload and integrity checksum. A single persistence writer orders records for an actor/aggregate; snapshot compaction MUST retain unresolved calls, inputs, deliveries and migration history.

`DurableAck` means the record has been flushed according to the documented filesystem durability profile, not merely placed in a worker queue. The writer flushes off the game thread; the actor processes the acknowledgement in its mailbox before dependent work. Snapshot publication writes a temporary file, flushes it, atomically replaces the prior snapshot and flushes the containing directory where supported. The implementation MUST document platform limitations instead of claiming identical power-loss guarantees everywhere.

On load, verify sequence/checksums and apply records after the snapshot's last applied sequence. An incomplete final record MAY be truncated to its verified boundary. Checksum failure or missing records inside a segment MUST enter read-only/degraded recovery; do not silently skip events. Failed persistence prevents acceptance of new durable inputs and new action admissions. Previously authorized safe local jobs MAY continue only under their documented interruption policy.

No filesystem write/flush waits on a tick thread. Input UI reports `PENDING_ACCEPTANCE` during an asynchronous flush and `QUEUED` only after durable acknowledgement. A failed flush yields a visible rejection; it MUST NOT produce an accepted receipt.

Safety control is the explicit exception: stop can be `APPLIED_LOCALLY / RECORDING_PENDING` before any flush. Never label that receipt durably accepted until acknowledgement. Before enabling job restoration/action admission, persist a runtime-start marker. Publish a clean-shutdown marker only after control state/jobs are quiesced and their records are durable. After an unclean or storage-degraded runtime, all restored physical jobs remain suspended until fresh owner authorization; stored pre-stop jobs MUST NOT automatically resume when a stop may have failed to persist. Graceful shutdown is bounded and fences late callbacks; an exhausted drain deadline leaves the runtime unclean.

### 3.3 World saves are a separate durability domain

The action journal and Minecraft chunk/entity/inventory saves do not form one atomic transaction. Two failures MUST be represented:

1. A mutation is saved by Minecraft before the observed result is flushed to the journal.
2. A result is flushed to the journal before Minecraft saves the corresponding mutation.

Recovery MUST compare recorded evidence with restored world state without assuming either store is newer or authoritative for every fact. A changed block alone cannot prove who changed it. Inventory transfers MUST retain source/destination evidence and identities, but MUST still enter `UNKNOWN` when attribution or conservation cannot be established.

The guarantee is **one execution admission per call key, repeatable delivery of recorded results, and explicit uncertainty across non-atomic world saves**. It is not exactly-once world modification. An uncertain destructive action MUST NOT be automatically retried. If an already-confirmed result disagrees with restored world state, preserve the historical result and create a visible discrepancy/recovery incident; do not silently replay the original call or erase the prior acknowledgement.

## 4. Input routing and scheduling

### 4.1 Commands

| Command/input | Behavior |
|---|---|
| Addressed chat, configurable name prefix | Verify speaker/ownership/consent; route through the shared intent router |
| `/chat say <text>` | Bypass address matching, then use the same router and durable input path |
| `/chat follow` | Deterministically follow the issuing owner; return local job receipt |
| `/chat stop` | Immediately fence/cancel local jobs; queue remote cancellation/reconciliation |
| `/chat move <x> <y> <z>` | Validated bounded same-dimension move; visible acceptance/rejection |
| `/chat agent` | Read-only correlated subsystem status |
| `/chat tasks` and `/chat cancel <jobId>` | Inspect owner jobs or cancel an authorized job |
| `/chat resume <jobId>` | Freshly authorize a suspended safe job after live validation; cannot replay an uncertain destructive step |
| `/chat agentmode on|off` | Configure bounded autonomy; does not grant new world-action permissions |
| `/chat privacy` | Show remote-processing and retention controls |
| `/chat recover` | Reconcile state without clearing identities, ledger or historical results |

Initial deterministic addressed grammar SHOULD recognize exact phrases such as `follow me` and `stop`; ambiguous natural language goes to the Agent. All sources share normalization and validation. Two identical owner requests create different message/intent IDs.

For a deterministic command, create a local `intentId` and durable receipt that is available to conversation as an already-handled fact. AI tools acting on that same intent MUST reuse/refer to its receipt rather than create another job. Physical tool requests MUST reference a server-issued authorized intent. An autonomous observation can carry an intent only for explicitly configured allowed behavior. A model cannot create a valid authorization by inventing an ID. Cancellation/safety stop is permitted even when storage or OpenAI is unavailable; the server fences immediately and then persists the event asynchronously, entering degraded recovery if that fails.

### 4.2 Queue and remote scheduling

One actor owns one durable user queue and a separate set of protocol obligations. Recovery and pending tool-result obligations gate dependent remote submissions; ordinary queue priority cannot override these prerequisites.

| Work | Scheduling rule |
|---|---|
| Safety/owner stop | Immediate local control lane; never waits behind HTTP or persistence |
| Required result delivery and reconciliation | Progress before submitting another dependent conversation turn |
| Explicit user commands | Local dispatcher first; durable receipts join user context |
| User conversation | FIFO by durable acceptance sequence |
| Maintenance | Coalesced; bounded; cannot duplicate reconciliation |
| Autonomous observations | Disposable/coalesced lowest priority; never displace accepted user input |

For v1, ordinary inputs MUST remain queued while a remote turn is active. Mid-turn steering is deferred until attribution, ordering and cancellation are tested. The API supports steering, but supporting it is not required to ship reliable queued conversation. Local stop and previously authorized tasks remain responsive throughout a remote turn.

Initial bounds: 128 accepted pending user inputs per companion, 4,096 Unicode code points and 16 KiB UTF-8 per input, 16 coalescible autonomous entries, and a configurable global pending-input budget. When full, reject new input visibly before acceptance. Never evict accepted user messages silently. An autonomous item can expire; an accepted user message can end only in processed, owner-cancelled or visibly failed disposition.

`InputState`: `PENDING_ACCEPTANCE -> QUEUED -> SUBMITTING -> ACCEPTED_REMOTE -> RESOLVED`; alternate terminal states `REJECTED`, `CANCELLED`, `FAILED`. A timeout in `SUBMITTING` is an unknown submission outcome, not evidence of rejection. Record the turn association separately from submission acknowledgement.

## 5. OpenAI Agents transport contract

Use Agents API with `environment: {"type":"none"}` for this Minecraft application. It does not require an Agent sandbox: physical tools are executed by Minecraft. Raw HTTP MUST send `OpenAI-Beta: agents=v1`. Preserve the requested default model `gpt-6-luna` with low reasoning; Sol escalation is policy/budget controlled.

All API schemas MUST be isolated behind `AgentsTransport`, pinned as reviewed fixtures and exercised by the fake service. Beta contract changes MUST fail visibly rather than make the adapter reinterpret unknown responses as successful work. [Agents overview](https://developers.openai.com/api/docs/guides/agents-api/overview)

### 5.1 Operations

| Operation | Endpoint |
|---|---|
| Create saved Agent, if using one | `POST /v1/agents` |
| Create session with initial input | `POST /v1/agents/sessions` |
| Retrieve session | `GET /v1/agents/sessions/{session_id}` |
| Observe session | `GET /v1/agents/sessions/{session_id}/events?stream=true` using SSE |
| Submit message, function result or cancellation | `POST /v1/agents/sessions/{session_id}/events` |
| Retrieve saved items | `GET /v1/agents/sessions/{session_id}/items` |
| Retrieve intended turn | `GET /v1/agents/sessions/{session_id}/turns/{turn_id}` |
| Retrieve items for one intended turn | `GET /v1/agents/sessions/{session_id}/turns/{turn_id}/items` |
| Discover sessions after uncertain creation | `GET /v1/agents/sessions` with bounded pagination |
| Update supported settings | `POST /v1/agents/sessions/{session_id}` |
| Delete session after safe retirement | `DELETE /v1/agents/sessions/{session_id}` |

A `none` session MUST be created with initial input. Do not implement an empty idle create followed by the first message. Before creation, durably record the first input, immutable create payload and random `create_operation_id`. Put that operation ID in supported session metadata and persist the remote session ID as soon as either response or stream provides it. The reviewed create reference does not document the message endpoint's `Idempotency-Key` parameter; creation MUST NOT use the generic automatic retry loop. [Session lifecycle](https://developers.openai.com/api/docs/guides/agents-api/sessions), [Create reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/create)

If no remote ID is known after response/stream loss, enter `CREATE_UNKNOWN`. A bounded paginated session-list scan MAY reattach one uniquely matching metadata operation ID after validating context/configuration. The list endpoint has no metadata filter. Initial discovery budget: three reconciliation passes, 120 seconds total, at most 1,000 inspected sessions. No match, multiple matches or exhausted budget enters `NEEDS_REVIEW`; none proves that creation failed. Preserve queued input and possible orphaned state. Operator resolution records reattachment or explicit authorization for replacement with a new operation ID; do not silently recreate. [List reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/list)

Submissions to the events endpoint return acceptance, not final turn completion. A function result is represented by `agent.session.input.tool_result` containing the original `turn_id`, `call_id`, `success` and recorded `output` or `error`. The output may be a string or supported content array; v1 SHOULD use a versioned JSON result serialized as a string. Use `success: false` plus the documented error form for rejected/failed handling. An accepted local job uses `success: true` but still does not claim the physical job has completed. Preserve the full output/error payload durably for repeatable delivery.

A pending function action MUST be taken from the current session's `required_actions` and discriminated by `type == function_call`; its identifiers, name and arguments are authoritative for correlation. Historical function-call items MUST NOT trigger execution. Other required-action types MUST be handled explicitly or place the session in visible incompatible/degraded state. [Agents functions](https://developers.openai.com/api/docs/guides/agents-api/tools/functions)

The following wire examples are the reviewed baseline; fixtures MUST also cover the documented failure/error envelope:

```json
{
  "agent": {
    "model": "gpt-6-luna",
    "reasoning": {"effort": "low"},
    "instructions": "<versioned Minecraft companion instructions>",
    "tools": []
  },
  "environment": {"type": "none"},
  "metadata": {"create_operation_id": "create_example"},
  "input": [{"role": "user", "content": [{"type": "input_text", "text": "Where are you?"}]}],
  "stream": true
}
```

Here `tools: []` represents the initial conversation-only build; add only the tested function objects from section 6 as each capability passes its gate.

```json
{
  "events": [{
    "type": "agent.session.input.message",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "How is the task going?"}]}]
  }]
}
```

```json
{
  "events": [{
    "type": "agent.session.input.tool_result",
    "turn_id": "turn_example",
    "call_id": "call_example",
    "success": true,
    "output": "{\"schema_version\":1,\"status\":\"accepted\",\"job_id\":\"job_example\"}"
  }]
}
```

Remote cancellation uses `{"events":[{"type":"agent.session.input.cancel"}]}`. It is a remote workflow request; the local control fence applies first.

### 5.2 Idempotency and acknowledgements

For supported input-message submission, persist the `Idempotency-Key`, session identity and exact immutable request payload. Keys are 1–256 characters. Retries of one logical message MUST reuse all three; distinct identical messages MUST use different keys. Do not assume a header provides indefinite or global deduplication, or automatically covers creation/results/cancellation. The reviewed reference does not establish an idempotency retention period. Record the verified scope for each endpoint, and always reconcile uncertain outcomes rather than inventing a deduplication window. [Events submission reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/subresources/events/methods/create)

HTTP `202` for an events submission means accepted. It does not prove a turn succeeded, a result was saved or Minecraft completed a task. Result confirmation MUST find a saved `function_call_output` associated with the same turn/call and consistent recorded output/error, or another explicit documented acknowledgement. Saved items have `status`, `output` and `error`, rather than the submission event's `success` field. A saved failed tool result can confirm successful delivery of a local failure. Absence from `required_actions` alone is insufficient. [Saved-items reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/subresources/items/methods/list)

### 5.3 Streaming handoff and recovery

Streams have no replay. Initial/follow-up observation MUST subscribe before sending work, or use the documented combined streamed creation path. After a disconnect:

1. Reconnect and buffer new events for the current runtime/actor generation.
2. Retrieve the session and paginate saved items with `order=asc&limit=100`; while `has_more`, continue using `after=last_id`. Filter intended turn IDs locally unless a verified endpoint supports that filter.
3. Retrieve the intended turn outcome where required; restore state keyed by item ID and call key.
4. Merge buffered events with the restored state, deduplicating stable identities.
5. Resume live event processing and gated queue draining.

If buffering reaches its limit, abandon that observer and repeat reconciliation; never drop lifecycle events silently. If streaming is unavailable, polling MAY recover saved state, but MUST continue turn-result and item checks rather than equating `idle` with success. Multiple consumers MUST NOT race to admit a call. A remotely failed session ends automatic stream reconnection; retire/replace it only through controlled recovery. [Session lifecycle](https://developers.openai.com/api/docs/guides/agents-api/sessions), [Events and recovery](https://developers.openai.com/api/docs/guides/agents-api/sessions/events)

### 5.4 Actor lifecycle

```text
LOADING -> CREATING_SESSION or RECONCILING
CREATING_SESSION -> RECONCILING              (save returned session identity first)
CREATING_SESSION -> CREATE_UNKNOWN           (no remote identity after response loss)
CREATE_UNKNOWN -> RECONCILING | NEEDS_REVIEW  (unique metadata recovery / bounded uncertainty)
RECONCILING -> IDLE | ACTIVE | REQUIRED_ACTION | DEGRADED | NEEDS_REVIEW
IDLE -> SUBMITTING -> ACTIVE                 (known acceptance / correlated turn)
SUBMITTING -> RECONCILING                    (uncertain request outcome)
ACTIVE -> REQUIRED_ACTION | RECONCILING      (terminal event or stream disconnect)
REQUIRED_ACTION -> ACTIVE or RECONCILING      (recorded results accepted/reconciled)
DEGRADED -> RECONCILING                      (bounded scheduled attempt / operator repair)
NEEDS_REVIEW -> RECONCILING                  (recorded explicit resolution)
any -> STOPPING -> CLOSED                    (runtime shutdown fences callbacks)
```

`IDLE` is local readiness after reconciliation, not a synonym for a successful remote turn. Wire session statuses are `idle`, `in_progress`, `requires_action`, `failed`; wire turn statuses are `queued`, `in_progress`, `waiting`, `completed`, `failed`, `cancelled`. Every local turn stores `SUCCEEDED`, `FAILED`, `CANCELLED` or unresolved outcome independently. A failed turn can still contain recoverable assistant output; deliver eligible items without hiding the failure. [Turn retrieval reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/subresources/turns/methods/retrieve)

### 5.5 Retry ownership

Only the actor's retry coordinator owns logical retries. Disable overlapping SDK/transport retry loops or account for them explicitly. Allow at most four attempts and a 120-second total deadline per eligible logical remote operation by default; these are configurable. Uncertain creation follows section 5.1 instead of automatic retries. Respect `Retry-After`; the 30-second cap applies only to computed exponential backoff with jitter. If a provider delay exceeds the remaining deadline, enter degraded state and record the earliest permitted next retry without retrying early. Timers must be cancellable; no blocked worker sleep is necessary.

| Failure | Required handling |
|---|---|
| Malformed `400` | Record payload/schema failure and fail visibly |
| `401` / `403` | Stop remote calls; report authentication/access issue without logging secrets |
| Missing session `404` | Preserve local history; verify context/access/deletion before explicit replacement |
| `409` | Retrieve state and classify conflict; incompatible executor/schema is not a generic retry |
| `429` | Honor retry delay and bounded budget; distinguish quota exhaustion from temporary rate limiting |
| Timeout/reset/`5xx` | Reconcile potentially accepted work before a safe retry |
| Failed/cancelled turn | Record actual outcome and recover saved eligible output |
| Retry budget exhausted | Enter visible degraded state with next permitted recovery time |

The same stored result is reused for every tool-result delivery attempt. No retry code path calls `GameExecutor.execute`. [Errors and recovery](https://developers.openai.com/api/docs/guides/agents-api/errors)

## 6. Tool and action contracts

All tool arguments are untrusted. Server validation MUST enforce schema, owner, context, permissions, reachability and bounded workload independently of model reasoning. Recursive object schemas MUST specify `additionalProperties: false`. Do not assume an undocumented `strict` field is available in the Agents function schema; maintain server-side schema validation regardless of model schema behavior.

### 6.1 Initial tools

All IDs below are strings in the wire schema; UUIDs, job IDs and server-issued intent IDs MUST be parsed/validated. Coordinates are integer block coordinates in an allowlisted dimension. No tool accepts code, arbitrary commands, selectors or free-form executable task descriptions.

Each mutating turn is bound to the companion's current control generation, capability revision and server-issued intent scope before submission. A tool call cannot substitute a newer generation merely by naming a fresh ID. Revoked turn scopes remain revoked until a new authorized turn starts. This prevents delayed pre-stop calls from using post-stop context to restart movement.

| Tool | Required arguments and bounds | Outcome |
|---|---|---|
| `get_companion_status` | None | Current immutable status/evidence |
| `inspect_nearby` | `radius` integer 1–16; `max_entities` integer 1–32 | Bounded permission-filtered snapshot with tick/dimension |
| `follow_player` | `intent_id`, `player_id`, `stop_distance` number 2–8 | Accepted continuous job ID; owner only in v1 |
| `move_to` | `intent_id`, `dimension`, `x`, `y`, `z`, `stop_distance` number 1–3 | Accepted bounded same-dimension job ID |
| `stop_action` | `intent_id` | Cancellation receipt and new control generation |
| `mine_block` | `intent_id`, `dimension`, `x`, `y`, `z` | Accepted one-block job ID or explicit rejection |
| `place_block` | `intent_id`, `dimension`, `x`, `y`, `z`, `inventory_slot` integer 0–35, `face` enum of six directions | Observed placement or explicit rejection/uncertainty |
| `interact_with_block` | `intent_id`, `dimension`, `x`, `y`, `z`, `face` enum | Allowlisted interaction result; unsupported semantics rejected |
| `collect_items` | `intent_id`, `radius` integer 1–8, `max_items` integer 1–32 | Accepted bounded job ID with transfer evidence |
| `attack_entity` | `intent_id`, `entity_id` | Accepted bounded eligible-target job ID; no PvP by default |
| `start_task` | `intent_id`, `task_type` enum from versioned registry, `task_spec` tagged bounded object | Accepted registered job; disabled for unimplemented types |
| `get_task_status` | `job_id` | Owner-visible local state and actual evidence |
| `cancel_task` | `intent_id`, `job_id` | Cancellation receipt; authorized owner only |

Schema fixtures MUST define each tagged `task_spec`; the first supported registry SHOULD contain only already-tested local tasks. Tools MUST not be advertised to the Agent until their adapter, authorization and tests exist. `mine_block` timing follows Minecraft/tool rules and does not imply instantaneous breaking. `start_task` is an allowlisted registry entry point, not a general planner execution engine.

Representative function object:

```json
{
  "type": "function",
  "name": "follow_player",
  "description": "Start or acknowledge an authorized continuous follow intent; acceptance does not mean arrival.",
  "parameters": {
    "type": "object",
    "properties": {
      "intent_id": {"type": "string"},
      "player_id": {"type": "string"},
      "stop_distance": {"type": "number", "minimum": 2, "maximum": 8}
    },
    "required": ["intent_id", "player_id", "stop_distance"],
    "additionalProperties": false
  }
}
```

Before tool availability, prototype placement orientation and vanilla interaction semantics on the exact target version. If the adapter cannot represent an operation faithfully, expose a narrower schema or reject it; do not silently force a default behavior.

### 6.2 Result envelope

```json
{
  "schema_version": 1,
  "status": "accepted",
  "intent_id": "intent_...",
  "job_id": "job_...",
  "action": "follow_player",
  "reason_code": "navigation_job_started",
  "evidence": {"server_tick": 12540, "control_generation": 12}
}
```

`status` is one of `accepted`, `observed`, `rejected`, `cancelled`, `unknown`. `accepted` means a local job was installed after validation; `observed` means the bounded requested effect was observed in the live authoritative world. Neither asserts crash-proof world persistence. A rejection MUST provide a stable reason code. Store this local envelope separately from the API event wrapper: the event's `success` indicates tool handling outcome, while the local `status` describes what Minecraft accepted or observed. An uncertain outcome uses the documented failed/error channel with `reason_code: unknown` and MUST not be represented as observed success.

Conversation MUST reflect these distinctions: accepted follow permits “I’ve started following”; arrival/completion language requires actual corresponding evidence. Normal assistant text is delivered through the output channel. A `say` function is not required. Do not fabricate action-start status when the Agent only produces text.

### 6.3 Execution admission

```text
PREPARED --durable admission record--> ADMITTED
ADMITTED --server validation/execution observed--> OBSERVED
OBSERVED --events HTTP acceptance--> RESULT_ACCEPTED
RESULT_ACCEPTED --saved output acknowledgement verified--> CONFIRMED

validation rejection -> REJECTED -> RESULT_ACCEPTED -> CONFIRMED
dispatched without attributable outcome / save discrepancy -> UNKNOWN
```

These are workflow milestones, not one overloaded persistent enum. Store two independent axes:

* `executionState`: `PREPARED`, `ADMITTED`, `OBSERVED`, `REJECTED`, `UNKNOWN`.
* `resultDeliveryState`: `NOT_READY`, `READY`, `SUBMITTING`, `RESULT_ACCEPTED`, `CONFIRMED`, `DELIVERY_UNKNOWN`.

A truthful unknown/failure result can be `executionState=UNKNOWN` and `resultDeliveryState=CONFIRMED`; remote acknowledgement does not remove the world incident. Preserve observed evidence and any later world-save discrepancy independently. Once a result is ready for submission, its output/error payload is immutable. Later corrective evidence is a new recorded status/incident update, not a replacement payload for the same call. Operator repair requires a fresh authorized intent and call/job identity; it does not re-admit the old call.

Recovery MAY transition a durably recorded `OBSERVED` or `REJECTED` result directly to `CONFIRMED` when canonical saved-result evidence proves delivery despite a lost HTTP response. It MUST NOT fabricate an observed `RESULT_ACCEPTED` acknowledgement. `UNKNOWN` remains an incident until attributable evidence or an explicit recorded resolution supplies a truthful outcome; result delivery never resolves uncertainty about world persistence by itself.

The call key, arguments hash and planned job ID are durably `PREPARED` before admission. The `ADMITTED` record is flushed before scheduling server execution. The server rechecks runtime epoch, control generation, live authorization and world preconditions immediately before acting.

This conservatively sacrifices automatic retry if the process crashes after admission but before dispatch. After a restart, an `ADMITTED` call MUST be reconciled; it MUST NOT be admitted again merely because no result exists. A `PREPARED` call with no admission MAY proceed only when the ordered journal proves no admission and current remote state still requires it. Unknown/corrupt history prevents this proof.

Duplicate required-action events while a call is in flight join the existing state/future; they do not trigger another execution. A repeated call key with a different tool/arguments hash is an integrity violation and quarantines that call. Hashing MUST use a pinned canonical serialization and retain the original arguments for diagnosis without printing private data in normal logs.

Read-only tool requests follow the same correlation/result pipeline; they need not claim a world-mutation guarantee. Continuing a restored safe follow job is job restoration under the same job identity, not re-execution of the remote function. Destructive uncertain jobs never resume blindly.

### 6.4 Authorization and evidence

For every world operation, validate owner/context, current consent/capability, dimension, companion existence, loaded chunk, allowed bounds, reach, target identity and protection-policy decision. Mine/place additionally validate inventory/tool availability, expected block state, break/drop/consumption semantics and protection events. Use the loader's supported gameplay hooks or tested player-interaction bridge; raw block replacement is not a substitute for mining semantics.

Initial limits: destinations within 64 blocks of the companion in the same loaded dimension, one active locomotion-owning job per companion, one world-interaction step per job tick with a configurable global budget. Explicit owner movement commands supersede existing movement jobs and fence older AI work. Conflicting model jobs are rejected unless their authorized intent explicitly permits replacement.

Modded machine interactions are denied unless their block/action adapter is allowlisted and tested. Protection integrations MUST identify their supported hooks/providers. If required protection semantics cannot be checked, reject the operation. Combat MUST validate target UUID, type, team/PvP policy, visibility/reach and permission continuously. No other player's inventory access in v1.

Evidence SHOULD include before/after block state, affected positions, inventory counts/identities, dropped/collected entity IDs, server tick and job ID as appropriate. It supports reconciliation but is not an atomic transaction with the world save.

## 7. Local jobs and navigation

```text
SUSPENDED -> RUNNING -> COMPLETED | CANCELLED | FAILED | UNKNOWN
RUNNING -> SUSPENDED                      (owner/companion temporarily unavailable)
follow RUNNING substatus: MOVING <-> HOLDING
```

Follow stays `RUNNING/HOLDING` inside its stop radius and returns to `MOVING` when the player leaves. It terminates only through cancellation or a documented invalid-target/failure condition. `move_to` completes when its arrival predicate is observed. Follow reports distance, target and movement state; it MUST NOT publish a made-up percentage of completion.

Persist job identity/configuration and milestones, not a live navigation path or every tick. Restarted jobs begin `SUSPENDED` and revalidate owner, companion, dimension, permissions and interruption policy before resuming. After an unclean/storage-degraded runtime they also require fresh owner authorization under section 3.2. Recompute navigation from current state. Owner disconnect and companion chunk unload suspend follow; cross-dimensional targets fail with `target_dimension_changed` in v1. Destructive jobs with unresolved last steps enter `UNKNOWN`.

Initial navigation policy: vanilla entity navigation through the adapter; speed set locally rather than chosen each tick by the model; repath no more often than every 15 ticks unless a critical local event invalidates the path. Arrival thresholds and repath intervals remain configurable. Check hazards before motion, bound path recalculation and do not load chunks permanently for a job.

A progress monitor MUST distinguish a valid path from actual position progress. After 100 ticks without material progress while outside arrival distance, attempt one bounded recovery/repath. After 200 further ticks without progress, fail with `no_reachable_path` or `stuck`, including evidence. Profile/tune these initial thresholds on the target pack. Local jobs continue through API downtime; safety, death, permissions and owner commands can suspend/cancel them independently.

Stop clears navigation and cancels/revokes affected jobs on the next applicable tick, increments control generation immediately, and prevents queued stale dispatch. Do not wait for a remote cancellation acknowledgement to stop the entity. Track the remote turn separately until reconciled; no subsequent remote work is submitted in a way that loses pending call/result obligations.

## 8. Output, speech and networking

### 8.1 Assistant output

Deliver only intended user-facing assistant message content, not reasoning, arguments or internal tool output. Default chat/TTS consume saved `type: message`, `role: assistant`, `phase: final_answer`, `status: completed` items with `output_text` content. Transient commentary/deltas MAY appear as explicitly provisional HUD progress; incomplete content MUST not be treated as completed speech. Persist a delivery intention keyed by session item ID and recipient before sending. The client deduplicates that key and acknowledges `RECEIVED` and `DISPLAYED` separately. Server state MUST retain the distinction between sent, displayed and acknowledged.

If chat was displayed but its acknowledgement was lost, resend the stable ID; the client must acknowledge without displaying again. Across a client crash, perfect atomic display/deduplication is impossible. The default restart policy SHOULD mark delivery intention before display and favor avoiding duplicate old messages; diagnostics expose uncertain delivery and an owner history view permits manual retrieval. Never claim exactly-once visible output across crashes.

Use one utterance ID derived from `(sessionId, itemId, recipientId, speechRevision)` for ordinary TTS delivery. Distinct item IDs with identical text remain distinct utterances. Audio cache keys include provider/model/voice/settings/text hash; cache identity is separate from utterance identity.

### 8.2 Speech

Remote speech synthesis and playback are optional. The initial multiplayer provider SHOULD synthesize on server/trusted gateway and send bounded audio to the authorized client. Personal-key/local synthesis mode MAY run on the client only through explicit configuration. The server's credential MUST never be sent to a client. Dedicated server class loading MUST not touch rendering, audio devices or client-native libraries.

Speech states: `QUEUED -> SYNTHESIZING -> READY -> PLAYBACK_ADMITTED -> PLAYBACK_STARTED -> PLAYBACK_COMPLETED`, with `FAILED`, `CANCELLED` and `PLAYBACK_UNCERTAIN` alternatives. `PLAYBACK_ADMITTED` is the durable client attempt marker written before starting sound. Only a completion acknowledgement becomes the diagnostic label `PLAYED`. Missing-client, synthesis and playback failures do not block chat or tool-result progress.

Persist the client's playback intention before playback. Do not automatically replay an uncertain `PLAYBACK_ADMITTED` or `PLAYBACK_STARTED` attempt after a crash, including loss before a start acknowledgement; expose uncertainty and offer explicit manual replay with a new playback-attempt ID. The underlying source utterance ID remains stable. This is at-most-once automatic playback admission, not guaranteed once-only audible completion. Deduplicate after reconnect across client sessions using stable IDs and bounded durable history.

Prototype Minecraft's sound facilities using bounded WAV/PCM decoding, without a new native codec dependency. The adapter MUST respect master/category volume, cancellation, selected device, audio resource cleanup and concurrency. Initial policy: one speaking utterance, four queued, 60-second maximum utterance; split longer text into identified segments. Decoder/synthesis work is off the game/render thread. Disclose that a generated voice is artificial. [OpenAI text-to-speech](https://developers.openai.com/api/docs/guides/text-to-speech)

### 8.3 Versioned payloads

Handshake MUST exchange protocol revision and capabilities: companion entity presentation, commands, diagnostics, chat delivery, speech provider/formats/chunks and acknowledgements. Reject incompatible revisions with a readable message; never attempt to decode unsupported packets.

Minimum payloads: capability handshake; authorized input; input receipt/disposition; companion/job status; assistant output; delivery acknowledgement; speech metadata; bounded audio chunk; speech acknowledgement; structured error. Each request includes a stable request/item/utterance ID where applicable. Bind owner UUID from the authenticated connection rather than trusting a client-supplied owner field.

Initial packet limits: 24 KiB audio chunks, 16 KiB user-input payload content, 4 MiB maximum decoded audio per utterance, bounded reassembly/timeout and no arbitrary URLs/paths. The networking spike MUST verify codec overhead and loader/version packet limits before fixing these values in protocol fixtures. Both sides validate size, sequence, recipient and schema before allocation.

NeoForge 1.21.1 payload handlers run on the main thread by default; a network-thread handler MUST use `enqueueWork` and handle exceptions from its returned future. Fabric 1.21.1 object-payload callbacks run on the server thread for C2S and render thread for S2C. Any worker completion still schedules game access explicitly. Client classes are loaded only through physical-client initialization; logical-side checks do not make a common static reference to a client class safe. NeoForge's documented packet caps are 1 MiB clientbound and under 32 KiB serverbound; application limits above are intentionally tighter. [NeoForge payloads](https://docs.neoforged.net/docs/1.21.1/networking/payload/), [NeoForge sides](https://docs.neoforged.net/docs/1.21.1/concepts/sides/), [Fabric C2S callbacks](https://github.com/FabricMC/fabric/blob/1.21.1/fabric-networking-api-v1/src/main/java/net/fabricmc/fabric/api/networking/v1/ServerPlayNetworking.java), [Fabric S2C callbacks](https://github.com/FabricMC/fabric/blob/1.21.1/fabric-networking-api-v1/src/client/java/net/fabricmc/fabric/api/client/networking/v1/ClientPlayNetworking.java)

The input adapter uses one server chat hook: NeoForge `ServerChatEvent` (respect cancellation; use raw text without rewriting signed chat) or Fabric `ServerMessageEvents.CHAT_MESSAGE` (permitted messages only). Do not also intercept the same chat client-side. Register `/chat` using `RegisterCommandsEvent` or `CommandRegistrationCallback`. Tick jobs at NeoForge `ServerTickEvent.Post` or Fabric `END_SERVER_TICK`, documenting the matching phase. Register object payloads through NeoForge `RegisterPayloadHandlersEvent`/`PayloadRegistrar` or Fabric `PayloadTypeRegistry` and play networking receivers.

## 9. Security, costs, retention and diagnostics

Remote conversation requires owner opt-in; remote speech requires separate opt-in. Observe addressed owner interactions by default. Nearby players' chat/inventories, microphone input and telemetry MUST NOT be collected implicitly. Agent mode does not bypass world permissions.

Server credentials load from protected environment/configuration outside the distributable JAR. Do not log plaintext keys, authorization headers or full API payloads by default. Diagnostic export MUST redact credentials, conversation and identifying coordinates according to owner choice. Locally persisted conversation remains sensitive even when logs are redacted; protect file access and document retention.

Set configurable per-player/server call/token/spend limits and autonomy cooldowns. Initial autonomy: off; if enabled, coalesce unchanged observations and allow at most one new AI observation per 30 seconds per companion, subject to stricter global budgets. Emergency local behavior does not wait for an AI budget. Pricing estimates MUST use the selected model's current context/cache/service-tier rates rather than treating short-context rates as universal. Verify cost accounting using provider usage fields and report uncertain estimates honestly. [OpenAI pricing](https://developers.openai.com/api/docs/pricing)

Initial global limits SHOULD be: eight non-stream remote requests, one mutating submission and one reconciliation operation per session, 32 subscribed active sessions, two synthesis/decoding jobs and one ordered persistence writer. Bound pending I/O admission to 256 operations; prioritize protocol obligations and reject/coalesce optional work. An active SSE observer is budgeted separately from short HTTP requests. Adapter profiling MAY tune these values, but effective limits MUST appear in diagnostics and tests. Multi-owner world conflicts are resolved by final server-thread validation, not by independent per-player actor ordering.

The product MUST offer remote-processing off, speech off, local conversation/audio clear, redacted export and remote-session retirement/deletion where supported. Remote-state deletion and local clear are separate operations with separate outcomes. Verify current Agents retention and data-control terms before release; the report's retention paragraph is a dated source claim, not a permanent product guarantee. [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)

`/chat agent` MUST report: loader/mod/protocol versions; runtime/control generation; owner context; configured model; remote session and intended turn state/outcome; pending action type/name/call; separate ledger execution/result-delivery states; queue counts/oldest age; retry attempt/deadline/next time; last HTTP category; companion loaded/health/dimension; job state/substatus/distance; actual movement age/path state; speech synthesis/playback state; persistence health and pending unknown incidents.

Structured logs MUST carry available session, turn, call, message, intent, item and job IDs. Use stable reason codes such as `permission_denied`, `stale_control_generation`, `target_unavailable`, `chunk_unloaded`, `stuck`, `storage_unavailable`, `result_acceptance_uncertain`, `world_save_discrepancy` and `unsupported_required_action`. Every degraded/recovering status includes what is pending, last attempt, next action/time or required operator resolution. “AI active” MUST never imply navigation or audio playback.

## 10. Testing, release gates and operational limits

Use JUnit 5 for ordinary Java contract tests; property tests for queue/ledger invariants; a deterministic fake Agents HTTP/SSE service for response loss/conflicts; and exact-version loader/world tests. The fake service MUST model accepted-but-response-lost requests, no-replay streams, saved items with pagination, current versus historical calls, and independent turn/session outcomes.

GameTest/client-test facilities MUST be established for Minecraft 1.21.1 in a foundation spike. Do not infer availability from the latest Fabric docs: the version-specific 1.21.1 testing guide covers unit tests. The versioned Fabric API does provide server GameTests via `fabric-gametest` entrypoints; NeoForge supplies GameTest registration and `runGameTestServer`. Prove compatible server-world invocations and use a documented custom launch harness or bounded manual client validation where client automation is unavailable. A required movement/world test cannot be silently replaced by a core unit test. [Fabric 1.21.1 testing](https://docs.fabricmc.net/1.21.1/develop/automatic-testing), [Fabric 1.21.1 GameTest API](https://github.com/FabricMC/fabric/blob/1.21.1/fabric-gametest-api-v1/src/main/java/net/fabricmc/fabric/api/gametest/v1/FabricGameTest.java), [NeoForge 1.21.1 GameTests](https://github.com/NeoForged/Documentation/blob/main/versioned_docs/version-1.21.1/misc/gametest.md)

| Gate | Required exit evidence |
|---|---|
| G0 Contracts | Approved DTOs/schemas, API fixtures, failure oracles, exact loader test-support plan and storage durability profile |
| G1 Foundation | Entity save/load, versioned networking, NeoForge client and headless server launches; no physical-client linkage on server |
| G2 Conversation | Persistent initial/follow-up sessions, durable queue/actor from W04.02, saved-output recovery, intended-turn failure/cancellation; interrupted create handled |
| G3 Reliability | No duplicate admission; stored-result delivery; both ledger/world save-order failures; stream handoff; bounded retries; corruption handling |
| G4 Movement | Follow/hold/re-follow, stop fences, move arrival, restart suspension, offline continuity and explicit stuck failure |
| G5 World actions | Protected mining/placement/collection/combat and allowlisted interactions with evidence; no unsupported advertised tools |
| G6 Speech/operations | Real client playback/control/deduplication, server audio isolation, useful diagnostic errors, consent/retention controls |
| G7 NeoForge release candidate | G0–G6 plus verifier/package audits, permission tests, repeated restarts and target Direwolf20 modpack smoke evidence |
| G8 Fabric supported | Equivalent requirements on Fabric 1.21.1, exact-version launch/world/audio evidence and clean packaged loader artifact |

A stable NeoForge release can proceed after G7. Advertising both loaders requires G8. The complete intended product is not finished until both pass. Optional integrations are advertised only with their own coverage evidence.

CI MUST compile with Java 21, test core contracts/failures, audit packaged classes/secrets/dependency metadata, and launch each advertised dedicated-server artifact with JVM verification (`-Xverify:all`). Client/world/audio checks use the available exact-version harness plus recorded manual checks where required. Full pack tests use an authorized installation and MUST record pack version, loader, Java and mod artifact hash.

Initial resource targets: no blocking HTTP or TTS on game threads; normally accept/enqueue within one tick after asynchronous durable write acknowledgement; emergency local stop by the next applicable tick; bounded observations of 16-block radius/32 entities; server work target below 1 ms average per active companion under a documented reference workload. The latter is a profiling target, not a guarantee. Add global navigation/interaction budgets so many companions cannot multiply unbounded per-tick work. Record tail tick time, queue age, request count, retries, storage latency, path failures, tokens and speech latency.

## 11. Upgrade, migration and rollback

Version these independently: `mod_version`, `storage_schema`, `agent_instruction_revision`, `tool_schema_revision`, `network_protocol`. Cosmetic changes MUST NOT reset sessions. Pending calls remain bound to the tool revision that created them.

Upgrade sequence: pause new autonomy/input submissions; fence/cancel unsafe local work; flush queues/ledger; make a verified backup; migrate storage transactionally; load old-tool compatibility handlers; reconcile current remote work; resolve/fail outstanding incompatible calls; only then create a replacement session if instructions/tools require it and resume queued work. Migration MUST not destroy the sole evidence for pending actions.

Supported session model/reasoning-effort/service-tier updates affect newly started turns after the update completes, not an active/steered turn. Changes to `instructions`, `tools`, `text`, `reasoning.summary` or `multi_agent` require a new session. Preserve old call handlers or resolve old work before switching. A migration summary MUST be bounded, consented and reference actual retained facts; do not blindly upload all logs. [Configure Agents](https://developers.openai.com/api/docs/guides/agents-api/configuration)

Remote deletion MUST first stop new input, preserve needed output/ledger evidence, cancel running execution and reconcile settlement. Deletion can abandon unpublished output; physical cleanup can remain asynchronous. Report deletion and cleanup outcomes separately and use bounded retries on unsettled conflicts. Clearing local history MUST not erase the sole record of unresolved physical work. [Delete reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/delete)

Legacy migration MUST inventory what 0.19.x data actually exists before promising an importer. Import only validated configuration, consent and conversation data with clear provenance. Do not import undocumented binary-patch state as trusted action evidence. Unknown remote credentials/session ownership require explicit setup; they are not reconstructed by guessing.

Rollback pairs the old JAR with a compatible storage snapshot or runs in read-only recovery. An old binary MUST refuse write access to a newer schema. World restoration can disagree with ledger history and MUST go through the same discrepancy process; swapping JARs alone does not undo world actions.

## 12. Implementation packages and estimates

The [backlog](implementation-backlog.csv) preserves the supplied planning baseline of **68 person-days**. This is an estimate, not a promised calendar schedule. Add 20–30% contingency (approximately 82–88 person-days) and separate modpack burn-in. API access, exact loader tooling, world-save recovery and audio integration are explicit risks to that estimate.

| Package | Days | Dependencies | Definition of done |
|---|---:|---|---|
| W01 Architecture/contracts | 4 | None | G0; DTOs, schema fixtures, recovery invariants, capability and exact-version harness decisions |
| W02 NeoForge foundation | 7 | W01 | G1; entity lifecycle, isolated source sets, handshake/commands, successful pinned builds |
| W03 Agents integration | 8 | W01 | Transport and G2 remote-flow evidence; no claim that success text proves execution |
| W04 Durable queue/recovery | 9 | W01, W03 contract fixtures | G3; asynchronous journal, actor scheduling, admission fences, fake-service fault injection |
| W05 Movement/world actions | 10 | W02, W04 | G4/G5; navigation/jobs, bounded interactions, authorization and world evidence |
| W06 Speech/diagnostics | 7 | W02, W03; W04 delivery identities | G6; speech provider/client prototype, playback acknowledgements and useful status/export |
| W07 Fabric adapter | 8 | W02 patterns, W04 shared contracts | G8 adapter equivalence after hardening; separate build and exact-version harness |
| W08 Hardening/compatibility | 10 | W04–W06; W07 for dual-loader release | G7/G8 test/audit evidence, actual pack smoke, bounded load and shutdown races |
| W09 Migration/release | 5 | W08 NeoForge; Fabric evidence for Fabric signoff only | Tested migration/rollback, legacy discovery outcome, documented setup and independent platform signoff |

W02 and W03 can run in parallel after W01. W04 can start when transport fixtures stabilize; W03.02/G2 require W04.02's durable actor queue, not storage alone. W06 audio prototyping can overlap W05; W07 follows stable adapter contracts. Reliability tests are written alongside features and G3 is not deferred until W08. All physical action admission/recovery invariants need one designated design owner across teams.

Backlog `depends_on` lists unconditional issue dependencies. `conditional_dependencies` entries such as `G8:W08.04` additionally gate Fabric signoff without blocking NeoForge G7. The overall 68-day estimate includes both platforms; releasing NeoForge first does not imply Fabric work has been completed.

Before implementation starts, record owners for each package, the concrete plugin/loader pins, supported storage durability profiles and protection-policy providers. Before live API validation, set up the authorized credential path and verify access with a bounded check; this specification work neither provisions credentials nor makes billable API calls.

## 13. Evidence and design decisions

The supplied report's opaque `turn...` citation markers are not portable source references. The following primary-source pages were used for contract review or identify the corresponding reference; implementation MUST capture reviewed response/request fixtures and version-specific source pins rather than relying only on links.

| Source | Purpose |
|---|---|
| [Agents overview](https://developers.openai.com/api/docs/guides/agents-api/overview) and [quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart) | Async sessions, environment choice and initial input |
| [Run and continue sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions) and [events/items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) | Stream subscription/recovery, idempotent input and saved output |
| [Functions](https://developers.openai.com/api/docs/guides/agents-api/tools/functions) and [API reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/subresources/items/methods/list) | Pending call identifiers, submission envelope and item acknowledgement |
| [Configuration](https://developers.openai.com/api/docs/guides/agents-api/configuration) and [errors/recovery](https://developers.openai.com/api/docs/guides/agents-api/errors) | Session revisions, cancellation and retry classification |
| [NeoForge 1.21.1 setup](https://docs.neoforged.net/docs/1.21.1/gettingstarted/), [sides](https://docs.neoforged.net/docs/1.21.1/concepts/sides/), [payloads](https://docs.neoforged.net/docs/1.21.1/networking/payload/) | Java/build, server safety and network scheduling |
| [Fabric 1.21.1 testing](https://docs.fabricmc.net/1.21.1/develop/automatic-testing) | Version-specific test-support boundary |
| [Speech](https://developers.openai.com/api/docs/guides/text-to-speech), [pricing](https://developers.openai.com/api/docs/pricing), [data controls](https://developers.openai.com/api/docs/guides/your-data) | Provider requirements and release-time verification |

Required design decisions are recorded as implementation tasks, not unanswered product scope: exact loader/mapping/build pins; exact-version client/world test harness; actual Agents creation/result idempotency scope; protection integration; speech playback adapter; legacy input formats. Each must close before the gate that depends on it. Any unavailable external contract MUST be reported as a blocker to that integration rather than replaced with invented methods. The [official FTB pack manifest](https://api.modpacks.ch/public/modpack/126) identifies Direwolf20 1.21 as Minecraft 1.21.1/NeoForge/Java 21; select one exact pack release as the G7 acceptance fixture, rather than claiming compatibility with every release.

### 13.1 Corrections carried forward from the report

* Follow is continuous: arrival is `HOLDING`, not completion.
* A durable action result and a saved Minecraft mutation are separate; both crash/save orderings are tested.
* Admission is persisted before dispatch; an unresolved admitted call becomes uncertain and cannot fall through to automatic execution.
* Stop and runtime shutdown use generations/epochs to block stale calls and callbacks.
* Journal acknowledgements are asynchronous; all actor state changes occur in its mailbox.
* Recovery/protocol obligations gate user submissions independently of queue priority.
* Stream recovery reconnects/buffers before fetching saved state, and item retrieval paginates.
* A `none` session includes initial input, events acceptance is not completion, and call disappearance is not result proof.
* Chat display, speech start and speech completion have separate persistent identities/acknowledgements.
* Fabric testing and shared mapped-source feasibility are verified specifically for 1.21.1.
