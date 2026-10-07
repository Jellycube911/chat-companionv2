package dev.chatcompanion.core;

import com.google.gson.JsonObject;

public record JournalRecord(long sequence, String eventId, String aggregateId, String type,
                            JsonObject payload, String checksum) {
    public JournalRecord { payload = payload.deepCopy(); }
    @Override public JsonObject payload() { return payload.deepCopy(); }
}
