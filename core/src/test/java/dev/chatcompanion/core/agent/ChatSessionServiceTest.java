package dev.chatcompanion.core.agent;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;
import dev.chatcompanion.core.ActionOutcome;
import dev.chatcompanion.core.ActionRequest;
import dev.chatcompanion.core.CompanionActor;
import dev.chatcompanion.core.DurableJournal;
import dev.chatcompanion.core.ExecutionState;
import dev.chatcompanion.core.GameBridge;
import dev.chatcompanion.core.MessagePriority;
import dev.chatcompanion.core.MessageRecord;
import dev.chatcompanion.core.MessageState;
import dev.chatcompanion.core.OutputDeliveryState;
import dev.chatcompanion.core.ResultDeliveryState;
import dev.chatcompanion.core.SessionKey;
import dev.chatcompanion.core.api.AgentsClient;
import dev.chatcompanion.core.api.AgentsException;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;

import static org.junit.jupiter.api.Assertions.*;

class ChatSessionServiceTest {
    @TempDir Path directory;

    @Test
    void firstInputIdentityIsDurableBeforeCreateAndFinalOutputIsRecovered() throws Exception {
        try (Fixture fixture = fixture("first")) {
            MessageRecord message = fixture.actor.enqueue("Hello companion", MessagePriority.USER_CHAT).join();
            fixture.client.beforeCreate = request -> {
                var snapshot = fixture.actor.snapshot().join();
                assertEquals("PREPARED", snapshot.protocolRecords().get("agents/create").get("state").getAsString());
                assertEquals(MessageState.SUBMITTING, snapshot.messages().getFirst().state());
                assertEquals(message.id().toString(), request.metadata().get("local_message_id"));
            };
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertEquals(1, fixture.client.creates.get());
            assertEquals(MessageState.RESOLVED, fixture.actor.snapshot().join().messages().getFirst().state());
            assertTrue(fixture.client.lastCreate.firstInput().contains("intent_id=" + message.id()));
            assertEquals(List.of("answer-1:Hello from saved output"), fixture.bridge.outputs);
            fixture.actor.acknowledgeOutput("answer-1", OutputDeliveryState.DISPLAYED).join();
            fixture.service.reconcile().get(5, TimeUnit.SECONDS);
            assertEquals(1, fixture.bridge.outputs.size());
        }
    }

    @Test
    void historicalFunctionCallsAreNeverAnExecutionQueue() throws Exception {
        try (Fixture fixture = fixture("historic")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            fixture.client.items.add(new AgentsClient.RemoteItem("old-call", "function_call", "old-turn", "old-call",
                    "completed", null, null, null, null, null));
            fixture.client.remote = new AgentsClient.RemoteSession("session-1", AgentsClient.SessionStatus.IDLE, List.of(), null);
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertEquals(0, fixture.bridge.executions.get());
            assertEquals(0, fixture.client.results.size());
        }
    }

    @Test
    void requiredActionExecutesOnceAndObserved202NeedsMatchingSavedProof() throws Exception {
        try (Fixture fixture = fixture("action")) {
            fixture.client.actionOnCreate = true;
            fixture.actor.enqueue("Follow me", MessagePriority.USER_CHAT).join();
            fixture.service.start().get(5, TimeUnit.SECONDS);
            var entry = fixture.actor.snapshot().join().tools().getFirst();
            assertEquals(ExecutionState.OBSERVED, entry.executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, entry.resultDeliveryState());
            assertEquals(1, fixture.bridge.executions.get());
            assertEquals(1, fixture.client.results.size());
            assertTrue(Files.readString(fixture.path).contains("RESULT_ACCEPTED"));
            fixture.service.reconcile().get(5, TimeUnit.SECONDS);
            assertEquals(1, fixture.bridge.executions.get());
        }
    }

