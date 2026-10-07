package dev.chatcompanion.core;

import java.util.List;

public record ReplayResult(List<JournalRecord> records, boolean healthy, String problem, long discardedTailBytes) {
    public ReplayResult { records = List.copyOf(records); }
}
