package dev.chatcompanion.core.agent;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.chatcompanion.core.ActorSnapshot;
import dev.chatcompanion.core.CompanionActor;
import dev.chatcompanion.core.MessagePriority;
import dev.chatcompanion.core.MessageRecord;
import dev.chatcompanion.core.MessageState;
import dev.chatcompanion.core.ToolCallKey;
import dev.chatcompanion.core.ToolEntry;
import dev.chatcompanion.core.ToolRequest;
import dev.chatcompanion.core.api.AgentsClient;
import dev.chatcompanion.core.api.AgentsClient.AgentEvent;
import dev.chatcompanion.core.api.AgentsClient.FunctionCall;
import dev.chatcompanion.core.api.AgentsClient.RemoteItem;
import dev.chatcompanion.core.api.AgentsClient.RemoteSession;
import dev.chatcompanion.core.api.AgentsClient.SessionStatus;
import dev.chatcompanion.core.api.AgentsClient.StreamSubscription;
import dev.chatcompanion.core.api.AgentsClient.ToolResult;
import dev.chatcompanion.core.api.AgentsException;
import dev.chatcompanion.core.api.SavedResultProof;

import java.time.Duration;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.Comparator;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

/**
 * Durable remote workflow coordinator. Its mailbox owns all remote lifecycle decisions; the actor
 * owns durable local state and is the only path to game execution. Stream events are hints, never
 * an execution queue. No HTTP callback touches a live world or directly mutates actor state.
 */
public final class ChatSessionService implements AutoCloseable {
    private static final String CREATE = "agents/create";
    private static final String ACTIVE = "agents/active_submission";
    private static final String STATUS = "agents/status";
    private static final String BLOCKED = "agents/remote_blocked";
    private static final String STREAM = "agents/stream";
    private static final String TURN_PREFIX = "agents/turn/";
    private static final int MAX_ATTEMPTS = 4;
    private static final Duration MAX_DELAY = Duration.ofSeconds(30);

    private final CompanionActor actor;
    private final AgentsClient client;
    private final String model;
    private final String instructions;
    private final List<String> toolsJson;
    private final ScheduledExecutorService mailbox;
    private final Set<CompletableFuture<?>> pendingAnswers = ConcurrentHashMap.newKeySet();
    private CompletableFuture<Void> tail = CompletableFuture.completedFuture(null);
    private CompletableFuture<Void> startFuture;
    private volatile StreamSubscription subscription;
    private String subscribedSession;
    private long subscriptionEpoch;
    private int automaticRecoveryAttempts;
    private boolean recoveryScheduled;
    private boolean pollingOnly;
    private boolean pollScheduled;
    private volatile boolean closed;

