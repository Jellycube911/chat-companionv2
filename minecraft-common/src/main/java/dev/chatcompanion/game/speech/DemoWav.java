package dev.chatcompanion.game.speech;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

/** Short two-tone diagnostic audio. This exercises playback and is never labelled as speech synthesis. */
public final class DemoWav {
    private DemoWav() {}

    public static byte[] create() {
        int sampleRate = 24_000;
        int frames = 18_000;
        ByteBuffer out = ByteBuffer.allocate(44 + frames * 2).order(ByteOrder.LITTLE_ENDIAN);
        tag(out, "RIFF"); out.putInt(out.capacity() - 8); tag(out, "WAVE");
        tag(out, "fmt "); out.putInt(16); out.putShort((short) 1); out.putShort((short) 1);
        out.putInt(sampleRate); out.putInt(sampleRate * 2); out.putShort((short) 2); out.putShort((short) 16);
        tag(out, "data"); out.putInt(frames * 2);
        for (int i = 0; i < frames; i++) {
            double seconds = (double) i / sampleRate;
            double frequency = i < frames / 2 ? 440 : 660;
            int segment = i % (frames / 2);
            double fade = Math.min(1.0, Math.min(segment, frames / 2 - segment - 1) / 400.0);
            out.putShort((short) (Math.sin(2 * Math.PI * frequency * seconds) * fade * 4_000));
        }
        return out.array();
    }

    private static void tag(ByteBuffer out, String tag) { out.put(tag.getBytes(StandardCharsets.US_ASCII)); }
}
