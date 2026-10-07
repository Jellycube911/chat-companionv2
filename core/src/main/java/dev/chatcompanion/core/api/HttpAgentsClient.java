package dev.chatcompanion.core.api;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.StringReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * JDK HTTP transport without retries. Bodies and SSE are decoded on a caller-owned bounded IO pool.
 * API credentials are supplied in memory and sent only to the explicitly configured origin.
 * HttpClient owns its asynchronous network machinery; blocking body reads never run on game threads.
 */
public final class HttpAgentsClient implements AgentsClient {
    private static final Gson JSON = new Gson();
    private final URI baseUri;
    private final boolean loopback;
    private final Supplier<String> credential;
    private final ExecutorService coreIo;
    private final ScheduledExecutorService timer;
    private final Options options;
    private final HttpClient http;
    private final AtomicBoolean closed = new AtomicBoolean();
    private final Set<CompletableFuture<?>> pending = ConcurrentHashMap.newKeySet();
    private final Set<InputStream> openBodies = ConcurrentHashMap.newKeySet();
    private final Set<SseSubscription> subscriptions = ConcurrentHashMap.newKeySet();
    private final Semaphore streamSlots;
    private final Semaphore requestSlots = new Semaphore(32);

    public record Options(Duration connectTimeout, Duration requestTimeout, Duration streamIdleTimeout,
                          int maxRequestBytes, int maxResponseBytes, int maxSseEventBytes,
                          int maxPages, int maxItems, int maxStreams) {
        public Options {
            if (connectTimeout == null || requestTimeout == null || streamIdleTimeout == null
                    || connectTimeout.isNegative() || connectTimeout.isZero() || requestTimeout.isNegative()
                    || requestTimeout.isZero() || streamIdleTimeout.isNegative() || streamIdleTimeout.isZero()
                    || maxRequestBytes < 1 || maxResponseBytes < 1 || maxSseEventBytes < 1
                    || maxPages < 1 || maxItems < 1 || maxStreams < 1)
                throw new IllegalArgumentException("Positive transport limits required");
        }
        public static Options defaults() {
            return new Options(Duration.ofSeconds(10), Duration.ofSeconds(30), Duration.ofSeconds(60),
                    1_048_576, 4_194_304, 262_144, 100, 10_000, 4);
        }
    }

    public HttpAgentsClient(URI baseUri, Supplier<String> credential, ExecutorService coreIo) {
        this(baseUri, credential, coreIo, Options.defaults());
    }
    public HttpAgentsClient(URI baseUri, Supplier<String> credential, ExecutorService coreIo, Options options) {
        this.baseUri = validatedBase(baseUri);
        this.loopback = isLoopback(this.baseUri.getHost());
        this.credential = credential == null ? () -> null : credential;
        this.coreIo = java.util.Objects.requireNonNull(coreIo);
        this.options = java.util.Objects.requireNonNull(options);
        // An SSE observer holds an IO worker. Reserve workers for response bodies/persistence so
        // long-lived observers cannot occupy a known fixed pool completely and stall tool results.
        int streams = options.maxStreams();
        if (coreIo instanceof ThreadPoolExecutor pool) streams = Math.min(streams, Math.max(0, pool.getMaximumPoolSize() - 2));
        this.streamSlots = new Semaphore(streams);
        this.timer = Executors.newSingleThreadScheduledExecutor(task -> {
            Thread thread = new Thread(task, "chat-companion-api-timeouts"); thread.setDaemon(true); return thread;
        });
        this.http = HttpClient.newBuilder().connectTimeout(options.connectTimeout())
                .followRedirects(HttpClient.Redirect.NEVER).build();
    }

