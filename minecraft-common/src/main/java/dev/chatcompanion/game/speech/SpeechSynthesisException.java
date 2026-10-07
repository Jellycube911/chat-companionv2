package dev.chatcompanion.game.speech;

/** Safe diagnostics: messages never contain credentials, submitted text, or remote response bodies. */
public final class SpeechSynthesisException extends RuntimeException {
    public enum Kind {
        MISSING_CREDENTIALS, AUTHENTICATION, BAD_REQUEST, RATE_LIMIT, QUOTA_EXHAUSTED,
        SERVICE_UNAVAILABLE, NETWORK, DEADLINE, RESPONSE_TOO_LARGE, INVALID_AUDIO, BUSY
    }
    private final Kind kind;
    private final int httpStatus;

    public SpeechSynthesisException(Kind kind, int httpStatus) {
        super("Speech synthesis failed: " + kind + (httpStatus == 0 ? "" : " (HTTP " + httpStatus + ")"));
        this.kind = kind;
        this.httpStatus = httpStatus;
    }
    public Kind kind() { return kind; }
    public int httpStatus() { return httpStatus; }
}
