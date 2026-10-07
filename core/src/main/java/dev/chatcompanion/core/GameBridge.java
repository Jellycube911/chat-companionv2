package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.CompletableFuture;

/** Loader implementation schedules world access onto the current logical server. */
public interface GameBridge {
    CompletionStage<ActionOutcome> execute(ActionRequest request);
    void deliver(String itemId, String text);
    CompletionStage<JsonObject> snapshot();
    /** Read-only restored-world verification. Never execute the original request here. */
    default CompletionStage<ActionOutcome> reconcile(ActionRequest request, ActionOutcome recorded) {
        return CompletableFuture.completedFuture(ActionOutcome.unknown("restored_world_unverifiable"));
    }
}