    @Override public CompletableFuture<RemoteSession> create(SessionCreate create) {
        return guarded(() -> {
            JsonObject body = new JsonObject();
            JsonObject agent = new JsonObject(); agent.addProperty("model", create.model());
            if (create.instructions() != null) agent.addProperty("instructions", create.instructions());
            JsonObject reasoning = new JsonObject(); reasoning.addProperty("effort", "low"); agent.add("reasoning", reasoning);
            JsonArray tools = new JsonArray();
            for (String tool : create.toolsJson()) {
                JsonElement parsed = parseJson(tool);
                if (!parsed.isJsonObject() || !"function".equals(string(parsed.getAsJsonObject(), "type", true)))
                    throw protocol();
                tools.add(parsed);
            }
            agent.add("tools", tools); body.add("agent", agent);
            JsonObject environment = new JsonObject(); environment.addProperty("type", "none"); body.add("environment", environment);
            body.add("input", input(create.firstInput()));
            JsonObject metadata = new JsonObject();
            if (create.metadata().size() > 16) throw validation();
            for (Map.Entry<String, String> entry : create.metadata().entrySet()) {
                if (entry.getKey().length() > 64 || entry.getValue().length() > 512) throw validation();
                metadata.addProperty(entry.getKey(), entry.getValue());
            }
            body.add("metadata", metadata);
            // Create idempotency is not a documented contract. Never retry creation in this transport.
            return request("POST", "agents/sessions", body, null, 201, HttpAgentsClient::session);
        });
    }
    @Override public CompletableFuture<RemoteSession> retrieve(String sessionId) {
        return guarded(() -> request("GET", sessionPath(sessionId), null, null, 200, HttpAgentsClient::session));
    }
    @Override public CompletableFuture<SubmissionAcceptance> submitMessage(String sessionId, String text, String key) {
        return guarded(() -> {
            if (key == null || key.isBlank() || key.length() > 256 || key.chars().anyMatch(c -> c < 32 || c > 126)) throw validation();
            JsonObject event = new JsonObject(); event.addProperty("type", "agent.session.input.message");
            event.add("input", input(text));
            return submit(sessionId, event, key);
        });
    }
    @Override public CompletableFuture<SubmissionAcceptance> submitToolResult(String sessionId, ToolResult result) {
        return guarded(() -> {
            identifier(result.turnId()); identifier(result.callId());
            JsonObject event = new JsonObject(); event.addProperty("type", "agent.session.input.tool_result");
            event.addProperty("turn_id", result.turnId()); event.addProperty("call_id", result.callId());
            event.addProperty("success", result.success());
            if (result.success()) event.addProperty("output", result.output()); else event.addProperty("error", result.error());
            return submit(sessionId, event, null);
        });
    }
    @Override public CompletableFuture<SubmissionAcceptance> cancel(String sessionId) {
        return guarded(() -> {
            JsonObject event = new JsonObject(); event.addProperty("type", "agent.session.input.cancel");
            return submit(sessionId, event, null);
        });
    }
    private CompletableFuture<SubmissionAcceptance> submit(String sessionId, JsonObject event, String key) {
        JsonObject body = new JsonObject(); JsonArray events = new JsonArray(); events.add(event); body.add("events", events);
        return request("POST", sessionPath(sessionId) + "/events", body, key, 202, ignored -> new SubmissionAcceptance(202));
    }
    @Override public CompletableFuture<List<RemoteItem>> items(String sessionId) {
        return guarded(() -> itemPage(sessionPath(sessionId) + "/items", null, new ArrayList<>(), new HashSet<>(), 0));
    }
    public CompletableFuture<List<RemoteItem>> turnItems(String sessionId, String turnId) {
        return guarded(() -> itemPage(sessionPath(sessionId) + "/turns/" + identifier(turnId) + "/items",
                null, new ArrayList<>(), new HashSet<>(), 0));
    }
    private CompletableFuture<List<RemoteItem>> itemPage(String path, String after, List<RemoteItem> all,
                                                         Set<String> cursors, int page) {
        if (page >= options.maxPages()) return CompletableFuture.failedFuture(capacity());
        String query = "?order=asc&limit=100" + (after == null ? "" : "&after=" + identifier(after));
        return request("GET", path + query, null, null, 200, Function.identity()).thenCompose(response -> {
            JsonObject object = object(response); JsonArray data = array(object, "data");
            for (JsonElement value : data) {
                if (all.size() >= options.maxItems()) throw capacity();
                all.add(item(object(value)));
            }
            boolean more = bool(object, "has_more", true);
            if (!more) return CompletableFuture.completedFuture(List.copyOf(all));
            String next = string(object, "last_id", true);
            if (data.isEmpty() || !cursors.add(next)) throw protocol();
            return itemPage(path, next, all, cursors, page + 1);
        });
    }
    @Override public CompletableFuture<RemoteTurn> turn(String sessionId, String turnId) {
        return guarded(() -> request("GET", sessionPath(sessionId) + "/turns/" + identifier(turnId),
                null, null, 200, HttpAgentsClient::turnFromJson));
    }

