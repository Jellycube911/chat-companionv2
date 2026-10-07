package dev.chatcompanion.neoforge.client.speech;

import dev.chatcompanion.game.speech.PcmAudio;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import javax.sound.sampled.AudioFormat;
import net.minecraft.client.sounds.AudioStream;

/** PCM provider for Minecraft's own sound channels; no separate device/native codec is opened. */
final class PcmAudioStream implements AudioStream {
    private final AudioFormat format;
    private final ByteBuffer samples;
    private volatile boolean exhausted;
    private volatile boolean closed;

    PcmAudioStream(PcmAudio audio) {
        format = new AudioFormat(audio.sampleRate(), 16, audio.channels(), true, false);
        samples = audio.samples();
    }

    @Override public AudioFormat getFormat() { return format; }

    @Override public ByteBuffer read(int size) {
        if (size < 0) throw new IllegalArgumentException("Negative sound buffer size");
        int count = closed ? 0 : Math.min(size, samples.remaining());
        count -= count % format.getFrameSize();
        ByteBuffer result = ByteBuffer.allocateDirect(count).order(ByteOrder.LITTLE_ENDIAN);
        if (count > 0) {
            ByteBuffer slice = samples.slice();
            slice.limit(count);
            result.put(slice);
            samples.position(samples.position() + count);
        }
        exhausted = !samples.hasRemaining();
        return result.flip();
    }

    @Override public void close() { closed = true; }
    boolean exhausted() { return exhausted; }
}