    @Test
    void savedErrorConfirmsLostResponseWithoutResolvingWorldUncertainty() throws Exception {
        try (Fixture fixture = fixture("lost-result")) {
            fixture.client.actionOnCreate = true;
            fixture.client.loseResultResponse = true;
            fixture.bridge.outcome = ActionOutcome.unknown("world_save_uncertain");
            fixture.actor.enqueue("Follow me", MessagePriority.USER_CHAT).join();
            fixture.service.start().get(5, TimeUnit.SECONDS);
            var entry = fixture.actor.snapshot().join().tools().getFirst();
            assertEquals(ExecutionState.UNKNOWN, entry.executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, entry.resultDeliveryState());
            assertEquals("world_save_uncertain", entry.outcome().reasonCode());
            assertFalse(Files.readString(fixture.path).contains("RESULT_ACCEPTED"), "lost HTTP 202 is never fabricated");
            assertEquals(1, fixture.bridge.executions.get());
            fixture.service.reconcile().get(5, TimeUnit.SECONDS);
            assertEquals(1, fixture.bridge.executions.get());
        }
    }

    @Test
    void disappearanceWithoutProofRemainsUnconfirmedAndVisible() throws Exception {
        try (Fixture fixture = fixture("no-proof")) {
            fixture.client.actionOnCreate = true;
            fixture.client.omitResultProof = true;
            fixture.actor.enqueue("Follow me", MessagePriority.USER_CHAT).join();
            assertThrows(ExecutionException.class, () -> fixture.service.start().get(5, TimeUnit.SECONDS));
            var snapshot = fixture.actor.snapshot().join();
            assertEquals(ResultDeliveryState.RESULT_ACCEPTED, snapshot.tools().getFirst().resultDeliveryState());
            assertEquals("DEGRADED", snapshot.protocolRecords().get("agents/status").get("state").getAsString());
            assertThrows(ExecutionException.class, () -> fixture.service.reconcile().get(5, TimeUnit.SECONDS));
            assertEquals(1, fixture.bridge.executions.get());
        }
    }

    @Test
    void queuedMessageCannotAcquireAnewGenerationAfterStop() throws Exception {
        try (Fixture fixture = fixture("stop")) {
            fixture.client.actionOnCreate = true;
            fixture.actor.enqueue("Follow me", MessagePriority.USER_CHAT).join();
            fixture.actor.stop().join();
            fixture.bridge.executions.set(0); // exclude the deterministic local safety stop
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertTrue(fixture.client.lastCreate.firstInput().contains("control_generation=0"));
            var entry = fixture.actor.snapshot().join().tools().getFirst();
            assertEquals("stale_control_generation", entry.outcome().reasonCode());
            assertEquals(0, fixture.bridge.executions.get());
        }
    }

    @Test
    void retriesInputWithItsOriginalIdempotencyKeyAndObserverAlreadyEstablished() throws Exception {
        try (Fixture fixture = fixture("input-retry")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            MessageRecord message = fixture.actor.enqueue("Tell me about this world", MessagePriority.USER_CHAT).join();
            fixture.client.remote = new AgentsClient.RemoteSession("session-1", AgentsClient.SessionStatus.IDLE, List.of(), null);
            fixture.client.failFirstInput = true;
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertEquals(List.of(message.idempotencyKey(), message.idempotencyKey()), fixture.client.inputKeys);
            assertEquals(fixture.client.inputs.getFirst(), fixture.client.inputs.getLast());
            assertTrue(fixture.client.observerEstablishedBeforeInput);
            assertEquals(MessageState.ACCEPTED_REMOTE, fixture.actor.snapshot().join().messages().getFirst().state());
            fixture.service.reconcile().get(5, TimeUnit.SECONDS);
            assertEquals(MessageState.RESOLVED, fixture.actor.snapshot().join().messages().getFirst().state());
        }
    }

    @Test
    void recoveredSubmittingMessageIsActuallySentWithPersistedScope() throws Exception {
        try (Fixture fixture = fixture("prepared-input")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            MessageRecord message = fixture.actor.enqueue("Hello", MessagePriority.USER_CHAT).join();
            fixture.actor.markMessage(message.id(), MessageState.SUBMITTING, "session-1", null).join();
            fixture.actor.putProtocolRecord("agents/active_submission", scope(message, "SUBMITTING")).join();
            fixture.client.remote = new AgentsClient.RemoteSession("session-1", AgentsClient.SessionStatus.IDLE, List.of(), null);
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertEquals(List.of(message.idempotencyKey()), fixture.client.inputKeys);
        }
    }

