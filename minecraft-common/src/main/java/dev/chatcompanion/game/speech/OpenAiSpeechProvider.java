package dev.chatcompanion.game.speech;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.Flow;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

/**
 * Optional server/gateway synthesis. Call only after separate remote-speech consent.
 * Produces bounded PCM16 WAV for client playback; never opens an audio device or retries a paid request.
 * Documentation checked 2026-10-07: https://developers.openai.com/api/docs/guides/text-to-speech
 */
public final class OpenAiSpeechProvider {
    public static final String DEFAULT_MODEL = "gpt-4o-mini-tts";
    public static final String DEFAULT_VOICE = "alloy";
    private final HttpClient client;
    private final URI endpoint;
    private final Supplier<String> credentials;
    private final String model;
    private final String voice;
    private final Duration deadline;
    private final Semaphore inFlight = new Semaphore(2);

    public OpenAiSpeechProvider(HttpClient client, URI baseUri, Supplier<String> credentials) {
        this(client, baseUri, credentials, DEFAULT_MODEL, DEFAULT_VOICE, Duration.ofSeconds(45));
    }

    public OpenAiSpeechProvider(HttpClient client, URI baseUri, Supplier<String> credentials,
            String model, String voice, Duration deadline) {
        this.client = Objects.requireNonNull(client);
        this.credentials = Objects.requireNonNull(credentials);
        Objects.requireNonNull(baseUri);
        if (baseUri.getRawUserInfo() != null || baseUri.getRawQuery() != null || baseUri.getRawFragment() != null
                || baseUri.getHost() == null || !("https".equalsIgnoreCase(baseUri.getScheme())
                || ("http".equalsIgnoreCase(baseUri.getScheme()) && isLoopback(baseUri.getHost())))) {
            throw new IllegalArgumentException("Speech API must use HTTPS (HTTP is allowed for loopback tests only)");
        }
        String normalized = baseUri.toString().replaceAll("/+$", "");
        this.endpoint = URI.create(normalized + "/audio/speech");
        this.model = identifier(model);
        this.voice = identifier(voice);
        this.deadline = Objects.requireNonNull(deadline);
        if (deadline.isNegative() || deadline.isZero() || deadline.compareTo(Duration.ofMinutes(2)) > 0) {
            throw new IllegalArgumentException("Speech deadline must be within 0–120 seconds");
        }
        if (client.followRedirects() != HttpClient.Redirect.NEVER) {
            throw new IllegalArgumentException("Speech HTTP client must not follow redirects with credentials");
        }
    }

