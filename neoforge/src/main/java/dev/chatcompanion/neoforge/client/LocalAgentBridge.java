package dev.chatcompanion.neoforge.client;

import com.google.gson.Gson;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.chatcompanion.neoforge.ChatCompanion;
import dev.chatcompanion.neoforge.CompanionEntity;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.fml.event.lifecycle.FMLClientSetupEvent;

/**
 * Tiny localhost-only HTTP bridge used to prove that an external agent can
 * observe and control the Chat Companion entity.
 *
 * <p>This is deliberately small. It is not the final MCP/OpenAI integration.
 */
@EventBusSubscriber(modid = ChatCompanion.MOD_ID, value = Dist.CLIENT, bus = EventBusSubscriber.Bus.MOD)
public final class LocalAgentBridge {
    private static final Gson GSON = new Gson();
    private static final int PORT = 8765;
    private static final int MAX_BODY_BYTES = 16 * 1024;
    private static final double COMPANION_SEARCH_RADIUS = 128.0;
    private static final double FORWARD_DISTANCE = 5.0;

    private static final ExecutorService HTTP_EXECUTOR = Executors.newCachedThreadPool(r -> {
        Thread thread = new Thread(r, "chat-companion-agent-http");
        thread.setDaemon(true);
        return thread;
    });

    private static volatile HttpServer server;

    private LocalAgentBridge() {}

    @SubscribeEvent
    public static void clientSetup(FMLClientSetupEvent event) {
        event.enqueueWork(LocalAgentBridge::start);
    }

    public static synchronized void start() {
        if (server != null) {
            return;
        }

        try {
            HttpServer created = HttpServer.create(new InetSocketAddress("127.0.0.1", PORT), 0);
            created.createContext("/state", LocalAgentBridge::handleState);
            created.createContext("/say", LocalAgentBridge::handleSay);
            created.createContext("/move-forward", LocalAgentBridge::handleMoveForward);
            created.setExecutor(HTTP_EXECUTOR);
            created.start();
            server = created;

            Runtime.getRuntime().addShutdownHook(new Thread(LocalAgentBridge::stop, "chat-companion-agent-shutdown"));
            System.out.println("[ChatCompanion] Local agent bridge listening on http://127.0.0.1:" + PORT);
        } catch (IOException failure) {
            System.err.println("[ChatCompanion] Could not start local agent bridge on port " + PORT);
            failure.printStackTrace();
        }
    }

    private static synchronized void stop() {
        HttpServer current = server;
        server = null;
        if (current != null) {
            current.stop(0);
        }
        HTTP_EXECUTOR.shutdownNow();
    }

    private static void handleState(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        Minecraft minecraft = Minecraft.getInstance();
        CompletableFuture<Map<String, Object>> result = new CompletableFuture<>();

        minecraft.execute(() -> {
            Map<String, Object> state = new LinkedHashMap<>();

            if (minecraft.player == null || minecraft.level == null) {
                state.put("inWorld", false);
                state.put("companionAvailable", false);
                result.complete(state);
                return;
            }

            state.put("inWorld", true);

            CompanionEntity companion = findCompanion(minecraft);
            if (companion == null) {
                state.put("companionAvailable", false);
                result.complete(state);
                return;
            }

            state.put("companionAvailable", true);
            state.put("body", "chatcompanion");
            state.put("entityId", companion.getId());
            state.put("x", companion.getX());
            state.put("y", companion.getY());
            state.put("z", companion.getZ());
            state.put("health", companion.getHealth());
            state.put("maxHealth", companion.getMaxHealth());
            state.put("dimension", companion.level().dimension().location().toString());
            state.put("yaw", companion.getYRot());
            state.put("jobType", companion.jobType().name());
            state.put("jobState", companion.jobState().name());
            state.put("jobReason", companion.reason());
            result.complete(state);
        });

        try {
            sendJson(exchange, 200, GSON.toJson(result.get(3, TimeUnit.SECONDS)));
        } catch (Exception failure) {
            sendJson(exchange, 500, "{\"error\":\"Could not read companion state\"}");
        }
    }

