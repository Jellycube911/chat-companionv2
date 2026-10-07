# Chat Companion acceptance and fault-injection plan

**Target:** Minecraft 1.21.1; Java 21; NeoForge first, Fabric second.
**Plan date:** 7 October 2026.
**Status:** Proposed tests and release gates. No test in this document is represented as executed or passed.

This plan verifies the [engineering specification](engineering-specification.md) using its requirement IDs R-01–R-13. P0 failures block the affected release. P1 failures block the feature or platform that depends on them; any exclusion must be explicit in release capabilities. Passing a fake-service test does not establish compatibility with the current OpenAI API or a Minecraft loader.

## Harness and common oracles

Use a fake Agents service with versioned, source-reviewed contract fixtures, a controllable clock, a fault-injecting journal, independently selectable Minecraft save images, an instrumented server-thread executor, and a client presentation/audio simulator. No paid API calls or credentials are required. Record dispatches, mutations, journal durable acknowledgements, HTTP statuses, remote saved items, output acknowledgements, permission changes, and runtime/control generations.

Every scenario starts with explicit session, message, call, job, companion, world and player identities. Use fixed fixture IDs and seeded scheduling. Inject disconnects and process termination at named barriers; do not substitute exceptions that allow normal shutdown. Restart from only the bytes and world saves selected for that scenario. Assertions compare final state and complete event traces, rather than logs alone.

The shared oracles are:

- Tool workflow milestones are `PREPARED → ADMITTED → OBSERVED → RESULT_ACCEPTED → CONFIRMED`, with `REJECTED` or `UNKNOWN` alternatives. Persist execution and result-delivery states separately; delivery of an unknown/error result can be confirmed while world uncertainty remains. `ADMITTED` must be durable before scheduling. A recovered admitted call must never be admitted or dispatched again. `OBSERVED` records an observed effect, not proof that Minecraft persisted it. `RESULT_ACCEPTED` requires an observed HTTP 202; it does not establish `CONFIRMED`. Recovery transitions must be specified explicitly.
- Count execution admission, server dispatch, actual mutation and result submission separately. A rejected call has zero mutations. A repeated call never receives a second execution admission. A lost result acknowledgement can cause further result submissions, but cannot cause further dispatches.
- All world access and mutations occur on the logical server thread. Actor state changes occur through the actor mailbox. Durable-write acknowledgements are asynchronous; server ticks never wait for HTTP, speech or filesystem synchronisation.
- Jobs use `SUSPENDED`, `RUNNING`, `COMPLETED`, `CANCELLED`, `FAILED` or `UNKNOWN`. Follow stays `RUNNING`, with substatus `MOVING` or `HOLDING`, until an explicit terminal condition. API activity is never evidence of physical progress.
- Advance virtual time beyond each configured deadline and assert the specified terminal, retry or degraded state. “Eventually” without a deadline is not a passing oracle.

## Test cases

### AT-01 — Addressed chat and explicit commands

**Requirements:** R-01, R-02. **Priority:** P0. **Layer:** Core plus loader input adapter.

Submit addressed chat, unrelated chat, `/chat say`, Unicode text and two identical intentional messages. Exercise the configured address rules and command permissions.

**Oracle:** Each accepted input receives a distinct durable logical identity and a visible receipt or queue status. Unrelated chat is not uploaded. `/chat say` enters the same processing workflow. Identical messages are not collapsed. A deterministic command executed locally and subsequently forwarded to the Agent carries its action receipt; a later matching tool request cannot duplicate that intent.

### AT-02 — Durable input and unknown submission outcome

**Requirements:** R-02, R-03. **Priority:** P0. **Layer:** Core persistence and fake service.

Delay or fail the input journal acknowledgement, then terminate after durable enqueue. In another run, accept the message remotely and drop the HTTP response.

**Oracle:** No durable-acceptance receipt precedes the journal acknowledgement. Storage failure produces a visible rejection. Restart recovers accepted queued messages. Retries of one logical submission retain its original supported idempotency identity and payload; another intentional message receives a different identity. Reconciliation cannot silently discard an accepted message or submit it as a new logical message.

