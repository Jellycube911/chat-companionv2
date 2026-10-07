package dev.chatcompanion.core.api;

import com.google.gson.JsonElement;
import com.google.gson.JsonParser;
import dev.chatcompanion.core.api.AgentsClient.RemoteItem;
import dev.chatcompanion.core.api.AgentsClient.ToolResult;

/** Saved proof confirms recorded delivery; it never establishes that a Minecraft action happened. */
public final class SavedResultProof {
    private SavedResultProof() {}
    public static boolean matches(RemoteItem item, ToolResult result) {
        if (!"function_call_output".equals(item.type()) || !result.turnId().equals(item.turnId())
                || !result.callId().equals(item.callId())) return false;
        if (result.success()) {
            if (!"completed".equals(item.status()) || item.error() != null || item.outputJson() == null) return false;
            try {
                JsonElement stored = JsonParser.parseString(item.outputJson());
                // output is a JSON string on the wire; compare the exact recorded string, not inferred semantics.
                return stored.isJsonPrimitive() && stored.getAsJsonPrimitive().isString()
                        && result.output().equals(stored.getAsString());
            } catch (RuntimeException invalid) { return false; }
        }
        return "failed".equals(item.status()) && result.error().equals(item.error()) && item.outputJson() == null;
    }
}
