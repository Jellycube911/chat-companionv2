package dev.chatcompanion.core;

import com.google.gson.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.function.Supplier;

/** Serial owner of durable companion decisions. Minecraft objects never enter this actor. */
public final class CompanionActor implements AutoCloseable {
    private static final Gson GSON = new GsonBuilder().serializeNulls().create();
    private static final Set<String> TOOLS = Set.of("get_companion_status", "inspect_nearby", "follow_player", "move_to",
            "stop_action", "mine_block", "place_block", "interact_with_block", "collect_items", "attack_entity", "start_task",
            "get_task_status", "cancel_task");
    private static final Set<String> READ_ONLY = Set.of("get_companion_status", "inspect_nearby", "get_task_status");
    private final SessionKey context;
    private final UUID runtimeEpoch;
    private final DurableJournal journal;
    private final GameBridge bridge;
    private final ExecutorService mailbox = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "chat-companion-actor"); thread.setDaemon(true); return thread;
    });
    // This atomic safety fence is intentionally updated synchronously, outside queued persistence.
    private final AtomicLong generation = new AtomicLong();
    private final AtomicBoolean closing = new AtomicBoolean();
    private final LinkedHashMap<UUID, MessageRecord> messages = new LinkedHashMap<>();
    private final Map<String, Long> intents = new HashMap<>();
    private final Map<ToolCallKey, ToolEntry> tools = new LinkedHashMap<>();
    private final Map<ToolCallKey, CompletableFuture<ToolEntry>> inFlight = new HashMap<>();
    private final Map<String, ToolCallKey> intentOwners = new HashMap<>();
    private final Map<String, JsonObject> protocol = new LinkedHashMap<>();
    private final Map<String, OutputRecord> outputs = new LinkedHashMap<>();
    private final Map<String, CompletableFuture<Void>> delivering = new HashMap<>();
    private final Map<String, String> deliveringText = new HashMap<>();
    private final CompletableFuture<Void> started = new CompletableFuture<>();
    private final AtomicBoolean startRequested = new AtomicBoolean();
    private boolean storageHealthy = true;
    private String storageProblem;
    private String sessionId;
    private long messageSequence;
    private int pendingEnqueues;

    public CompanionActor(SessionKey context, UUID runtimeEpoch, DurableJournal journal, GameBridge bridge) {
        this.context = Objects.requireNonNull(context); this.runtimeEpoch = Objects.requireNonNull(runtimeEpoch);
        this.journal = Objects.requireNonNull(journal); this.bridge = Objects.requireNonNull(bridge);
    }
    public UUID runtimeEpoch() { return runtimeEpoch; }
    public long controlGeneration() { return generation.get(); }
    public CompletableFuture<Long> intentGeneration(String intentId) { return ask(() -> intents.getOrDefault(intentId, -1L)); }

    public CompletableFuture<Void> start() {
        if (!startRequested.compareAndSet(false, true)) return started;
        journal.load().whenComplete((replay, failure) -> dispatch(() -> {
            if (failure != null) { unhealthy("storage_load_failed"); started.completeExceptionally(failure); return; }
            try {
                storageHealthy = replay.healthy(); storageProblem = replay.problem();
                for (JournalRecord record : replay.records()) {
                    if (!record.aggregateId().equals(context.storageKey())) throw new IllegalStateException("context_mismatch");
                    apply(record.type(), record.payload());
                }
                List<CompletableFuture<?>> repairs = new ArrayList<>();
                for (ToolEntry tool : List.copyOf(tools.values())) {
                    if (tool.executionState() == ExecutionState.ADMITTED) {
                        ToolEntry uncertain = tool.executed(ExecutionState.UNKNOWN, ActionOutcome.unknown("restart_after_admission"));
                        tools.put(tool.key(), uncertain);
                        if (storageHealthy) repairs.add(persist("TOOL", json(uncertain)).thenRun(() -> {}));
                    } else if (tool.executionState() == ExecutionState.OBSERVED && tool.outcome() != null && tool.outcome().success()
                            && !READ_ONLY.contains(tool.name()) && !Set.of("stop_action", "cancel_task").contains(tool.name())) {
                        repairs.add(reconcileRecovered(tool));
                    }
                }
                CompletableFuture.allOf(repairs.toArray(CompletableFuture[]::new)).whenComplete((unused, repairFailure) -> dispatch(() -> {
                    if (repairFailure != null) unhealthy("recovery_persistence_failed");
                    started.complete(null);
                }));
            } catch (Throwable invalid) { unhealthy("invalid_replay:" + invalid.getMessage()); started.complete(null); }
        }));
        return started;
    }

    private CompletableFuture<Void> reconcileRecovered(ToolEntry recorded) {
        CompletableFuture<Void> result = new CompletableFuture<>();
        try {
            bridge.reconcile(new ActionRequest(recorded.key(), recorded.name(), recorded.arguments(), runtimeEpoch,
                    recorded.controlGeneration()), recorded.outcome()).whenComplete((verification, failure) -> dispatch(() -> {
                if (failure == null && verification != null && verification.success()) { result.complete(null); return; }
                // Retain the immutable historical result, even if its delivery was already confirmed.
                ToolEntry uncertain = recorded.executed(ExecutionState.UNKNOWN, recorded.outcome());
                mergeTool(uncertain);
                JsonObject incident = new JsonObject(); incident.addProperty("call_key", recorded.key().storageKey());
                incident.addProperty("reason", verification == null ? "restored_world_unverifiable" : verification.reasonCode());
                incident.addProperty("historical_result_preserved", true);
                String key = "world/discrepancy/" + CanonicalJson.hash(recorded.key().storageKey());
                protocol.put(key, incident);
                if (!storageHealthy) { result.complete(null); return; }
                JsonObject envelope = new JsonObject(); envelope.addProperty("key", key); envelope.add("value", incident);
                persist("TOOL", json(uncertain)).thenCompose(unused -> persist("PROTOCOL", envelope))
                        .whenComplete((unused, saveFailure) -> dispatch(() -> {
                            if (saveFailure == null) result.complete(null); else result.completeExceptionally(saveFailure);
                        }));
            }));
        } catch (Throwable failure) { result.completeExceptionally(failure); }
        return result;
    }

    public CompletableFuture<MessageRecord> enqueue(String text, MessagePriority priority) {
        Objects.requireNonNull(text); Objects.requireNonNull(priority);
        if (text.isBlank() || text.codePointCount(0, text.length()) > 4096 || text.getBytes(StandardCharsets.UTF_8).length > 16384)
            return CompletableFuture.failedFuture(new IllegalArgumentException("message_size_or_empty"));
        return askAsync(() -> {
            requireReady(true);
            long count = messages.values().stream().filter(message -> !message.terminal()).count();
            if (count + pendingEnqueues >= 128) throw new IllegalStateException("input_queue_full");
            if (priority == MessagePriority.AUTONOMOUS) throw new IllegalStateException("autonomy_not_enabled");
            UUID id = UUID.randomUUID(); long intentGeneration = generation.get();
            MessageRecord record = new MessageRecord(id, UUID.randomUUID().toString(), ++messageSequence, text,
                    priority, MessageState.QUEUED, null, null);
            JsonObject payload = json(record); payload.addProperty("intent_generation", intentGeneration);
            pendingEnqueues++;
            CompletableFuture<MessageRecord> accepted = new CompletableFuture<>();
            persist("MESSAGE", payload).whenComplete((unused, failure) -> dispatch(() -> {
                pendingEnqueues--;
                if (failure != null) accepted.completeExceptionally(failure);
                else { messages.put(id, record); intents.put(id.toString(), intentGeneration); accepted.complete(record); }
            }));
            return accepted;
        });
    }

    public CompletableFuture<Void> markMessage(UUID id, MessageState state, String remoteSession, String turnId) {
        return askAsync(() -> {
            requireReady(true); MessageRecord old = Objects.requireNonNull(messages.get(id), "Unknown message");
            if (old.terminal() && old.state() != state) throw new IllegalStateException("terminal_message_transition");
            if (old.state() == state && Objects.equals(old.sessionId(), remoteSession) && Objects.equals(old.turnId(), turnId))
                return CompletableFuture.completedFuture(null);
            MessageRecord next = old.withState(state, remoteSession, turnId);
            JsonObject payload = json(next); payload.addProperty("intent_generation", intents.getOrDefault(id.toString(), -1L));
            return persist("MESSAGE", payload).thenCompose(unused -> ask(() -> { messages.put(id, next); return null; }));
        });
    }

    public CompletableFuture<ToolEntry> admitLocal(String name, JsonObject arguments) {
        Objects.requireNonNull(name); JsonObject copied = arguments.deepCopy();
        long captured = Set.of("follow_player", "move_to").contains(name) ? generation.incrementAndGet() : generation.get();
        String intent = UUID.randomUUID().toString(); copied.addProperty("intent_id", intent);
        return askAsync(() -> {
            requireReady(true); JsonObject payload = new JsonObject(); payload.addProperty("intent_id", intent);
            payload.addProperty("generation", captured);
            return persist("LOCAL_INTENT", payload).thenCompose(unused -> askAsync(() -> {
                intents.put(intent, captured);
                ToolCallKey key = new ToolCallKey("local:" + context.storageKey(), intent, intent);
                return beginAdmission(new ToolRequest(key, name, copied, captured));
            }));
        });
    }

    public CompletableFuture<ToolEntry> admit(ToolRequest request) {
        Objects.requireNonNull(request); return askAsync(() -> { requireReady(true); return beginAdmission(request); });
    }

    private CompletableFuture<ToolEntry> beginAdmission(ToolRequest request) {
        String hash = CanonicalJson.hash(request.arguments());
        ToolEntry previous = tools.get(request.key());
        if (previous != null && (!previous.name().equals(request.name()) || !previous.argumentsHash().equals(hash)))
            return CompletableFuture.failedFuture(new IllegalStateException("call_arguments_integrity_violation"));
        if (inFlight.containsKey(request.key())) return inFlight.get(request.key());
        if (previous != null && previous.executionState() != ExecutionState.PREPARED) return CompletableFuture.completedFuture(previous);
        ToolEntry prepared = previous == null ? new ToolEntry(request.key(), request.name(), request.arguments(), hash,
                request.controlGeneration(), ExecutionState.PREPARED, ResultDeliveryState.UNSUBMITTED, null) : previous;
        CompletableFuture<ToolEntry> result = new CompletableFuture<>();
        tools.put(request.key(), prepared); inFlight.put(request.key(), result);
        String requestIntent = intent(request.arguments());
        if (requestIntent != null && !READ_ONLY.contains(request.name())) intentOwners.putIfAbsent(requestIntent, request.key());
        CompletableFuture<Void> preparation = previous == null ? persist("TOOL", json(prepared)) : CompletableFuture.completedFuture(null);
        preparation.whenComplete((unused, failure) -> dispatch(() -> {
            if (failure != null) { finishException(request.key(), result, failure); return; }
            String invalid = invalidRequest(request);
            if (invalid != null) { saveOutcome(prepared, ActionOutcome.rejected(invalid), result); return; }
            // Reuse a durable deterministic command receipt rather than execute the same intent again.
            String intent = intent(request.arguments());
            ToolCallKey owner = intent == null ? null : intentOwners.get(intent);
            ToolEntry receipt = owner == null || owner.equals(request.key()) ? null : tools.get(owner);
            if (receipt != null) {
                if (!receipt.name().equals(request.name()) || !receipt.argumentsHash().equals(hash))
                    saveOutcome(prepared, ActionOutcome.rejected("intent_conflict"), result);
                else if (receipt.outcome() != null) saveOutcome(prepared, receipt.outcome(), result);
                else {
                    CompletableFuture<ToolEntry> pending = inFlight.get(receipt.key());
                    if (pending == null) saveOutcome(prepared, ActionOutcome.unknown("intent_outcome_unresolved"), result);
                    else pending.whenComplete((recorded, pendingFailure) -> dispatch(() -> saveOutcome(prepared,
                            pendingFailure == null && recorded.outcome() != null ? recorded.outcome()
                                    : ActionOutcome.unknown("intent_outcome_unresolved"), result)));
                }
                return;
            }
            ToolEntry admitted = prepared.executed(ExecutionState.ADMITTED, null);
            persist("TOOL", json(admitted)).whenComplete((ignored, admitFailure) -> dispatch(() -> {
                if (admitFailure != null) { finishException(request.key(), result, admitFailure); return; }
                tools.put(request.key(), admitted);
                String stale = invalidRequest(request);
                if (stale != null) { saveOutcome(admitted, ActionOutcome.rejected(stale), result); return; }
                try {
                    CompletionStage<ActionOutcome> execution = bridge.execute(new ActionRequest(request.key(), request.name(), request.arguments(),
                            runtimeEpoch, request.controlGeneration()));
                    execution.whenComplete((outcome, executeFailure) -> dispatch(() -> {
                        if (closing.get()) { finishException(request.key(), result, new IllegalStateException("runtime_closed")); return; }
                        saveOutcome(admitted, executeFailure == null && outcome != null ? outcome : ActionOutcome.unknown("execution_outcome_uncertain"), result);
                    }));
                } catch (Throwable executeFailure) { saveOutcome(admitted, ActionOutcome.unknown("execution_outcome_uncertain"), result); }
            }));
        }));
        return result;
    }

    private String invalidRequest(ToolRequest request) {
        if (closing.get()) return "runtime_closed";
        if (request.controlGeneration() != generation.get()) return "stale_control_generation";
        if (!TOOLS.contains(request.name())) return "unsupported_tool";
        if (CanonicalJson.encode(request.arguments()).getBytes(StandardCharsets.UTF_8).length > 65536) return "arguments_too_large";
        if (!READ_ONLY.contains(request.name())) {
            String intent = intent(request.arguments()); Long authorized = intent == null ? null : intents.get(intent);
            if (authorized == null || authorized != request.controlGeneration()) return "unauthorized_intent";
        }
        return null;
    }

    private static String intent(JsonObject args) {
        try { return args.has("intent_id") ? args.get("intent_id").getAsString() : null; }
        catch (RuntimeException invalid) { return null; }
    }

    private void saveOutcome(ToolEntry entry, ActionOutcome outcome, CompletableFuture<ToolEntry> result) {
        ExecutionState state = outcome.status().equals("unknown") ? ExecutionState.UNKNOWN
                : outcome.success() ? ExecutionState.OBSERVED : ExecutionState.REJECTED;
        ToolEntry completed = entry.executed(state, outcome);
        persist("TOOL", json(completed)).whenComplete((unused, failure) -> dispatch(() -> {
            if (failure != null) {
                tools.put(entry.key(), entry.executed(ExecutionState.UNKNOWN, null)); finishException(entry.key(), result, failure);
            } else { mergeTool(completed); inFlight.remove(entry.key()); result.complete(tools.get(entry.key())); }
        }));
    }
    private void finishException(ToolCallKey key, CompletableFuture<ToolEntry> result, Throwable failure) {
        inFlight.remove(key); result.completeExceptionally(failure);
    }

    public CompletableFuture<Void> markResultAccepted(ToolCallKey key, int httpStatus) {
        if (httpStatus != 202) return CompletableFuture.failedFuture(new IllegalArgumentException("events_acceptance_requires_202"));
        return updateDelivery(key, ResultDeliveryState.RESULT_ACCEPTED);
    }
    /** Caller must provide canonical saved-result proof before calling this method. */
    public CompletableFuture<Void> confirmResult(ToolCallKey key) { return updateDelivery(key, ResultDeliveryState.CONFIRMED); }
    private CompletableFuture<Void> updateDelivery(ToolCallKey key, ResultDeliveryState delivery) {
        return askAsync(() -> {
            requireReady(true); ToolEntry old = Objects.requireNonNull(tools.get(key), "Unknown call");
            if (old.outcome() == null) throw new IllegalStateException("no_durable_result");
            if (old.resultDeliveryState().ordinal() >= delivery.ordinal()) return CompletableFuture.completedFuture(null);
            ToolEntry next = old.delivered(delivery);
            return persist("TOOL", json(next)).thenCompose(unused -> ask(() -> { mergeTool(next); return null; }));
        });
    }
    private void mergeTool(ToolEntry next) {
        ToolEntry old = tools.get(next.key());
        if (old != null) {
            if (!old.name().equals(next.name()) || !old.argumentsHash().equals(next.argumentsHash()))
                throw new IllegalStateException("call_arguments_integrity_violation");
            if (old.outcome() != null && next.outcome() != null
                    && !CanonicalJson.encode(old.outcome().toJson()).equals(CanonicalJson.encode(next.outcome().toJson())))
                throw new IllegalStateException("immutable_result_integrity_violation");
            if (old.resultDeliveryState().ordinal() > next.resultDeliveryState().ordinal()) next = next.delivered(old.resultDeliveryState());
        }
        tools.put(next.key(), next);
    }

    /** Fence synchronously; storage errors affect durability, never the already-applied safety control. */
    public CompletableFuture<StopReceipt> stop() {
        long captured = generation.incrementAndGet();
        return askAsync(() -> {
            JsonObject args = new JsonObject(); args.addProperty("reason", "owner_stop");
            ToolCallKey key = new ToolCallKey("local:" + context.storageKey(), "stop", UUID.randomUUID().toString());
            try { bridge.execute(new ActionRequest(key, "stop_action", args, runtimeEpoch, captured)); }
            catch (Throwable ignored) { /* Loader safety lane already stops locally; fence remains authoritative. */ }
            JsonObject payload = new JsonObject(); payload.addProperty("generation", captured);
            CompletableFuture<StopReceipt> receipt = new CompletableFuture<>();
            persist("CONTROL", payload).whenComplete((unused, failure) -> dispatch(() ->
                    receipt.complete(new StopReceipt(captured, failure == null, failure == null ? "stopped" : "stopped_persistence_uncertain"))));
            return receipt;
        });
    }

    public CompletableFuture<Void> saveRemoteSessionId(String remoteId) {
        Objects.requireNonNull(remoteId); return askAsync(() -> {
            requireReady(true); JsonObject payload = new JsonObject(); payload.addProperty("session_id", remoteId);
            return persist("SESSION", payload).thenCompose(unused -> ask(() -> { sessionId = remoteId; return null; }));
        });
    }
    public CompletableFuture<Void> putProtocolRecord(String key, JsonObject value) {
        Objects.requireNonNull(key); JsonObject copy = value.deepCopy(); return askAsync(() -> {
            requireReady(true); JsonObject payload = new JsonObject(); payload.addProperty("key", key); payload.add("value", copy);
            return persist("PROTOCOL", payload).thenCompose(unused -> ask(() -> { protocol.put(key, copy); return null; }));
        });
    }
    public CompletableFuture<Void> deliverOutput(String itemId, String text) {
        Objects.requireNonNull(itemId); Objects.requireNonNull(text); return askAsync(() -> {
            requireReady(true);
            OutputRecord previous = outputs.get(itemId);
            if (previous != null && !previous.text().equals(text)) throw new IllegalStateException("output_identity_integrity_violation");
            if (deliveringText.containsKey(itemId) && !deliveringText.get(itemId).equals(text))
                throw new IllegalStateException("output_identity_integrity_violation");
            if (previous != null && previous.state() == OutputDeliveryState.DISPLAYED) return CompletableFuture.completedFuture(null);
            if (delivering.containsKey(itemId)) return delivering.get(itemId);
            OutputRecord intended = previous == null ? new OutputRecord(itemId, text, OutputDeliveryState.INTENDED) : previous;
            CompletableFuture<Void> result = new CompletableFuture<>(); delivering.put(itemId, result); deliveringText.put(itemId, text);
            CompletableFuture<Void> intention = previous == null ? persist("OUTPUT", json(intended)) : CompletableFuture.completedFuture(null);
            intention.whenComplete((unused, failure) -> dispatch(() -> {
                if (failure != null) { delivering.remove(itemId); deliveringText.remove(itemId); result.completeExceptionally(failure); return; }
                outputs.put(itemId, intended);
                try {
                    bridge.deliver(itemId, text);
                    OutputRecord sent = new OutputRecord(itemId, text, OutputDeliveryState.SENT);
                    persist("OUTPUT", json(sent)).whenComplete((ignored, sentFailure) -> dispatch(() -> {
                        if (sentFailure == null) mergeOutput(sent);
                        delivering.remove(itemId); deliveringText.remove(itemId);
                        if (sentFailure == null) result.complete(null); else result.completeExceptionally(sentFailure);
                    }));
                } catch (Throwable deliveryFailure) { delivering.remove(itemId); deliveringText.remove(itemId); result.completeExceptionally(deliveryFailure); }
            }));
            return result;
        });
    }
    public CompletableFuture<Void> acknowledgeOutput(String itemId, OutputDeliveryState state) {
        if (state != OutputDeliveryState.RECEIVED && state != OutputDeliveryState.DISPLAYED)
            return CompletableFuture.failedFuture(new IllegalArgumentException("client_ack_requires_received_or_displayed"));
        return askAsync(() -> {
            requireReady(true); OutputRecord previous = Objects.requireNonNull(outputs.get(itemId), "Unknown output");
            if (previous.state().ordinal() >= state.ordinal()) return CompletableFuture.completedFuture(null);
            OutputRecord next = new OutputRecord(itemId, previous.text(), state);
            return persist("OUTPUT", json(next)).thenCompose(unused -> ask(() -> { mergeOutput(next); return null; }));
        });
    }
    private void mergeOutput(OutputRecord next) {
        OutputRecord old = outputs.get(next.itemId());
        if (old == null || next.state().ordinal() >= old.state().ordinal()) outputs.put(next.itemId(), next);
    }
    public CompletableFuture<ActorSnapshot> snapshot() { return ask(() -> new ActorSnapshot(context, runtimeEpoch, generation.get(), sessionId,
            storageHealthy, storageProblem, new ArrayList<>(messages.values()), new ArrayList<>(tools.values()), new ArrayList<>(outputs.values()), protocol)); }

    private CompletableFuture<Void> persist(String type, JsonObject payload) {
        if (!storageHealthy) return CompletableFuture.failedFuture(new IllegalStateException("storage_read_only:" + storageProblem));
        CompletableFuture<Void> result = new CompletableFuture<>();
        journal.append(context.storageKey(), type, payload).whenComplete((record, failure) -> dispatch(() -> {
            if (failure == null) result.complete(null);
            else { unhealthy("storage_append_failed"); result.completeExceptionally(failure); }
        }));
        return result;
    }
    private void apply(String type, JsonObject payload) {
        switch (type) {
            case "MESSAGE" -> {
                MessageRecord record = GSON.fromJson(payload, MessageRecord.class); messages.put(record.id(), record);
                messageSequence = Math.max(messageSequence, record.sequence());
                intents.put(record.id().toString(), payload.get("intent_generation").getAsLong());
            }
            case "TOOL" -> {
                ToolEntry entry = GSON.fromJson(payload, ToolEntry.class); mergeTool(entry);
                String intent = intent(entry.arguments());
                if (intent != null && !READ_ONLY.contains(entry.name())) intentOwners.putIfAbsent(intent, entry.key());
            }
            case "LOCAL_INTENT" -> {
                long value = payload.get("generation").getAsLong(); generation.accumulateAndGet(value, Math::max);
                intents.put(payload.get("intent_id").getAsString(), value);
            }
            case "CONTROL" -> generation.accumulateAndGet(payload.get("generation").getAsLong(), Math::max);
            case "SESSION" -> sessionId = payload.get("session_id").getAsString();
            case "PROTOCOL" -> protocol.put(payload.get("key").getAsString(), payload.getAsJsonObject("value").deepCopy());
            case "OUTPUT" -> mergeOutput(GSON.fromJson(payload, OutputRecord.class));
            default -> throw new IllegalStateException("unsupported_event:" + type);
        }
    }
    private static JsonObject json(Object object) { return GSON.toJsonTree(object).getAsJsonObject(); }
    private void unhealthy(String problem) { storageHealthy = false; storageProblem = problem; }
    private void requireReady(boolean write) {
        if (!started.isDone()) throw new IllegalStateException("actor_not_started");
        if (closing.get()) throw new IllegalStateException("runtime_closed");
        if (write && !storageHealthy) throw new IllegalStateException("storage_read_only:" + storageProblem);
    }
    private void dispatch(Runnable task) {
        try { mailbox.execute(task); } catch (RejectedExecutionException ignored) { /* Closed runtime callbacks are fenced. */ }
    }
    private <T> CompletableFuture<T> ask(Supplier<T> action) {
        CompletableFuture<T> result = new CompletableFuture<>();
        if (mailbox.isShutdown()) return CompletableFuture.failedFuture(new IllegalStateException("actor_closed"));
        dispatch(() -> { try { result.complete(action.get()); } catch (Throwable failure) { result.completeExceptionally(failure); } });
        return result;
    }
    private <T> CompletableFuture<T> askAsync(Supplier<? extends CompletionStage<T>> action) {
        return ask(action).thenCompose(stage -> stage.toCompletableFuture());
    }
    public CompletableFuture<Void> closeAsync() {
        if (!closing.compareAndSet(false, true)) return CompletableFuture.completedFuture(null);
        generation.incrementAndGet();
        return askAsync(() -> {
            inFlight.values().forEach(future -> future.completeExceptionally(new IllegalStateException("runtime_closed")));
            inFlight.clear();
            return journal.closeAsync();
        }).whenComplete((unused, failure) -> mailbox.shutdown());
    }
    @Override public void close() { closeAsync(); }
}