### AT-03 — Disconnect before the first session-create response

**Requirements:** R-02, R-03. **Priority:** P0. **Layer:** Transport and recovery.

Persist session-creation intent, let the fake service create a session, and disconnect before the client receives its ID. Restart with no locally known remote session ID. Also test failure before remote creation.

**Oracle:** Recovery uses only creation idempotency or discovery mechanisms verified for the pinned contract. Where the contract cannot identify the outcome, state remains visibly unresolved and requires the defined recovery procedure; the implementation must not blindly create another session or claim creation failed. Queued input survives, and authorised local jobs continue. The report identifies possible orphaned remote state.

### AT-04 — Idle, failed and incomplete turns

**Requirements:** R-03, R-08, R-12. **Priority:** P0. **Layer:** Recovery.

Return an idle session following a failed turn, an idle session with undelivered assistant items, and an active turn with no recent stream activity.

**Oracle:** Idle alone never marks a turn successful. Saved turn/item evidence determines the outcome. Recoverable output is delivered once according to the output policy. An active stale turn remains observable and reaches its reconciliation deadline; it is not silently reset. Diagnostics show remote state, last verified outcome and next recovery attempt separately.

### AT-05 — Stream handoff and reconciliation race

**Requirements:** R-03, R-04, R-08. **Priority:** P0. **Layer:** Transport and actor.

Disconnect the stream before and after assistant-item completion and required-action notification. Overlap a replacement stream with a session/items reconciliation response; deliver duplicates and delay an older response until after newer evidence.

**Oracle:** Events and canonical saved evidence converge to one state. Old responses cannot overwrite newer progress. No output or call is missed at handoff, no call is admitted twice, and no session becomes permanently stuck because the stream closed. Streaming deltas are not mistaken for completed durable utterances.

### AT-06 — Historic calls and other required-action types

**Requirements:** R-03, R-04. **Priority:** P0. **Layer:** Recovery.

Include historic function calls in saved items while current `required_actions` is empty. Separately include a currently pending non-function action and an unsupported action type.

**Oracle:** Historic calls cause zero world dispatches. Only currently pending supported function actions enter tool admission. Other action types follow their explicit handler or visible unsupported state; no heuristic interpretation as Minecraft tools occurs.

### AT-07 — Duplicate required actions while execution is in flight

**Requirements:** R-04, R-05. **Priority:** P0. **Layer:** Actor, journal and server executor.

Pause journal acknowledgement, then pause the server execution future. During both pauses deliver the same pending call repeatedly through streams and reconciliation. Restart after durable admission but before observation.

**Oracle:** Exactly one durable admission and at most one original-runtime dispatch occur. Duplicate notifications join the existing lifecycle. On restart, unresolved admitted execution becomes `UNKNOWN` or is reconciled without redispatch. All result retries reuse the original stored outcome.

### AT-08 — Call identity with mismatched arguments

**Requirements:** R-04, R-10. **Priority:** P0. **Layer:** Tool admission.

Reuse a session/turn/call key with different tool names or semantically different arguments. Include harmless JSON property-order changes to exercise the defined canonical hashing rule.

**Oracle:** Equivalent canonical arguments match. A real mismatch is quarantined as an integrity error with zero additional dispatches. It neither overwrites stored evidence nor receives an unrelated cached success result. Diagnostics identify the key without exposing secrets or unnecessary conversation content.

### AT-09 — Slow persistence without blocking ticks

**Requirements:** R-02, R-04, R-05, R-12. **Priority:** P0. **Layer:** Actor and threading harness.

Hold durable acknowledgements while ticks continue. Complete independent writes and HTTP callbacks in adversarial order; fail the admission write.

**Oracle:** No action is scheduled before its admission is durable. Server ticks and emergency stop remain responsive. All ledger/queue transitions occur through the actor mailbox; workers do not mutate actor state. Failed admission produces zero dispatches. Trace order preserves the specified causal dependencies despite callback reordering.