    @Test
    void uncertainCreateIsNotBlindlyRepeatedAfterRestart() throws Exception {
        Path path = directory.resolve("uncertain-create.jsonl");
        SessionKey key = key();
        try (Fixture first = new Fixture(path, key)) {
            first.actor.enqueue("Hello", MessagePriority.USER_CHAT).join();
            first.client.createFailure = new AgentsException(AgentsException.Kind.TIMEOUT, 0, null, null);
            assertThrows(ExecutionException.class, () -> first.service.start().get(5, TimeUnit.SECONDS));
            assertEquals("UNKNOWN", first.actor.snapshot().join().protocolRecords().get("agents/create").get("state").getAsString());
        }
        try (Fixture restored = new Fixture(path, key)) {
            assertThrows(ExecutionException.class, () -> restored.service.start().get(5, TimeUnit.SECONDS));
            assertEquals(0, restored.client.creates.get());
            assertEquals(MessageState.SUBMITTING, restored.actor.snapshot().join().messages().getFirst().state());
        }
    }

    @Test
    void creationReceiptRecoversCrashBeforeSessionIdentityPublication() throws Exception {
        try (Fixture fixture = fixture("create-receipt")) {
            MessageRecord message = fixture.actor.enqueue("Hello", MessagePriority.USER_CHAT).join();
            JsonObject operation = new JsonObject();
            operation.addProperty("state", "CONFIRMED");
            operation.addProperty("operation_id", "create-operation-1");
            operation.addProperty("session_id", "session-1");
            operation.addProperty("message_id", message.id().toString());
            fixture.actor.putProtocolRecord("agents/create", operation).join();
            fixture.actor.putProtocolRecord("agents/active_submission", scope(message, "SUBMITTING")).join();
            fixture.client.completeTurn("turn-1", "Hello from saved output");
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertEquals("session-1", fixture.actor.snapshot().join().remoteSessionId());
            assertEquals(0, fixture.client.creates.get());
            assertEquals(0, fixture.client.inputs.size());
        }
    }

    @Test
    void idleWithoutTurnEvidenceDoesNotResolveAcceptedMessage() throws Exception {
        try (Fixture fixture = fixture("idle")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            MessageRecord message = fixture.actor.enqueue("Hello", MessagePriority.USER_CHAT).join();
            fixture.actor.markMessage(message.id(), MessageState.ACCEPTED_REMOTE, "session-1", null).join();
            fixture.actor.putProtocolRecord("agents/active_submission", scope(message, "ACCEPTED_REMOTE")).join();
            fixture.client.remote = new AgentsClient.RemoteSession("session-1", AgentsClient.SessionStatus.IDLE, List.of(), null);
            fixture.service.start().get(5, TimeUnit.SECONDS);
            assertEquals(MessageState.ACCEPTED_REMOTE, fixture.actor.snapshot().join().messages().getFirst().state());
            assertEquals("AWAITING_CANONICAL_TURN_ID", fixture.actor.snapshot().join().protocolRecords()
                    .get("agents/status").get("reason_code").getAsString());
            assertEquals(0, fixture.client.inputs.size());
        }
    }

    @Test
    void closeCompletesPendingCallerAndFencesLateNetworkCompletion() throws Exception {
        try (Fixture fixture = fixture("close")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            fixture.client.retrieveGate = new CompletableFuture<>();
            CompletableFuture<Void> started = fixture.service.start();
            fixture.client.retrieveRequested.get(5, TimeUnit.SECONDS);
            fixture.service.close();
            assertThrows(ExecutionException.class, () -> started.get(1, TimeUnit.SECONDS));
            fixture.client.retrieveGate.complete(new AgentsClient.RemoteSession("session-1",
                    AgentsClient.SessionStatus.IDLE, List.of(), null));
            assertEquals(0, fixture.bridge.outputs.size());
            assertTrue(fixture.client.closed);
        }
    }

    @Test
    void disconnectedStreamReconnectsAndRetrievesMissedSavedOutput() throws Exception {
        try (Fixture fixture = fixture("stream-recovery")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            fixture.service.start().get(5, TimeUnit.SECONDS);
            fixture.client.completeTurn("turn-1", "Recovered after disconnect");
            fixture.client.streams.getFirst().completeExceptionally(new IllegalStateException("disconnected"));
            fixture.bridge.outputDelivered.get(5, TimeUnit.SECONDS);
            assertEquals(List.of("answer-1:Recovered after disconnect"), fixture.bridge.outputs);
            assertEquals(2, fixture.client.subscriptions.get());
        }
    }