    @Override public CompletableFuture<StreamSubscription> subscribe(String sessionId, Consumer<AgentEvent> observer) {
        return guarded(() -> {
            if (observer == null) throw validation();
            if (!streamSlots.tryAcquire()) throw capacity();
            final HttpRequest request;
            try { request = build("GET", sessionPath(sessionId) + "/events?stream=true", null, null, true); }
            catch (RuntimeException failure) { streamSlots.release(); throw failure; }
            CompletableFuture<StreamSubscription> result = new CompletableFuture<>();
            CompletableFuture<HttpResponse<InputStream>> responseFuture = track(http.sendAsync(request, HttpResponse.BodyHandlers.ofInputStream()));
            result.whenComplete((ignored, failure) -> { if (result.isCancelled()) responseFuture.cancel(true); });
            responseFuture.whenComplete((response, failure) -> {
                if (failure != null) { streamSlots.release(); result.completeExceptionally(normalize(failure)); return; }
                if (closed.get() || result.isCancelled()) { closeBody(response.body()); streamSlots.release(); result.completeExceptionally(closedError()); return; }
                if (response.statusCode() != 200) {
                    streamSlots.release();
                    decode(response).whenComplete((body, error) -> result.completeExceptionally(
                            error == null ? httpError(response, body) : normalize(error)));
                    return;
                }
                if (!response.headers().firstValue("Content-Type").orElse("").toLowerCase(Locale.ROOT).startsWith("text/event-stream")) {
                    closeBody(response.body()); streamSlots.release(); result.completeExceptionally(protocol()); return;
                }
                SseSubscription subscription = new SseSubscription(response.body(), observer);
                subscriptions.add(subscription); openBodies.add(response.body());
                try {
                    subscription.start();
                    // Observer is connected before this future completes. Early events are delivered to caller's buffer.
                    result.complete(subscription);
                } catch (RuntimeException rejected) {
                    subscription.fail(capacity()); result.completeExceptionally(capacity());
                }
            });
            return track(result);
        });
    }