### AT-10 — Minecraft save ahead of the action ledger

**Requirements:** R-04, R-05, R-13. **Priority:** P0. **Layer:** Crash recovery with independent save images.

Persist admission, execute a block/inventory action, retain the resulting Minecraft save, but terminate before the observed result becomes durable. Restart with the admitted journal and changed world. Include ambiguous evidence caused by another player's intervention.

**Oracle:** The call is never automatically re-admitted or redispatched. Reconciliation records only evidence it can establish; ambiguous attribution yields `UNKNOWN`. No duplicate drop, inventory debit or placement occurs. Remote result delivery uses a reconciled outcome, never an invented historical success.

### AT-11 — Action ledger ahead of the Minecraft save

**Requirements:** R-04, R-05, R-13. **Priority:** P0. **Layer:** Crash recovery with independent save images.

Make the observed result durable, optionally retain remote acceptance/confirmation, then terminate before the relevant Minecraft data is saved. Restart with the older block/entity/inventory image.

**Oracle:** A retained success result does not prove that the world effect survived. Detectable divergence becomes an explicit uncertain/recovery condition without automatically repeating the action or relabelling the restored world as successful. Remote confirmation remains a transport fact. Any repair procedure is explicit, separately authorised where needed, and has a new identity.

### AT-12 — Tool result accepted remotely but response lost

**Requirements:** R-03, R-04. **Priority:** P0. **Layer:** Fake service and result delivery.

Accept the stored result remotely and lose the HTTP 202 response. Exercise an observed 202, an unexpected success status, an absent pending call without saved-result evidence, and canonical evidence confirming the original result.

**Oracle:** Result delivery never invokes the action executor. `RESULT_ACCEPTED` is recorded only after observing 202. HTTP acceptance or disappearance from pending actions alone does not establish `CONFIRMED`. Canonical confirmation can resolve the lost-response case through the documented recovery transition. Missing evidence remains uncertain rather than fabricated. Confirming delivery of a truthful unknown-result error leaves `executionState=UNKNOWN` and its world incident unresolved; it cannot convert the physical outcome to success.

### AT-13 — Bounded and classified retries

**Requirements:** R-03, R-04, R-12. **Priority:** P0. **Layer:** Transport and virtual clock.

Parameterise conflicts, authentication failures, malformed payloads, missing sessions, quota exhaustion, rate limits with `Retry-After`, and transient timeouts/service failures. Advance through the configured request and total-operation budgets.

**Oracle:** Conflict and unknown outcomes cause reconciliation before eligible retries. Rate-limit scheduling honours the validated delay. Permanent failures stop remote retries visibly. There is one retry owner, bounded concurrency and a finite deadline; nested retries do not multiply traffic. No branch repeats a physical action or silently resets a session.

### AT-14 — Continuous follow, path progress and offline operation

**Requirements:** R-05, R-06, R-12. **Priority:** P0. **Layer:** Minecraft world harness.

Follow a reachable moving player, let the companion reach the radius, move the player again, then disconnect OpenAI. Also create an unreachable target, unloaded boundary and obstacle-induced stuck condition.

**Oracle:** Arrival changes follow to `RUNNING/HOLDING`; renewed separation changes it to `RUNNING/MOVING`. On a reachable fixture, navigation produces measurable displacement and reduced distance within the configured deadline. API loss does not stop authorised local follow. Repath and chunk policy remain bounded. Unreachable/stuck cases produce the specified failure or suspended state, never a fabricated arrival.

### AT-15 — Task restore and restart validation

**Requirements:** R-06, R-10, R-13. **Priority:** P0. **Layer:** Task persistence and world harness.

Restart during follow and a finite task. Vary owner presence, companion presence, dimension, target validity and restored world/job consistency.

**Oracle:** Restored jobs begin `SUSPENDED` until validation. After unclean/storage-degraded shutdown they also require fresh owner authorization before physical resumption. Valid continuous jobs retain their ID when explicitly resumed and recompute navigation; invalid or ambiguous jobs remain suspended or terminate explicitly. No duplicate companion, job admission or inventory effect occurs. Persisted finite-task progress cannot substitute for checking restored world effects. Loader-specific path objects are not restored as live references.

