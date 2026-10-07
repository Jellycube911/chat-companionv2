package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.CompletionException;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DurableJournalTest {

    @TempDir
    Path temporaryDirectory;

    @Test
    void replaysSequentialRecordsAndChecksumsAfterReopening() {
        Path path = temporaryDirectory.resolve("actions.jsonl");
        String firstChecksum;
        String secondChecksum;

        DurableJournal journal = new DurableJournal(path);
        try {
            assertTrue(journal.load().join().healthy());
            var first = journal.append("call-1", "PREPARED", payload("first")).join();
            var second = journal.append("call-2", "EXECUTED", payload("second")).join();

            assertEquals(1L, first.sequence());
            assertEquals(2L, second.sequence());
            assertNotNull(first.checksum());
            assertFalse(first.checksum().isBlank());
            assertNotNull(second.checksum());
            assertFalse(second.checksum().isBlank());
            firstChecksum = first.checksum();
            secondChecksum = second.checksum();
        } finally {
            journal.close();
        }

        DurableJournal reopened = new DurableJournal(path);
        try {
            var replay = reopened.load().join();
            assertTrue(replay.healthy(), () -> String.valueOf(replay.problem()));
            assertEquals(0L, replay.discardedTailBytes());
            assertEquals(2, replay.records().size());

            var first = replay.records().get(0);
            var second = replay.records().get(1);
            assertEquals(1L, first.sequence());
            assertEquals("call-1", first.aggregateId());
            assertEquals("PREPARED", first.type());
            assertEquals(payload("first"), first.payload());
            assertEquals(firstChecksum, first.checksum());
            assertEquals(2L, second.sequence());
            assertEquals("call-2", second.aggregateId());
            assertEquals("EXECUTED", second.type());
            assertEquals(payload("second"), second.payload());
            assertEquals(secondChecksum, second.checksum());

            var third = reopened.append("call-3", "CONFIRMED", payload("third")).join();
            assertEquals(3L, third.sequence(), "replay must restore the next sequence");
        } finally {
            reopened.close();
        }
    }

    @Test
    void discardsOnlyIncompleteTrailingRecordAndCanAppendAfterRecovery() throws Exception {
        Path path = temporaryDirectory.resolve("actions.jsonl");
        writeRecords(path, "one", "two");
        byte[] durablePrefix = Files.readAllBytes(path);
        byte[] incompleteTail = "{\"sequence\":3,\"aggregateId\":\"partial".getBytes(StandardCharsets.UTF_8);
        Files.write(path, concat(durablePrefix, incompleteTail));

        DurableJournal journal = new DurableJournal(path);
        try {
            var replay = journal.load().join();
            assertTrue(replay.healthy(), () -> String.valueOf(replay.problem()));
            assertEquals(2, replay.records().size());
            assertEquals(incompleteTail.length, replay.discardedTailBytes());
            assertArrayEquals(durablePrefix, Files.readAllBytes(path),
                "recovery must leave complete durable lines unchanged");
            assertEquals(3L, journal.append("call-3", "EXECUTED", payload("three")).join().sequence());
        } finally {
            journal.close();
        }

        DurableJournal reopened = new DurableJournal(path);
        try {
            var replay = reopened.load().join();
            assertTrue(replay.healthy());
            assertEquals(3, replay.records().size());
            assertEquals(payload("three"), replay.records().get(2).payload());
            assertEquals(0L, replay.discardedTailBytes());
        } finally {
            reopened.close();
        }
    }

    @Test
    void refusesCompleteCorruptRecordWithoutChangingJournalBytes() throws Exception {
        Path path = temporaryDirectory.resolve("actions.jsonl");
        writeRecords(path, "one", "original-payload");
        String valid = Files.readString(path, StandardCharsets.UTF_8);
        String corrupted = valid.replace("original-payload", "tampered-payload");
        assertFalse(valid.equals(corrupted), "fixture must modify a checksum-covered payload");
        Files.writeString(path, corrupted, StandardCharsets.UTF_8);
        byte[] beforeRecovery = Files.readAllBytes(path);

        DurableJournal journal = new DurableJournal(path);
        try {
            var replay = journal.load().join();
            assertFalse(replay.healthy(), "a complete line with an invalid checksum is corruption");
            assertNotNull(replay.problem());
            assertEquals(0L, replay.discardedTailBytes(), "complete corrupt lines are never torn tails");
            assertArrayEquals(beforeRecovery, Files.readAllBytes(path));
            assertThrows(CompletionException.class,
                () -> journal.append("call-3", "EXECUTED", payload("three")).join());
            assertArrayEquals(beforeRecovery, Files.readAllBytes(path),
                "an unhealthy journal must reject append without overwriting evidence");
        } finally {
            journal.close();
        }
    }

    @Test
    void detectsMissingMiddleSequenceEvenWhenRemainingChecksumsAreValid() throws Exception {
        Path path = temporaryDirectory.resolve("actions.jsonl");
        writeRecords(path, "one", "two", "three");
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        assertEquals(3, lines.size());
        Files.writeString(path, lines.get(0) + "\n" + lines.get(2) + "\n", StandardCharsets.UTF_8);
        byte[] beforeRecovery = Files.readAllBytes(path);

        DurableJournal journal = new DurableJournal(path);
        try {
            var replay = journal.load().join();
            assertFalse(replay.healthy(), "sequence 1 followed by 3 must not be accepted");
            assertNotNull(replay.problem());
            assertEquals(0L, replay.discardedTailBytes());
            assertThrows(CompletionException.class,
                () -> journal.append("call-4", "EXECUTED", payload("four")).join());
            assertArrayEquals(beforeRecovery, Files.readAllBytes(path));
        } finally {
            journal.close();
        }
    }

    @Test
    void snapshotsMutableInputBeforeAsynchronousAppend() throws Exception {
        Path path = temporaryDirectory.resolve("actions.jsonl");
        JsonObject original = payload("original");
        JsonObject nested = new JsonObject();
        nested.addProperty("target", "owner");
        original.add("nested", nested);
        JsonObject expected = original.deepCopy();

        DurableJournal journal = new DurableJournal(path);
        try {
            assertTrue(journal.load().join().healthy());
            var pending = journal.append("call-1", "PREPARED", original);
            original.addProperty("value", "changed-after-submission");
            nested.addProperty("target", "different-player");
            original.addProperty("new-field", true);

            var recorded = pending.join();
            assertEquals(expected, recorded.payload(), "append must own a deep snapshot of input");
            byte[] saved = Files.readAllBytes(path);
            original.remove("nested");
            original.addProperty("value", "changed-after-completion");
            assertArrayEquals(saved, Files.readAllBytes(path));
        } finally {
            journal.close();
        }

        DurableJournal reopened = new DurableJournal(path);
        try {
            var replay = reopened.load().join();
            assertTrue(replay.healthy());
            assertEquals(1, replay.records().size());
            assertEquals(expected, replay.records().get(0).payload());
        } finally {
            reopened.close();
        }
    }

    @Test
    void secondJournalCannotWriteWhileOriginalOwnerHoldsFileLock() throws Exception {
        Path path = temporaryDirectory.resolve("owned.jsonl");
        DurableJournal owner = new DurableJournal(path);
        DurableJournal contender = new DurableJournal(path);
        try {
            assertTrue(owner.load().join().healthy());
            owner.append("call-1", "PREPARED", payload("one")).join();
            byte[] beforeContention = Files.readAllBytes(path);

            assertThrows(CompletionException.class, () -> contender.load().join());
            assertThrows(CompletionException.class,
                () -> contender.append("foreign-call", "EXECUTED", payload("foreign")).join());
            assertArrayEquals(beforeContention, Files.readAllBytes(path));
            assertEquals(2L, owner.append("call-2", "EXECUTED", payload("two")).join().sequence());
        } finally {
            contender.close();
            owner.close();
        }

        DurableJournal reopened = new DurableJournal(path);
        try {
            var replay = reopened.load().join();
            assertTrue(replay.healthy());
            assertEquals(2, replay.records().size());
            assertEquals(payload("two"), replay.records().get(1).payload());
        } finally {
            reopened.close();
        }
    }

    @Test
    void checkpointDetectsDeletionOfWholePreviouslyAcknowledgedJournal() throws Exception {
        Path path = temporaryDirectory.resolve("deleted-journal.jsonl");
        writeRecords(path, "one", "two");
        Path checkpoint = path.resolveSibling(path.getFileName() + ".checkpoint");
        assertTrue(Files.exists(checkpoint), "acknowledged records require an independent durable anchor");
        byte[] anchorBeforeDeletion = Files.readAllBytes(checkpoint);
        Files.delete(path);

        DurableJournal journal = new DurableJournal(path);
        try {
            var replay = journal.load().join();
            assertFalse(replay.healthy(), "missing acknowledged history cannot be mistaken for a new journal");
            assertNotNull(replay.problem());
            assertThrows(CompletionException.class,
                () -> journal.append("call-3", "EXECUTED", payload("three")).join());
            assertFalse(Files.exists(path), "recovery must not replace the missing journal with empty history");
            assertArrayEquals(anchorBeforeDeletion, Files.readAllBytes(checkpoint));
        } finally {
            journal.close();
        }
    }

    @Test
    void checkpointDetectsMissingFinalCompleteAcknowledgedRecord() throws Exception {
        Path path = temporaryDirectory.resolve("deleted-final-record.jsonl");
        writeRecords(path, "one", "two", "three");
        Path checkpoint = path.resolveSibling(path.getFileName() + ".checkpoint");
        byte[] anchorBeforeTruncation = Files.readAllBytes(checkpoint);
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        assertEquals(3, lines.size());
        Files.writeString(path, lines.get(0) + "\n" + lines.get(1) + "\n", StandardCharsets.UTF_8);
        byte[] truncatedHistory = Files.readAllBytes(path);

        DurableJournal journal = new DurableJournal(path);
        try {
            var replay = journal.load().join();
            assertFalse(replay.healthy(), "a valid prefix is insufficient when a later record was acknowledged");
            assertNotNull(replay.problem());
            assertEquals(2, replay.records().size());
            assertEquals(0L, replay.discardedTailBytes(), "the missing record ended at a complete-line boundary");
            assertThrows(CompletionException.class,
                () -> journal.append("call-3-replacement", "EXECUTED", payload("replacement")).join());
            assertArrayEquals(truncatedHistory, Files.readAllBytes(path));
            assertArrayEquals(anchorBeforeTruncation, Files.readAllBytes(checkpoint));
        } finally {
            journal.close();
        }
    }

    private static void writeRecords(Path path, String... values) {
        DurableJournal journal = new DurableJournal(path);
        try {
            assertTrue(journal.load().join().healthy());
            for (int index = 0; index < values.length; index++) {
                journal.append("call-" + (index + 1), "EXECUTED", payload(values[index])).join();
            }
        } finally {
            journal.close();
        }
    }

    private static JsonObject payload(String value) {
        JsonObject payload = new JsonObject();
        payload.addProperty("value", value);
        return payload;
    }

    private static byte[] concat(byte[] prefix, byte[] tail) {
        byte[] combined = new byte[prefix.length + tail.length];
        System.arraycopy(prefix, 0, combined, 0, prefix.length);
        System.arraycopy(tail, 0, combined, prefix.length, tail.length);
        return combined;
    }
}
