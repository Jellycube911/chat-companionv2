package dev.chatcompanion.core;

import com.google.gson.JsonObject;

public record ToolEntry(ToolCallKey key, String name, JsonObject arguments, String argumentsHash,
                        long controlGeneration, ExecutionState executionState,
                        ResultDeliveryState resultDeliveryState, ActionOutcome outcome) {
    public ToolEntry { arguments = arguments.deepCopy(); }
    @Override public JsonObject arguments() { return arguments.deepCopy(); }
    public ToolEntry executed(ExecutionState state, ActionOutcome result) {
        return new ToolEntry(key, name, arguments, argumentsHash, controlGeneration, state, resultDeliveryState, result);
    }
    public ToolEntry delivered(ResultDeliveryState state) {
        return new ToolEntry(key, name, arguments, argumentsHash, controlGeneration, executionState, state, outcome);
    }
}
