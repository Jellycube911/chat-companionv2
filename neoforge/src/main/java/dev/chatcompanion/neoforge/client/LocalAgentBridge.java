package dev.chatcompanion.neoforge.client;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.chatcompanion.core.ActionOutcome;
import dev.chatcompanion.neoforge.ChatCompanion;
import dev.chatcompanion.neoforge.CompanionEntity;
import dev.chatcompanion.neoforge.CompanionService;
import dev.chatcompanion.neoforge.LocalAgentInbox;
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
import net.minecraft.core.Direction;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.SimpleContainer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.CraftingInput;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
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
    private static final int MAX_ENTITIES = 16;
    private static final int BLOCK_RADIUS = 8;
    private static final int BLOCK_VERTICAL_RADIUS = 4;
    private static final int MAX_BLOCK_TYPES = 24;

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
            created.createContext("/vision", LocalAgentBridge::handleVision);
            created.createContext("/find-blocks", LocalAgentBridge::handleFindBlocks);
            created.createContext("/inventory", LocalAgentBridge::handleInventory);
            created.createContext("/say", LocalAgentBridge::handleSay);
            created.createContext("/move-forward", LocalAgentBridge::handleMoveForward);
            created.createContext("/move-to", LocalAgentBridge::handleMoveTo);
            created.createContext("/look-at", LocalAgentBridge::handleLookAt);
            created.createContext("/follow-owner", LocalAgentBridge::handleFollowOwner);
            created.createContext("/stop-action", LocalAgentBridge::handleStopAction);
            created.createContext("/resume-action", LocalAgentBridge::handleResumeAction);
            created.createContext("/collect-items", LocalAgentBridge::handleCollectItems);
            created.createContext("/mine-block", LocalAgentBridge::handleMineBlock);
            created.createContext("/place-block", LocalAgentBridge::handlePlaceBlock);
            created.createContext("/attack-entity", LocalAgentBridge::handleAttackEntity);
            created.createContext("/take-held-item", LocalAgentBridge::handleTakeHeldItem);
            created.createContext("/craft", LocalAgentBridge::handleCraft);
            created.createContext("/equip-slot", LocalAgentBridge::handleEquipSlot);
            created.createContext("/chat-inbox", LocalAgentBridge::handleChatInbox);
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
                result.put("serverState", "running");

                boolean jobActive = companion.jobState() == CompanionEntity.JobState.RUNNING
                        || companion.jobState() == CompanionEntity.JobState.SUSPENDED;
                result.put("jobActive", jobActive);

                if (jobActive) {
                    result.put("jobType", companion.jobType().name());
                    result.put("jobState", companion.jobState().name());
                    result.put("jobReason", companion.reason());
                    result.put("jobProgress", companion.jobProgress());
                } else {
                    Map<String, Object> lastJob = new LinkedHashMap<>();
                    lastJob.put("type", companion.jobType().name());
                    lastJob.put("state", companion.jobState().name());
                    lastJob.put("reason", companion.reason());
                    result.put("lastJob", lastJob);
                }

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

    private static void handleVision(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        try {
            Map<String, Object> observation = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                ServerPlayer owner = context.owner();
                ServerLevel world = context.world();

                final double range = 18.0;
                final double horizontalFov = 100.0;
                final double verticalFov = 60.0;
                double[] yawOffsets = {-50, -35, -20, -10, 0, 10, 20, 35, 50};
                double[] pitchOffsets = {-25, -12, 0, 12, 25};

                Vec3 eye = companion.getEyePosition();
                List<Map<String, Object>> rays = new ArrayList<>();
                java.util.Set<BlockPos> seenBlocks = new java.util.HashSet<>();

                for (double pitchOffset : pitchOffsets) {
                    for (double yawOffset : yawOffsets) {
                        float yaw = (float) (companion.getYRot() + yawOffset);
                        float pitch = (float) (companion.getXRot() + pitchOffset);
                        double yawRad = Math.toRadians(-yaw - 180.0);
                        double pitchRad = Math.toRadians(-pitch);
                        double cosPitch = Math.cos(pitchRad);
                        Vec3 direction = new Vec3(
                                Math.sin(yawRad) * cosPitch,
                                Math.sin(pitchRad),
                                Math.cos(yawRad) * cosPitch);
                        Vec3 end = eye.add(direction.scale(range));

                        BlockHitResult hit = world.clip(new ClipContext(
                                eye,
                                end,
                                ClipContext.Block.OUTLINE,
                                ClipContext.Fluid.NONE,
                                companion));

                        if (hit.getType() != HitResult.Type.BLOCK) continue;
                        BlockPos pos = hit.getBlockPos();
                        if (!seenBlocks.add(pos)) continue;

                        BlockState state = world.getBlockState(pos);
                        Map<String, Object> ray = new LinkedHashMap<>();
                        ray.put("type", BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString());
                        ray.put("x", pos.getX());
                        ray.put("y", pos.getY());
                        ray.put("z", pos.getZ());
                        ray.put("distance", eye.distanceTo(hit.getLocation()));
                        ray.put("yawOffset", yawOffset);
                        ray.put("pitchOffset", pitchOffset);
                        rays.add(ray);
                    }
                }

                List<Map<String, Object>> visibleEntities = new ArrayList<>();
                Vec3 forward = companion.getViewVector(1.0F).normalize();
                List<Entity> nearby = new ArrayList<>(world.getEntities(
                        companion,
                        companion.getBoundingBox().inflate(range),
                        Entity::isAlive));
                nearby.sort(Comparator.comparingDouble(companion::distanceToSqr));

                for (Entity entity : nearby) {
                    if (visibleEntities.size() >= 12) break;
                    Vec3 toEntity = entity.getEyePosition().subtract(eye);
                    double distance = toEntity.length();
                    if (distance <= 0.001 || distance > range) continue;
                    double dot = forward.dot(toEntity.normalize());
                    if (dot < Math.cos(Math.toRadians(horizontalFov / 2.0))) continue;
                    if (!companion.hasLineOfSight(entity)) continue;

                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("uuid", entity.getUUID().toString());
                    item.put("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                    item.put("distance", distance);
                    item.put("x", entity.getX());
                    item.put("y", entity.getY());
                    item.put("z", entity.getZ());
                    item.put("owner", entity.getUUID().equals(owner.getUUID()));
                    item.put("hostile", entity instanceof Enemy);
                    if (entity instanceof LivingEntity living) {
                        item.put("health", living.getHealth());
                    }
                    if (entity instanceof ItemEntity dropped) {
                        ItemStack stack = dropped.getItem();
                        item.put("item", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                        item.put("count", stack.getCount());
                    }
                    visibleEntities.add(item);
                }

                Map<String, Object> result = new LinkedHashMap<>();
                result.put("range", range);
                result.put("horizontalFov", horizontalFov);
                result.put("verticalFov", verticalFov);
                result.put("yaw", companion.getYRot());
                result.put("pitch", companion.getXRot());
                result.put("blocks", rays);
                result.put("entities", visibleEntities);
                return result;
            });
            sendJson(exchange, 200, GSON.toJson(observation));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleFindBlocks(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        FindBlocksRequest request;
        try {
            request = readOptionalJson(exchange, FindBlocksRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        int radius = request == null || request.radius() == null ? 12 : request.radius();
        int limit = request == null || request.limit() == null ? 32 : request.limit();
        if (radius < 1 || radius > 20 || limit < 1 || limit > 128) {
            sendJson(exchange, 400, "{\"error\":\"radius must be 1-20 and limit must be 1-128\"}");
            return;
        }

        List<String> exact = request == null || request.exact() == null ? List.of() : request.exact();
        List<String> contains = request == null || request.contains() == null ? List.of() : request.contains();
        boolean exposedOnly = request != null && Boolean.TRUE.equals(request.exposed_only());
        if (exact.isEmpty() && contains.isEmpty()) {
            sendJson(exchange, 400, "{\"error\":\"provide exact block ids or contains patterns\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                CompanionEntity companion = context.companion();
                ServerLevel world = context.world();
                BlockPos origin = companion.blockPosition();
                List<Map<String, Object>> matches = new ArrayList<>();

                int minY = Math.max(world.getMinBuildHeight(), origin.getY() - radius);
                int maxY = Math.min(world.getMaxBuildHeight() - 1, origin.getY() + radius);

                for (int x = origin.getX() - radius; x <= origin.getX() + radius; x++) {
                    for (int y = minY; y <= maxY; y++) {
                        for (int z = origin.getZ() - radius; z <= origin.getZ() + radius; z++) {
                            BlockPos pos = new BlockPos(x, y, z);
                            if (!world.hasChunkAt(pos)) continue;
                            BlockState state = world.getBlockState(pos);
                            if (state.isAir()) continue;

                            String id = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
                            boolean matched = exact.contains(id);
                            if (!matched) {
                                for (String pattern : contains) {
                                    if (pattern != null && !pattern.isBlank() && id.contains(pattern)) {
                                        matched = true;
                                        break;
                                    }
                                }
                            }
                            if (!matched) continue;

                            if (exposedOnly) {
                                boolean exposed = false;
                                for (Direction direction : Direction.values()) {
                                    BlockPos neighbor = pos.relative(direction);
                                    BlockState neighborState = world.getBlockState(neighbor);
                                    if (neighborState.isAir() || neighborState.canBeReplaced()) {
                                        exposed = true;
                                        break;
                                    }
                                }
                                if (!exposed) continue;
                            }

                            Map<String, Object> block = new LinkedHashMap<>();
                            block.put("type", id);
                            block.put("x", pos.getX());
                            block.put("y", pos.getY());
                            block.put("z", pos.getZ());
                            block.put("distance", companion.position().distanceTo(Vec3.atCenterOf(pos)));
                            matches.add(block);
                        }
                    }
                }

                matches.sort(Comparator.comparingDouble(item -> ((Number) item.get("distance")).doubleValue()));
                if (matches.size() > limit) {
                    matches = new ArrayList<>(matches.subList(0, limit));
                }

                return Map.of(
                        "radius", radius,
                        "count", matches.size(),
                        "blocks", matches);
            });
            sendJson(exchange, 200, GSON.toJson(result));
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


    private static void handleLookAt(HttpExchange exchange) throws IOException {
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
                CompanionEntity companion = context.companion();
                double dx = request.x() + 0.5 - companion.getX();
                double dy = request.y() + 0.5 - companion.getEyeY();
                double dz = request.z() + 0.5 - companion.getZ();
                double horizontal = Math.sqrt(dx * dx + dz * dz);
                float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
                float pitch = (float) -Math.toDegrees(Math.atan2(dy, horizontal));
                pitch = Math.max(-90.0F, Math.min(90.0F, pitch));

                companion.getNavigation().stop();
                companion.setYRot(yaw);
                companion.setYBodyRot(yaw);
                companion.setYHeadRot(yaw);
                companion.setXRot(pitch);

                return Map.of(
                        "ok", true,
                        "yaw", yaw,
                        "pitch", pitch);
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
                ActionOutcome outcome = context.service().localAction(context.owner(), "follow_player", args);
                requireSuccess(outcome);
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
                JsonObject args = new JsonObject();
                args.addProperty("radius", radius);
                args.addProperty("max_items", maxItems);
                ActionOutcome outcome = context.service().localAction(context.owner(), "collect_items", args);
                requireSuccess(outcome);
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
                JsonObject args = blockArgs(context.companion(), request.x(), request.y(), request.z());
                ActionOutcome outcome = context.service().localAction(context.owner(), "mine_block", args);
                requireSuccess(outcome);
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
                JsonObject args = blockArgs(context.companion(), request.x(), request.y(), request.z());
                args.addProperty("inventory_slot", request.inventory_slot());
                args.addProperty("face", face);
                ActionOutcome outcome = context.service().localAction(context.owner(), "place_block", args);
                requireSuccess(outcome);
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
                JsonObject args = new JsonObject();
                args.addProperty("entity_id", request.entity_id());
                ActionOutcome outcome = context.service().localAction(context.owner(), "attack_entity", args);
                requireSuccess(outcome);
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

    private static void handleEquipSlot(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        EquipRequest request;
        try {
            request = readJson(exchange, EquipRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        if (request == null || request.slot() < 0 || request.slot() > 35) {
            sendJson(exchange, 400, "{\"error\":\"slot must be between 0 and 35\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                SimpleContainer inventory = context.companion().companionInventory();
                if (request.slot() == 0) {
                    ItemStack current = inventory.getItem(0);
                    return Map.of(
                            "ok", true,
                            "slot", 0,
                            "item", current.isEmpty() ? "" : BuiltInRegistries.ITEM.getKey(current.getItem()).toString());
                }

                ItemStack selected = inventory.getItem(request.slot()).copy();
                ItemStack oldMain = inventory.getItem(0).copy();
                inventory.setItem(0, selected);
                inventory.setItem(request.slot(), oldMain);
                companion.setItemSlot(EquipmentSlot.MAINHAND, selected.copy());

                return Map.of(
                        "ok", true,
                        "slot", 0,
                        "item", selected.isEmpty() ? "" : BuiltInRegistries.ITEM.getKey(selected.getItem()).toString());
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleChatInbox(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> {
                List<Map<String, Object>> messages = new ArrayList<>();
                for (LocalAgentInbox.Message message : LocalAgentInbox.drain(context.owner().getUUID(), 8)) {
                    messages.add(Map.of(
                            "id", message.id(),
                            "text", message.text()));
                }
                return Map.of("messages", messages);
            });
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static void handleCraft(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }

        CraftRequest request;
        try {
            request = readJson(exchange, CraftRequest.class);
        } catch (IllegalArgumentException failure) {
            sendJson(exchange, 400, GSON.toJson(Map.of("error", failure.getMessage())));
            return;
        }

        if (request == null
                || request.width() < 1 || request.width() > 3
                || request.height() < 1 || request.height() > 3
                || request.grid() == null
                || request.grid().size() != request.width() * request.height()) {
            sendJson(exchange, 400, "{\"error\":\"width/height must be 1-3 and grid length must equal width*height\"}");
            return;
        }

        int times = request.times() == null ? 1 : request.times();
        if (times < 1 || times > 64) {
            sendJson(exchange, 400, "{\"error\":\"times must be between 1 and 64\"}");
            return;
        }

        try {
            Map<String, Object> result = withCompanion(context -> craft(context, request, times));
            sendJson(exchange, 200, GSON.toJson(result));
        } catch (Exception failure) {
            sendFailure(exchange, failure);
        }
    }

    private static Map<String, Object> craft(ServerContext context, CraftRequest request, int times) {
        CompanionEntity companion = context.companion();
        ServerLevel world = context.world();

        if ((request.width() > 2 || request.height() > 2) && !hasNearbyCraftingTable(companion, world)) {
            throw new IllegalStateException("A 3x3 recipe requires a crafting table within reach.");
        }

        List<String> normalizedGrid = new ArrayList<>(request.grid().size());
        List<ItemStack> recipeStacks = new ArrayList<>(request.grid().size());
        for (String raw : request.grid()) {
            String id = raw == null ? "" : raw.strip();
            normalizedGrid.add(id);
            if (id.isEmpty()) {
                recipeStacks.add(ItemStack.EMPTY);
                continue;
            }

            ResourceLocation location;
            try {
                location = ResourceLocation.parse(id);
            } catch (RuntimeException failure) {
                throw new IllegalArgumentException("Invalid item id in crafting grid: " + id);
            }

            var item = BuiltInRegistries.ITEM.get(location);
            if (item == null || BuiltInRegistries.ITEM.getKey(item).equals(ResourceLocation.withDefaultNamespace("air"))) {
                throw new IllegalArgumentException("Unknown item id in crafting grid: " + id);
            }
            recipeStacks.add(new ItemStack(item));
        }

        CraftingInput input = CraftingInput.of(request.width(), request.height(), recipeStacks);
        var recipe = world.getRecipeManager()
                .getRecipeFor(RecipeType.CRAFTING, input, world)
                .orElseThrow(() -> new IllegalArgumentException("The supplied grid does not match a crafting recipe."));

        ItemStack sampleOutput = recipe.value().assemble(input, world.registryAccess());
        if (sampleOutput.isEmpty()) {
            throw new IllegalStateException("Crafting recipe produced no output.");
        }

        int completed = 0;
        int totalOutput = 0;
        for (int iteration = 0; iteration < times; iteration++) {
            if (!hasCraftingInputs(companion.companionInventory(), normalizedGrid)) break;

            consumeCraftingInputs(companion.companionInventory(), normalizedGrid);
            var remainders = recipe.value().getRemainingItems(input);
            for (ItemStack remainder : remainders) {
                if (!remainder.isEmpty()) addOrDrop(companion, remainder.copy());
            }

            ItemStack output = sampleOutput.copy();
            totalOutput += output.getCount();
            addOrDrop(companion, output);
            completed++;
        }

        if (completed == 0) {
            throw new IllegalStateException("Required crafting ingredients are not present in the companion inventory.");
        }

        return Map.of(
                "ok", true,
                "recipe", recipe.id().toString(),
                "item", BuiltInRegistries.ITEM.getKey(sampleOutput.getItem()).toString(),
                "count", totalOutput,
                "crafts", completed);
    }

    private static boolean hasCraftingInputs(SimpleContainer inventory, List<String> grid) {
        Map<String, Integer> needed = new LinkedHashMap<>();
        for (String id : grid) if (!id.isEmpty()) needed.merge(id, 1, Integer::sum);

        Map<String, Integer> available = new LinkedHashMap<>();
        for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
            ItemStack stack = inventory.getItem(slot);
            if (!stack.isEmpty()) {
                available.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(), stack.getCount(), Integer::sum);
            }
        }

        return needed.entrySet().stream().allMatch(entry ->
                available.getOrDefault(entry.getKey(), 0) >= entry.getValue());
    }

    private static void consumeCraftingInputs(SimpleContainer inventory, List<String> grid) {
        Map<String, Integer> needed = new LinkedHashMap<>();
        for (String id : grid) if (!id.isEmpty()) needed.merge(id, 1, Integer::sum);

        for (Map.Entry<String, Integer> entry : needed.entrySet()) {
            int remaining = entry.getValue();
            for (int slot = 0; slot < inventory.getContainerSize() && remaining > 0; slot++) {
                ItemStack stack = inventory.getItem(slot);
                if (stack.isEmpty()) continue;
                if (!BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(entry.getKey())) continue;
                int take = Math.min(remaining, stack.getCount());
                stack.shrink(take);
                remaining -= take;
                if (stack.isEmpty()) inventory.setItem(slot, ItemStack.EMPTY);
            }
        }
    }

    private static void addOrDrop(CompanionEntity companion, ItemStack stack) {
        ItemStack remainder = companion.companionInventory().addItem(stack);
        if (!remainder.isEmpty()) companion.spawnAtLocation(remainder);
    }

    private static boolean hasNearbyCraftingTable(CompanionEntity companion, ServerLevel world) {
        BlockPos origin = companion.blockPosition();
        for (BlockPos pos : BlockPos.betweenClosed(origin.offset(-4, -2, -4), origin.offset(4, 2, 4))) {
            if (!world.getBlockState(pos).is(Blocks.CRAFTING_TABLE)) continue;
            if (companion.getEyePosition().distanceToSqr(Vec3.atCenterOf(pos)) <= 4.5 * 4.5) return true;
        }
        return false;
    }

    private static void requireSuccess(ActionOutcome outcome) {
        if (!outcome.success()) {
            throw new IllegalStateException(outcome.reasonCode());
        }
    }

    private static Map<String, Object> queued(String action) {
        return Map.of("ok", true, "action", action);
    }

    private static JsonObject blockArgs(CompanionEntity companion, int x, int y, int z) {
        JsonObject args = new JsonObject();
        args.addProperty("dimension", companion.level().dimension().location().toString());
        args.addProperty("x", x);
        args.addProperty("y", y);
        args.addProperty("z", z);
        return args;
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
    private record CraftRequest(int width, int height, List<String> grid, Integer times) {}
    private record EquipRequest(int slot) {}
    private record FindBlocksRequest(List<String> exact, List<String> contains, Integer radius, Integer limit, Boolean exposed_only) {}

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
