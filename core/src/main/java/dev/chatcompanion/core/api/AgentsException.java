package dev.chatcompanion.core.api;

import java.time.Duration;

/** Never includes provider response bodies, request content, credentials, or underlying exception messages. */
public final class AgentsException extends RuntimeException {
    private static final long serialVersionUID = 1L;
    public enum Kind { VALIDATION, AUTHENTICATION, NOT_FOUND, CONFLICT, RATE_LIMIT, SERVICE, TIMEOUT,
                       CONNECTION, PROTOCOL, CAPACITY, CLOSED, CREDENTIAL_REQUIRED }
    private final Kind kind;
    private final int statusCode;
    private final Duration retryAfter;
    private final String errorCode;
    public AgentsException(Kind kind, int statusCode, Duration retryAfter, String errorCode) {
        super("Agents transport: " + kind + (statusCode == 0 ? "" : " (HTTP " + statusCode + ")"));
        this.kind = kind; this.statusCode = statusCode; this.retryAfter = retryAfter;
        this.errorCode = knownCode(errorCode);
    }
    public Kind kind() { return kind; }
    public int statusCode() { return statusCode; }
    public Duration retryAfter() { return retryAfter; }
    public String errorCode() { return errorCode; }
    /** Submission/create connection and timeout errors are uncertain outcomes, not rejection evidence. */
    public boolean outcomeMayBeUnknown() { return kind == Kind.CONNECTION || kind == Kind.TIMEOUT || kind == Kind.SERVICE; }
    static String knownCode(String code) {
        if (code == null) return null;
        return switch (code) {
            case "invalid_request_error", "invalid_beta", "agent_not_persisted", "invalid_otlp_endpoint",
                 "invalid_otlp_header", "unauthorized", "forbidden", "not_found_error", "model_not_found",
                 "conflict_error", "executor_version_incompatible", "mcp_server_startup_failed",
                 "files_api_rate_limit_exceeded", "rate_limit_exceeded", "insufficient_quota", "internal_error",
                 "server_error", "connection_failed", "request_timeout", "context_length_exceeded",
                 "active_turn_not_steerable", "flex_unavailable", "model_overloaded", "idle_timeout",
                 "max_turn_tokens_exceeded", "max_session_tokens_exceeded", "budget_exceeded" -> code;
            default -> null; // Do not echo arbitrary server-supplied strings into diagnostics.
        };
    }
}
