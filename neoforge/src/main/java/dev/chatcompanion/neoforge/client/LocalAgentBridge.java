package dev.chatcompanion.neoforge.client;

import com.google.gson.Gson;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.chatcompanion.neoforge.ChatCompanion;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
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
import net.neoforged.neoforge.client.event.MovementInputUpdateEvent;
import net.neoforged.neoforge.common.NeoForge;

/**
 * Tiny localhost-only HTTP bridge used to prove that an external agent can
 * observe and control the Minecraft client.
 *
 * <p>This is deliberately small. It is not the final MCP/OpenAI integration.
 */
@EventBusSubscriber(modid = ChatCompanion.MOD_ID, value = Dist.CLIENT, bus = EventBusSubscriber.Bus.MOD)
public final class LocalAgentBridge {
    private static final Gson GSON = new Gson();
    private static final int PORT = 8765;
    private static final int MAX_BODY_BYTES = 16 * 1024;
    private static final long MOVE_FORWARD_DURATION_NANOS = TimeUnit.MILLISECONDS.toNanos(500);

    private static final ExecutorService HTTP_EXECUTOR = Executors.newCachedThreadPool(r -> {
        Thread thread = new Thread(r, "chat-companion-agent-http");
        thread.setDaemon(true);
        return thread;
    });

    private static volatile HttpServer server;
    private static volatile long moveForwardUntilNanos;

    private LocalAgentBridge() {}

    @SubscribeEvent
    public static void clientSetup(FMLClientSetupEvent event) {
        event.enqueueWork(() -> {
            NeoForge.EVENT_BUS.addListener(LocalAgentBridge::movementInput);
            start();
        });
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

    private static void movementInput(MovementInputUpdateEvent event) {
        Minecraft minecraft = Minecraft.getInstance();
        if (event.getEntity() != minecraft.player || System.nanoTime() >= moveForwardUntilNanos) {
            return;
        }

        event.getInput().up = true;
        event.getInput().down = false;
        event.getInput().forwardImpulse = 1.0F;
    }

    private static void handleState(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        Minecraft minecraft = Minecraft.getInstance();
        CompletableFuture<Map<String, Object>> result = new CompletableFuture<>();

        minecraft.execute(() -> {
            Map<String, Object> state = new LinkedHashMap<>();
            if (minecraft.player == null) {
                state.put("inWorld", false);
            } else {
                state.put("inWorld", true);
                state.put("x", minecraft.player.getX());
                state.put("y", minecraft.player.getY());
                state.put("z", minecraft.player.getZ());
                state.put("health", minecraft.player.getHealth());
                state.put("food", minecraft.player.getFoodData().getFoodLevel());
                state.put("dimension", minecraft.player.level().dimension().location().toString());
            }
            result.complete(state);
        });

        try {
            sendJson(exchange, 200, GSON.toJson(result.get(3, TimeUnit.SECONDS)));
        } catch (Exception failure) {
            sendJson(exchange, 500, "{\"error\":\"Could not read Minecraft state\"}");
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
        if (minecraft.player == null) {
            sendJson(exchange, 409, "{\"error\":\"Player is not in a world\"}");
            return;
        }

        moveForwardUntilNanos = System.nanoTime() + MOVE_FORWARD_DURATION_NANOS;
        sendJson(exchange, 200, "{\"ok\":true}");
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