### AT-16 — Immediate cancellation and control-generation fence

**Requirements:** R-04, R-06, R-07. **Priority:** P0. **Layer:** Server executor and actor.

Issue stop while follow admission is awaiting durability, after admission but before server dispatch, while a task is running, and while remote cancellation fails. Deliver a delayed pre-stop action afterward.

**Oracle:** Applicable local movement stops on the next server tick. Stop increments the control generation before stale work can act. Older-generation calls cannot restart or replace the stopped job. Admitted-but-cancelled work gets a recorded truthful outcome without execution; fresh authorised post-stop commands remain possible. Remote failure does not undo local cancellation. If the stop's journal flush fails, its receipt says applied locally rather than durably accepted; after a crash, an unclean/storage-degraded runtime cannot automatically restore the older job without fresh owner authorization.

### AT-17 — Server-runtime and ownership lifecycle fence

**Requirements:** R-03, R-05, R-06, R-07. **Priority:** P0. **Layer:** Lifecycle harness.

Pause HTTP, persistence and server-dispatch completions; stop the server and start another runtime, including a different world. Repeat around logout, companion removal and an ownership change.

**Oracle:** Every scheduled action validates the runtime epoch and applicable ownership/control generation. Old work cannot mutate a new runtime, cross worlds, resume a removed entity or update a replacement actor. Recoverable durable records remain associated with their original context. Shutdown rejects new work and follows its bounded persistence policy.

### AT-18 — Permission changes after admission

**Requirements:** R-05, R-06, R-10. **Priority:** P0. **Layer:** Permission adapter and server executor.

Revoke a capability, change protection/team rules or invalidate owner/target after validation but before dispatch. Repeat during mining, interaction and combat jobs.

**Oracle:** Execution revalidates current permissions and action preconditions on the server thread. Continuing work rechecks permissions at its specified boundaries and stops within the configured bound. Rejected actions have no subsequent effects. Model-supplied UUIDs do not establish ownership or authorisation.

### AT-19 — Authoritative world actions and truthful receipts

**Requirements:** R-04, R-05, R-10. **Priority:** P0. **Layer:** Minecraft world harness.

Exercise mine, place and interact with stale block state, wrong dimension, missing inventory/tool, invalid reach, protected blocks and policy-disallowed modded interactions. Attempt equivalent operations from a client payload.

**Oracle:** The logical server derives authority and validates actual current state. Mutation paths respect the supported protection and gameplay semantics. Invalid requests have zero mutations. An accepted job receipt identifies acceptance, not completed world success. Completion requires observable evidence; assistant text, remote tool requests and navigation flags alone cannot satisfy the oracle.

### AT-20 — Output delivery with lost acknowledgement

**Requirements:** R-03, R-08. **Priority:** P0. **Layer:** Client/server presentation simulator.

Deliver a completed assistant item, lose its acknowledgement, reconnect and reconcile it again. Terminate around the client deduplication marker and visible display boundary.

**Oracle:** Stable item IDs persist through recovery and do not create new logical output. Received and displayed acknowledgements are distinct. Duplicate retries obey the documented display policy. The crash window between deduplication storage and visible presentation is exposed as uncertainty; the implementation does not promise exactly-once display across that boundary. An explicit replay remains available when presentation is uncertain.

### AT-21 — Speech uncertain-start deduplication

**Requirements:** R-08, R-09. **Priority:** P0. **Layer:** Client audio simulator and persistence.

Terminate after the durable playback-attempt marker but before start acknowledgement; repeat after audio starts and halfway through playback. Reconnect and redeliver the utterance. Include two distinct utterance IDs with identical text.

**Oracle:** An uncertain previous playback attempt is never automatically replayed. Uncertainty is visible; explicit replay creates a new attempt identity. Distinct intentional utterances remain independently eligible even when they share cached audio. No `PLAYED` completion is fabricated after a lost start or completion acknowledgement.

