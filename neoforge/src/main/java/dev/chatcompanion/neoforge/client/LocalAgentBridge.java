package dev.chatcompanion.neoforge.client;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.chatcompanion.neoforge.ChatCompanion;
import dev.chatcompanion.neoforge.CompanionEntity;
import dev.chatcompanion.neoforge.CompanionService;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.SimpleContainer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.Vec3;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.fml.event.lifecycle.FMLClientSetupEvent;

/**
 * Localhost-only HTTP bridge used by the external development agent.
 *
 * <p>Observation and physical control are performed against the authoritative
 * integrated-server CompanionEntity, not the human player's client body.
 */
@EventBusSubscriber(modid = ChatCompanion.MOD_ID, value = Dist.CLIENT, bus = EventBusSubscriber.Bus.MOD)
public final class LocalAgentBridge {
    private static final Gson GSON = new Gson();
    private static final int PORT = 8765;
    private static final int MAX_BODY_BYTES = 16 * 1024;
    private static final double FORWARD_DISTANCE = 5.0;
    private static final double MAX_MOVE_DISTANCE = 64.0;
    private static final double ENTITY_RADIUS = 16.0;
    private static final int MAX_ENTITIES = 32;
    private static final int BLOCK_RADIUS = 8;
    private static final int BLOCK_VERTICAL_RADIUS = 4;
    private static final int MAX_BLOCK_TYPES = 48;

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
            created.createContext("/nearby-entities", LocalAgentBridge::handleNearbyEntities);
            created.createContext("/nearby-blocks", LocalAgentBridge::handleNearbyBlocks);
            created.createContext("/inventory", LocalAgentBridge::handleInventory);
            created.createContext("/say", LocalAgentBridge::handleSay);
            created.createContext("/move-forward", LocalAgentBridge::handleMoveForward);
            created.createContext("/move-to", LocalAgentBridge::handleMoveTo);
            created.createContext("/follow-owner", LocalAgentBridge::handleFollowOwner);
            created.createContext("/stop-action", LocalAgentBridge::handleStopAction);
            created.createContext("/resume-action", LocalAgentBridge::handleResumeAction);
            created.createContext("/collect-items", LocalAgentBridge::handleCollectItems);
            created.createContext("/mine-block", LocalAgentBridge::handleMineBlock);
            created.createContext("/place-block", LocalAgentBridge::handlePlaceBlock);
            created.createContext("/attack-entity", LocalAgentBridge::handleAttackEntity);
            created.createContext("/take-held-item", LocalAgentBridge::handleTakeHeldItem);
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

