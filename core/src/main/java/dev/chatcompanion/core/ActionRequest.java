package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import java.util.Objects;
import java.util.UUID;

/** Immutable request. The bridge MUST recheck epoch, generation and permissions on its server thread. */
public record ActionRequest(ToolCallKey key, String name, JsonObject arguments,
                            UUID runtimeEpoch, long controlGeneration) {
    public ActionRequest {
        Objects.requireNonNull(key); Objects.requireNonNull(name);
        arguments = Objects.requireNonNull(arguments).deepCopy();
        Objects.requireNonNull(runtimeEpoch);
    }
    @Override public JsonObject arguments() { return arguments.deepCopy(); }
}