    private <T> CompletableFuture<T> request(String method, String path, JsonObject payload, String key,
                                              int expectedStatus, Function<JsonElement, T> mapper) {
        HttpRequest request = build(method, path, payload, key, false);
        if (!requestSlots.tryAcquire()) return CompletableFuture.failedFuture(capacity());
        final CompletableFuture<HttpResponse<InputStream>> wire;
        try { wire = track(http.sendAsync(request, HttpResponse.BodyHandlers.ofInputStream())); }
        catch (RuntimeException rejected) { requestSlots.release(); throw rejected; }
        AtomicBoolean expired = new AtomicBoolean();
        AtomicReference<InputStream> ownedBody = new AtomicReference<>();
        AtomicReference<CompletableFuture<String>> decoder = new AtomicReference<>();
        CompletableFuture<T> result = wire.thenCompose(response -> {
            ownedBody.set(response.body());
            if (expired.get()) { closeBody(response.body()); return CompletableFuture.failedFuture(timeout()); }
            CompletableFuture<String> decoded = decode(response);
            decoder.set(decoded);
            return decoded.thenApply(body -> {
            if (response.statusCode() != expectedStatus) throw httpError(response, body);
            // Events acceptance does not have an output schema. Do not turn a 202 into fictitious
            // rejection because an irrelevant body is empty or not JSON; saved proof follows later.
            JsonElement parsed = expectedStatus == 202 || body.isBlank() ? new JsonObject() : parseJson(body);
            try { return mapper.apply(parsed); }
            catch (AgentsException safe) { throw safe; }
            catch (RuntimeException | StackOverflowError malformed) { throw protocol(); }
            });
        });
        // HttpRequest.timeout alone does not bound a streamed InputStream body. This deadline
        // covers headers, IO-pool queuing and the complete response body as one operation.
        final ScheduledFuture<?> deadline;
        try {
            deadline = timer.schedule(() -> {
                expired.set(true); result.completeExceptionally(timeout()); wire.cancel(true);
                InputStream body = ownedBody.get(); if (body != null) closeBody(body);
                CompletableFuture<String> read = decoder.get(); if (read != null) read.cancel(true);
            }, options.requestTimeout().toMillis(), TimeUnit.MILLISECONDS);
        } catch (RejectedExecutionException stopped) {
            result.completeExceptionally(closedError()); wire.cancel(true);
            InputStream body = ownedBody.get(); if (body != null) closeBody(body);
            requestSlots.release(); return CompletableFuture.failedFuture(closedError());
        }
        result.whenComplete((ignored, failure) -> {
            deadline.cancel(false); requestSlots.release();
            if (result.isCancelled()) {
                expired.set(true); wire.cancel(true);
                InputStream body = ownedBody.get(); if (body != null) closeBody(body);
                CompletableFuture<String> read = decoder.get(); if (read != null) read.cancel(true);
            }
        });
        CompletableFuture<T> safe = result.exceptionallyCompose(failure -> CompletableFuture.failedFuture(normalize(failure)));
        safe.whenComplete((ignored, failure) -> { if (safe.isCancelled()) { wire.cancel(true); result.cancel(true); } });
        return track(safe);
    }
    private HttpRequest build(String method, String path, JsonObject payload, String key, boolean streaming) {
        if (closed.get()) throw closedError();
        URI target = baseUri.resolve(path);
        if (!sameOrigin(baseUri, target) || !target.getPath().startsWith(baseUri.getPath())) throw validation();
        HttpRequest.Builder builder = HttpRequest.newBuilder(target).timeout(options.requestTimeout())
                .header("OpenAI-Beta", "agents=v1").header("Accept", streaming ? "text/event-stream" : "application/json");
        final String token;
        try { token = credential.get(); } catch (RuntimeException secretFailure) { throw credentialError(); }
        if (token != null && !token.isBlank()) {
            if (token.length() > 4096 || token.chars().anyMatch(c -> c <= 32 || c >= 127)) throw credentialError();
            builder.header("Authorization", "Bearer " + token);
        } else if (!loopback) throw credentialError();
        if (key != null) builder.header("Idempotency-Key", key);
        byte[] body = payload == null ? new byte[0] : JSON.toJson(payload).getBytes(StandardCharsets.UTF_8);
        if (body.length > options.maxRequestBytes()) throw capacity();
        if (payload != null) builder.header("Content-Type", "application/json");
        return builder.method(method, payload == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofByteArray(body)).build();
    }
    private CompletableFuture<String> decode(HttpResponse<InputStream> response) {
        InputStream body = response.body(); openBodies.add(body);
        CompletableFuture<String> result = new CompletableFuture<>();
        AtomicBoolean timedOut = new AtomicBoolean();
        ScheduledFuture<?> deadline;
        try {
            deadline = timer.schedule(() -> { timedOut.set(true); closeBody(body); result.completeExceptionally(timeout()); },
                    options.requestTimeout().toMillis(), TimeUnit.MILLISECONDS);
        } catch (RejectedExecutionException stopped) { closeBody(body); openBodies.remove(body); return CompletableFuture.failedFuture(closedError()); }
        result.whenComplete((ignored, failure) -> { deadline.cancel(false); if (result.isCancelled()) closeBody(body); });
        try {
            coreIo.execute(() -> {
                try (body; ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
                    byte[] buffer = new byte[8192]; int count;
                    while ((count = body.read(buffer)) != -1) {
                        if (bytes.size() + count > options.maxResponseBytes()) throw capacity();
                        bytes.write(buffer, 0, count);
                    }
                    result.complete(utf8(bytes.toByteArray()));
                } catch (RuntimeException | IOException failure) {
                    result.completeExceptionally(timedOut.get() ? timeout() : normalize(failure));
                } finally { openBodies.remove(body); }
            });
        } catch (RejectedExecutionException saturated) {
            closeBody(body); openBodies.remove(body); result.completeExceptionally(capacity());
        }
        return track(result);
    }

