package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import java.util.Objects;

public record ToolRequest(ToolCallKey key, String name, JsonObject arguments, long controlGeneration) {
    public ToolRequest { Objects.requireNonNull(key); Objects.requireNonNull(name);
        arguments = Objects.requireNonNull(arguments).deepCopy(); }
    @Override public JsonObject arguments() { return arguments.deepCopy(); }
}