    @Test
    void toolResultRetriesAreBoundedAndNeverReexecuteWorldAction() throws Exception {
        try (Fixture fixture = fixture("result-budget")) {
            fixture.client.actionOnCreate = true;
            fixture.client.omitResultProof = true;
            fixture.client.keepResultPending = true;
            fixture.actor.enqueue("Follow me", MessagePriority.USER_CHAT).join();
            assertThrows(ExecutionException.class, () -> fixture.service.start().get(5, TimeUnit.SECONDS));
            assertEquals(4, fixture.client.results.size());
            assertEquals(1, fixture.bridge.executions.get());
            assertEquals(1, fixture.client.results.stream().distinct().count(), "every retry submits the same immutable result");
        }
    }

    @Test
    void authenticationFailureDoesNotEnterAutomaticTransportRetryLoop() throws Exception {
        try (Fixture fixture = fixture("authentication")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            fixture.client.retrieveFailure = new AgentsException(AgentsException.Kind.AUTHENTICATION, 401, null, null);
            assertThrows(ExecutionException.class, () -> fixture.service.start().get(5, TimeUnit.SECONDS));
            assertEquals(1, fixture.client.retrievals.get());
            assertEquals("AUTHENTICATION", fixture.actor.snapshot().join().protocolRecords()
                    .get("agents/status").get("reason_code").getAsString());
        }
    }

    @Test
    void durableInputAcceptanceDoesNotWaitForOutstandingHttp() throws Exception {
        try (Fixture fixture = fixture("local-enqueue")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            fixture.client.retrieveGate = new CompletableFuture<>();
            CompletableFuture<Void> remoteWork = fixture.service.start();
            fixture.client.retrieveRequested.get(5, TimeUnit.SECONDS);
            MessageRecord accepted = fixture.service.enqueue("Please remember this message").get(1, TimeUnit.SECONDS);
            assertEquals(MessageState.QUEUED, accepted.state());
            assertEquals(accepted.id(), fixture.actor.snapshot().join().messages().getFirst().id());
            assertFalse(remoteWork.isDone(), "acceptance did not wait for the paused remote request");
        }
    }

    @Test
    void permanentRemoteFailureIsPersistedUntilExplicitOperatorRetry() throws Exception {
        Path path = directory.resolve("blocked-remote.jsonl");
        SessionKey key = key();
        try (Fixture first = new Fixture(path, key)) {
            first.actor.saveRemoteSessionId("session-1").join();
            first.client.retrieveFailure = new AgentsException(AgentsException.Kind.AUTHENTICATION, 401, null, null);
            assertThrows(ExecutionException.class, () -> first.service.start().get(5, TimeUnit.SECONDS));
        }
        try (Fixture restored = new Fixture(path, key)) {
            restored.service.start().get(5, TimeUnit.SECONDS);
            assertEquals(0, restored.client.retrievals.get());
            restored.service.enqueue("A new user message").get(1, TimeUnit.SECONDS);
            assertEquals(MessageState.QUEUED, restored.actor.snapshot().join().messages().getFirst().state());
            restored.service.reconcile().get(5, TimeUnit.SECONDS);
            assertEquals(1, restored.client.retrievals.get());
        }
    }

    @Test
    void quotaFailureDuringCreateIsNotRetriedAsOrdinaryRateLimit() throws Exception {
        try (Fixture fixture = fixture("quota")) {
            fixture.actor.enqueue("Hello", MessagePriority.USER_CHAT).join();
            fixture.client.createFailure = new AgentsException(AgentsException.Kind.RATE_LIMIT, 429, java.time.Duration.ZERO,
                    "insufficient_quota");
            assertThrows(ExecutionException.class, () -> fixture.service.start().get(5, TimeUnit.SECONDS));
            assertEquals(1, fixture.client.creates.get());
            assertTrue(fixture.actor.snapshot().join().protocolRecords().get("agents/remote_blocked").get("enabled").getAsBoolean());
        }
    }