    private static void handleSay(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        byte[] body = exchange.getRequestBody().readAllBytes();
        if (body.length > MAX_BODY_BYTES) {
            sendJson(exchange, 413, "{\"error\":\"Request body too large\"}");
            return;
        }

        SayRequest request;
        try {
            request = GSON.fromJson(new String(body, StandardCharsets.UTF_8), SayRequest.class);
        } catch (RuntimeException failure) {
            sendJson(exchange, 400, "{\"error\":\"Invalid JSON\"}");
            return;
        }

        if (request == null || request.message() == null || request.message().isBlank()) {
            sendJson(exchange, 400, "{\"error\":\"Missing message\"}");
            return;
        }
        if (request.message().length() > 4096) {
            sendJson(exchange, 400, "{\"error\":\"Message too long\"}");
            return;
        }

        Minecraft minecraft = Minecraft.getInstance();
        minecraft.execute(() ->
                minecraft.gui.getChat().addMessage(Component.literal("[AI] " + request.message())));

        sendJson(exchange, 200, "{\"ok\":true}");
    }

    private static void handleMoveForward(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        Minecraft minecraft = Minecraft.getInstance();
        CompletableFuture<Map<String, Object>> result = new CompletableFuture<>();

        minecraft.execute(() -> {
            Map<String, Object> response = new LinkedHashMap<>();

            if (minecraft.player == null || minecraft.level == null || minecraft.player.connection == null) {
                response.put("ok", false);
                response.put("error", "Player is not connected to a world");
                result.complete(response);
                return;
            }

            CompanionEntity companion = findCompanion(minecraft);
            if (companion == null) {
                response.put("ok", false);
                response.put("error", "No nearby Chat Companion entity found. Spawn one first with /chat spawn.");
                result.complete(response);
                return;
            }

            double yawRadians = Math.toRadians(companion.getYRot());
            double dx = -Math.sin(yawRadians) * FORWARD_DISTANCE;
            double dz = Math.cos(yawRadians) * FORWARD_DISTANCE;

            int targetX = (int) Math.floor(companion.getX() + dx);
            int targetY = (int) Math.floor(companion.getY());
            int targetZ = (int) Math.floor(companion.getZ() + dz);

            minecraft.player.connection.sendCommand(
                    "chat move " + targetX + " " + targetY + " " + targetZ
            );

            response.put("ok", true);
            response.put("body", "chatcompanion");
            response.put("targetX", targetX);
            response.put("targetY", targetY);
            response.put("targetZ", targetZ);
            result.complete(response);
        });

        try {
            Map<String, Object> response = result.get(3, TimeUnit.SECONDS);
            int status = Boolean.TRUE.equals(response.get("ok")) ? 200 : 409;
            sendJson(exchange, status, GSON.toJson(response));
        } catch (Exception failure) {
            sendJson(exchange, 500, "{\"error\":\"Could not command companion movement\"}");
        }
    }

    private static CompanionEntity findCompanion(Minecraft minecraft) {
        if (minecraft.player == null || minecraft.level == null) {
            return null;
        }

        return minecraft.level
                .getEntitiesOfClass(
                        CompanionEntity.class,
                        minecraft.player.getBoundingBox().inflate(COMPANION_SEARCH_RADIUS))
                .stream()
                .min(Comparator.comparingDouble(entity -> entity.distanceToSqr(minecraft.player)))
                .orElse(null);
    }

    private static boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (method.equalsIgnoreCase(exchange.getRequestMethod())) {
            return true;
        }
        exchange.getResponseHeaders().set("Allow", method);
        sendJson(exchange, 405, "{\"error\":\"" + method + " only\"}");
        return false;
    }

    private static void sendJson(HttpExchange exchange, int status, String json) throws IOException {
        byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) {
            output.write(bytes);
        }
    }

    private record SayRequest(String message) {}
}
