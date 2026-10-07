package dev.chatcompanion.game.speech;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.time.Duration;
import java.util.Objects;

/** Immutable, bounded, little-endian signed PCM16 audio. No Minecraft/client dependencies. */
public final class PcmAudio {
    private final int sampleRate;
    private final int channels;
    private final byte[] samples;

    public PcmAudio(int sampleRate, int channels, byte[] samples) {
        if (sampleRate < 8_000 || sampleRate > 48_000 || channels < 1 || channels > 2) {
            throw new IllegalArgumentException("Unsupported PCM format");
        }
        Objects.requireNonNull(samples, "samples");
        if (samples.length == 0 || samples.length % (channels * 2) != 0
                || samples.length > WavDecoder.MAX_BYTES
                || (long) samples.length > (long) sampleRate * channels * 2 * WavDecoder.MAX_SECONDS) {
            throw new IllegalArgumentException("Invalid PCM length");
        }
        this.sampleRate = sampleRate;
        this.channels = channels;
        this.samples = samples.clone();
    }

    public int sampleRate() { return sampleRate; }
    public int channels() { return channels; }
    public int frameSize() { return channels * 2; }
    public int byteLength() { return samples.length; }
    public Duration duration() {
        return Duration.ofNanos((long) samples.length * 1_000_000_000L / (sampleRate * frameSize()));
    }

    /** Independent read-only cursor; callers cannot mutate the stored audio. */
    public ByteBuffer samples() {
        return ByteBuffer.wrap(samples).asReadOnlyBuffer().order(ByteOrder.LITTLE_ENDIAN);
    }
}
