package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import java.util.*;

public record ActorSnapshot(SessionKey context, UUID runtimeEpoch, long controlGeneration,
                            String remoteSessionId, boolean storageHealthy, String storageProblem,
                            List<MessageRecord> messages, List<ToolEntry> tools, List<OutputRecord> outputs,
                            Map<String, JsonObject> protocolRecords) {
    public ActorSnapshot {
        messages = List.copyOf(messages); tools = List.copyOf(tools); outputs = List.copyOf(outputs);
        Map<String, JsonObject> copied = new LinkedHashMap<>();
        protocolRecords.forEach((key, value) -> copied.put(key, value.deepCopy())); protocolRecords = Collections.unmodifiableMap(copied);
    }
    @Override public Map<String, JsonObject> protocolRecords() {
        Map<String, JsonObject> copied = new LinkedHashMap<>();
        protocolRecords.forEach((key, value) -> copied.put(key, value.deepCopy())); return Collections.unmodifiableMap(copied);
    }
}