        try {
            Map<String, Object> state = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                ServerPlayer owner = context.owner();

                Map<String, Object> result = new LinkedHashMap<>();
                result.put("inWorld", true);
                result.put("companionAvailable", true);
                result.put("body", "chatcompanion");
                result.put("entityId", companion.getId());
                result.put("uuid", companion.getUUID().toString());
                result.put("x", companion.getX());
                result.put("y", companion.getY());
                result.put("z", companion.getZ());
                result.put("health", companion.getHealth());
                result.put("maxHealth", companion.getMaxHealth());
                result.put("dimension", companion.level().dimension().location().toString());
                result.put("yaw", companion.getYRot());
                result.put("pitch", companion.getXRot());
                result.put("facing", facingName(companion.getYRot()));
                result.put("jobType", companion.jobType().name());
                result.put("jobState", companion.jobState().name());
                result.put("jobReason", companion.reason());
                result.put("worldActionsAllowed", companion.actionsAllowed());

                Map<String, Object> ownerState = new LinkedHashMap<>();
                ownerState.put("x", owner.getX());
                ownerState.put("y", owner.getY());
                ownerState.put("z", owner.getZ());
                ownerState.put("distance", companion.distanceTo(owner));
                ownerState.put("dx", owner.getX() - companion.getX());
                ownerState.put("dy", owner.getY() - companion.getY());
                ownerState.put("dz", owner.getZ() - companion.getZ());
                result.put("owner", ownerState);

                return result;
            });
            sendJson(exchange, 200, GSON.toJson(state));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleNearbyEntities(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        try {
            Map<String, Object> observation = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                ServerPlayer owner = context.owner();
                ServerLevel world = context.world();

                List<Entity> entities = new ArrayList<>(world.getEntities(
                        companion,
                        companion.getBoundingBox().inflate(ENTITY_RADIUS),
                        Entity::isAlive));
                entities.sort(Comparator.comparingDouble(companion::distanceToSqr));

                List<Map<String, Object>> items = new ArrayList<>();
                for (Entity entity : entities) {
                    if (items.size() >= MAX_ENTITIES) {
                        break;
                    }

                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("uuid", entity.getUUID().toString());
                    item.put("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                    item.put("name", entity.getName().getString());
                    item.put("distance", companion.distanceTo(entity));
                    item.put("x", entity.getX());
                    item.put("y", entity.getY());
                    item.put("z", entity.getZ());
                    item.put("isOwner", entity.getUUID().equals(owner.getUUID()));
                    item.put("hostile", entity instanceof Enemy);
                    if (entity instanceof LivingEntity living) {
                        item.put("health", living.getHealth());
                        item.put("maxHealth", living.getMaxHealth());
                    }
                    if (entity instanceof ItemEntity dropped) {
                        ItemStack stack = dropped.getItem();
                        item.put("droppedItem", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                        item.put("itemName", stack.getHoverName().getString());
                        item.put("count", stack.getCount());
                    }
                    items.add(item);
                }

                Map<String, Object> result = new LinkedHashMap<>();
                result.put("radius", ENTITY_RADIUS);
                result.put("count", items.size());
                result.put("entities", items);
                return result;
            });
            sendJson(exchange, 200, GSON.toJson(observation));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleNearbyBlocks(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        try {
            Map<String, Object> observation = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                ServerLevel world = context.world();
                BlockPos origin = companion.blockPosition();

                Map<String, BlockAggregate> aggregates = new LinkedHashMap<>();
                int minY = Math.max(world.getMinBuildHeight(), origin.getY() - BLOCK_VERTICAL_RADIUS);
                int maxY = Math.min(world.getMaxBuildHeight() - 1, origin.getY() + BLOCK_VERTICAL_RADIUS);

                for (int x = origin.getX() - BLOCK_RADIUS; x <= origin.getX() + BLOCK_RADIUS; x++) {
                    for (int y = minY; y <= maxY; y++) {
                        for (int z = origin.getZ() - BLOCK_RADIUS; z <= origin.getZ() + BLOCK_RADIUS; z++) {
                            BlockPos pos = new BlockPos(x, y, z);
                            BlockState state = world.getBlockState(pos);
                            if (state.isAir()) {
                                continue;
                            }

                            String id = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
                            double distanceSquared = companion.position().distanceToSqr(Vec3.atCenterOf(pos));
                            aggregates.computeIfAbsent(id, ignored -> new BlockAggregate())
                                    .observe(pos, distanceSquared);
                        }
                    }
                }

                List<Map.Entry<String, BlockAggregate>> sorted = new ArrayList<>(aggregates.entrySet());
                sorted.sort(Comparator.comparingDouble(entry -> entry.getValue().nearestDistanceSquared));

                List<Map<String, Object>> blocks = new ArrayList<>();
                for (Map.Entry<String, BlockAggregate> entry : sorted) {
                    if (blocks.size() >= MAX_BLOCK_TYPES) {
                        break;
                    }

                    BlockAggregate aggregate = entry.getValue();
                    Map<String, Object> block = new LinkedHashMap<>();
                    block.put("type", entry.getKey());
                    block.put("count", aggregate.count);
                    block.put("nearestDistance", Math.sqrt(aggregate.nearestDistanceSquared));
                    block.put("nearestX", aggregate.nearestX);
                    block.put("nearestY", aggregate.nearestY);
                    block.put("nearestZ", aggregate.nearestZ);
                    blocks.add(block);
                }

                Map<String, Object> result = new LinkedHashMap<>();
                result.put("horizontalRadius", BLOCK_RADIUS);
                result.put("verticalRadius", BLOCK_VERTICAL_RADIUS);
                result.put("uniqueTypes", aggregates.size());
                result.put("blocks", blocks);
                return result;
            });
            sendJson(exchange, 200, GSON.toJson(observation));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleInventory(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        try {
            Map<String, Object> inventory = withCompanion(context -> {
                SimpleContainer container = context.companion().companionInventory();
                List<Map<String, Object>> items = new ArrayList<>();

                for (int slot = 0; slot < container.getContainerSize(); slot++) {
                    ItemStack stack = container.getItem(slot);
                    if (stack.isEmpty()) {
                        continue;
                    }

                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("slot", slot);
                    item.put("item", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                    item.put("name", stack.getHoverName().getString());
                    item.put("count", stack.getCount());
                    items.add(item);
                }

                Map<String, Object> result = new LinkedHashMap<>();
                result.put("size", container.getContainerSize());
                result.put("usedSlots", items.size());
                result.put("items", items);
                return result;
            });
            sendJson(exchange, 200, GSON.toJson(inventory));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleSay(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        SayRequest request;
        try {
            request = readJson(exchange, SayRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
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

        try {
            Map<String, Object> result = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                double yawRadians = Math.toRadians(companion.getYRot());
                Vec3 target = new Vec3(
                        companion.getX() - Math.sin(yawRadians) * FORWARD_DISTANCE,
                        companion.getY(),
                        companion.getZ() + Math.cos(yawRadians) * FORWARD_DISTANCE);

                validateMove(context.world(), companion, target, 1.5);
                UUID jobId = companion.move(target, 1.5);

                Map<String, Object> response = new LinkedHashMap<>();
                response.put("ok", true);
                response.put("body", "chatcompanion");
                response.put("jobId", jobId.toString());
                response.put("targetX", target.x);
                response.put("targetY", target.y);
                response.put("targetZ", target.z);
                return response;
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleMoveTo(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        MoveToRequest request;
        try {
            request = readJson(exchange, MoveToRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        if (request == null
                || !Double.isFinite(request.x())
                || !Double.isFinite(request.y())
                || !Double.isFinite(request.z())) {
            sendJson(exchange, 400, "{\"error\":\"x, y and z must be finite numbers\"}");
            return;
        }

        double stopDistance = request.stop_distance() == null ? 1.5 : request.stop_distance();
        if (!Double.isFinite(stopDistance) || stopDistance < 1.0 || stopDistance > 3.0) {
            sendJson(exchange, 400, "{\"error\":\"stop_distance must be between 1 and 3\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                Vec3 target = new Vec3(request.x(), request.y(), request.z());
                validateMove(context.world(), companion, target, stopDistance);
                UUID jobId = companion.move(target, stopDistance);

                Map<String, Object> response = new LinkedHashMap<>();
                response.put("ok", true);
                response.put("jobId", jobId.toString());
                response.put("targetX", target.x);
                response.put("targetY", target.y);
                response.put("targetZ", target.z);
                response.put("stopDistance", stopDistance);
                return response;
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }


    private static void handleFollowOwner(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        FollowRequest request;
        try {
            request = readOptionalJson(exchange, FollowRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        double stopDistance = request == null || request.stop_distance() == null ? 3.0 : request.stop_distance();
        if (!Double.isFinite(stopDistance) || stopDistance < 2.0 || stopDistance > 8.0) {
            sendJson(exchange, 400, "{\"error\":\"stop_distance must be between 2 and 8\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                JsonObject args = new JsonObject();
                args.addProperty("player_id", context.owner().getUUID().toString());
                args.addProperty("stop_distance", stopDistance);
                context.service().action(context.owner(), "follow_player", args);
                return queued("follow_player");
            });
            sendJson(exchange, 202, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleStopAction(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                context.service().stop(context.owner());
                return Map.of("ok", true, "action", "stop");
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleResumeAction(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                context.service().resume(context.owner());
                return Map.of("ok", true, "action", "resume");
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleCollectItems(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        CollectRequest request;
        try {
            request = readOptionalJson(exchange, CollectRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        int radius = request == null || request.radius() == null ? 8 : request.radius();
        int maxItems = request == null || request.max_items() == null ? 16 : request.max_items();
        if (radius < 1 || radius > 8 || maxItems < 1 || maxItems > 32) {
            sendJson(exchange, 400, "{\"error\":\"radius must be 1-8 and max_items must be 1-32\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                requireWorldActions(context.companion());
                JsonObject args = new JsonObject();
                args.addProperty("radius", radius);
                args.addProperty("max_items", maxItems);
                context.service().action(context.owner(), "collect_items", args);
                return queued("collect_items");
            });
            sendJson(exchange, 202, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleMineBlock(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        BlockRequest request;
        try {
            request = readJson(exchange, BlockRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                requireWorldActions(context.companion());
                JsonObject args = blockArgs(context.companion(), request.x(), request.y(), request.z());
                context.service().action(context.owner(), "mine_block", args);
                return queued("mine_block");
            });
            sendJson(exchange, 202, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handlePlaceBlock(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        PlaceRequest request;
        try {
            request = readJson(exchange, PlaceRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        if (request.inventory_slot() < 0 || request.inventory_slot() > 35) {
            sendJson(exchange, 400, "{\"error\":\"inventory_slot must be between 0 and 35\"}");
            return;
        }
        String face = request.face() == null ? "up" : request.face().toLowerCase(java.util.Locale.ROOT);
        if (!java.util.Set.of("up", "down", "north", "south", "east", "west").contains(face)) {
            sendJson(exchange, 400, "{\"error\":\"face must be up, down, north, south, east, or west\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                requireWorldActions(context.companion());
                JsonObject args = blockArgs(context.companion(), request.x(), request.y(), request.z());
                args.addProperty("inventory_slot", request.inventory_slot());
                args.addProperty("face", face);
                context.service().action(context.owner(), "place_block", args);
                return queued("place_block");
            });
            sendJson(exchange, 202, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleAttackEntity(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        AttackRequest request;
        try {
            request = readJson(exchange, AttackRequest.class);
            UUID.fromString(request.entity_id());
        } catch (Exception failure) {
            sendJson(exchange, 400, "{\"error\":\"entity_id must be a valid entity UUID\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                requireWorldActions(context.companion());
                JsonObject args = new JsonObject();
                args.addProperty("entity_id", request.entity_id());
                context.service().action(context.owner(), "attack_entity", args);
                return queued("attack_entity");
            });
            sendJson(exchange, 202, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleTakeHeldItem(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                context.service().give(context.owner());
                return Map.of("ok", true, "action", "take_held_item");
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static Map<String, Object> queued(String action) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("queued", true);
        result.put("action", action);
        result.put("note", "The server job was submitted; use get_state/get_inventory to verify its result.");
        return result;
    }

    private static JsonObject blockArgs(CompanionEntity companion, int x, int y, int z) {
        JsonObject args = new JsonObject();
        args.addProperty("dimension", companion.level().dimension().location().toString());
        args.addProperty("x", x);
        args.addProperty("y", y);
        args.addProperty("z", z);
        return args;
    }

    private static void requireWorldActions(CompanionEntity companion) {
        if (!companion.actionsAllowed()) {
            throw new IllegalStateException("World-changing actions are disabled. Run /chat actions on in Minecraft first.");
        }
    }

    private static void validateMove(ServerLevel world, CompanionEntity companion, Vec3 target, double stopDistance) {
        BlockPos pos = BlockPos.containing(target);
        if (target.y < world.getMinBuildHeight() || target.y >= world.getMaxBuildHeight()) {
            throw new IllegalArgumentException("Destination height is outside the world");
        }
        if (!world.getWorldBorder().isWithinBounds(pos)) {
            throw new IllegalArgumentException("Destination is outside the world border");
        }
        if (!world.hasChunkAt(pos)) {
            throw new IllegalArgumentException("Destination chunk is not loaded");
        }
        if (companion.position().distanceTo(target) > MAX_MOVE_DISTANCE) {
            throw new IllegalArgumentException("Destination is more than 64 blocks away");
        }
        if (!Double.isFinite(stopDistance)) {
            throw new IllegalArgumentException("Invalid stop distance");
        }
    }

    private static <T> T withCompanion(Function<ServerContext, T> operation) throws Exception {
        Minecraft minecraft = Minecraft.getInstance();
        if (minecraft.player == null) {
            throw new IllegalStateException("Player is not connected to a world");
        }

        UUID ownerId = minecraft.player.getUUID();
        MinecraftServer integratedServer = minecraft.getSingleplayerServer();
        if (integratedServer == null) {
            throw new IllegalStateException("The local agent bridge currently requires a singleplayer/integrated server");
        }

        CompletableFuture<T> result = new CompletableFuture<>();
        integratedServer.execute(() -> {
            try {
                ServerPlayer owner = integratedServer.getPlayerList().getPlayer(ownerId);
                if (owner == null) {
                    throw new IllegalStateException("Owner player is unavailable on the server");
                }

                CompanionService service = ChatCompanion.service();
                if (service == null) {
                    throw new IllegalStateException("Companion server service is not ready");
                }

                CompanionEntity companion = service.find(ownerId);
                if (companion == null) {
                    throw new IllegalStateException("No loaded companion found. Use /chat spawn first.");
                }

                if (!(companion.level() instanceof ServerLevel world)) {
                    throw new IllegalStateException("Companion world is unavailable");
                }

                result.complete(operation.apply(new ServerContext(owner, companion, world, service)));
            } catch (Throwable failure) {
                result.completeExceptionally(failure);
            }
        });

        return result.get(5, TimeUnit.SECONDS);
    }

    private static <T> T readOptionalJson(HttpExchange exchange, Class<T> type) throws IOException {
        byte[] body = exchange.getRequestBody().readAllBytes();
        if (body.length == 0) {
            return null;
        }
        if (body.length > MAX_BODY_BYTES) {
            throw new IllegalArgumentException("Request body too large");
        }

        try {
            return GSON.fromJson(new String(body, StandardCharsets.UTF_8), type);
        } catch (RuntimeException failure) {
            throw new IllegalArgumentException("Invalid JSON");
        }
    }

    private static <T> T readJson(HttpExchange exchange, Class<T> type) throws IOException {
        byte[] body = exchange.getRequestBody().readAllBytes();
        if (body.length > MAX_BODY_BYTES) {
            throw new IllegalArgumentException("Request body too large");
        }

        try {
            return GSON.fromJson(new String(body, StandardCharsets.UTF_8), type);
        } catch (RuntimeException failure) {
            throw new IllegalArgumentException("Invalid JSON");
        }
    }

    private static String facingName(float yaw) {
        int index = Math.floorMod(Math.round(yaw / 90.0F), 4);
        return switch (index) {
            case 0 -> "south";
            case 1 -> "west";
            case 2 -> "north";
            default -> "east";
        };
    }

    private static boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (method.equalsIgnoreCase(exchange.getRequestMethod())) {
            return true;
        }
        exchange.getResponseHeaders().set("Allow", method);
        sendJson(exchange, 405, "{\"error\":\"" + method + " only\"}");
        return false;
    }

    private static void sendFailure(HttpExchange exchange, Exception failure) throws IOException {
        Throwable cause = failure;
        while (cause.getCause() != null
                && (cause instanceof java.util.concurrent.ExecutionException
                    || cause instanceof java.util.concurrent.CompletionException)) {
            cause = cause.getCause();
        }

        String message = cause.getMessage();
        if (message == null || message.isBlank()) {
            message = "Minecraft bridge operation failed";
        }

        int status = cause instanceof IllegalArgumentException ? 400 : 409;
        sendJson(exchange, status, GSON.toJson(Map.of("error", message)));
    }

    private static void sendJson(HttpExchange exchange, int status, String json) throws IOException {
        byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) {
            output.write(bytes);
        }
    }

    private record ServerContext(ServerPlayer owner, CompanionEntity companion, ServerLevel world, CompanionService service) {}
    private record SayRequest(String message) {}
    private record MoveToRequest(double x, double y, double z, Double stop_distance) {}
    private record FollowRequest(Double stop_distance) {}
    private record CollectRequest(Integer radius, Integer max_items) {}
    private record BlockRequest(int x, int y, int z) {}
    private record PlaceRequest(int x, int y, int z, int inventory_slot, String face) {}
    private record AttackRequest(String entity_id) {}

    private static final class BlockAggregate {
        private int count;
        private double nearestDistanceSquared = Double.POSITIVE_INFINITY;
        private int nearestX;
        private int nearestY;
        private int nearestZ;

        private void observe(BlockPos pos, double distanceSquared) {
            count++;
            if (distanceSquared < nearestDistanceSquared) {
                nearestDistanceSquared = distanceSquared;
                nearestX = pos.getX();
                nearestY = pos.getY();
                nearestZ = pos.getZ();
            }
        }
    }
}