    private final class SseSubscription implements StreamSubscription {
        private final InputStream body;
        private final Consumer<AgentEvent> observer;
        private final CompletableFuture<Void> completion = new CompletableFuture<>();
        private final AtomicBoolean done = new AtomicBoolean();
        private ScheduledFuture<?> idleDeadline;
        SseSubscription(InputStream body, Consumer<AgentEvent> observer) { this.body = body; this.observer = observer; }
        void start() { resetDeadline(); coreIo.execute(this::read); }
        synchronized void resetDeadline() {
            if (done.get()) return;
            if (idleDeadline != null) idleDeadline.cancel(false);
            idleDeadline = timer.schedule(() -> fail(timeout()), options.streamIdleTimeout().toMillis(), TimeUnit.MILLISECONDS);
        }
        void read() {
            ByteArrayOutputStream line = new ByteArrayOutputStream(); StringBuilder data = new StringBuilder();
            int frameBytes = 0; boolean skipLf = false;
            try {
                while (!done.get()) {
                    int next = body.read();
                    if (next == -1) { if (!done.get()) fail(connection()); return; }
                    if (skipLf && next == '\n') { skipLf = false; continue; }
                    skipLf = false;
                    if (++frameBytes > options.maxSseEventBytes()) throw capacity();
                    if (next != '\n' && next != '\r') { line.write(next); continue; }
                    skipLf = next == '\r';
                    String value = utf8(line.toByteArray()); line.reset(); resetDeadline();
                    if (value.isEmpty()) {
                        if (!data.isEmpty()) {
                            if (data.charAt(data.length() - 1) == '\n') data.setLength(data.length() - 1);
                            JsonObject event = object(parseJson(data.toString()));
                            observer.accept(eventFromJson(event));
                        }
                        data.setLength(0); frameBytes = 0;
                    } else if (value.startsWith("data:")) {
                        String content = value.substring(5); if (content.startsWith(" ")) content = content.substring(1);
                        data.append(content).append('\n');
                    }
                    // Ignore id/retry/event/comment fields. No Last-Event-ID or replay is supported.
                }
            } catch (IOException failure) { if (!done.get()) fail(connection()); }
            catch (RuntimeException | StackOverflowError failure) { if (!done.get()) fail(normalize(failure)); }
        }
        void fail(AgentsException failure) { finish(failure); }
        private void finish(AgentsException failure) {
            if (!done.compareAndSet(false, true)) return;
            synchronized (this) { if (idleDeadline != null) idleDeadline.cancel(false); }
            closeBody(body); openBodies.remove(body); subscriptions.remove(this); streamSlots.release();
            if (failure == null) completion.complete(null); else completion.completeExceptionally(failure);
        }
        @Override public CompletableFuture<Void> completion() { return completion; }
        @Override public void close() { finish(null); }
    }