### AT-22 — Speech failures and completion semantics

**Requirements:** R-09, R-11, R-12. **Priority:** P0. **Layer:** Client audio adapter plus simulator.

Inject synthesis timeout, malformed/oversized audio, unavailable device, disabled volume, decode failure, cancelled playback and completion. Exercise a disconnected client and audio queue saturation.

**Oracle:** Synthesis, decoding, start and completion have separate observable outcomes. Only verified completion produces `PLAYED`; starting audio does not. Speech failure cannot crash the game or hide chat output. Synthesis and decode do not block game/render threads. Packet/cache/queue limits are enforced, and headless servers never attempt audio-device playback.

### AT-23 — Privacy, session isolation and secret redaction

**Requirements:** R-03, R-10. **Priority:** P0. **Layer:** Core, networking and packaging audit.

Use multiple owners/worlds and unrelated nearby chat. Toggle remote conversation and remote speech consent independently. Inspect fake-service submissions, packets, JARs, logs and diagnostic exports using planted non-production secret sentinels.

**Oracle:** Sessions and permissions cannot leak across owner/world/companion context. Only permitted observations and interactions reach the remote service. Revoked consent prevents new relevant uploads. Server credentials never appear in client payloads, chat, distributable artefacts or diagnostic exports. Redaction preserves operational identifiers needed for recovery without exporting sensitive content by default.

### AT-24 — Bounded resources, queue fairness and diagnostics

**Requirements:** R-01, R-02, R-03, R-12. **Priority:** P0. **Layer:** Load harness and virtual clock.

Saturate autonomous observations, user messages, pending result deliveries, audio and global HTTP slots. Delay remote responses while local tasks run. Reorder completion callbacks and exhaust configured budgets.

**Oracle:** Required-result/recovery obligations progress under the defined gate; user work remains FIFO and receives service once remote prerequisites permit, or remains visibly queued/degraded within retry deadlines. A continuous remote outage is not claimed to guarantee successful message processing. Disposable observations coalesce, while accepted user messages survive. Full queues reject visibly rather than drop silently. Worker, storage, observation and retry limits hold. Diagnostics distinguish remote processing, action admission, job progress, movement and speech, with pending age, correlation IDs and the next recovery action.

### AT-25 — Missing, truncated and corrupt journal data

**Requirements:** R-02, R-04, R-13. **Priority:** P0. **Layer:** Persistence fault harness.

Remove a middle segment, corrupt a complete record, truncate the final append and mismatch snapshot/checkpoint generation. Include a world change whose admitted call cannot be reconstructed because relevant records are missing.

**Oracle:** Validation detects unsupported gaps and corruption; they are not silently skipped as empty history. Only the documented incomplete-tail recovery is allowed. Preserve original bytes and report the affected range. Affected workflows enter safe recovery/read-only operation; no uncertain world action is redispatched and no lost accepted input is claimed processed. Valid unaffected state is recovered only when its integrity can be established.

### AT-26 — Storage/tool migration and rollback

**Requirements:** R-03, R-04, R-13. **Priority:** P0. **Layer:** Versioned fixtures.

Upgrade with queued messages, an admitted uncertain call, an old pending tool and active jobs. Interrupt migration. Start an older executable against a newer schema; restore an intentionally mismatched world snapshot and ledger backup.

**Oracle:** Migration is versioned, backed up and restartable without losing identities or granting fresh admission. Old calls use explicit compatibility handlers or truthful rejection. Breaking session changes preserve unresolved obligations. Unsupported downgrade is read-only or rejected before writes. Restoring a backup does not erase possible later side effects or justify replay; world/ledger divergence requires recovery.

### AT-27 — NeoForge client and dedicated-server isolation

**Requirements:** R-11. **Priority:** P0. **Layer:** Exact-version loader CI.

Pin Minecraft, NeoForge, mappings, Java, Gradle and adapter versions. Build and launch the client and an independently packaged dedicated server with `-Xverify:all`; exercise registration, spawn/save/load and presentation payload handling.