    @Test
    void streamCapacityFallsBackToCanonicalPollingWithoutLosingOutput() throws Exception {
        try (Fixture fixture = fixture("polling")) {
            fixture.actor.saveRemoteSessionId("session-1").join();
            MessageRecord message = fixture.actor.enqueue("Hello", MessagePriority.USER_CHAT).join();
            fixture.actor.markMessage(message.id(), MessageState.ACCEPTED_REMOTE, "session-1", null).join();
            fixture.actor.putProtocolRecord("agents/active_submission", scope(message, "ACCEPTED_REMOTE")).join();
            fixture.client.subscribeFailure = new AgentsException(AgentsException.Kind.CAPACITY, 0, null, null);
            fixture.service.start().get(5, TimeUnit.SECONDS);
            fixture.client.completeTurn("turn-1", "Recovered by polling");
            fixture.bridge.outputDelivered.get(5, TimeUnit.SECONDS);
            assertEquals(1, fixture.client.subscriptions.get());
            assertEquals("POLLING", fixture.actor.snapshot().join().protocolRecords().get("agents/stream").get("mode").getAsString());
            assertEquals(List.of("answer-1:Recovered by polling"), fixture.bridge.outputs);
        }
    }

    private Fixture fixture(String name) { return new Fixture(directory.resolve(name + ".jsonl"), key()); }
    private static SessionKey key() { return new SessionKey(UUID.randomUUID(), "test-server", "test-world", UUID.randomUUID()); }
    private static JsonObject scope(MessageRecord message, String state) {
        JsonObject scope = new JsonObject();
        scope.addProperty("message_id", message.id().toString());
        scope.addProperty("session_id", "session-1");
        scope.addProperty("state", state);
        scope.addProperty("control_generation", 0);
        scope.addProperty("created_at_ms", System.currentTimeMillis());
        scope.add("baseline_turns", new JsonArray());
        return scope;
    }

    private static final class Fixture implements AutoCloseable {
        final Path path;
        final Bridge bridge = new Bridge();
        final FakeClient client = new FakeClient();
        final CompanionActor actor;
        final ChatSessionService service;
        Fixture(Path path, SessionKey key) {
            this.path = path;
            actor = new CompanionActor(key, UUID.randomUUID(), new DurableJournal(path), bridge);
            actor.start().join();
            service = new ChatSessionService(actor, client, "gpt-6-luna", "Be a reliable companion", List.of());
        }
        @Override public void close() {
            service.close();
            actor.closeAsync().join();
        }
    }

    private static final class Bridge implements GameBridge {
        final AtomicInteger executions = new AtomicInteger();
        final List<String> outputs = new java.util.concurrent.CopyOnWriteArrayList<>();
        final CompletableFuture<Void> outputDelivered = new CompletableFuture<>();
        ActionOutcome outcome = ActionOutcome.accepted("job-1");
        @Override public CompletionStage<ActionOutcome> execute(ActionRequest request) {
            executions.incrementAndGet();
            return CompletableFuture.completedFuture(outcome);
        }
        @Override public void deliver(String itemId, String text) {
            outputs.add(itemId + ":" + text);
            outputDelivered.complete(null);
        }
        @Override public CompletionStage<JsonObject> snapshot() { return CompletableFuture.completedFuture(new JsonObject()); }
    }

    private static final class FakeClient implements AgentsClient {
        final AtomicInteger creates = new AtomicInteger();
        final AtomicInteger subscriptions = new AtomicInteger();
        final AtomicInteger retrievals = new AtomicInteger();
        final List<RemoteItem> items = new ArrayList<>();
        final List<ToolResult> results = new ArrayList<>();
        final List<String> inputKeys = new ArrayList<>();
        final List<String> inputs = new ArrayList<>();
        final List<CompletableFuture<Void>> streams = new ArrayList<>();
        final CompletableFuture<Void> retrieveRequested = new CompletableFuture<>();
        RemoteSession remote = new RemoteSession("session-1", SessionStatus.IDLE, List.of(), null);
        SessionCreate lastCreate;
        Consumer<SessionCreate> beforeCreate = request -> {};
        AgentsException createFailure;
        AgentsException retrieveFailure;
        AgentsException subscribeFailure;
        CompletableFuture<RemoteSession> retrieveGate;
        boolean actionOnCreate;
        boolean loseResultResponse;
        boolean omitResultProof;
        boolean keepResultPending;
        boolean failFirstInput;
        boolean subscribed;
        boolean observerEstablishedBeforeInput;
        boolean closed;

