package dev.chatcompanion.game.speech;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Objects;

/** Accepts RIFF/WAVE PCM16 only, rejecting malformed and oversized input before playback. */
public final class WavDecoder {
    public static final int MAX_BYTES = 4 * 1024 * 1024;
    public static final int MAX_SECONDS = 60;

    private WavDecoder() {}

    public static PcmAudio decode(byte[] wav) {
        Objects.requireNonNull(wav, "wav");
        if (wav.length < 44 || wav.length > MAX_BYTES) {
            throw new IllegalArgumentException("WAV size outside allowed bounds");
        }
        ByteBuffer in = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        if (!tag(wav, 0).equals("RIFF") || !tag(wav, 8).equals("WAVE")) {
            throw new IllegalArgumentException("Expected RIFF/WAVE audio");
        }
        long declaredSize = Integer.toUnsignedLong(in.getInt(4)) + 8;
        if (declaredSize != wav.length) {
            throw new IllegalArgumentException("WAV length does not match RIFF header");
        }
        int channels = 0;
        int sampleRate = 0;
        int dataStart = -1;
        int dataLength = 0;
        boolean formatSeen = false;
        for (int position = 12; position < wav.length; ) {
            if (wav.length - position < 8) {
                throw new IllegalArgumentException("Truncated WAV chunk header");
            }
            String chunk = tag(wav, position);
            long length = Integer.toUnsignedLong(in.getInt(position + 4));
            long end = (long) position + 8 + length;
            long next = end + (length & 1);
            if (end > wav.length || next > wav.length) {
                throw new IllegalArgumentException("Truncated WAV chunk");
            }
            int start = position + 8;
            if (chunk.equals("fmt ")) {
                if (formatSeen || length < 16) {
                    throw new IllegalArgumentException("Invalid or duplicate WAV format");
                }
                formatSeen = true;
                int format = Short.toUnsignedInt(in.getShort(start));
                channels = Short.toUnsignedInt(in.getShort(start + 2));
                sampleRate = in.getInt(start + 4);
                long byteRate = Integer.toUnsignedLong(in.getInt(start + 8));
                int frameSize = Short.toUnsignedInt(in.getShort(start + 12));
                int bits = Short.toUnsignedInt(in.getShort(start + 14));
                if (format != 1 || bits != 16 || channels < 1 || channels > 2
                        || sampleRate < 8_000 || sampleRate > 48_000
                        || frameSize != channels * 2 || byteRate != (long) sampleRate * frameSize) {
                    throw new IllegalArgumentException("Only 8–48 kHz mono/stereo PCM16 WAV is supported");
                }
            } else if (chunk.equals("data")) {
                if (dataStart != -1 || length == 0) {
                    throw new IllegalArgumentException("Invalid or duplicate WAV data");
                }
                dataStart = start;
                dataLength = (int) length;
            }
            position = (int) next;
        }
        if (!formatSeen || dataStart < 0) {
            throw new IllegalArgumentException("Missing WAV format or sample data");
        }
        return new PcmAudio(sampleRate, channels, Arrays.copyOfRange(wav, dataStart, dataStart + dataLength));
    }

    private static String tag(byte[] source, int position) {
        return new String(source, position, 4, StandardCharsets.US_ASCII);
    }
}