**Oracle:** No linkage, verifier or registration failures. Dedicated-server execution never resolves forbidden client rendering/audio classes. Metadata and protocol negotiation expose supported capabilities. The audited JAR has no embedded secrets, accidental native audio backend or conflicting logging implementation. Record the exact dependency and launch artefact versions in CI evidence.

### AT-28 — Versioned Fabric test-support spike

**Requirements:** R-05, R-06, R-11. **Priority:** P1; mandatory before claiming Fabric support. **Layer:** Adapter capability spike.

Pin candidate Minecraft 1.21.1, Fabric Loader, Fabric API, Loom, mappings and Java versions. Prove which server/client automated test facilities actually compile and run for those versions; exercise a minimal launch, entity lifecycle and follow/stop fixture.

**Oracle:** Commit a reproducible capability report, exact versions and invocation with retained results. Do not infer 1.21.1 GameTest/client-test availability from current documentation. If a facility is unavailable, identify and run an alternative harness and keep required manual gates explicit. Fabric parity is claimed only after the shared contracts pass through its real adapter; the spike itself is not parity certification.

### AT-29 — Concurrent actors and shared world resources

**Requirements:** R-04, R-05, R-06, R-10, R-12. **Priority:** P0. **Layer:** Multi-owner server harness.

Race two owners for one block/item, two movement jobs for one companion, and several pending calls in one turn. Deliver async acknowledgements in different orders.

**Oracle:** Per-player actors do not imply global resource ownership. Final server-thread validation and the specified conflict policy prevent double consumption or unauthorised control. Each call retains its own result identity. Job replacement/cancellation follows explicit rules. Unspecified remote call ordering is not treated as a dependency guarantee, and no execution accesses a world snapshot as live authority.

### AT-30 — Release world tests and Direwolf20 compatibility

**Requirements:** R-01–R-13. **Priority:** P0 for NeoForge release; P1 for Fabric parity. **Layer:** Real loader and modpack release gates.

Run the supported world-action suite through each actual loader's proven test harness, then perform integrated-server and dedicated-server sessions. For the primary release, use a pinned, authorised installation of the target Direwolf20 pack and retain its version/manifest evidence. Exercise restart, offline operation, speech devices and representative allowed/protected modded interactions through a predeclared burn-in duration.

**Oracle:** Follow changes measured position; stop meets its tick bound; mine/place/interact preserve actual inventory/world semantics. Restarts do not duplicate effects or companions. Real audio plays with configured volume/device behaviour. There are no new fatal errors, registry conflicts or unbounded memory/queue/tick growth. Publish the measured duration, load, exclusions and exact versions; a successful vanilla/fake harness does not count as modpack compatibility evidence.

## CI and release gates

| Gate | Required evidence | Blocking rule |
| --- | --- | --- |
| Core/transport CI | AT-01–AT-13, AT-20–AT-26 and core portions of AT-29; fixed-clock traces and crash images | Any applicable P0 failure blocks merge/release. |
| NeoForge adapter CI | Java 21 pinned build; AT-14–AT-19, AT-27, adapter portions of AT-29; demonstrated test harness | Any applicable P0 failure blocks the NeoForge candidate. |
| Fabric readiness | AT-28 capability report first, then the same applicable contract/action suite and isolated launches | An unproven harness or incomplete parity blocks a Fabric support claim. |
| Client/audio gate | AT-20–AT-22 simulator results plus declared operating-system/device playback checks | A synthesis-only success is insufficient to claim working speech. |
| Compatibility/release gate | AT-30, migration/rollback evidence, dependency/JAR audit and versioned diagnostics | Missing modpack evidence blocks a Direwolf20 compatibility claim. |

Every retained result names the build commit, pinned dependency/fixture versions, test ID, seed, expected outcome and observed outcome. CI must fail on timeout and preserve fault traces. Live API compatibility checks, if later authorised, are a separate credentialed gate against the pinned contract; they do not replace failure injection. Do not mark planned, skipped, unavailable or manual tests as passing automated coverage.
