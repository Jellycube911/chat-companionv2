package dev.chatcompanion.core;

import com.google.gson.*;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;

/** Single-writer JSONL journal. All disk reads/writes/force calls run on its I/O worker. */
public final class DurableJournal implements AutoCloseable {
    private static final int MAX_RECORD_BYTES = 1_048_576;
    private static final long MAX_JOURNAL_BYTES = 64L * 1024 * 1024;
    private final Path file;
    private final ExecutorService io = new ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, new ArrayBlockingQueue<>(256), r -> {
        Thread thread = new Thread(r, "chat-companion-journal"); thread.setDaemon(true); return thread;
    });
    private final List<JournalRecord> records = new ArrayList<>();
    private FileChannel channel;
    private FileLock ownership;
    private ReplayResult replay;
    private volatile boolean closed;
    private volatile String durabilityProfile = "file_and_directory_sync";
    private final CompletableFuture<Void> closeFuture = new CompletableFuture<>();

    public DurableJournal(Path file) { this.file = Objects.requireNonNull(file).toAbsolutePath(); }
    public String durabilityProfile() { return durabilityProfile; }

    public CompletableFuture<ReplayResult> load() { return submit(() -> { initialize(); return currentReplay(); }); }

    public CompletableFuture<JournalRecord> append(String aggregateId, String type, JsonObject payload) {
        Objects.requireNonNull(aggregateId); Objects.requireNonNull(type); Objects.requireNonNull(payload);
        JsonObject copied = payload.deepCopy();
        return submit(() -> {
            initialize();
            if (!replay.healthy()) throw new IOException("Journal is read-only: " + replay.problem());
            if (channel.size() >= MAX_JOURNAL_BYTES) throw new IOException("journal_capacity_exhausted");
            long sequence = records.isEmpty() ? 1 : records.getLast().sequence() + 1;
            JsonObject body = new JsonObject(); body.addProperty("schema", 1); body.addProperty("sequence", sequence);
            body.addProperty("event_id", UUID.randomUUID().toString()); body.addProperty("aggregate_id", aggregateId);
            body.addProperty("type", type); body.add("payload", copied);
            String checksum = CanonicalJson.hash(body); body.addProperty("checksum", checksum);
            byte[] bytes = (CanonicalJson.encode(body) + "\n").getBytes(StandardCharsets.UTF_8);
            if (bytes.length > MAX_RECORD_BYTES) throw new IllegalArgumentException("Journal event exceeds byte limit");
            try {
                channel.position(channel.size());
                ByteBuffer buffer = ByteBuffer.wrap(bytes); while (buffer.hasRemaining()) channel.write(buffer);
                channel.force(true);
                writeCheckpoint(sequence, checksum);
            } catch (IOException failure) {
                replay = new ReplayResult(records, false, "append_outcome_uncertain", replay.discardedTailBytes());
                throw failure;
            }
            JournalRecord record = decode(body); records.add(record); return record;
        });
    }

    private ReplayResult currentReplay() {
        return new ReplayResult(records, replay.healthy(), replay.problem(), replay.discardedTailBytes());
    }

    private void initialize() throws IOException {
        if (replay != null) return;
        Files.createDirectories(file.getParent());
        if (!Files.exists(file) && Files.exists(checkpointPath())) {
            replay = new ReplayResult(records, false, "journal_missing_after_durable_checkpoint", 0); return;
        }
        channel = FileChannel.open(file, StandardOpenOption.CREATE, StandardOpenOption.READ, StandardOpenOption.WRITE);
        try {
            ownership = channel.tryLock();
            if (ownership == null) throw new IOException("journal_already_owned");
        } catch (Exception failure) { channel.close(); channel = null; throw new IOException("journal_already_owned", failure); }
        if (channel.size() > MAX_JOURNAL_BYTES + MAX_RECORD_BYTES) {
            replay = new ReplayResult(records, false, "journal_capacity_exceeded", 0); return;
        }
        // Bounded per-line reads; retaining history is deliberate until snapshot compaction is introduced.
        long lastBoundary = 0, position = 0, discarded = 0;
        java.io.ByteArrayOutputStream line = new java.io.ByteArrayOutputStream();
        ByteBuffer buffer = ByteBuffer.allocate(8192);
        String problem = null; Set<String> eventIds = new HashSet<>();
        while (channel.read(buffer) != -1 && problem == null) {
            buffer.flip();
            while (buffer.hasRemaining() && problem == null) {
                byte b = buffer.get(); position++;
                if (b == '\n') {
                    try {
                        String encoded = line.toString(StandardCharsets.UTF_8);
                        JsonObject body = JsonParser.parseString(encoded).getAsJsonObject();
                        String checksum = body.remove("checksum").getAsString();
                        if (!checksum.equals(CanonicalJson.hash(body))) throw new IOException("checksum_mismatch");
                        if (body.get("schema").getAsInt() != 1) throw new IOException("unsupported_schema");
                        long expected = records.size() + 1L;
                        if (body.get("sequence").getAsLong() != expected) throw new IOException("sequence_gap");
                        body.addProperty("checksum", checksum);
                        JournalRecord record = decode(body);
                        if (!eventIds.add(record.eventId())) throw new IOException("duplicate_event_id");
                        records.add(record); lastBoundary = position; line.reset();
                    } catch (Exception invalid) {
                        problem = "invalid_complete_record_at_" + (records.size() + 1) + ":" + invalid.getMessage();
                    }
                } else {
                    line.write(b);
                    if (line.size() > MAX_RECORD_BYTES) problem = "record_exceeds_limit";
                }
            }
            buffer.clear();
        }
        if (problem == null) problem = verifyCheckpoint();
        if (problem == null && line.size() > 0) {
            discarded = channel.size() - lastBoundary;
            channel.truncate(lastBoundary); channel.force(true);
        }
        replay = new ReplayResult(records, problem == null, problem, discarded);
        channel.position(channel.size());
    }

    private static JournalRecord decode(JsonObject body) {
        return new JournalRecord(body.get("sequence").getAsLong(), body.get("event_id").getAsString(),
                body.get("aggregate_id").getAsString(), body.get("type").getAsString(),
                body.getAsJsonObject("payload"), body.get("checksum").getAsString());
    }

    private Path checkpointPath() { return file.resolveSibling(file.getFileName() + ".checkpoint"); }
    private String verifyCheckpoint() {
        if (!Files.exists(checkpointPath())) return null;
        try {
            JsonObject checkpoint = JsonParser.parseString(Files.readString(checkpointPath())).getAsJsonObject();
            String integrity = checkpoint.remove("integrity").getAsString();
            if (!integrity.equals(CanonicalJson.hash(checkpoint))) return "checkpoint_checksum_mismatch";
            if (checkpoint.get("schema").getAsInt() != 1) return "checkpoint_schema_unsupported";
            long sequence = checkpoint.get("sequence").getAsLong();
            if (sequence < 1 || sequence > records.size()) return "durable_records_missing";
            if (!records.get((int) sequence - 1).checksum().equals(checkpoint.get("record_checksum").getAsString()))
                return "checkpoint_record_mismatch";
            return null;
        } catch (Exception invalid) { return "checkpoint_invalid"; }
    }
    private void writeCheckpoint(long sequence, String recordChecksum) throws IOException {
        JsonObject checkpoint = new JsonObject(); checkpoint.addProperty("schema", 1);
        checkpoint.addProperty("sequence", sequence); checkpoint.addProperty("record_checksum", recordChecksum);
        checkpoint.addProperty("integrity", CanonicalJson.hash(checkpoint));
        Path target = checkpointPath(); Path temporary = target.resolveSibling(target.getFileName() + ".tmp");
        byte[] bytes = CanonicalJson.encode(checkpoint).getBytes(StandardCharsets.UTF_8);
        try (FileChannel output = FileChannel.open(temporary, StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING)) {
            ByteBuffer data = ByteBuffer.wrap(bytes); while (data.hasRemaining()) output.write(data); output.force(true);
        }
        Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        try (FileChannel directory = FileChannel.open(file.getParent(), StandardOpenOption.READ)) { directory.force(true); }
        catch (FileSystemException unsupported) {
            // Windows does not generally expose directory handles through FileChannel. File force and
            // atomic publication remain required; this weaker power-loss profile is observable.
            if (!System.getProperty("os.name", "").toLowerCase(Locale.ROOT).startsWith("windows")) throw unsupported;
            durabilityProfile = "file_sync_atomic_publication_directory_sync_unavailable";
        }
    }

    private <T> CompletableFuture<T> submit(Callable<T> action) {
        CompletableFuture<T> result = new CompletableFuture<>();
        if (closed) return CompletableFuture.failedFuture(new IllegalStateException("Journal closed"));
        try { io.execute(() -> {
            try { result.complete(action.call()); } catch (Throwable failure) { result.completeExceptionally(failure); }
        }); } catch (RejectedExecutionException failure) { result.completeExceptionally(failure); }
        return result;
    }

    public CompletableFuture<Void> closeAsync() {
        synchronized (this) {
            if (closed) return closeFuture;
            closed = true; io.shutdown();
            // Cleanup cannot compete for capacity in the ordinary bounded append queue.
            Thread.ofVirtual().name("chat-companion-journal-close").start(() -> {
                try {
                    if (!io.awaitTermination(30, TimeUnit.SECONDS)) throw new IOException("journal_shutdown_deadline");
                    if (ownership != null) ownership.release(); if (channel != null) channel.close(); closeFuture.complete(null);
                } catch (Exception failure) { closeFuture.completeExceptionally(failure); }
            });
            return closeFuture;
        }
    }

    /** May block; callers on a game thread must use closeAsync(). */
    @Override public void close() { closeAsync().join(); }
}
