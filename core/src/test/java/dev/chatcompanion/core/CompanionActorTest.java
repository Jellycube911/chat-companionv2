package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CompanionActorTest {

    @TempDir
    Path temporaryDirectory;

    private final SessionKey context = new SessionKey(
        UUID.randomUUID(), "test-server", "test-world", UUID.randomUUID());

    @Test
    void identicalMessagesHaveIndependentDurableIdentitiesAcrossRestart() throws Exception {
        Path journalPath = temporaryDirectory.resolve("messages.jsonl");
        CompanionActor actor = actor(journalPath, new ControlledBridge());
        MessageRecord first;
        MessageRecord second;
        try {
            first = await(actor.enqueue("follow me", MessagePriority.USER_COMMAND));
            second = await(actor.enqueue("follow me", MessagePriority.USER_COMMAND));
            assertNotEquals(first.id(), second.id());
            assertNotEquals(first.idempotencyKey(), second.idempotencyKey());
            assertEquals(first.sequence() + 1, second.sequence());
            await(actor.markMessage(first.id(), MessageState.ACCEPTED_REMOTE, "session-1", "turn-1"));
        } finally {
            await(actor.closeAsync());
        }

        CompanionActor reopened = actor(journalPath, new ControlledBridge());
        try {
            List<MessageRecord> messages = await(reopened.snapshot()).messages();
            assertEquals(2, messages.size());
            assertEquals(first.id(), messages.get(0).id());
            assertEquals(first.idempotencyKey(), messages.get(0).idempotencyKey());
            assertEquals(MessageState.ACCEPTED_REMOTE, messages.get(0).state());
            assertEquals("session-1", messages.get(0).sessionId());
            assertEquals("turn-1", messages.get(0).turnId());
            assertEquals(second, messages.get(1));
            MessageRecord third = await(reopened.enqueue("follow me", MessagePriority.USER_COMMAND));
            assertEquals(second.sequence() + 1, third.sequence());
            assertNotEquals(first.idempotencyKey(), third.idempotencyKey());
            assertNotEquals(second.id(), third.id());
        } finally {
            await(reopened.closeAsync());
        }
    }

    @Test
    void duplicatePendingReadOnlyCallExecutesOnceAndSharesRecordedOutcome() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(temporaryDirectory.resolve("pending.jsonl"), bridge);
        try {
            ToolRequest request = readRequest("call-1", 0);
            CompletableFuture<ToolEntry> first = actor.admit(request);
            PendingAction execution = bridge.next();
            CompletableFuture<ToolEntry> duplicate = actor.admit(request);
            await(actor.snapshot()); // The mailbox has processed the duplicate admission.
            assertEquals(1, bridge.actions.size());
            assertFalse(first.isDone());
            assertFalse(duplicate.isDone());

            ActionOutcome outcome = new ActionOutcome(true, "observed", "status_read", value("health", "20"));
            execution.outcome.complete(outcome);
            ToolEntry recorded = await(first);
            assertEquals(recorded, await(duplicate));
            assertEquals(ExecutionState.OBSERVED, recorded.executionState());
            assertEquals(outcome, recorded.outcome());
            assertEquals(recorded, await(actor.admit(request)));
            assertEquals(1, bridge.actions.size());
        } finally {
            await(actor.closeAsync());
        }
    }

    @Test
    void mismatchedArgumentsForSameCallFailWithoutSecondExecution() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(temporaryDirectory.resolve("integrity.jsonl"), bridge);
        try {
            ToolRequest request = readRequest("call-1", 0);
            CompletableFuture<ToolEntry> original = actor.admit(request);
            PendingAction execution = bridge.next();
            JsonObject changed = request.arguments();
            changed.addProperty("radius", 8);
            ToolRequest conflicting = new ToolRequest(request.key(), request.name(), changed, 0);

            ExecutionException failure = assertThrows(ExecutionException.class,
                () -> await(actor.admit(conflicting)));
            assertTrue(failure.getCause().getMessage().contains("integrity_violation"));
            assertEquals(1, bridge.actions.size());
            execution.outcome.complete(ActionOutcome.accepted("job-1"));
            assertEquals(ExecutionState.OBSERVED, await(original).executionState());
            assertEquals(1, bridge.actions.size());
        } finally {
            await(actor.closeAsync());
        }
    }

    @Test
    void admittedCallIsUnknownAfterRestartAndNeverAutomaticallyExecutedAgain() throws Exception {
        Path path = temporaryDirectory.resolve("restart-admitted.jsonl");
        ControlledBridge originalBridge = new ControlledBridge();
        CompanionActor original = actor(path, originalBridge);
        ToolRequest request = readRequest("call-1", 0);
        CompletableFuture<ToolEntry> pending = original.admit(request);
        PendingAction oldExecution = originalBridge.next();
        assertEquals(ExecutionState.ADMITTED, await(original.snapshot()).tools().get(0).executionState());
        await(original.closeAsync());
        assertTrue(pending.isCompletedExceptionally());

        ControlledBridge recoveredBridge = new ControlledBridge();
        CompanionActor recovered = actor(path, recoveredBridge);
        try {
            ToolEntry uncertain = await(recovered.snapshot()).tools().get(0);
            assertEquals(ExecutionState.UNKNOWN, uncertain.executionState());
            assertEquals("restart_after_admission", uncertain.outcome().reasonCode());
            assertEquals(uncertain, await(recovered.admit(request)));
            assertEquals(0, recoveredBridge.actions.size());

            // A callback from the previous server runtime cannot change the replacement actor.
            oldExecution.outcome.complete(ActionOutcome.accepted("obsolete-job"));
            assertEquals(uncertain, await(recovered.snapshot()).tools().get(0));
            assertEquals(0, recoveredBridge.actions.size());
        } finally {
            await(recovered.closeAsync());
        }
    }

    @Test
    void retainedWorldSuccessBecomesUnknownWhenRestoredWorldCannotBeVerified() throws Exception {
        Path path = temporaryDirectory.resolve("ledger-ahead-of-world.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor original = actor(path, bridge);
        ToolEntry historical;
        try {
            CompletableFuture<ToolEntry> pending = original.admitLocal("place_block", value("block", "minecraft:stone"));
            bridge.next().outcome.complete(new ActionOutcome(true, "observed", "block_placed", value("transaction_id", "placement-1")));
            historical = await(pending);
            await(original.confirmResult(historical.key()));
            historical = await(original.snapshot()).tools().get(0);
            assertEquals(ExecutionState.OBSERVED, historical.executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, historical.resultDeliveryState());
        } finally {
            await(original.closeAsync());
        }

        ControlledBridge recoveredBridge = new ControlledBridge();
        CompanionActor recovered = actor(path, recoveredBridge);
        try {
            ActorSnapshot snapshot = await(recovered.snapshot());
            ToolEntry uncertain = snapshot.tools().get(0);
            assertEquals(ExecutionState.UNKNOWN, uncertain.executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, uncertain.resultDeliveryState());
            assertEquals(historical.outcome(), uncertain.outcome(),
                "world discrepancy cannot rewrite a previously delivered historical result");
            assertEquals(0, recoveredBridge.actions.size());
            assertEquals(1, recoveredBridge.reconciliations.size());
            assertEquals(recovered.runtimeEpoch(), recoveredBridge.reconciliations.get(0).request.runtimeEpoch());
            assertEquals(historical.outcome(), recoveredBridge.reconciliations.get(0).recorded);
            var incident = snapshot.protocolRecords().entrySet().stream()
                .filter(entry -> entry.getKey().startsWith("world/discrepancy/"))
                .findFirst().orElseThrow();
            assertEquals(historical.key().storageKey(), incident.getValue().get("call_key").getAsString());
            assertTrue(incident.getValue().get("historical_result_preserved").getAsBoolean());
            assertEquals("restored_world_unverifiable", incident.getValue().get("reason").getAsString());
        } finally {
            await(recovered.closeAsync());
        }

        ControlledBridge secondRecoveryBridge = new ControlledBridge();
        CompanionActor reopened = actor(path, secondRecoveryBridge);
        try {
            ActorSnapshot snapshot = await(reopened.snapshot());
            assertEquals(ExecutionState.UNKNOWN, snapshot.tools().get(0).executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, snapshot.tools().get(0).resultDeliveryState());
            assertEquals(historical.outcome(), snapshot.tools().get(0).outcome());
            assertTrue(snapshot.protocolRecords().keySet().stream().anyMatch(key -> key.startsWith("world/discrepancy/")));
            assertEquals(0, secondRecoveryBridge.actions.size());
            assertEquals(0, secondRecoveryBridge.reconciliations.size(), "unresolved world outcomes remain fenced");
        } finally {
            await(reopened.closeAsync());
        }
    }

    @Test
    void verifiedRestoredWorldKeepsObservedHistoricalResultWithoutExecutingAgain() throws Exception {
        Path path = temporaryDirectory.resolve("world-verification.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor original = actor(path, bridge);
        ToolEntry historical;
        try {
            CompletableFuture<ToolEntry> pending = original.admitLocal("follow_player", value("player_id", context.playerUuid().toString()));
            bridge.next().outcome.complete(ActionOutcome.accepted("follow-job-1"));
            historical = await(pending);
            await(original.confirmResult(historical.key()));
            historical = await(original.snapshot()).tools().get(0);
        } finally {
            await(original.closeAsync());
        }

        ControlledBridge recoveredBridge = new ControlledBridge();
        recoveredBridge.reconciliationOutcome = new ActionOutcome(true, "observed", "restored_job_verified", value("job_id", "follow-job-1"));
        CompanionActor recovered = actor(path, recoveredBridge);
        try {
            ActorSnapshot snapshot = await(recovered.snapshot());
            assertEquals(historical, snapshot.tools().get(0));
            assertEquals(ExecutionState.OBSERVED, snapshot.tools().get(0).executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, snapshot.tools().get(0).resultDeliveryState());
            assertEquals(0, recoveredBridge.actions.size());
            assertEquals(1, recoveredBridge.reconciliations.size());
            assertFalse(snapshot.protocolRecords().keySet().stream().anyMatch(key -> key.startsWith("world/discrepancy/")));
        } finally {
            await(recovered.closeAsync());
        }
    }

    @Test
    void duplicateIntentAcrossCallKeysJoinsAlreadyRunningExecution() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(temporaryDirectory.resolve("same-intent-running.jsonl"), bridge);
        try {
            MessageRecord intent = await(actor.enqueue("place stone", MessagePriority.USER_COMMAND));
            JsonObject arguments = value("block", "minecraft:stone");
            arguments.addProperty("intent_id", intent.id().toString());
            ToolRequest firstRequest = new ToolRequest(key("call-1"), "place_block", arguments, actor.controlGeneration());
            ToolRequest duplicateRequest = new ToolRequest(key("call-2"), "place_block", arguments, actor.controlGeneration());
            CompletableFuture<ToolEntry> first = actor.admit(firstRequest);
            PendingAction executing = bridge.next();
            CompletableFuture<ToolEntry> duplicate = actor.admit(duplicateRequest);
            await(actor.snapshot());
            assertEquals(1, bridge.actions.size());
            assertFalse(duplicate.isDone());

            executing.outcome.complete(new ActionOutcome(true, "observed", "block_placed", value("transaction_id", "placement-1")));
            ToolEntry recordedFirst = await(first);
            ToolEntry recordedDuplicate = await(duplicate);
            assertNotEquals(recordedFirst.key(), recordedDuplicate.key());
            assertEquals(recordedFirst.outcome(), recordedDuplicate.outcome());
            assertEquals(ExecutionState.OBSERVED, recordedDuplicate.executionState());
            assertEquals(1, bridge.actions.size());
        } finally {
            await(actor.closeAsync());
        }
    }

    @Test
    void simultaneousSameIntentAdmissionsCannotWaitOnEachOther() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(temporaryDirectory.resolve("same-intent-prepared.jsonl"), bridge);
        try {
            MessageRecord intent = await(actor.enqueue("place stone", MessagePriority.USER_COMMAND));
            JsonObject arguments = value("block", "minecraft:stone");
            arguments.addProperty("intent_id", intent.id().toString());
            bridge.deliveryEntered = new CountDownLatch(1);
            bridge.releaseDelivery = new CountDownLatch(1);
            CompletableFuture<Void> gate = actor.deliverOutput("gate", "Gate");
            assertTrue(bridge.deliveryEntered.await(5, TimeUnit.SECONDS));

            CompletableFuture<ToolEntry> first = actor.admit(new ToolRequest(key("call-1"), "place_block", arguments, actor.controlGeneration()));
            CompletableFuture<ToolEntry> duplicate = actor.admit(new ToolRequest(key("call-2"), "place_block", arguments, actor.controlGeneration()));
            bridge.releaseDelivery.countDown();
            await(gate);
            PendingAction executing = bridge.next();
            assertEquals(1, bridge.actions.size());
            executing.outcome.complete(new ActionOutcome(true, "observed", "block_placed", value("transaction_id", "placement-1")));
            assertEquals(await(first).outcome(), await(duplicate).outcome());
            assertEquals(1, bridge.actions.size());
        } finally {
            if (bridge.releaseDelivery != null) bridge.releaseDelivery.countDown();
            await(actor.closeAsync());
        }
    }

    @Test
    void confirmingUnknownResultDoesNotResolveWorldUncertainty() throws Exception {
        Path path = temporaryDirectory.resolve("independent-states.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(path, bridge);
        ToolCallKey key = key("call-1");
        try {
            CompletableFuture<ToolEntry> pending = actor.admit(readRequest("call-1", 0));
            bridge.next().outcome.complete(ActionOutcome.unknown("lost_world_acknowledgement"));
            ToolEntry unknown = await(pending);
            assertEquals(ExecutionState.UNKNOWN, unknown.executionState());
            assertEquals(ResultDeliveryState.UNSUBMITTED, unknown.resultDeliveryState());

            await(actor.markResultAccepted(key, 202));
            await(actor.confirmResult(key));
            ToolEntry confirmed = await(actor.snapshot()).tools().get(0);
            assertEquals(ExecutionState.UNKNOWN, confirmed.executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, confirmed.resultDeliveryState());
            assertEquals(unknown.outcome(), confirmed.outcome());
        } finally {
            await(actor.closeAsync());
        }

        ControlledBridge recoveredBridge = new ControlledBridge();
        CompanionActor reopened = actor(path, recoveredBridge);
        try {
            ToolEntry confirmed = await(reopened.snapshot()).tools().get(0);
            assertEquals(ExecutionState.UNKNOWN, confirmed.executionState());
            assertEquals(ResultDeliveryState.CONFIRMED, confirmed.resultDeliveryState());
            assertFalse(confirmed.outcome().success());
            assertEquals(0, recoveredBridge.actions.size());
        } finally {
            await(reopened.closeAsync());
        }
    }

    @Test
    void resultAcceptanceRequires202AndCannotAdvanceWithoutDurableOutcome() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(temporaryDirectory.resolve("result-acceptance.jsonl"), bridge);
        try {
            CompletableFuture<ToolEntry> pending = actor.admit(readRequest("call-1", 0));
            PendingAction execution = bridge.next();
            assertThrows(ExecutionException.class, () -> await(actor.markResultAccepted(key("call-1"), 202)));
            assertThrows(ExecutionException.class, () -> await(actor.markResultAccepted(key("call-1"), 200)));
            assertEquals(ResultDeliveryState.UNSUBMITTED, await(actor.snapshot()).tools().get(0).resultDeliveryState());
            execution.outcome.complete(ActionOutcome.accepted("job-1"));
            await(pending);
            await(actor.markResultAccepted(key("call-1"), 202));
            assertEquals(ResultDeliveryState.RESULT_ACCEPTED,
                await(actor.snapshot()).tools().get(0).resultDeliveryState());
        } finally {
            await(actor.closeAsync());
        }
    }

    @Test
    void lateAcceptanceCannotRegressConcurrentSavedResultConfirmation() throws Exception {
        Path path = temporaryDirectory.resolve("monotonic-result-delivery.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(path, bridge);
        try {
            CompletableFuture<ToolEntry> pending = actor.admit(readRequest("call-1", 0));
            bridge.next().outcome.complete(ActionOutcome.unknown("world_outcome_uncertain"));
            await(pending);

            // Hold the mailbox so confirmation and a late HTTP acknowledgement are both queued
            // before either persistence acknowledgement can update the actor projection.
            bridge.deliveryEntered = new CountDownLatch(1);
            bridge.releaseDelivery = new CountDownLatch(1);
            CompletableFuture<Void> gate = actor.deliverOutput("gate", "Gate");
            assertTrue(bridge.deliveryEntered.await(5, TimeUnit.SECONDS));
            CompletableFuture<Void> confirmation = actor.confirmResult(key("call-1"));
            CompletableFuture<Void> acceptance = actor.markResultAccepted(key("call-1"), 202);
            bridge.releaseDelivery.countDown();
            await(gate);
            await(confirmation);
            await(acceptance);
            assertEquals(ResultDeliveryState.CONFIRMED,
                await(actor.snapshot()).tools().get(0).resultDeliveryState());
        } finally {
            if (bridge.releaseDelivery != null) bridge.releaseDelivery.countDown();
            await(actor.closeAsync());
        }

        CompanionActor recovered = actor(path, new ControlledBridge());
        try {
            assertEquals(ResultDeliveryState.CONFIRMED,
                await(recovered.snapshot()).tools().get(0).resultDeliveryState());
            assertEquals(ExecutionState.UNKNOWN, await(recovered.snapshot()).tools().get(0).executionState());
        } finally {
            await(recovered.closeAsync());
        }
    }

    @Test
    void stopFencesImmediatelyWhileExistingActionIsUnresolved() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(temporaryDirectory.resolve("safety-stop.jsonl"), bridge);
        try {
            CompletableFuture<ToolEntry> following = actor.admitLocal("follow_player", value("player_id", context.playerUuid().toString()));
            PendingAction execution = bridge.next();
            long previousGeneration = actor.controlGeneration();
            assertEquals(previousGeneration, execution.request.controlGeneration());

            CompletableFuture<StopReceipt> stopping = actor.stop();
            assertEquals(previousGeneration + 1, actor.controlGeneration(),
                "the fence must update before asynchronous mailbox or persistence work");
            assertNotEquals(actor.controlGeneration(), execution.request.controlGeneration());
            PendingAction stop = bridge.next();
            assertEquals("stop_action", stop.request.name());
            assertEquals(actor.controlGeneration(), stop.request.controlGeneration());
            StopReceipt receipt = await(stopping);
            assertTrue(receipt.durable());
            assertEquals(actor.controlGeneration(), receipt.controlGeneration());

            // The server bridge detects the stale request before an outstanding operation can act.
            execution.outcome.complete(ActionOutcome.rejected("stale_control_generation"));
            assertEquals(ExecutionState.REJECTED, await(following).executionState());
            assertEquals(2, bridge.actions.size());
        } finally {
            await(actor.closeAsync());
        }
    }

    @Test
    void retainedOutcomeAndArgumentsAreImmutableAndReusedAfterRestart() throws Exception {
        Path path = temporaryDirectory.resolve("immutable-result.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(path, bridge);
        ToolRequest request = readRequest("call-1", 0);
        ToolEntry original;
        try {
            CompletableFuture<ToolEntry> pending = actor.admit(request);
            PendingAction execution = bridge.next();
            JsonObject evidence = value("position", "1,2,3");
            ActionOutcome observed = new ActionOutcome(true, "observed", "status_read", evidence);
            execution.outcome.complete(observed);
            original = await(pending);
            evidence.addProperty("position", "9,9,9");
            original.outcome().evidence().addProperty("position", "7,7,7");
            original.arguments().addProperty("radius", 100);
            ToolEntry retained = await(actor.admit(request));
            assertEquals(original, retained);
            assertEquals("1,2,3", retained.outcome().evidence().get("position").getAsString());
            assertEquals(4, retained.arguments().get("radius").getAsInt());
            assertEquals(1, bridge.actions.size());
        } finally {
            await(actor.closeAsync());
        }

        ControlledBridge recoveredBridge = new ControlledBridge();
        CompanionActor recovered = actor(path, recoveredBridge);
        try {
            assertEquals(original, await(recovered.admit(request)));
            assertEquals(0, recoveredBridge.actions.size());
        } finally {
            await(recovered.closeAsync());
        }
    }

    @Test
    void sessionAndProtocolStatePersistWithDefensiveCopies() throws Exception {
        Path path = temporaryDirectory.resolve("protocol.jsonl");
        CompanionActor actor = actor(path, new ControlledBridge());
        JsonObject protocol = value("last_item", "item-17");
        try {
            await(actor.saveRemoteSessionId("session-123"));
            CompletableFuture<Void> stored = actor.putProtocolRecord("output_cursor", protocol);
            protocol.addProperty("last_item", "mutated-after-submission");
            await(stored);
            ActorSnapshot snapshot = await(actor.snapshot());
            assertEquals("session-123", snapshot.remoteSessionId());
            assertEquals("item-17", snapshot.protocolRecords().get("output_cursor").get("last_item").getAsString());
            snapshot.protocolRecords().get("output_cursor").addProperty("last_item", "mutated-snapshot");
            assertEquals("item-17", await(actor.snapshot()).protocolRecords().get("output_cursor").get("last_item").getAsString());
        } finally {
            await(actor.closeAsync());
        }

        CompanionActor recovered = actor(path, new ControlledBridge());
        try {
            ActorSnapshot snapshot = await(recovered.snapshot());
            assertEquals("session-123", snapshot.remoteSessionId());
            assertEquals("item-17", snapshot.protocolRecords().get("output_cursor").get("last_item").getAsString());
        } finally {
            await(recovered.closeAsync());
        }
    }

    @Test
    void displayedOutputDeduplicatesByItemIdAndReceivedOutputRetransmitsAfterRestart() throws Exception {
        Path path = temporaryDirectory.resolve("outputs.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        CompanionActor actor = actor(path, bridge);
        try {
            await(actor.deliverOutput("item-1", "Hello"));
            assertEquals(OutputDeliveryState.SENT, await(actor.snapshot()).outputs().get(0).state());
            await(actor.acknowledgeOutput("item-1", OutputDeliveryState.DISPLAYED));
            await(actor.deliverOutput("item-1", "Hello"));
            await(actor.deliverOutput("item-2", "Hello"));
            await(actor.acknowledgeOutput("item-2", OutputDeliveryState.RECEIVED));
            assertEquals(OutputDeliveryState.DISPLAYED, await(actor.snapshot()).outputs().get(0).state());
            assertEquals(OutputDeliveryState.RECEIVED, await(actor.snapshot()).outputs().get(1).state());
            assertEquals(List.of(new DeliveredOutput("item-1", "Hello"), new DeliveredOutput("item-2", "Hello")), bridge.deliveries);
        } finally {
            await(actor.closeAsync());
        }

        ControlledBridge recoveredBridge = new ControlledBridge();
        CompanionActor recovered = actor(path, recoveredBridge);
        try {
            await(recovered.deliverOutput("item-1", "Hello"));
            await(recovered.deliverOutput("item-2", "Hello"));
            assertEquals(List.of(new DeliveredOutput("item-2", "Hello")), recoveredBridge.deliveries);
            assertEquals(OutputDeliveryState.RECEIVED, await(recovered.snapshot()).outputs().get(1).state(),
                "retransmission cannot regress a recorded client receipt");
            await(recovered.acknowledgeOutput("item-2", OutputDeliveryState.DISPLAYED));
            await(recovered.deliverOutput("item-2", "Hello"));
            await(recovered.deliverOutput("item-3", "Hello"));
            assertEquals(List.of(new DeliveredOutput("item-2", "Hello"), new DeliveredOutput("item-3", "Hello")), recoveredBridge.deliveries);
        } finally {
            await(recovered.closeAsync());
        }
    }

    @Test
    void concurrentOutputDeliverySharesOnePendingTransmission() throws Exception {
        ControlledBridge bridge = new ControlledBridge();
        bridge.deliveryEntered = new CountDownLatch(1);
        bridge.releaseDelivery = new CountDownLatch(1);
        CompanionActor actor = actor(temporaryDirectory.resolve("concurrent-output.jsonl"), bridge);
        try {
            CompletableFuture<Void> first = actor.deliverOutput("item-1", "Hello");
            assertTrue(bridge.deliveryEntered.await(5, TimeUnit.SECONDS));
            CompletableFuture<Void> duplicate = actor.deliverOutput("item-1", "Hello");
            CompletableFuture<Void> third = actor.deliverOutput("item-1", "Hello");
            bridge.releaseDelivery.countDown();
            await(first);
            await(duplicate);
            await(third);
            assertEquals(List.of(new DeliveredOutput("item-1", "Hello")), bridge.deliveries);
            assertEquals(OutputDeliveryState.SENT, await(actor.snapshot()).outputs().get(0).state());
        } finally {
            bridge.releaseDelivery.countDown();
            await(actor.closeAsync());
        }
    }

    @Test
    void intendedOutputSurvivesFailedTransmissionAndRemainsEligibleForRecovery() throws Exception {
        Path path = temporaryDirectory.resolve("intended-output.jsonl");
        ControlledBridge bridge = new ControlledBridge();
        bridge.deliveryFailure = new IllegalStateException("client_disconnected");
        CompanionActor actor = actor(path, bridge);
        try {
            assertThrows(ExecutionException.class, () -> await(actor.deliverOutput("item-1", "Hello")));
            assertEquals(OutputDeliveryState.INTENDED, await(actor.snapshot()).outputs().get(0).state());
            assertTrue(await(actor.snapshot()).storageHealthy());
            assertEquals(0, bridge.deliveries.size());
        } finally {
            await(actor.closeAsync());
        }

        ControlledBridge recoveredBridge = new ControlledBridge();
        CompanionActor recovered = actor(path, recoveredBridge);
        try {
            assertEquals(OutputDeliveryState.INTENDED, await(recovered.snapshot()).outputs().get(0).state());
            await(recovered.deliverOutput("item-1", "Hello"));
            assertEquals(List.of(new DeliveredOutput("item-1", "Hello")), recoveredBridge.deliveries);
            await(recovered.acknowledgeOutput("item-1", OutputDeliveryState.DISPLAYED));
            await(recovered.acknowledgeOutput("item-1", OutputDeliveryState.RECEIVED));
            assertEquals(OutputDeliveryState.DISPLAYED, await(recovered.snapshot()).outputs().get(0).state(),
                "a late receipt acknowledgement cannot regress displayed state");
            assertThrows(ExecutionException.class,
                () -> await(recovered.deliverOutput("item-1", "Changed content")));
            assertEquals(1, recoveredBridge.deliveries.size());
        } finally {
            await(recovered.closeAsync());
        }
    }

    private CompanionActor actor(Path journalPath, ControlledBridge bridge) throws Exception {
        CompanionActor actor = new CompanionActor(context, UUID.randomUUID(), new DurableJournal(journalPath), bridge);
        await(actor.start());
        assertTrue(await(actor.snapshot()).storageHealthy());
        return actor;
    }

    private static ToolRequest readRequest(String callId, long generation) {
        return new ToolRequest(key(callId), "inspect_nearby", value("radius", 4), generation);
    }

    private static ToolCallKey key(String callId) {
        return new ToolCallKey("session-1", "turn-1", callId);
    }

    private static JsonObject value(String property, String value) {
        JsonObject object = new JsonObject();
        object.addProperty(property, value);
        return object;
    }

    private static JsonObject value(String property, int value) {
        JsonObject object = new JsonObject();
        object.addProperty(property, value);
        return object;
    }

    private static <T> T await(CompletionStage<T> stage) throws Exception {
        return stage.toCompletableFuture().get(5, TimeUnit.SECONDS);
    }

    private record PendingAction(ActionRequest request, CompletableFuture<ActionOutcome> outcome) {}

    private record DeliveredOutput(String itemId, String text) {}

    private record Reconciliation(ActionRequest request, ActionOutcome recorded) {}

    private static final class ControlledBridge implements GameBridge {
        private final List<PendingAction> actions = new CopyOnWriteArrayList<>();
        private final LinkedBlockingQueue<PendingAction> executions = new LinkedBlockingQueue<>();
        private final List<DeliveredOutput> deliveries = new CopyOnWriteArrayList<>();
        private final List<Reconciliation> reconciliations = new CopyOnWriteArrayList<>();
        private volatile RuntimeException deliveryFailure;
        private ActionOutcome reconciliationOutcome;
        private CountDownLatch deliveryEntered;
        private CountDownLatch releaseDelivery;

        @Override
        public CompletionStage<ActionOutcome> execute(ActionRequest request) {
            CompletableFuture<ActionOutcome> outcome = new CompletableFuture<>();
            PendingAction action = new PendingAction(request, outcome);
            actions.add(action);
            executions.add(action);
            if (request.name().equals("stop_action")) outcome.complete(ActionOutcome.accepted("stopped"));
            return outcome;
        }

        @Override
        public void deliver(String itemId, String text) {
            if (deliveryFailure != null) throw deliveryFailure;
            if (deliveryEntered != null) {
                deliveryEntered.countDown();
                try {
                    if (!releaseDelivery.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("test_delivery_timeout");
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException(interrupted);
                }
            }
            deliveries.add(new DeliveredOutput(itemId, text));
        }

        @Override
        public CompletionStage<JsonObject> snapshot() {
            return CompletableFuture.completedFuture(new JsonObject());
        }

        @Override
        public CompletionStage<ActionOutcome> reconcile(ActionRequest request, ActionOutcome recorded) {
            reconciliations.add(new Reconciliation(request, recorded));
            return reconciliationOutcome == null ? GameBridge.super.reconcile(request, recorded)
                : CompletableFuture.completedFuture(reconciliationOutcome);
        }

        private PendingAction next() throws InterruptedException {
            PendingAction action = executions.poll(5, TimeUnit.SECONDS);
            assertNotNull(action, "expected execution was not dispatched");
            return action;
        }
    }
}
