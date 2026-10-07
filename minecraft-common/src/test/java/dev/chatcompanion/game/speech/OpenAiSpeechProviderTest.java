package dev.chatcompanion.game.speech;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

class OpenAiSpeechProviderTest {
    @Test void synthesisesOnlyAgainstLoopbackAndUsesWavEnvelope() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        AtomicReference<JsonObject> sent = new AtomicReference<>();
        server.createContext("/v1/audio/speech", exchange -> {
            sent.set(JsonParser.parseString(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)).getAsJsonObject());
            byte[] wav = DemoWav.create();
            exchange.getResponseHeaders().set("Content-Type", "audio/wav");
            exchange.sendResponseHeaders(200, wav.length);
            exchange.getResponseBody().write(wav);
            exchange.close();
        });
        server.start();
        try {
            OpenAiSpeechProvider provider = provider(server, "loopback-test-token", Duration.ofSeconds(2));
            byte[] audio = provider.synthesise("Hello \"owner\"\nCafé").get(3, TimeUnit.SECONDS);
            assertEquals(750, WavDecoder.decode(audio).duration().toMillis());
            assertEquals("wav", sent.get().get("response_format").getAsString());
            assertEquals("gpt-4o-mini-tts", sent.get().get("model").getAsString());
            assertEquals("Hello \"owner\"\nCafé", sent.get().get("input").getAsString());
        } finally { server.stop(0); }
    }

    @Test void permanentErrorDoesNotExposeCredentialOrResponseBody() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/audio/speech", exchange -> {
            byte[] body = "do not disclose submitted chat or loopback-test-token".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(401, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        try {
            ExecutionException failure = assertThrows(ExecutionException.class,
                    () -> provider(server, "loopback-test-token", Duration.ofSeconds(2)).synthesise("private chat").get());
            SpeechSynthesisException safe = assertInstanceOf(SpeechSynthesisException.class, failure.getCause());
            assertEquals(SpeechSynthesisException.Kind.AUTHENTICATION, safe.kind());
            assertFalse(safe.toString().contains("loopback-test-token"));
            assertFalse(safe.toString().contains("private chat"));
            assertNull(safe.getCause());
        } finally { server.stop(0); }
    }

    @Test void stalledBodyHasAnOverallDeadline() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/audio/speech", exchange -> {
            exchange.sendResponseHeaders(200, 0);
            exchange.getResponseBody().write(new byte[] {1});
            exchange.getResponseBody().flush();
            try { Thread.sleep(1000); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
            exchange.close();
        });
        server.start();
        try {
            ExecutionException failure = assertThrows(ExecutionException.class,
                    () -> provider(server, "loopback-test-token", Duration.ofMillis(100)).synthesise("hello").get(2, TimeUnit.SECONDS));
            SpeechSynthesisException safe = assertInstanceOf(SpeechSynthesisException.class, failure.getCause());
            assertTrue(safe.kind() == SpeechSynthesisException.Kind.DEADLINE || safe.kind() == SpeechSynthesisException.Kind.NETWORK);
        } finally { server.stop(0); }
    }

    @Test void missingCredentialsAndUnsafeEndpointsFailBeforeRemoteWork() {
        HttpClient http = HttpClient.newHttpClient();
        OpenAiSpeechProvider absent = new OpenAiSpeechProvider(http, URI.create("https://api.openai.com/v1"), () -> null);
        ExecutionException failure = assertThrows(ExecutionException.class, () -> absent.synthesise("hello").get());
        assertEquals(SpeechSynthesisException.Kind.MISSING_CREDENTIALS,
                assertInstanceOf(SpeechSynthesisException.class, failure.getCause()).kind());
        assertThrows(IllegalArgumentException.class, () -> new OpenAiSpeechProvider(http, URI.create("http://example.com/v1"), () -> "unused"));
        assertThrows(IllegalArgumentException.class, () -> new OpenAiSpeechProvider(http, URI.create("https://key@example.com/v1"), () -> "unused"));
        HttpClient redirects = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.ALWAYS).build();
        assertThrows(IllegalArgumentException.class, () -> new OpenAiSpeechProvider(redirects, URI.create("https://api.openai.com/v1"), () -> "unused"));
    }

    @Test void oversizedResponseIsCancelledWithoutAllocatingAnUnboundedBody() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/audio/speech", exchange -> {
            exchange.sendResponseHeaders(200, WavDecoder.MAX_BYTES + 1);
            try {
                byte[] chunk = new byte[64 * 1024];
                for (int sent = 0; sent < WavDecoder.MAX_BYTES; sent += chunk.length) exchange.getResponseBody().write(chunk);
                exchange.getResponseBody().write(0);
            } catch (java.io.IOException cancelled) {
                // The client is expected to cancel reception after its bound.
            } finally { exchange.close(); }
        });
        server.start();
        try {
            ExecutionException failure = assertThrows(ExecutionException.class,
                    () -> provider(server, "loopback-test-token", Duration.ofSeconds(2)).synthesise("hello").get(3, TimeUnit.SECONDS));
            assertEquals(SpeechSynthesisException.Kind.RESPONSE_TOO_LARGE,
                    assertInstanceOf(SpeechSynthesisException.class, failure.getCause()).kind());
        } finally { server.stop(0); }
    }

    private static OpenAiSpeechProvider provider(HttpServer server, String fakeKey, Duration deadline) {
        return new OpenAiSpeechProvider(HttpClient.newHttpClient(),
                URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/v1"),
                () -> fakeKey, OpenAiSpeechProvider.DEFAULT_MODEL, OpenAiSpeechProvider.DEFAULT_VOICE, deadline);
    }
}
