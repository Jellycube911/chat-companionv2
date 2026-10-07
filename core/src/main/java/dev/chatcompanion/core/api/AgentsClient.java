package dev.chatcompanion.core.api;

import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.function.Consumer;

/** Remote workflow transport only. Callers persist identities and marshal callbacks into their actor. */
public interface AgentsClient extends AutoCloseable {
    CompletableFuture<RemoteSession> create(SessionCreate request);
    CompletableFuture<RemoteSession> retrieve(String sessionId);
    CompletableFuture<SubmissionAcceptance> submitMessage(String sessionId, String text, String idempotencyKey);
    CompletableFuture<SubmissionAcceptance> submitToolResult(String sessionId, ToolResult result);
    CompletableFuture<SubmissionAcceptance> cancel(String sessionId);
    CompletableFuture<List<RemoteItem>> items(String sessionId);
    CompletableFuture<RemoteTurn> turn(String sessionId, String turnId);
    /** Completes after SSE response headers establish the observer, before any subsequent submission. */
    CompletableFuture<StreamSubscription> subscribe(String sessionId, Consumer<AgentEvent> observer);
    @Override void close();

    record SessionCreate(String model, String instructions, List<String> toolsJson, String firstInput,
                         Map<String, String> metadata) {
        public SessionCreate {
            Objects.requireNonNull(model); Objects.requireNonNull(firstInput);
            toolsJson = List.copyOf(toolsJson); metadata = Map.copyOf(metadata);
            if (model.isBlank() || firstInput.isBlank()) throw new IllegalArgumentException("Initial input and model required");
        }
        public static SessionCreate companion(String instructions, List<String> toolsJson, String firstInput,
                                               Map<String, String> metadata) {
            return new SessionCreate("gpt-6-luna", instructions, toolsJson, firstInput, metadata);
        }
        @Override public String toString() { return "SessionCreate[configuration=redacted]"; }
    }

    enum SessionStatus { IDLE, IN_PROGRESS, REQUIRES_ACTION, FAILED, UNKNOWN }
    enum TurnStatus { QUEUED, IN_PROGRESS, WAITING, COMPLETED, FAILED, CANCELLED, UNKNOWN }
    sealed interface RequiredAction permits FunctionCall, UnsupportedAction {}
    record FunctionCall(String turnId, String callId, String name, String argumentsJson) implements RequiredAction {
        public FunctionCall {
            Objects.requireNonNull(turnId); Objects.requireNonNull(callId);
            Objects.requireNonNull(name); Objects.requireNonNull(argumentsJson);
        }
        @Override public String toString() { return "FunctionCall[content=redacted]"; }
    }
    record UnsupportedAction(String type) implements RequiredAction {
        @Override public String toString() { return "UnsupportedAction[type=redacted]"; }
    }
    record RemoteSession(String id, SessionStatus status, List<RequiredAction> requiredActions, String error) {
        public RemoteSession { requiredActions = List.copyOf(requiredActions); }
        @Override public String toString() { return "RemoteSession[status=" + status + ", actions=" + requiredActions.size() + ", error=redacted]"; }
    }
    /** Immutable saved item. arguments/output remain serialized; history is never an execution queue. */
    record RemoteItem(String id, String type, String turnId, String callId, String status,
                      String role, String phase, String outputJson, String error, String text) {
        public boolean completedFinalAnswer() {
            return "message".equals(type) && "assistant".equals(role)
                    && "completed".equals(status) && "final_answer".equals(phase) && id != null;
        }
        @Override public String toString() { return "RemoteItem[content=redacted]"; }
    }
    record RemoteTurn(String id, TurnStatus status, String errorCode) {
        public boolean terminal() { return status == TurnStatus.COMPLETED || status == TurnStatus.FAILED || status == TurnStatus.CANCELLED; }
        @Override public String toString() { return "RemoteTurn[status=" + status + ", errorCode=" + AgentsException.knownCode(errorCode) + "]"; }
    }
    /** Wire success is separate from acceptance of this submitted event. */
    record ToolResult(String turnId, String callId, boolean success, String output, String error) {
        public ToolResult {
            Objects.requireNonNull(turnId); Objects.requireNonNull(callId);
            if (success && (output == null || error != null)) throw new IllegalArgumentException("Successful result requires output only");
            if (!success && (error == null || output != null)) throw new IllegalArgumentException("Failed result requires error only");
        }
        public static ToolResult succeeded(String turnId, String callId, String output) {
            return new ToolResult(turnId, callId, true, output, null);
        }
        public static ToolResult failed(String turnId, String callId, String error) {
            return new ToolResult(turnId, callId, false, null, error);
        }
        @Override public String toString() { return "ToolResult[success=" + success + ", content=redacted]"; }
    }
    record SubmissionAcceptance(int statusCode) {
        public SubmissionAcceptance { if (statusCode != 202) throw new IllegalArgumentException("Events require HTTP 202"); }
    }
    /** Stream events are hints; reconnect and reconcile saved state rather than assuming replay. */
    record AgentEvent(String type, RemoteSession session, RemoteTurn turn, RemoteItem item, String itemId) {
        @Override public String toString() { return "AgentEvent[content=redacted]"; }
    }
    interface StreamSubscription extends AutoCloseable {
        CompletableFuture<Void> completion();
        @Override void close();
    }
}
