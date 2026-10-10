package dev.chatcompanion.neoforge;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicLong;

/** Small bounded handoff queue from addressed Minecraft chat to the local Python agent. */
public final class LocalAgentInbox {
    private static final int MAX_QUEUED = 64;
    private static final AtomicLong NEXT_ID = new AtomicLong();
    private static final ConcurrentLinkedQueue<Message> MESSAGES = new ConcurrentLinkedQueue<>();

    private LocalAgentInbox() {}

    public static void offer(UUID owner, String text) {
        String normalized = text == null ? "" : text.strip();
        if (normalized.isEmpty()) return;
        while (MESSAGES.size() >= MAX_QUEUED) MESSAGES.poll();
        MESSAGES.offer(new Message(NEXT_ID.incrementAndGet(), owner, normalized));
    }

    public static List<Message> drain(UUID owner, int max) {
        int limit = Math.max(1, Math.min(16, max));
        List<Message> result = new ArrayList<>(limit);
        List<Message> deferred = new ArrayList<>();
        Message message;
        while (result.size() < limit && (message = MESSAGES.poll()) != null) {
            if (owner.equals(message.owner())) result.add(message);
            else deferred.add(message);
        }
        deferred.forEach(MESSAGES::offer);
        return result;
    }

    public record Message(long id, UUID owner, String text) {}
}
