package dev.chatcompanion.game.speech;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.ReadOnlyBufferException;
import java.util.UUID;
import java.nio.file.Path;
import java.nio.file.Files;
import java.io.IOException;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class SpeechAudioTest {
    @TempDir Path temporary;

    @Test void durableAdmissionSurvivesRestartAndCorruptionFailsClosed() throws Exception {
        Path journal = temporary.resolve("admissions.log");
        UUID id = UUID.randomUUID();
        assertTrue(new SpeechAdmissionStore(journal).admit(id));
        assertFalse(new SpeechAdmissionStore(journal).admit(id));
        String valid = Files.readString(journal);
        char replacement = valid.charAt(0) == '0' ? '1' : '0';
        Files.writeString(journal, replacement + valid.substring(1));
        assertThrows(IOException.class, () -> new SpeechAdmissionStore(journal));
        Files.writeString(journal, "incomplete");
        assertThrows(IOException.class, () -> new SpeechAdmissionStore(journal));
    }
    @Test void decodesDiagnosticWavAndProtectsSamples() {
        byte[] wav = DemoWav.create();
        PcmAudio audio = WavDecoder.decode(wav);
        assertEquals(24_000, audio.sampleRate());
        assertEquals(1, audio.channels());
        assertEquals(750, audio.duration().toMillis());
        byte first = audio.samples().get(100);
        wav[144] ^= 0x7f;
        assertEquals(first, audio.samples().get(100));
        assertThrows(ReadOnlyBufferException.class, () -> audio.samples().put((byte) 0));
    }

    @Test void rejectsUnsupportedFormatAndForgedChunkSizes() {
        byte[] wav = DemoWav.create();
        ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN).putShort(20, (short) 3);
        assertThrows(IllegalArgumentException.class, () -> WavDecoder.decode(wav));
        byte[] forged = DemoWav.create();
        ByteBuffer.wrap(forged).order(ByteOrder.LITTLE_ENDIAN).putInt(40, -1);
        assertThrows(IllegalArgumentException.class, () -> WavDecoder.decode(forged));
        byte[] truncated = java.util.Arrays.copyOf(DemoWav.create(), 50);
        assertThrows(IllegalArgumentException.class, () -> WavDecoder.decode(truncated));
    }

    @Test void rejectsAudioOverDurationBudgetBeforePlayback() {
        assertThrows(IllegalArgumentException.class, () -> new PcmAudio(8_000, 1, new byte[8_000 * 2 * 91]));
        assertThrows(IllegalArgumentException.class, () -> new PcmAudio(24_000, 2, new byte[3]));
    }

    @Test void queueRequiresBackendAcknowledgementsAndDeduplicates() {
        SpeechQueue queue = new SpeechQueue(2, 4);
        UUID id = UUID.randomUUID();
        PcmAudio audio = WavDecoder.decode(DemoWav.create());
        assertEquals(SpeechQueue.EnqueueResult.ACCEPTED, queue.enqueue(id, audio));
        assertEquals(SpeechQueue.EnqueueResult.DUPLICATE, queue.enqueue(id, audio));
        assertEquals(id, queue.beginNext().orElseThrow().utteranceId());
        assertEquals(SpeechQueue.State.STARTING, queue.snapshot().active().state());
        assertThrows(IllegalStateException.class, () -> queue.completed(id));
        queue.started(id);
        queue.completed(id);
        assertEquals(SpeechQueue.State.PLAYED, queue.snapshot().last().state());
        assertEquals(SpeechQueue.EnqueueResult.DUPLICATE, queue.enqueue(id, audio));
    }

    @Test void saturationAndCancellationStayVisible() {
        SpeechQueue queue = new SpeechQueue(1, 4);
        PcmAudio audio = WavDecoder.decode(DemoWav.create());
        assertEquals(SpeechQueue.EnqueueResult.ACCEPTED, queue.enqueue(UUID.randomUUID(), audio));
        assertEquals(SpeechQueue.EnqueueResult.FULL, queue.enqueue(UUID.randomUUID(), audio));
        queue.beginNext();
        queue.enqueue(UUID.randomUUID(), audio);
        queue.cancelAll("disconnect");
        assertNull(queue.snapshot().active());
        assertEquals(0, queue.snapshot().queued());
        assertEquals(SpeechQueue.State.CANCELLED, queue.snapshot().last().state());
    }
}
