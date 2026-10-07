package dev.chatcompanion.core;

import java.util.Objects;

public record ToolCallKey(String sessionId, String turnId, String callId) {
    public ToolCallKey {
        Objects.requireNonNull(sessionId);
        Objects.requireNonNull(turnId);
        Objects.requireNonNull(callId);
    }

    public String storageKey() { return sessionId + "/" + turnId + "/" + callId; }
}
