package dev.chatcompanion.game.speech;

import java.util.ArrayDeque;
import java.util.LinkedHashMap;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;

/** Client-actor-owned queue. Playback state changes only on explicit backend acknowledgements. */
public final class SpeechQueue {
    public enum State { QUEUED, STARTING, PLAYING, PLAYED, FAILED, CANCELLED }
    public enum EnqueueResult { ACCEPTED, DUPLICATE, FULL }
    public record QueuedSpeech(UUID utteranceId, PcmAudio audio) {}
    public record SpeechStatus(UUID utteranceId, State state, String detail) {}
    public record Snapshot(int queued, SpeechStatus active, SpeechStatus last) {}

    private final int queueCapacity;
    private final int historyCapacity;
    private final ArrayDeque<QueuedSpeech> pending = new ArrayDeque<>();
    private final LinkedHashMap<UUID, SpeechStatus> history = new LinkedHashMap<>();
    private QueuedSpeech active;
    private SpeechStatus last;

    public SpeechQueue(int queueCapacity, int historyCapacity) {
        if (queueCapacity < 1 || historyCapacity < queueCapacity + 1) {
            throw new IllegalArgumentException("Invalid speech queue limits");
        }
        this.queueCapacity = queueCapacity;
        this.historyCapacity = historyCapacity;
    }

    public EnqueueResult enqueue(UUID id, PcmAudio audio) {
        Objects.requireNonNull(id, "id");
        Objects.requireNonNull(audio, "audio");
        if (history.containsKey(id)) return EnqueueResult.DUPLICATE;
        if (pending.size() >= queueCapacity) return EnqueueResult.FULL;
        pending.addLast(new QueuedSpeech(id, audio));
        record(id, State.QUEUED, "Awaiting client audio source");
        return EnqueueResult.ACCEPTED;
    }

    public Optional<QueuedSpeech> beginNext() {
        if (active != null) return Optional.empty();
        active = pending.pollFirst();
        if (active == null) return Optional.empty();
        record(active.utteranceId(), State.STARTING, "Submitted to Minecraft sound engine");
        return Optional.of(active);
    }

    public void started(UUID id) {
        requireActive(id, State.STARTING);
        record(id, State.PLAYING, "Minecraft audio source started");
    }

    public void completed(UUID id) {
        requireActive(id, State.PLAYING);
        finish(State.PLAYED, "Minecraft audio source completed");
    }

    public void failed(UUID id, String detail) {
        if (active == null || !active.utteranceId().equals(id)) {
            throw new IllegalStateException("Speech is not active");
        }
        finish(State.FAILED, Objects.requireNonNull(detail));
    }

    public List<SpeechStatus> cancelAll(String reason) {
        Objects.requireNonNull(reason);
        List<SpeechStatus> cancelled = new ArrayList<>();
        if (active != null) {
            finish(State.CANCELLED, reason);
            cancelled.add(last);
        }
        QueuedSpeech next;
        while ((next = pending.pollFirst()) != null) {
            record(next.utteranceId(), State.CANCELLED, reason);
            cancelled.add(last);
        }
        return List.copyOf(cancelled);
    }

    public Snapshot snapshot() {
        return new Snapshot(pending.size(), active == null ? null : history.get(active.utteranceId()), last);
    }

    private void requireActive(UUID id, State expected) {
        if (active == null || !active.utteranceId().equals(id) || history.get(id).state() != expected) {
            throw new IllegalStateException("Invalid speech transition");
        }
    }

    private void finish(State state, String detail) {
        record(active.utteranceId(), state, detail);
        active = null;
    }

    private void record(UUID id, State state, String detail) {
        last = new SpeechStatus(id, state, detail);
        history.put(id, last);
        while (history.size() > historyCapacity) {
            UUID candidate = history.keySet().iterator().next();
            SpeechStatus status = history.get(candidate);
            if (status.state() == State.QUEUED || status.state() == State.STARTING || status.state() == State.PLAYING) {
                // Keep active/pending deduplication even when older terminal entries fill the history.
                Map.Entry<UUID, SpeechStatus> terminal = history.entrySet().stream()
                        .filter(e -> e.getValue().state() != State.QUEUED
                                && e.getValue().state() != State.STARTING && e.getValue().state() != State.PLAYING)
                        .findFirst().orElseThrow();
                candidate = terminal.getKey();
            }
            history.remove(candidate);
        }
    }
}