    /** baseUri includes /v1, e.g. https://api.openai.com/v1. Credential is read only when explicitly invoked. */
    public CompletableFuture<byte[]> synthesise(String input) {
        Objects.requireNonNull(input);
        if (input.isBlank() || input.length() > 4096) {
            return CompletableFuture.failedFuture(new SpeechSynthesisException(SpeechSynthesisException.Kind.BAD_REQUEST, 0));
        }
        String key;
        try {
            key = credentials.get();
        } catch (RuntimeException ignored) {
            return CompletableFuture.failedFuture(new SpeechSynthesisException(SpeechSynthesisException.Kind.MISSING_CREDENTIALS, 0));
        }
        if (key == null || key.isBlank() || key.length() > 16_384
                || key.chars().anyMatch(character -> character < 33 || character > 126)) {
            return CompletableFuture.failedFuture(new SpeechSynthesisException(SpeechSynthesisException.Kind.MISSING_CREDENTIALS, 0));
        }
        if (!inFlight.tryAcquire()) {
            return CompletableFuture.failedFuture(new SpeechSynthesisException(SpeechSynthesisException.Kind.BUSY, 0));
        }
        JsonObject payload = new JsonObject();
        payload.addProperty("model", model);
        payload.addProperty("voice", voice);
        payload.addProperty("input", input);
        payload.addProperty("response_format", "wav");
        HttpRequest request = HttpRequest.newBuilder(endpoint).timeout(deadline)
                .header("Authorization", "Bearer " + key)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(payload.toString(), StandardCharsets.UTF_8)).build();
        AtomicReference<BoundedBody> received = new AtomicReference<>();
        CompletableFuture<byte[]> result = new CompletableFuture<>();
        CompletableFuture<HttpResponse<byte[]>> exchange;
        try {
            exchange = client.sendAsync(request, info -> {
                BoundedBody body = new BoundedBody(WavDecoder.MAX_BYTES);
                received.set(body);
                return body;
            });
        } catch (RuntimeException failure) {
            inFlight.release();
            return CompletableFuture.failedFuture(new SpeechSynthesisException(SpeechSynthesisException.Kind.NETWORK, 0));
        }
        exchange.whenComplete((response, failure) -> {
            if (failure != null) {
                Throwable cause = unwrap(failure);
                result.completeExceptionally(cause instanceof SpeechSynthesisException ? cause
                        : cause instanceof HttpTimeoutException
                        ? new SpeechSynthesisException(SpeechSynthesisException.Kind.DEADLINE, 0)
                        : new SpeechSynthesisException(SpeechSynthesisException.Kind.NETWORK, 0));
            } else if (response.statusCode() < 200 || response.statusCode() >= 300) {
                result.completeExceptionally(classify(response));
            } else {
                try {
                    WavDecoder.decode(response.body());
                    result.complete(response.body());
                } catch (RuntimeException invalid) {
                    result.completeExceptionally(new SpeechSynthesisException(SpeechSynthesisException.Kind.INVALID_AUDIO, response.statusCode()));
                }
            }
        });
        // HttpRequest timeout is insufficient for a body that stalls after headers; bound the entire operation.
        result.orTimeout(deadline.toMillis(), TimeUnit.MILLISECONDS);
        AtomicBoolean released = new AtomicBoolean();
        result.whenComplete((audio, failure) -> {
            if (!exchange.isDone()) {
                BoundedBody body = received.get();
                if (body != null) body.cancel();
                exchange.cancel(true);
            }
            if (released.compareAndSet(false, true)) inFlight.release();
        });
        CompletableFuture<byte[]> publicResult = new CompletableFuture<>();
        result.whenComplete((audio, failure) -> {
            if (failure == null) publicResult.complete(audio);
            else {
                Throwable cause = unwrap(failure);
                publicResult.completeExceptionally(cause instanceof TimeoutException
                        ? new SpeechSynthesisException(SpeechSynthesisException.Kind.DEADLINE, 0) : cause);
            }
        });
        publicResult.whenComplete((audio, failure) -> { if (publicResult.isCancelled()) result.cancel(true); });
        return publicResult;
    }

    private static SpeechSynthesisException classify(HttpResponse<byte[]> response) {
        int status = response.statusCode();
        SpeechSynthesisException.Kind kind = switch (status) {
            case 401, 403 -> SpeechSynthesisException.Kind.AUTHENTICATION;
            case 429 -> SpeechSynthesisException.Kind.RATE_LIMIT;
            case 500, 502, 503, 504 -> SpeechSynthesisException.Kind.SERVICE_UNAVAILABLE;
            default -> SpeechSynthesisException.Kind.BAD_REQUEST;
        };
        if (status == 429) {
            try {
                JsonObject error = JsonParser.parseString(new String(response.body(), StandardCharsets.UTF_8))
                        .getAsJsonObject().getAsJsonObject("error");
                if (error != null && error.has("code") && "insufficient_quota".equals(error.get("code").getAsString())) {
                    kind = SpeechSynthesisException.Kind.QUOTA_EXHAUSTED;
                }
            } catch (RuntimeException ignored) {
                // Classification cannot expose or depend on untrusted error text.
            }
        }
        return new SpeechSynthesisException(kind, status);
    }

    private static Throwable unwrap(Throwable failure) {
        while (failure instanceof CompletionException && failure.getCause() != null) failure = failure.getCause();
        return failure;
    }

    private static String identifier(String value) {
        if (value == null || !value.matches("[a-zA-Z0-9_.-]{1,80}")) {
            throw new IllegalArgumentException("Invalid speech model or voice identifier");
        }
        return value;
    }

    private static boolean isLoopback(String host) {
        return host.equals("127.0.0.1") || host.equals("localhost") || host.equals("[::1]") || host.equals("::1");
    }

    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final int limit;
        private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        private final CompletableFuture<byte[]> completed = new CompletableFuture<>();
        private volatile Flow.Subscription subscription;

        BoundedBody(int limit) { this.limit = limit; }
        @Override public CompletableFuture<byte[]> getBody() { return completed; }
        @Override public void onSubscribe(Flow.Subscription subscription) {
            this.subscription = subscription;
            if (completed.isDone()) subscription.cancel(); else subscription.request(1);
        }
        @Override public void onNext(List<ByteBuffer> chunks) {
            if (completed.isDone()) return;
            for (ByteBuffer chunk : chunks) {
                if (chunk.remaining() > limit - bytes.size()) {
                    completed.completeExceptionally(new SpeechSynthesisException(SpeechSynthesisException.Kind.RESPONSE_TOO_LARGE, 0));
                    subscription.cancel();
                    return;
                }
                byte[] copy = new byte[chunk.remaining()];
                chunk.get(copy);
                bytes.writeBytes(copy);
            }
            subscription.request(1);
        }
        @Override public void onError(Throwable failure) { completed.completeExceptionally(failure); }
        @Override public void onComplete() { completed.complete(bytes.toByteArray()); }
        void cancel() {
            completed.cancel(false);
            Flow.Subscription current = subscription;
            if (current != null) current.cancel();
        }
    }
}