    private static RemoteSession session(JsonElement value) {
        JsonObject object = object(value); String id = string(object, "id", true);
        SessionStatus status = enumValue(SessionStatus.class, string(object, "status", true), SessionStatus.UNKNOWN);
        JsonArray actions = array(object, "required_actions"); List<RequiredAction> required = new ArrayList<>();
        for (JsonElement candidate : actions) {
            JsonObject action = object(candidate); String type = string(action, "type", true);
            if ("function_call".equals(type)) {
                JsonElement arguments = action.get("arguments"); if (arguments == null || !arguments.isJsonObject()) throw protocol();
                required.add(new FunctionCall(string(action, "turn_id", true), string(action, "call_id", true),
                        string(action, "name", true), JSON.toJson(arguments)));
            } else required.add(new UnsupportedAction(type));
        }
        String error = string(object, "error", false);
        return new RemoteSession(id, status, required, error == null ? null : "session_failed");
    }
    private static RemoteTurn turnFromJson(JsonElement value) {
        JsonObject object = object(value); String errorCode = null;
        if (object.has("error") && !object.get("error").isJsonNull()) errorCode = string(object(object.get("error")), "code", false);
        return new RemoteTurn(string(object, "id", true), enumValue(TurnStatus.class, string(object, "status", true), TurnStatus.UNKNOWN),
                safeCode(errorCode));
    }
    private static RemoteItem item(JsonObject object) {
        String type = string(object, "type", true); String text = null;
        if ("message".equals(type)) {
            StringBuilder output = new StringBuilder();
            for (JsonElement element : array(object, "content")) {
                JsonObject part = object(element);
                if ("output_text".equals(string(part, "type", true))) output.append(string(part, "text", true));
            }
            text = output.toString();
        }
        JsonElement output = object.get("output");
        return new RemoteItem(string(object, "id", false), type, string(object, "turn_id", true),
                string(object, "call_id", false), string(object, "status", false), string(object, "role", false),
                string(object, "phase", false), output == null || output.isJsonNull() ? null : JSON.toJson(output),
                "function_call_output".equals(type) ? string(object, "error", false) : null, text);
    }
    private static AgentEvent eventFromJson(JsonObject event) {
        String type = string(event, "type", true);
        RemoteSession remoteSession = event.has("session") && !event.get("session").isJsonNull() ? session(event.get("session")) : null;
        RemoteTurn remoteTurn = event.has("turn") && !event.get("turn").isJsonNull() ? turnFromJson(event.get("turn")) : null;
        RemoteItem remoteItem = event.has("item") && !event.get("item").isJsonNull() ? item(object(event.get("item"))) : null;
        return new AgentEvent(type, remoteSession, remoteTurn, remoteItem, string(event, "item_id", false));
    }
    private static JsonArray input(String text) {
        if (text == null || text.isBlank()) throw validation();
        JsonObject message = new JsonObject(); message.addProperty("role", "user");
        JsonObject part = new JsonObject(); part.addProperty("type", "input_text"); part.addProperty("text", text);
        JsonArray content = new JsonArray(); content.add(part); message.add("content", content);
        JsonArray input = new JsonArray(); input.add(message); return input;
    }
    private <T> CompletableFuture<T> guarded(Supplier<CompletableFuture<T>> action) {
        try { return action.get(); } catch (RuntimeException failure) { return CompletableFuture.failedFuture(normalize(failure)); }
    }
    private <T> CompletableFuture<T> track(CompletableFuture<T> future) {
        pending.add(future); future.whenComplete((ignored, failure) -> pending.remove(future));
        if (closed.get()) future.completeExceptionally(closedError()); return future;
    }
    private static AgentsException httpError(HttpResponse<?> response, String body) {
        int code = response.statusCode(); AgentsException.Kind kind = switch (code) {
            case 400, 422 -> AgentsException.Kind.VALIDATION;
            case 401, 403 -> AgentsException.Kind.AUTHENTICATION;
            case 404 -> AgentsException.Kind.NOT_FOUND;
            case 409 -> AgentsException.Kind.CONFLICT;
            case 429 -> AgentsException.Kind.RATE_LIMIT;
            case 408, 504 -> AgentsException.Kind.TIMEOUT;
            default -> code >= 500 ? AgentsException.Kind.SERVICE : AgentsException.Kind.PROTOCOL;
        };
        String providerCode = null;
        try {
            JsonObject error = object(object(parseJson(body)).get("error")); providerCode = safeCode(string(error, "code", false));
        } catch (RuntimeException | StackOverflowError ignored) { /* Raw error text is intentionally discarded. */ }
        return new AgentsException(kind, code, retryAfter(response.headers().firstValue("Retry-After").orElse(null)), providerCode);
    }
    static Duration retryAfter(String value) {
        if (value == null || value.length() > 128) return null;
        try { long seconds = Long.parseLong(value.trim()); return seconds >= 0 ? Duration.ofSeconds(seconds) : null; }
        catch (NumberFormatException invalidSeconds) {
            try {
                Duration wait = Duration.between(Instant.now(), ZonedDateTime.parse(value, DateTimeFormatter.RFC_1123_DATE_TIME).toInstant());
                return wait.isNegative() ? Duration.ZERO : wait;
            } catch (RuntimeException invalidDate) { return null; }
        }
    }
    private static URI validatedBase(URI input) {
        if (input == null || input.getHost() == null || input.getRawUserInfo() != null || input.getRawQuery() != null
                || input.getRawFragment() != null || (!"https".equalsIgnoreCase(input.getScheme())
                && !("http".equalsIgnoreCase(input.getScheme()) && isLoopback(input.getHost()))))
            throw new IllegalArgumentException("HTTPS or loopback HTTP base URI required");
        String path = input.getPath();
        if (path == null || path.isEmpty() || "/".equals(path)) path = "/v1/";
        else if (!path.endsWith("/")) path += "/";
        if (!path.matches("/[A-Za-z0-9_/-]+/") || path.contains("//") || path.contains(".."))
            throw new IllegalArgumentException("Invalid API base path");
        try { return new URI(input.getScheme(), null, input.getHost(), input.getPort(), path, null, null); }
        catch (java.net.URISyntaxException invalid) { throw new IllegalArgumentException("Invalid API base URI"); }
    }
    private static boolean isLoopback(String host) {
        return "localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host) || "::1".equals(host) || "[::1]".equals(host);
    }
    private static boolean sameOrigin(URI a, URI b) {
        return a.getScheme().equalsIgnoreCase(b.getScheme()) && a.getHost().equalsIgnoreCase(b.getHost()) && a.getPort() == b.getPort();
    }
    private static String identifier(String id) {
        if (id == null || !id.matches("[A-Za-z0-9_-]{1,256}")) throw validation(); return id;
    }
    private static String sessionPath(String id) { return "agents/sessions/" + identifier(id); }
    private static JsonElement parseJson(String text) {
        try {
            int depth = 0; boolean quoted = false; boolean escaped = false;
            for (int i = 0; i < text.length(); i++) {
                char c = text.charAt(i);
                if (quoted) {
                    if (escaped) escaped = false;
                    else if (c == '\\') escaped = true;
                    else if (c == '"') quoted = false;
                } else if (c == '"') quoted = true;
                else if (c == '{' || c == '[') { if (++depth > 64) throw protocol(); }
                else if (c == '}' || c == ']') { if (--depth < 0) throw protocol(); }
            }
            if (depth != 0 || quoted) throw protocol();
            // JsonParser is permissive in Gson 2.10.1. Validate the complete JSON document first
            // with strict streaming syntax, then construct an immutable DTO from the parsed tree.
            try (JsonReader reader = new JsonReader(new StringReader(text))) {
                reader.setLenient(false); reader.skipValue();
                if (reader.peek() != JsonToken.END_DOCUMENT) throw protocol();
            }
            return JsonParser.parseString(text);
        } catch (RuntimeException | IOException | StackOverflowError invalid) { throw protocol(); }
    }
    private static JsonObject object(JsonElement element) {
        if (element == null || !element.isJsonObject()) throw protocol(); return element.getAsJsonObject();
    }
    private static JsonArray array(JsonObject object, String field) {
        JsonElement value = object.get(field); if (value == null || !value.isJsonArray()) throw protocol(); return value.getAsJsonArray();
    }
    private static String string(JsonObject object, String field, boolean required) {
        JsonElement value = object.get(field);
        if (value == null || value.isJsonNull()) { if (required) throw protocol(); return null; }
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString()) throw protocol(); return value.getAsString();
    }
    private static boolean bool(JsonObject object, String field, boolean required) {
        JsonElement value = object.get(field);
        if (value == null || value.isJsonNull()) { if (required) throw protocol(); return false; }
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isBoolean()) throw protocol(); return value.getAsBoolean();
    }
    private static <T extends Enum<T>> T enumValue(Class<T> type, String value, T fallback) {
        try { return Enum.valueOf(type, value.toUpperCase(Locale.ROOT)); } catch (IllegalArgumentException unknown) { return fallback; }
    }
    private static String safeCode(String code) { return AgentsException.knownCode(code); }
    private static String utf8(byte[] bytes) throws CharacterCodingException {
        return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString();
    }
    private static AgentsException normalize(Throwable failure) {
        while (failure instanceof CompletionException && failure.getCause() != null) failure = failure.getCause();
        if (failure instanceof AgentsException safe) return safe;
        if (failure instanceof HttpTimeoutException) return timeout();
        if (failure instanceof RejectedExecutionException) return capacity();
        if (failure instanceof IOException) return connection();
        return protocol();
    }
    private static AgentsException validation() { return new AgentsException(AgentsException.Kind.VALIDATION, 0, null, null); }
    private static AgentsException protocol() { return new AgentsException(AgentsException.Kind.PROTOCOL, 0, null, null); }
    private static AgentsException capacity() { return new AgentsException(AgentsException.Kind.CAPACITY, 0, null, null); }
    private static AgentsException connection() { return new AgentsException(AgentsException.Kind.CONNECTION, 0, null, null); }
    private static AgentsException timeout() { return new AgentsException(AgentsException.Kind.TIMEOUT, 0, null, null); }
    private static AgentsException closedError() { return new AgentsException(AgentsException.Kind.CLOSED, 0, null, null); }
    private static AgentsException credentialError() { return new AgentsException(AgentsException.Kind.CREDENTIAL_REQUIRED, 0, null, null); }
    private static void closeBody(InputStream body) { try { body.close(); } catch (IOException ignored) {} }
    @Override public void close() {
        if (!closed.compareAndSet(false, true)) return;
        for (SseSubscription subscription : subscriptions) subscription.close();
        for (InputStream body : openBodies) closeBody(body);
        for (CompletableFuture<?> future : pending) future.completeExceptionally(closedError());
        timer.shutdownNow(); http.shutdownNow();
        // coreIo is caller-owned and may also carry actor/persistence work.
    }
}
