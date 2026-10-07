package dev.chatcompanion.core.api;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.chatcompanion.core.api.AgentsClient.*;
import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

final class HttpAgentsClientTest {
    private HttpServer server;
    private ExecutorService serverIo;
    private ExecutorService coreIo;
    private HttpAgentsClient client;
    private final AtomicReference<Handler> handler = new AtomicReference<>();
    private final AtomicReference<Throwable> serverFailure = new AtomicReference<>();
    private URI endpoint;

    @FunctionalInterface private interface Handler { void handle(HttpExchange exchange) throws Exception; }
    @BeforeEach void setup() throws IOException {
        coreIo = new ThreadPoolExecutor(4, 4, 0, TimeUnit.MILLISECONDS, new LinkedBlockingQueue<>(32));
        serverIo = Executors.newFixedThreadPool(4);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(serverIo);
        server.createContext("/", exchange -> {
            try { handler.get().handle(exchange); }
            catch (Throwable failure) { serverFailure.compareAndSet(null, failure); exchange.close(); }
        });
        server.start();
        endpoint = URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/");
        client = new HttpAgentsClient(endpoint, () -> null, coreIo, limits(4_194_304));
    }
    @AfterEach void teardown() throws InterruptedException {
        client.close(); server.stop(0); serverIo.shutdown(); coreIo.shutdownNow();
        // A released SSE/body handler must drain before examining failures. Interrupting it
        // immediately races the latch release and turns a successful cancellation into a failure.
        if (!serverIo.awaitTermination(5, TimeUnit.SECONDS)) {
            serverIo.shutdownNow();
            fail("Fake service handlers did not finish");
        }
        if (serverFailure.get() != null) throw new AssertionError("Fake service failed", serverFailure.get());
    }
    private static HttpAgentsClient.Options limits(int responseBytes) {
        return new HttpAgentsClient.Options(Duration.ofSeconds(2), Duration.ofSeconds(2), Duration.ofSeconds(2),
                1_048_576, responseBytes, 262_144, 8, 1000, 2);
    }
    private static void json(HttpExchange exchange, int status, String value) throws IOException {
        byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) { output.write(bytes); }
    }
    private static JsonObject request(HttpExchange exchange) throws IOException {
        return JsonParser.parseString(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)).getAsJsonObject();
    }
    private static <T> T await(CompletableFuture<T> future) throws Exception { return future.get(5, TimeUnit.SECONDS); }
    private static AgentsException failure(CompletableFuture<?> future) {
        ExecutionException exception = assertThrows(ExecutionException.class, () -> future.get(5, TimeUnit.SECONDS));
        assertInstanceOf(AgentsException.class, exception.getCause()); return (AgentsException) exception.getCause();
    }
    private static String idle() { return "{\"id\":\"sess_a\",\"status\":\"idle\",\"required_actions\":[],\"error\":null}"; }

    @Test void createsNoneSessionWithDurableFirstInputAndMetadataWithoutCreationRetryHeader() throws Exception {
        AtomicReference<JsonObject> body = new AtomicReference<>();
        handler.set(exchange -> {
            assertEquals("POST", exchange.getRequestMethod());
            assertEquals("/v1/agents/sessions", exchange.getRequestURI().getPath());
            assertEquals("agents=v1", exchange.getRequestHeaders().getFirst("OpenAI-Beta"));
            assertNull(exchange.getRequestHeaders().getFirst("Authorization"));
            assertNull(exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            body.set(request(exchange)); json(exchange, 201, idle());
        });
        RemoteSession session = await(client.create(SessionCreate.companion("Versioned instruction", List.of(),
                "Follow me.", Map.of("create_operation_id", "operation_a"))));
        assertEquals(SessionStatus.IDLE, session.status());
        JsonObject create = body.get();
        assertEquals("none", create.getAsJsonObject("environment").get("type").getAsString());
        assertEquals("gpt-6-luna", create.getAsJsonObject("agent").get("model").getAsString());
        assertEquals("low", create.getAsJsonObject("agent").getAsJsonObject("reasoning").get("effort").getAsString());
        assertEquals("Follow me.", create.getAsJsonArray("input").get(0).getAsJsonObject()
                .getAsJsonArray("content").get(0).getAsJsonObject().get("text").getAsString());
        assertEquals("operation_a", create.getAsJsonObject("metadata").get("create_operation_id").getAsString());
    }
    @Test void messageUsesCallerOwnedKeyAnd202IsOnlyAcceptance() throws Exception {
        List<String> keys = new ArrayList<>(); List<JsonObject> events = new ArrayList<>();
        handler.set(exchange -> {
            keys.add(exchange.getRequestHeaders().getFirst("Idempotency-Key")); events.add(request(exchange));
            json(exchange, 202, "accepted");
        });
        assertEquals(202, await(client.submitMessage("sess_a", "Stop", "logical_a")).statusCode());
        await(client.submitMessage("sess_a", "Stop", "logical_a"));
        await(client.submitMessage("sess_a", "Stop", "logical_b"));
        assertEquals(List.of("logical_a", "logical_a", "logical_b"), keys);
        assertEquals("agent.session.input.message", events.getFirst().getAsJsonArray("events").get(0)
                .getAsJsonObject().get("type").getAsString());
    }
    @Test void submitsSuccessFailureAndCancellationWithTheirExactWireFields() throws Exception {
        List<JsonObject> events = new ArrayList<>();
        handler.set(exchange -> { events.add(request(exchange).getAsJsonArray("events").get(0).getAsJsonObject()); json(exchange, 202, "{}"); });
        await(client.submitToolResult("sess_a", ToolResult.succeeded("turn_a", "call_a", "{\"status\":\"accepted\"}")));
        await(client.submitToolResult("sess_a", ToolResult.failed("turn_a", "call_b", "permission_denied")));
        await(client.cancel("sess_a"));
        assertTrue(events.get(0).get("success").getAsBoolean()); assertFalse(events.get(0).has("error"));
        assertEquals("{\"status\":\"accepted\"}", events.get(0).get("output").getAsString());
        assertFalse(events.get(1).get("success").getAsBoolean()); assertFalse(events.get(1).has("output"));
        assertEquals("call_b", events.get(1).get("call_id").getAsString());
        assertEquals("agent.session.input.cancel", events.get(2).get("type").getAsString());
    }
    @Test void onlyCurrentRequiredActionsAreExecutableAndUnsupportedTypesRemainDistinct() throws Exception {
        handler.set(exchange -> json(exchange, 200, """
                {"id":"sess_a","status":"requires_action","error":null,"required_actions":[
                  {"type":"function_call","turn_id":"turn_a","call_id":"call_a","name":"follow_player","arguments":{"stop_distance":3}},
                  {"type":"environment_connection","environment_id":"env_a"}],
                 "items":[{"type":"function_call","call_id":"historic","name":"mine_block"}]}
                """));
        RemoteSession session = await(client.retrieve("sess_a"));
        assertEquals(2, session.requiredActions().size());
        FunctionCall call = assertInstanceOf(FunctionCall.class, session.requiredActions().get(0));
        assertEquals("call_a", call.callId()); assertEquals("{\"stop_distance\":3}", call.argumentsJson());
        assertInstanceOf(UnsupportedAction.class, session.requiredActions().get(1));
    }
    @Test void paginatesAllItemsAscendingAndSeparatesFinalAnswerFromCommentary() throws Exception {
        AtomicInteger pages = new AtomicInteger();
        handler.set(exchange -> {
            int page = pages.incrementAndGet(); assertTrue(exchange.getRequestURI().getRawQuery().startsWith("order=asc&limit=100"));
            if (page == 1) json(exchange, 200, """
                    {"data":[{"id":"item_a","type":"message","turn_id":"turn_a","status":"completed",
                     "role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"Thinking"}]}],
                     "has_more":true,"last_id":"item_a"}
                    """);
            else {
                assertEquals("order=asc&limit=100&after=item_a", exchange.getRequestURI().getRawQuery());
                json(exchange, 200, """
                    {"data":[{"id":"item_b","type":"message","turn_id":"turn_a","status":"completed",
                     "role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"Hello"}]}],
                     "has_more":false,"last_id":"item_b"}
                    """);
            }
        });
        List<RemoteItem> items = await(client.items("sess_a"));
        assertEquals(2, pages.get()); assertFalse(items.get(0).completedFinalAnswer());
        assertTrue(items.get(1).completedFinalAnswer()); assertEquals("Hello", items.get(1).text());
    }
    @Test void repeatedPaginationCursorFailsWithoutInfiniteRequests() {
        AtomicInteger pages = new AtomicInteger();
        handler.set(exchange -> { pages.incrementAndGet(); json(exchange, 200, """
                {"data":[{"id":"item_a","type":"reasoning","turn_id":"turn_a","status":null}],
                 "has_more":true,"last_id":"item_a"}
                """); });
        assertEquals(AgentsException.Kind.PROTOCOL, failure(client.items("sess_a")).kind()); assertEquals(2, pages.get());
    }
    @Test void lostCreationResponseIsUncertainAndNeverAutomaticallyRetried() {
        AtomicInteger calls = new AtomicInteger();
        handler.set(exchange -> { calls.incrementAndGet(); request(exchange); exchange.close(); });
        AgentsException exception = failure(client.create(SessionCreate.companion("", List.of(), "Hello", Map.of())));
        assertTrue(exception.outcomeMayBeUnknown()); assertEquals(1, calls.get());
    }
    @Test void errorClassificationHonorsRetryAfterAndDoesNotLeakSecretsOrContent() {
        String privateMarker = "private-credential-and-conversation";
        client.close(); client = new HttpAgentsClient(endpoint, () -> privateMarker, coreIo, limits(4_194_304));
        handler.set(exchange -> {
            assertEquals("Bearer " + privateMarker, exchange.getRequestHeaders().getFirst("Authorization"));
            exchange.getResponseHeaders().add("Retry-After", "37");
            json(exchange, 429, "{\"error\":{\"code\":\"rate_limit_exceeded\",\"message\":\"" + privateMarker + "\"}}");
        });
        AgentsException exception = failure(client.submitMessage("sess_a", privateMarker, "logical_a"));
        assertEquals(AgentsException.Kind.RATE_LIMIT, exception.kind()); assertEquals(Duration.ofSeconds(37), exception.retryAfter());
        assertEquals("rate_limit_exceeded", exception.errorCode()); assertNull(exception.getCause());
        StringWriter output = new StringWriter(); exception.printStackTrace(new PrintWriter(output));
        assertFalse(output.toString().contains(privateMarker));
        assertFalse(SessionCreate.companion(privateMarker, List.of(), privateMarker, Map.of()).toString().contains(privateMarker));
        assertFalse(new SessionCreate(privateMarker, privateMarker, List.of(), privateMarker, Map.of()).toString().contains(privateMarker));
        assertFalse(new FunctionCall(privateMarker, privateMarker, privateMarker, privateMarker).toString().contains(privateMarker));
        assertFalse(new UnsupportedAction(privateMarker).toString().contains(privateMarker));
        assertFalse(new RemoteItem(privateMarker, privateMarker, privateMarker, privateMarker, privateMarker,
                privateMarker, privateMarker, privateMarker, privateMarker, privateMarker).toString().contains(privateMarker));
        assertFalse(new AgentEvent(privateMarker, null, null, null, privateMarker).toString().contains(privateMarker));
        assertFalse(new RemoteTurn(privateMarker, TurnStatus.FAILED, privateMarker).toString().contains(privateMarker));
        assertFalse(ToolResult.succeeded("turn_a", "call_a", privateMarker).toString().contains(privateMarker));
    }
    @Test void refusesCredentialRedirectsAndUnsafeOriginsOrPathIds() {
        AtomicInteger requests = new AtomicInteger();
        handler.set(exchange -> { requests.incrementAndGet(); exchange.getResponseHeaders().add("Location", "https://other.example/v1/"); json(exchange, 302, "{}"); });
        assertEquals(AgentsException.Kind.PROTOCOL, failure(client.retrieve("sess_a")).kind()); assertEquals(1, requests.get());
        assertThrows(IllegalArgumentException.class, () -> new HttpAgentsClient(URI.create("http://remote.example/v1/"), () -> "unused", coreIo));
        assertEquals(AgentsException.Kind.VALIDATION, failure(client.retrieve("../escape")).kind()); assertEquals(1, requests.get());
    }
    @Test void refusesOversizedResponseAndMalformedPendingArguments() {
        client.close(); client = new HttpAgentsClient(endpoint, () -> null, coreIo, limits(64));
        handler.set(exchange -> json(exchange, 200, "x".repeat(65)));
        assertEquals(AgentsException.Kind.CAPACITY, failure(client.retrieve("sess_a")).kind());
        client.close(); client = new HttpAgentsClient(endpoint, () -> null, coreIo, limits(4_194_304));
        handler.set(exchange -> json(exchange, 200, """
                {"id":"sess_a","status":"requires_action","required_actions":[
                 {"type":"function_call","turn_id":"turn_a","call_id":"call_a","name":"follow_player","arguments":"untrusted"}]}
                """));
        assertEquals(AgentsException.Kind.PROTOCOL, failure(client.retrieve("sess_a")).kind());
    }
    @Test void savedProofRequiresExactOutputAndCorrelationAndCanConfirmFailure() throws Exception {
        handler.set(exchange -> json(exchange, 200, """
                {"data":[
                  {"id":"item_a","type":"function_call_output","turn_id":"turn_a","call_id":"call_a","status":"completed","output":"accepted","error":null},
                  {"id":"item_b","type":"function_call_output","turn_id":"turn_a","call_id":"call_b","status":"failed","output":null,"error":"permission_denied"}],
                 "has_more":false,"last_id":"item_b"}
                """));
        List<RemoteItem> items = await(client.items("sess_a"));
        assertTrue(SavedResultProof.matches(items.get(0), ToolResult.succeeded("turn_a", "call_a", "accepted")));
        assertFalse(SavedResultProof.matches(items.get(0), ToolResult.succeeded("turn_a", "different_call", "accepted")));
        assertFalse(SavedResultProof.matches(items.get(0), ToolResult.succeeded("turn_a", "call_a", "completed")));
        assertTrue(SavedResultProof.matches(items.get(1), ToolResult.failed("turn_a", "call_b", "permission_denied")));
    }
    @Test void streamHasNoReplayCursorAndEstablishesObserverBeforeFollowup() throws Exception {
        CountDownLatch observed = new CountDownLatch(1); CountDownLatch closeStream = new CountDownLatch(1);
        AtomicReference<AgentEvent> received = new AtomicReference<>();
        handler.set(exchange -> {
            assertEquals("stream=true", exchange.getRequestURI().getRawQuery());
            assertNull(exchange.getRequestHeaders().getFirst("Last-Event-ID"));
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream"); exchange.sendResponseHeaders(200, 0);
            var output = exchange.getResponseBody();
            output.write("id: ignored\nevent: agent.session.idle\ndata: {\"type\":\"agent.session.idle\",\"session\":{\"id\":\"sess_a\",\"status\":\"idle\",\"required_actions\":[]}}\n\n".getBytes(StandardCharsets.UTF_8));
            output.flush(); closeStream.await(3, TimeUnit.SECONDS); exchange.close();
        });
        StreamSubscription subscription = await(client.subscribe("sess_a", event -> { received.set(event); observed.countDown(); }));
        assertTrue(observed.await(2, TimeUnit.SECONDS)); assertEquals("agent.session.idle", received.get().type());
        assertEquals(SessionStatus.IDLE, received.get().session().status());
        subscription.close(); await(subscription.completion()); closeStream.countDown();
    }
    @Test void idleSessionDoesNotBecomeSuccessfulTurn() throws Exception {
        handler.set(exchange -> json(exchange, 200, "{\"id\":\"turn_a\",\"status\":\"failed\",\"error\":{\"code\":\"request_timeout\",\"message\":\"private\"}}"));
        RemoteTurn turn = await(client.turn("sess_a", "turn_a"));
        assertEquals(TurnStatus.FAILED, turn.status()); assertTrue(turn.terminal()); assertEquals("request_timeout", turn.errorCode());
    }
    @Test void responseDeadlineIncludesBodyReadAndDoesNotWaitForServerEof() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        client.close();
        client = new HttpAgentsClient(endpoint, () -> null, coreIo,
                new HttpAgentsClient.Options(Duration.ofSeconds(1), Duration.ofMillis(250), Duration.ofSeconds(2),
                        1_048_576, 4_194_304, 262_144, 8, 1000, 2));
        handler.set(exchange -> {
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, 0);
            exchange.getResponseBody().write('{'); exchange.getResponseBody().flush();
            release.await(3, TimeUnit.SECONDS); exchange.close();
        });
        try {
            assertEquals(AgentsException.Kind.TIMEOUT, failure(client.retrieve("sess_a")).kind());
            assertEquals(1, release.getCount(), "Remote body is still open when local timeout completes");
        } finally { release.countDown(); }
    }
}
