package dev.chatcompanion.core;

import java.util.UUID;

public record MessageRecord(UUID id, String idempotencyKey, long sequence, String text,
                            MessagePriority priority, MessageState state, String sessionId, String turnId) {
    public MessageRecord withState(MessageState next, String session, String turn) {
        return new MessageRecord(id, idempotencyKey, sequence, text, priority, next,
                session == null ? sessionId : session, turn == null ? turnId : turn);
    }
    public boolean terminal() { return state == MessageState.RESOLVED || state == MessageState.FAILED || state == MessageState.CANCELLED; }
}