        @Override public CompletableFuture<RemoteSession> create(SessionCreate request) {
            creates.incrementAndGet();
            beforeCreate.accept(request);
            lastCreate = request;
            if (createFailure != null) return CompletableFuture.failedFuture(createFailure);
            items.add(user("turn-1"));
            if (actionOnCreate) {
                JsonObject arguments = new JsonObject();
                arguments.addProperty("intent_id", request.metadata().get("local_message_id"));
                arguments.addProperty("player_id", UUID.randomUUID().toString());
                arguments.addProperty("stop_distance", 3);
                remote = new RemoteSession("session-1", SessionStatus.REQUIRES_ACTION,
                        List.of(new FunctionCall("turn-1", "call-1", "follow_player", arguments.toString())), null);
            } else completeTurn("turn-1", "Hello from saved output");
            return CompletableFuture.completedFuture(remote);
        }
        @Override public CompletableFuture<RemoteSession> retrieve(String id) {
            retrievals.incrementAndGet();
            retrieveRequested.complete(null);
            if (retrieveFailure != null) return CompletableFuture.failedFuture(retrieveFailure);
            return retrieveGate != null ? retrieveGate : CompletableFuture.completedFuture(remote);
        }
        @Override public CompletableFuture<SubmissionAcceptance> submitMessage(String id, String text, String key) {
            observerEstablishedBeforeInput = subscribed;
            inputKeys.add(key);
            inputs.add(text);
            if (failFirstInput && inputs.size() == 1)
                return CompletableFuture.failedFuture(new AgentsException(AgentsException.Kind.CONNECTION, 0, null, null));
            items.add(user("turn-2"));
            completeTurn("turn-2", "Hello from saved output");
            return CompletableFuture.completedFuture(new SubmissionAcceptance(202));
        }
        @Override public CompletableFuture<SubmissionAcceptance> submitToolResult(String id, ToolResult result) {
            results.add(result);
            if (!omitResultProof) items.add(new RemoteItem("result-1", "function_call_output", result.turnId(), result.callId(),
                    result.success() ? "completed" : "failed", null, null,
                    result.success() ? new JsonPrimitive(result.output()).toString() : null,
                    result.error(), null));
            if (!keepResultPending) completeTurn(result.turnId(), "Hello from saved output");
            if (loseResultResponse) return CompletableFuture.failedFuture(new AgentsException(AgentsException.Kind.CONNECTION, 0, null, null));
            return CompletableFuture.completedFuture(new SubmissionAcceptance(202));
        }
        @Override public CompletableFuture<SubmissionAcceptance> cancel(String id) { return CompletableFuture.completedFuture(new SubmissionAcceptance(202)); }
        @Override public CompletableFuture<List<RemoteItem>> items(String id) { return CompletableFuture.completedFuture(List.copyOf(items)); }
        @Override public CompletableFuture<RemoteTurn> turn(String id, String turnId) {
            return CompletableFuture.completedFuture(new RemoteTurn(turnId,
                    remote.status() == SessionStatus.REQUIRES_ACTION ? TurnStatus.WAITING : TurnStatus.COMPLETED, null));
        }
        @Override public CompletableFuture<StreamSubscription> subscribe(String id, Consumer<AgentEvent> observer) {
            subscribed = true;
            subscriptions.incrementAndGet();
            if (subscribeFailure != null) return CompletableFuture.failedFuture(subscribeFailure);
            CompletableFuture<Void> completion = new CompletableFuture<>();
            streams.add(completion);
            return CompletableFuture.completedFuture(new StreamSubscription() {
                @Override public CompletableFuture<Void> completion() { return completion; }
                @Override public void close() { subscribed = false; }
            });
        }
        void completeTurn(String turnId, String answer) {
            remote = new RemoteSession("session-1", SessionStatus.IDLE, List.of(), null);
            items.add(new RemoteItem("answer-1", "message", turnId, null, "completed", "assistant", "final_answer", null, null, answer));
        }
        private static RemoteItem user(String turnId) {
            return new RemoteItem("input-" + turnId, "message", turnId, null, "completed", "user", null, null, null, "redacted");
        }
        @Override public void close() { closed = true; }
    }
}