    public ChatSessionService(CompanionActor actor, AgentsClient client, String model,
                              String instructions, List<String> toolsJson) {
        this.actor = Objects.requireNonNull(actor);
        this.client = Objects.requireNonNull(client);
        this.model = Objects.requireNonNull(model);
        this.instructions = Objects.requireNonNull(instructions);
        this.toolsJson = List.copyOf(toolsJson);
        if (model.isBlank()) throw new IllegalArgumentException("Model required");
        mailbox = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "chat-companion-session");
            thread.setDaemon(true);
            return thread;
        });
    }

    /** Idempotently load the actor and reconcile an existing session before sending new input. */
    public synchronized CompletableFuture<Void> start() {
        if (startFuture == null) startFuture = serial(() -> actor.start()
                .thenComposeAsync(ignored -> cycleWithDiagnostics(), mailbox));
        return startFuture;
    }

    /** Acceptance means the message is durable; an API outage leaves it visibly queued. */
    public CompletableFuture<MessageRecord> enqueue(String text) {
        Objects.requireNonNull(text);
        if (closed) return closedFailure();
        // Local durability must not queue behind a slow HTTP exchange on the remote workflow tail.
        return actor.start().thenCompose(ignored -> closed ? ChatSessionService.<MessageRecord>closedFailure()
                : actor.enqueue(text, MessagePriority.USER_CHAT)).thenApply(message -> {
            if (!closed) {
                try { mailbox.execute(() -> {
                    if (closed) return;
                    automaticRecoveryAttempts = 0;
                    scheduleRecovery(Duration.ZERO);
                }); } catch (RejectedExecutionException ignored) { /* Accepted input remains durable. */ }
            }
            return message;
        });
    }

    public CompletableFuture<Void> reconcile() {
        // An explicit reconciliation is the operator's retry after repairing a permanent failure.
        return serial(() -> {
            pollingOnly = false;
            JsonObject enabled = new JsonObject();
            enabled.addProperty("enabled", false);
            return actor.putProtocolRecord(BLOCKED, enabled)
                    .thenComposeAsync(ignored -> cycleWithDiagnostics(), mailbox);
        });
    }

    private CompletableFuture<Void> cycleWithDiagnostics() {
        return cycle().<CompletableFuture<Void>>handleAsync((ignored, failure) -> {
            if (failure == null) return CompletableFuture.<Void>completedFuture(null);
            Throwable cause = unwrap(failure);
            CompletableFuture<Void> gate = CompletableFuture.completedFuture(null);
            if (permanent(cause)) {
                JsonObject disabled = new JsonObject();
                disabled.addProperty("enabled", true);
                disabled.addProperty("reason_code", code(cause));
                gate = actor.putProtocolRecord(BLOCKED, disabled);
            }
            return gate.thenComposeAsync(ignoredAgain -> diagnostic("DEGRADED", code(cause)), mailbox)
                    .<Void>handleAsync((saved, recordFailure) -> {
                throw new CompletionException(cause);
            }, mailbox);
        }, mailbox).thenComposeAsync(stage -> stage, mailbox);
    }

    private CompletableFuture<Void> cycle() {
        if (closed) return closedFailure();
        return actor.snapshot().thenComposeAsync(snapshot -> {
            if (!snapshot.storageHealthy()) return fail("LOCAL_STORAGE_UNHEALTHY");
            JsonObject blocked = snapshot.protocolRecords().get(BLOCKED);
            if (blocked != null && blocked.has("enabled") && blocked.get("enabled").getAsBoolean())
                return diagnostic("DEGRADED", string(blocked, "reason_code"));
            String sessionId = snapshot.remoteSessionId();
            if (sessionId == null || sessionId.isBlank()) return createIfQueued(snapshot);
            return observe(sessionId).thenComposeAsync(ignored -> readSession(sessionId), mailbox)
                    .thenComposeAsync(remote -> reconcileRemote(remote), mailbox);
        }, mailbox);
    }

    private CompletableFuture<Void> createIfQueued(ActorSnapshot snapshot) {
        JsonObject previous = snapshot.protocolRecords().get(CREATE);
        if (previous != null && "CONFIRMED".equals(string(previous, "state"))
                && string(previous, "session_id") != null) return resumeCreatedSession(snapshot, previous);
        if (previous != null && Set.of("PREPARED", "UNKNOWN").contains(string(previous, "state")))
            return fail("CREATE_OUTCOME_UNKNOWN");
        MessageRecord first = snapshot.messages().stream()
                .filter(message -> message.state() == MessageState.QUEUED
                        || message.state() == MessageState.SUBMITTING)
                .sorted(Comparator.comparing((MessageRecord message) -> message.state() != MessageState.SUBMITTING)
                        .thenComparing(MessageRecord::priority).thenComparingLong(MessageRecord::sequence))
                .findFirst().orElse(null);
        if (first == null) return diagnostic("READY", "WAITING_FOR_INPUT");
        return actor.intentGeneration(first.id().toString()).thenComposeAsync(generation -> {
        if (generation < 0) return fail("MESSAGE_WITHOUT_DURABLE_INTENT");
        JsonObject scope = submission(first, generation, List.of());
        JsonObject operation = new JsonObject();
        operation.addProperty("operation_id", previous == null
                ? UUID.randomUUID().toString() : string(previous, "operation_id"));
        operation.addProperty("message_id", first.id().toString());
        operation.addProperty("state", "PREPARED");
        operation.addProperty("control_generation", generation);
        AgentsClient.SessionCreate request = new AgentsClient.SessionCreate(model, instructions,
                toolsJson, input(first, generation), Map.of(
                    "local_operation_id", string(operation, "operation_id"),
                    "local_message_id", first.id().toString()));
        return actor.putProtocolRecord(CREATE, operation)
                .thenComposeAsync(ignored -> actor.putProtocolRecord(ACTIVE, scope), mailbox)
                .thenComposeAsync(ignored -> actor.markMessage(first.id(), MessageState.SUBMITTING,
                        null, null), mailbox)
                .thenComposeAsync(ignored -> safeCreate(request, 0), mailbox)
                .<CompletableFuture<Void>>handleAsync((remote, failure) -> {
                    if (closed) return closedFailure();
                    if (failure != null) {
                        Throwable cause = unwrap(failure);
                        JsonObject failed = operation.deepCopy();
                        failed.addProperty("state", mayBeUnknown(cause) ? "UNKNOWN" : "FAILED");
                        failed.addProperty("error_code", code(cause));
                        return actor.putProtocolRecord(CREATE, failed)
                                .thenComposeAsync(ignored -> CompletableFuture.<Void>failedFuture(cause), mailbox);
                    }
                    if (remote.id() == null || remote.id().isBlank())
                        return fail("CREATE_RESPONSE_WITHOUT_SESSION_ID");
                    JsonObject confirmed = operation.deepCopy();
                    confirmed.addProperty("state", "CONFIRMED");
                    confirmed.addProperty("session_id", remote.id());
                    scope.addProperty("session_id", remote.id());
                    scope.addProperty("state", "ACCEPTED_REMOTE");
                    // Save the creation identity before publishing SESSION, so a crash cannot resubmit
                    // create's first input as an ordinary input event.
                    return actor.putProtocolRecord(CREATE, confirmed)
                            .thenComposeAsync(ignored -> actor.putProtocolRecord(ACTIVE, scope), mailbox)
                            .thenComposeAsync(ignored -> actor.markMessage(first.id(),
                                    MessageState.ACCEPTED_REMOTE, remote.id(), null), mailbox)
                            .thenComposeAsync(ignored -> actor.saveRemoteSessionId(remote.id()), mailbox)
                            .thenComposeAsync(ignored -> observe(remote.id()), mailbox)
                            .thenComposeAsync(ignored -> readSession(remote.id()), mailbox)
                            .thenComposeAsync(this::reconcileRemote, mailbox);
                }, mailbox).thenComposeAsync(stage -> stage, mailbox);
        }, mailbox);
    }

    private CompletableFuture<Void> resumeCreatedSession(ActorSnapshot snapshot, JsonObject operation) {
        String sessionId = string(operation, "session_id");
        String messageId = string(operation, "message_id");
        MessageRecord message = snapshot.messages().stream()
                .filter(value -> value.id().toString().equals(messageId)).findFirst().orElse(null);
        if (message == null) return fail("CREATE_WITHOUT_DURABLE_MESSAGE");
        JsonObject active = snapshot.protocolRecords().get(ACTIVE);
        if (active == null || !messageId.equals(string(active, "message_id")))
            return fail("CREATE_WITHOUT_DURABLE_SUBMISSION_SCOPE");
        JsonObject resumed = active.deepCopy();
        resumed.addProperty("session_id", sessionId);
        if (!terminal(string(resumed, "state"))) resumed.addProperty("state", "ACCEPTED_REMOTE");
        return actor.putProtocolRecord(ACTIVE, resumed)
                .thenComposeAsync(ignored -> message.terminal() ? CompletableFuture.completedFuture(null)
                        : actor.markMessage(message.id(), MessageState.ACCEPTED_REMOTE, sessionId, string(resumed, "turn_id")), mailbox)
                .thenComposeAsync(ignored -> actor.saveRemoteSessionId(sessionId), mailbox)
                .thenComposeAsync(ignored -> observe(sessionId), mailbox)
                .thenComposeAsync(ignored -> readSession(sessionId), mailbox)
                .thenComposeAsync(this::reconcileRemote, mailbox);
    }

    /** No idempotency contract exists for create: never retry uncertain creation. */
    private CompletableFuture<RemoteSession> safeCreate(AgentsClient.SessionCreate request, int attempt) {
        return invoke(() -> client.create(request)).handleAsync((remote, failure) -> {
            if (failure == null) return CompletableFuture.completedFuture(remote);
            Throwable cause = unwrap(failure);
            if (cause instanceof AgentsException exception && exception.kind() == AgentsException.Kind.RATE_LIMIT
                    && !permanent(exception) && attempt + 1 < MAX_ATTEMPTS) {
                Duration wait = retryDelay(exception, attempt);
                if (wait != null) return delay(wait).thenComposeAsync(ignored -> safeCreate(request, attempt + 1), mailbox);
            }
            return CompletableFuture.<RemoteSession>failedFuture(cause);
        }, mailbox).thenComposeAsync(stage -> stage, mailbox);
    }

    private CompletableFuture<Void> reconcileRemote(RemoteSession remote) {
        return readItems(remote.id()).thenComposeAsync(items -> actor.snapshot()
                .thenComposeAsync(snapshot -> discoverTurn(snapshot, items)
                        .thenComposeAsync(ignored -> confirmSavedResults(snapshot, items), mailbox)
                        .thenComposeAsync(ignored -> deliverSavedOutput(items), mailbox)
                        .thenComposeAsync(ignored -> processRequiredActions(remote), mailbox)
                        .thenComposeAsync(ignored -> verifyNoOrphanResults(remote), mailbox)
                        .thenComposeAsync(ignored -> resolveActiveTurn(remote), mailbox)
                        .thenComposeAsync(ignored -> maybeSchedulePoll(), mailbox), mailbox), mailbox);
    }

    private CompletableFuture<Void> verifyNoOrphanResults(RemoteSession remote) {
        return actor.snapshot().thenComposeAsync(snapshot -> {
            for (ToolEntry entry : snapshot.tools()) {
                if (!entry.key().sessionId().equals(remote.id()) || entry.outcome() == null
                        || "CONFIRMED".equals(entry.resultDeliveryState().name())) continue;
                boolean pending = remote.requiredActions().stream().anyMatch(action -> action instanceof FunctionCall call
                        && entry.key().turnId().equals(call.turnId()) && entry.key().callId().equals(call.callId()));
                if (!pending) return fail("RESULT_DELIVERY_UNCONFIRMED");
            }
            return CompletableFuture.completedFuture(null);
        }, mailbox);
    }

    private CompletableFuture<Void> deliverSavedOutput(List<RemoteItem> items) {
        CompletableFuture<Void> chain = CompletableFuture.completedFuture(null);
        for (RemoteItem item : items) {
            if (!item.completedFinalAnswer() || item.text() == null) continue;
            chain = chain.thenComposeAsync(ignored -> closed ? closedFailure()
                    : actor.deliverOutput(item.id(), item.text()), mailbox);
        }
        return chain;
    }

    private CompletableFuture<Void> confirmSavedResults(ActorSnapshot snapshot, List<RemoteItem> items) {
        CompletableFuture<Void> chain = CompletableFuture.completedFuture(null);
        for (ToolEntry entry : snapshot.tools()) {
            if (!Objects.equals(entry.key().sessionId(), snapshot.remoteSessionId()) || entry.outcome() == null
                    || "CONFIRMED".equals(entry.resultDeliveryState().name())) continue;
            ToolResult result = result(entry);
            if (items.stream().noneMatch(item -> SavedResultProof.matches(item, result))) continue;
            // Canonical proof can recover a lost HTTP response; never manufacture an observed 202.
            chain = chain.thenComposeAsync(ignored -> closed ? closedFailure() : actor.confirmResult(entry.key()), mailbox);
        }
        return chain;
    }

    private CompletableFuture<Void> processRequiredActions(RemoteSession remote) {
        if (remote.status() != SessionStatus.REQUIRES_ACTION) return CompletableFuture.completedFuture(null);
        CompletableFuture<Void> chain = CompletableFuture.completedFuture(null);
        for (AgentsClient.RequiredAction action : remote.requiredActions()) {
            if (!(action instanceof FunctionCall call)) {
                chain = chain.thenComposeAsync(ignored -> fail("UNSUPPORTED_REQUIRED_ACTION"), mailbox);
                continue;
            }
            chain = chain.thenComposeAsync(ignored -> scopeForTurn(call.turnId())
                    .thenComposeAsync(generation -> admitAndReturn(remote.id(), call, generation), mailbox), mailbox);
        }
        return chain;
    }

    private CompletableFuture<Void> admitAndReturn(String sessionId, FunctionCall call, long generation) {
        if (closed) return closedFailure();
        JsonObject arguments;
        try {
            JsonElement parsed = JsonParser.parseString(call.argumentsJson());
            if (!parsed.isJsonObject()) return fail("INVALID_FUNCTION_ARGUMENTS");
            arguments = parsed.getAsJsonObject();
        } catch (RuntimeException invalid) {
            return fail("INVALID_FUNCTION_ARGUMENTS");
        }
        ToolRequest request = new ToolRequest(new ToolCallKey(sessionId, call.turnId(), call.callId()),
                call.name(), arguments, generation);
        return actor.admit(request).thenComposeAsync(entry -> {
            if (entry.outcome() == null) return diagnostic("RECOVERING", "LOCAL_ACTION_PENDING");
            if ("CONFIRMED".equals(entry.resultDeliveryState().name())) return CompletableFuture.completedFuture(null);
            return submitResult(entry, 0);
        }, mailbox);
    }

    private CompletableFuture<Void> submitResult(ToolEntry entry, int attempt) {
        ToolResult recorded = result(entry);
        return invoke(() -> client.submitToolResult(entry.key().sessionId(), recorded))
                .handleAsync((acceptance, failure) -> {
                    if (closed) return ChatSessionService.<Void>closedFailure();
                    CompletableFuture<Void> accepted = failure == null
                            ? actor.markResultAccepted(entry.key(), acceptance.statusCode())
                            : CompletableFuture.completedFuture(null);
                    Throwable cause = failure == null ? null : unwrap(failure);
                    return accepted.thenComposeAsync(ignored -> readItems(entry.key().sessionId()), mailbox)
                            .thenComposeAsync(items -> {
                                if (items.stream().anyMatch(item -> SavedResultProof.matches(item, recorded)))
                                    return actor.confirmResult(entry.key());
                                return readSession(entry.key().sessionId()).thenComposeAsync(remote -> {
                                    boolean pending = remote.requiredActions().stream().anyMatch(action ->
                                            action instanceof FunctionCall call
                                                    && entry.key().turnId().equals(call.turnId())
                                                    && entry.key().callId().equals(call.callId()));
                                    if (!pending) return ChatSessionService.<Void>fail("RESULT_DELIVERY_UNCONFIRMED");
                                    if (cause != null && !retryable(cause))
                                        return CompletableFuture.<Void>failedFuture(cause);
                                    if (attempt + 1 >= MAX_ATTEMPTS) return fail("RESULT_RETRY_BUDGET_EXHAUSTED");
                                    Duration wait = retryDelay(cause, attempt);
                                    if (wait == null) return fail("RESULT_RETRY_DEADLINE_EXCEEDED");
                                    return delay(wait).thenComposeAsync(ignoredAgain -> submitResult(entry, attempt + 1), mailbox);
                                }, mailbox);
                            }, mailbox);
                }, mailbox).thenComposeAsync(stage -> stage, mailbox);
    }

    private CompletableFuture<Void> resolveActiveTurn(RemoteSession remote) {
        return actor.snapshot().thenComposeAsync(snapshot -> {
            JsonObject active = snapshot.protocolRecords().get(ACTIVE);
            String turnId = string(active, "turn_id");
            if (active != null && !terminal(string(active, "state"))) {
                if (turnId == null) {
                    if ("SUBMITTING".equals(string(active, "state"))) {
                        MessageRecord message = snapshot.messages().stream()
                                .filter(value -> value.id().toString().equals(string(active, "message_id")))
                                .findFirst().orElse(null);
                        if (message == null) return fail("SUBMISSION_WITHOUT_DURABLE_MESSAGE");
                        return submitInput(message, active, 0);
                    }
                    if (System.currentTimeMillis() - number(active, "created_at_ms") > 120_000L)
                        return fail("CANONICAL_TURN_RECONCILIATION_DEADLINE_EXCEEDED");
                    return diagnostic("RECOVERING", "AWAITING_CANONICAL_TURN_ID");
                }
                return read(() -> client.turn(remote.id(), turnId), 0).thenComposeAsync(turn -> {
                    if (!turn.terminal()) return diagnostic("READY", "REMOTE_TURN_ACTIVE");
                    MessageState state = switch (turn.status()) {
                        case COMPLETED -> MessageState.RESOLVED;
                        case CANCELLED -> MessageState.CANCELLED;
                        default -> MessageState.FAILED;
                    };
                    JsonObject completed = active.deepCopy();
                    completed.addProperty("state", state.name());
                    return actor.markMessage(UUID.fromString(string(active, "message_id")), state, remote.id(), turnId)
                            .thenComposeAsync(ignored -> actor.putProtocolRecord(ACTIVE, completed), mailbox)
                            .thenComposeAsync(ignored -> remote.status() == SessionStatus.IDLE
                                    ? drain(remote.id()) : diagnostic("READY", "AWAITING_REMOTE_IDLE"), mailbox);
                }, mailbox);
            }
            if (remote.status() == SessionStatus.FAILED) return fail("REMOTE_SESSION_FAILED");
            return remote.status() == SessionStatus.IDLE ? drain(remote.id())
                    : diagnostic("READY", "REMOTE_TURN_ACTIVE");
        }, mailbox);
    }

    private CompletableFuture<Void> drain(String sessionId) {
        return actor.snapshot().thenComposeAsync(snapshot -> {
            MessageRecord next = snapshot.messages().stream().filter(message -> message.state() == MessageState.QUEUED)
                    .sorted(Comparator.comparing(MessageRecord::priority).thenComparingLong(MessageRecord::sequence))
                    .findFirst().orElse(null);
            if (next == null) return diagnostic("READY", "IDLE");
            return actor.intentGeneration(next.id().toString()).thenComposeAsync(generation -> readItems(sessionId).thenComposeAsync(items -> {
                if (generation < 0) return fail("MESSAGE_WITHOUT_DURABLE_INTENT");
                JsonObject scope = submission(next, generation, items);
                scope.addProperty("session_id", sessionId);
                return actor.putProtocolRecord(ACTIVE, scope)
                        .thenComposeAsync(ignored -> actor.markMessage(next.id(), MessageState.SUBMITTING, sessionId, null), mailbox)
                        .thenComposeAsync(ignored -> submitInput(next, scope, 0), mailbox);
            }, mailbox), mailbox);
        }, mailbox);
    }

    private CompletableFuture<Void> submitInput(MessageRecord message, JsonObject scope, int attempt) {
        String sessionId = string(scope, "session_id");
        return invoke(() -> client.submitMessage(sessionId, input(message, number(scope, "control_generation")),
                        message.idempotencyKey()))
                .handleAsync((accepted, failure) -> {
                    if (closed) return ChatSessionService.<Void>closedFailure();
                    if (failure == null) {
                        JsonObject recorded = scope.deepCopy();
                        recorded.addProperty("state", "ACCEPTED_REMOTE");
                        recorded.addProperty("observed_http_status", accepted.statusCode());
                        return actor.markMessage(message.id(), MessageState.ACCEPTED_REMOTE, sessionId, null)
                                .thenComposeAsync(ignored -> actor.putProtocolRecord(ACTIVE, recorded), mailbox)
                                .thenComposeAsync(ignored -> diagnostic("READY", "INPUT_ACCEPTED"), mailbox);
                    }
                    Throwable cause = unwrap(failure);
                    return readSession(sessionId).thenComposeAsync(remote -> readItems(sessionId), mailbox)
                            .thenComposeAsync(items -> actor.snapshot().thenComposeAsync(snapshot -> discoverTurn(snapshot, items), mailbox), mailbox)
                            .thenComposeAsync(ignored -> actor.snapshot(), mailbox)
                            .thenComposeAsync(snapshot -> {
                                JsonObject recovered = snapshot.protocolRecords().get(ACTIVE);
                                if (string(recovered, "turn_id") != null) {
                                    JsonObject recorded = recovered.deepCopy();
                                    recorded.addProperty("state", "ACCEPTED_REMOTE");
                                    recorded.addProperty("acceptance_source", "canonical_turn");
                                    return actor.markMessage(message.id(), MessageState.ACCEPTED_REMOTE, sessionId,
                                                    string(recovered, "turn_id"))
                                            .thenComposeAsync(ignoredAgain -> actor.putProtocolRecord(ACTIVE, recorded), mailbox);
                                }
                                if (!retryable(cause) || attempt + 1 >= MAX_ATTEMPTS)
                                    return CompletableFuture.<Void>failedFuture(cause);
                                Duration wait = retryDelay(cause, attempt);
                                if (wait == null) return fail("INPUT_RETRY_DEADLINE_EXCEEDED");
                                // Same logical input and persisted idempotency key, including after a lost response.
                                return delay(wait).thenComposeAsync(ignoredAgain -> submitInput(message, scope, attempt + 1), mailbox);
                            }, mailbox);
                }, mailbox).thenComposeAsync(stage -> stage, mailbox);
    }

    private CompletableFuture<Void> discoverTurn(ActorSnapshot snapshot, List<RemoteItem> items) {
        JsonObject active = snapshot.protocolRecords().get(ACTIVE);
        if (active == null || terminal(string(active, "state")) || string(active, "turn_id") != null)
            return CompletableFuture.completedFuture(null);
        Set<String> baseline = baseline(active);
        Set<String> candidates = new LinkedHashSet<>();
        for (RemoteItem item : items)
            if (item.turnId() != null && !baseline.contains(item.turnId())) candidates.add(item.turnId());
        if (candidates.size() > 1) return fail("AMBIGUOUS_REMOTE_TURN");
        return candidates.isEmpty() ? CompletableFuture.completedFuture(null)
                : bindTurn(snapshot, candidates.iterator().next());
    }

    private CompletableFuture<Long> scopeForTurn(String turnId) {
        return actor.snapshot().thenComposeAsync(snapshot -> {
            JsonObject known = snapshot.protocolRecords().get(TURN_PREFIX + turnId);
            if (known != null) return CompletableFuture.completedFuture(number(known, "control_generation"));
            return bindTurn(snapshot, turnId).thenComposeAsync(ignored -> actor.snapshot(), mailbox)
                    .thenApplyAsync(updated -> {
                        JsonObject scope = updated.protocolRecords().get(TURN_PREFIX + turnId);
                        if (scope == null) throw protocol("TURN_WITHOUT_DURABLE_AUTHORIZATION_SCOPE");
                        return number(scope, "control_generation");
                    }, mailbox);
        }, mailbox);
    }

    private CompletableFuture<Void> bindTurn(ActorSnapshot snapshot, String turnId) {
        if (turnId == null || turnId.isBlank()) return fail("INVALID_REMOTE_TURN_ID");
        if (snapshot.protocolRecords().containsKey(TURN_PREFIX + turnId)) return CompletableFuture.completedFuture(null);
        JsonObject active = snapshot.protocolRecords().get(ACTIVE);
        if (active == null || terminal(string(active, "state")) || baseline(active).contains(turnId))
            return fail("TURN_WITHOUT_DURABLE_AUTHORIZATION_SCOPE");
        String previous = string(active, "turn_id");
        if (previous != null && !previous.equals(turnId)) return fail("AMBIGUOUS_REMOTE_TURN");
        JsonObject scope = active.deepCopy();
        scope.addProperty("turn_id", turnId);
        JsonObject turn = new JsonObject();
        turn.addProperty("message_id", string(active, "message_id"));
        turn.addProperty("control_generation", number(active, "control_generation"));
        turn.addProperty("session_id", string(active, "session_id"));
        return actor.putProtocolRecord(TURN_PREFIX + turnId, turn)
                .thenComposeAsync(ignored -> actor.putProtocolRecord(ACTIVE, scope), mailbox)
                .thenComposeAsync(ignored -> actor.markMessage(UUID.fromString(string(active, "message_id")),
                        MessageState.ACCEPTED_REMOTE, string(active, "session_id"), turnId), mailbox);
    }

    private CompletableFuture<Void> observe(String sessionId) {
        if (pollingOnly) return CompletableFuture.completedFuture(null);
        if (subscription != null && sessionId.equals(subscribedSession)) return CompletableFuture.completedFuture(null);
        if (subscription != null) subscription.close();
        subscription = null;
        long epoch = ++subscriptionEpoch;
        return read(() -> client.subscribe(sessionId, event -> onEvent(epoch, event)), 0)
                .<CompletableFuture<Void>>handleAsync((stream, failure) -> {
                    if (failure != null) {
                        Throwable cause = unwrap(failure);
                        if (!(cause instanceof AgentsException exception) || exception.kind() != AgentsException.Kind.CAPACITY)
                            return CompletableFuture.failedFuture(cause);
                        pollingOnly = true;
                        JsonObject status = new JsonObject();
                        status.addProperty("mode", "POLLING");
                        status.addProperty("reason_code", "STREAM_CAPACITY");
                        return actor.putProtocolRecord(STREAM, status);
                    }
                    if (closed) { stream.close(); return closedFailure(); }
                    subscription = stream;
                    subscribedSession = sessionId;
                    stream.completion().whenComplete((ignored, streamFailure) -> serial(() -> {
                        if (subscriptionEpoch != epoch || closed) return CompletableFuture.completedFuture(null);
                        subscription = null;
                        subscribedSession = null;
                        scheduleRecovery(Duration.ofMillis(250));
                        return diagnostic("RECOVERING", "STREAM_DISCONNECTED");
                    }));
                    JsonObject status = new JsonObject();
                    status.addProperty("mode", "STREAMING");
                    return actor.putProtocolRecord(STREAM, status);
                }, mailbox).thenComposeAsync(stage -> stage, mailbox);
    }

    private CompletableFuture<Void> maybeSchedulePoll() {
        if (!pollingOnly || pollScheduled || closed) return CompletableFuture.completedFuture(null);
        return actor.snapshot().thenAcceptAsync(snapshot -> {
            JsonObject active = snapshot.protocolRecords().get(ACTIVE);
            if (active == null || terminal(string(active, "state"))) return;
            pollScheduled = true;
            mailbox.schedule(() -> {
                pollScheduled = false;
                if (closed) return;
                serial(this::cycleWithDiagnostics).whenComplete((ignored, failure) -> {
                    if (failure != null && !closed) serial(() -> {
                        if (retryable(unwrap(failure))) scheduleRecovery(Duration.ofSeconds(1));
                        return CompletableFuture.completedFuture(null);
                    });
                });
            }, 2, TimeUnit.SECONDS);
        }, mailbox);
    }

    private void onEvent(long epoch, AgentEvent event) {
        if (event == null || closed) return;
        boolean relevant = event.turn() != null || event.session() != null
                || (event.item() != null && event.item().completedFinalAnswer());
        if (!relevant) return;
        serial(() -> {
            if (closed || epoch != subscriptionEpoch) return CompletableFuture.completedFuture(null);
            automaticRecoveryAttempts = 0;
            return actor.snapshot().thenComposeAsync(snapshot -> event.turn() != null
                            ? bindTurn(snapshot, event.turn().id()) : CompletableFuture.completedFuture(null), mailbox)
                    .thenComposeAsync(ignored -> cycleWithDiagnostics(), mailbox);
        });
    }

    private void scheduleRecovery(Duration wait) {
        if (closed || recoveryScheduled || automaticRecoveryAttempts >= MAX_ATTEMPTS) return;
        recoveryScheduled = true;
        automaticRecoveryAttempts++;
        mailbox.schedule(() -> {
            recoveryScheduled = false;
            serial(this::cycleWithDiagnostics).whenComplete((ignored, failure) -> {
                if (failure != null && !closed) serial(() -> {
                    if (retryable(unwrap(failure))) scheduleRecovery(Duration.ofSeconds(1));
                    return CompletableFuture.completedFuture(null);
                });
            });
        }, wait.toMillis(), TimeUnit.MILLISECONDS);
    }

    private CompletableFuture<RemoteSession> readSession(String id) { return read(() -> client.retrieve(id), 0); }
    private CompletableFuture<List<RemoteItem>> readItems(String id) { return read(() -> client.items(id), 0); }

    private <T> CompletableFuture<T> read(Supplier<CompletableFuture<T>> operation, int attempt) {
        if (closed) return closedFailure();
        return invoke(operation).handleAsync((value, failure) -> {
            if (failure == null) return CompletableFuture.completedFuture(value);
            Throwable cause = unwrap(failure);
            if (retryable(cause) && attempt + 1 < MAX_ATTEMPTS) {
                Duration wait = retryDelay(cause, attempt);
                if (wait != null) return delay(wait).thenComposeAsync(ignored -> read(operation, attempt + 1), mailbox);
            }
            return CompletableFuture.<T>failedFuture(cause);
        }, mailbox).thenComposeAsync(stage -> stage, mailbox);
    }

    private CompletableFuture<Void> delay(Duration wait) {
        CompletableFuture<Void> future = new CompletableFuture<>();
        if (closed) return closedFailure();
        mailbox.schedule(() -> {
            if (closed) future.completeExceptionally(protocol("SERVICE_CLOSED"));
            else future.complete(null);
        }, wait.toMillis(), TimeUnit.MILLISECONDS);
        return future;
    }

    private CompletableFuture<Void> diagnostic(String state, String reason) {
        JsonObject status = new JsonObject();
        status.addProperty("state", state);
        status.addProperty("reason_code", reason);
        return actor.putProtocolRecord(STATUS, status);
    }

    private <T> CompletableFuture<T> serial(Supplier<? extends CompletionStage<T>> operation) {
        CompletableFuture<T> answer = new CompletableFuture<>();
        if (closed) { answer.completeExceptionally(protocol("SERVICE_CLOSED")); return answer; }
        pendingAnswers.add(answer);
        answer.whenComplete((value, failure) -> pendingAnswers.remove(answer));
        try {
            mailbox.execute(() -> {
                CompletableFuture<T> task = tail.handleAsync((ignored, failure) -> null, mailbox)
                        .thenComposeAsync(ignored -> {
                            if (closed) return closedFailure();
                            try { return operation.get().toCompletableFuture(); }
                            catch (Throwable failure) { return CompletableFuture.failedFuture(failure); }
                        }, mailbox);
                tail = task.handleAsync((value, failure) -> {
                    if (failure == null) answer.complete(value);
                    else answer.completeExceptionally(unwrap(failure));
                    return null;
                }, mailbox);
            });
        } catch (RejectedExecutionException stopped) {
            answer.completeExceptionally(protocol("SERVICE_CLOSED"));
        }
        return answer;
    }

    private static <T> CompletableFuture<T> invoke(Supplier<CompletableFuture<T>> operation) {
        try { return operation.get(); }
        catch (Throwable failure) { return CompletableFuture.failedFuture(failure); }
    }

    private static JsonObject submission(MessageRecord message, long generation, List<RemoteItem> items) {
        JsonObject scope = new JsonObject();
        scope.addProperty("message_id", message.id().toString());
        scope.addProperty("control_generation", generation);
        scope.addProperty("state", "SUBMITTING");
        scope.addProperty("created_at_ms", System.currentTimeMillis());
        JsonArray baseline = new JsonArray();
        Set<String> turns = new LinkedHashSet<>();
        for (RemoteItem item : items) if (item.turnId() != null) turns.add(item.turnId());
        turns.forEach(baseline::add);
        scope.add("baseline_turns", baseline);
        return scope;
    }

    private static String input(MessageRecord message, long generation) {
        return "Server-issued action context: intent_id=" + message.id() + "; control_generation=" + generation
                + ". Actions must use this exact intent_id and remain within this user's request.\nUser message:\n" + message.text();
    }

    private static ToolResult result(ToolEntry entry) {
        String recorded = entry.outcome().toJson().toString();
        return entry.outcome().success()
                ? ToolResult.succeeded(entry.key().turnId(), entry.key().callId(), recorded)
                : ToolResult.failed(entry.key().turnId(), entry.key().callId(), recorded);
    }

    private static Set<String> baseline(JsonObject scope) {
        Set<String> values = new LinkedHashSet<>();
        JsonElement stored = scope.get("baseline_turns");
        if (stored != null && stored.isJsonArray())
            for (JsonElement element : stored.getAsJsonArray()) values.add(element.getAsString());
        return values;
    }

    private static String string(JsonObject value, String key) {
        if (value == null || !value.has(key) || value.get(key).isJsonNull()) return null;
        return value.get(key).getAsString();
    }

    private static long number(JsonObject value, String key) {
        if (value == null || !value.has(key)) throw protocol("MISSING_DURABLE_GENERATION");
        return value.get(key).getAsLong();
    }

    private static boolean terminal(String state) { return Set.of("RESOLVED", "FAILED", "CANCELLED").contains(state == null ? "" : state); }
    private static boolean mayBeUnknown(Throwable failure) {
        return !(failure instanceof AgentsException exception) || exception.outcomeMayBeUnknown()
                || exception.kind() == AgentsException.Kind.PROTOCOL || exception.kind() == AgentsException.Kind.CAPACITY;
    }
    private static boolean permanent(Throwable failure) {
        if (!(failure instanceof AgentsException exception)) return false;
        if (Set.of("insufficient_quota", "billing_hard_limit_reached", "billing_not_active", "usage_limit_exceeded",
                "incompatible_executor", "context_length_exceeded", "session_budget_exhausted")
                .contains(exception.errorCode() == null ? "" : exception.errorCode())) return true;
        return switch (exception.kind()) {
            case AUTHENTICATION, VALIDATION, NOT_FOUND, CREDENTIAL_REQUIRED, PROTOCOL -> true;
            default -> false;
        };
    }
    private static boolean retryable(Throwable failure) {
        if (!(failure instanceof AgentsException exception)) return false;
        if (permanent(exception)) return false;
        return switch (exception.kind()) {
            case CONFLICT, RATE_LIMIT, SERVICE, TIMEOUT, CONNECTION -> true;
            default -> false;
        };
    }
    private static Duration retryDelay(Throwable failure, int attempt) {
        if (failure instanceof AgentsException exception && exception.retryAfter() != null) {
            if (exception.retryAfter().compareTo(MAX_DELAY) > 0) return null;
            return exception.retryAfter().isNegative() ? Duration.ZERO : exception.retryAfter();
        }
        long base = Math.min(2_000L, 100L << attempt);
        return Duration.ofMillis(base + ThreadLocalRandom.current().nextLong(Math.max(1L, base / 4)));
    }
    private static Throwable unwrap(Throwable failure) {
        while ((failure instanceof CompletionException || failure instanceof java.util.concurrent.ExecutionException)
                && failure.getCause() != null) failure = failure.getCause();
        return failure;
    }
    private static String code(Throwable failure) {
        if (failure instanceof AgentsException exception)
            return exception.errorCode() != null ? exception.errorCode() : exception.kind().name();
        return "LOCAL_WORKFLOW_FAILURE";
    }
    private static AgentsException protocol(String code) {
        return new AgentsException(AgentsException.Kind.PROTOCOL, 0, null, code.toLowerCase(java.util.Locale.ROOT));
    }
    private static <T> CompletableFuture<T> fail(String code) { return CompletableFuture.failedFuture(protocol(code)); }
    private static <T> CompletableFuture<T> closedFailure() { return fail("SERVICE_CLOSED"); }

    /** Invalidates stream callbacks immediately. The actor and shared journal remain loader-owned. */
    @Override public void close() {
        if (closed) return;
        closed = true;
        pendingAnswers.forEach(answer -> answer.completeExceptionally(protocol("SERVICE_CLOSED")));
        if (subscription != null) subscription.close();
        client.close();
        mailbox.shutdownNow();
    }
}
