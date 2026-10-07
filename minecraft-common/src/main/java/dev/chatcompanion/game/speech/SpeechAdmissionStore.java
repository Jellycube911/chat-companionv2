package dev.chatcompanion.game.speech;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.zip.CRC32;

/**
 * Durable playback admission journal. Owned by one I/O actor; never call from a game/render thread.
 * An admitted ID is never replayed after restart, including a crash before source creation.
 * This favours suppressing an uncertain utterance over speaking it twice. Corruption fails closed.
 */
public final class SpeechAdmissionStore {
    private static final int MAX_ENTRIES = 100_000;
    private static final long MAX_FILE_BYTES = (long) MAX_ENTRIES * 46;
    private final Path journal;
    private final Set<UUID> admitted = new HashSet<>();

    public SpeechAdmissionStore(Path journal) throws IOException {
        this.journal = journal.toAbsolutePath();
        Files.createDirectories(this.journal.getParent());
        if (Files.exists(this.journal)) {
            if (Files.size(this.journal) > MAX_FILE_BYTES) throw new IOException("Speech admission journal exceeds capacity");
            byte[] contents = Files.readAllBytes(this.journal);
            if (contents.length > 0 && contents[contents.length - 1] != '\n') {
                throw new IOException("Incomplete speech admission journal; refusing uncertain replay");
            }
            try {
                for (String line : new String(contents, StandardCharsets.US_ASCII).split("\n")) {
                    if (!line.isEmpty()) {
                        if (line.length() != 45 || line.charAt(36) != ' ') throw new IllegalArgumentException();
                        String uuid = line.substring(0, 36);
                        UUID id = UUID.fromString(uuid);
                        if (!uuid.equals(id.toString()) || !line.substring(37).equals(checksum(uuid))) {
                            throw new IllegalArgumentException();
                        }
                        admitted.add(id);
                    }
                }
            } catch (IllegalArgumentException invalid) {
                throw new IOException("Invalid speech admission journal; refusing uncertain replay");
            }
        }
    }

    /** true only when a new ID has been forced to disk before playback side effects. */
    public boolean admit(UUID id) throws IOException {
        if (admitted.contains(id)) return false;
        if (admitted.size() >= MAX_ENTRIES) throw new IOException("Speech admission journal is full; archive it to resume speech");
        String uuid = id.toString();
        ByteBuffer entry = StandardCharsets.US_ASCII.encode(uuid + " " + checksum(uuid) + "\n");
        try (FileChannel file = FileChannel.open(journal, StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.APPEND)) {
            while (entry.hasRemaining()) file.write(entry);
            file.force(true);
        }
        admitted.add(id);
        return true;
    }

    private static String checksum(String uuid) {
        CRC32 crc = new CRC32();
        crc.update(uuid.getBytes(StandardCharsets.US_ASCII));
        return "%08x".formatted(crc.getValue());
    }
}
