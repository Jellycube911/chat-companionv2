package dev.chatcompanion.fabric;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.chatcompanion.core.*;
import dev.chatcompanion.core.api.HttpAgentsClient;
import dev.chatcompanion.core.agent.ChatSessionService;
import dev.chatcompanion.game.CompanionProfile;
import dev.chatcompanion.game.speech.OpenAiSpeechProvider;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import java.net.URI;
import java.net.http.HttpClient;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.Supplier;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceKey;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.core.registries.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.context.UseOnContext;
import net.minecraft.world.level.GameType;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.storage.LevelResource;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;

/** Logical-server boundary. All world access is scheduled through MinecraftServer.execute. */
public final class CompanionService implements AutoCloseable {
    private final MinecraftServer server;
    private final UUID runtimeEpoch;
    private final Map<UUID, Session> sessions = new HashMap<>();
    private final ConcurrentHashMap<UUID, Long> commandSequences = new ConcurrentHashMap<>();
    private final ExecutorService io = new ThreadPoolExecutor(4, 4, 0, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(256), r -> { Thread t = new Thread(r, "chat-companion-io"); t.setDaemon(true); return t; }, new ThreadPoolExecutor.AbortPolicy());
    private final CompletableFuture<String> contextId;
    private volatile boolean closed;
    private final OpenAiSpeechProvider speech;

    private static final class Session {
        final UUID owner;
        final UUID companion;
        final Bridge bridge;
        final CompanionActor actor;
        final CompletableFuture<Void> ready;
        volatile ChatSessionService remote;
        volatile HttpAgentsClient remoteClient;
        volatile boolean remoteConsent;
        volatile String remoteState = "WORKFLOW NOT STARTED";
        final Map<String, String> deliveries = new HashMap<>();
        final Map<String, List<String>> deliverySegments = new HashMap<>();
        final Map<String, String> utterances = new LinkedHashMap<>();
        String speechState = "IDLE";
        Session(UUID owner, UUID companion, Bridge bridge, CompanionActor actor, CompletableFuture<Void> ready) {
            this.owner = owner; this.companion = companion; this.bridge = bridge; this.actor = actor; this.ready = ready;
        }
    }
    CompanionService(MinecraftServer server, UUID epoch) {
        this.server = server; this.runtimeEpoch = epoch;
        Path data = server.getWorldPath(LevelResource.ROOT).resolve("chatcompanion");
        contextId = CompletableFuture.supplyAsync(() -> {
            try {
                Files.createDirectories(data);
                Path identity = data.resolve("context-id");
                if (!Files.exists(identity)) Files.writeString(identity, UUID.randomUUID().toString(), StandardOpenOption.CREATE_NEW, StandardOpenOption.SYNC);
                return UUID.fromString(Files.readString(identity).trim()).toString();
            } catch (Exception e) { throw new CompletionException(new IllegalStateException("Companion context storage unavailable")); }
        }, io);

        HttpClient client = HttpClient.newBuilder().executor(io).connectTimeout(Duration.ofSeconds(15)).followRedirects(HttpClient.Redirect.NEVER).build();
        speech = new OpenAiSpeechProvider(client, URI.create("https://api.openai.com/v1"), () -> System.getenv("OPENAI_API_KEY"));
    }

    public CompanionEntity find(UUID owner) {
        assertServer();
        CompanionRegistry.Reference ref = CompanionRegistry.get(server).get(owner);
        if (ref == null) return null;
        ServerLevel world = server.getLevel(ResourceKey.create(Registries.DIMENSION, ResourceLocation.parse(ref.dimension())));
        if (world == null) return null;
        Entity entity = world.getEntity(ref.entity());
        return entity instanceof CompanionEntity companion && owner.equals(companion.owner()) ? companion : null;
    }
    public CompanionEntity spawn(ServerPlayer owner) {
        assertServer();
        CompanionEntity existing = find(owner.getUUID());
        if (existing != null) return existing;
        if (CompanionRegistry.get(server).get(owner.getUUID()) != null) throw new IllegalStateException("Your companion is unloaded. Return to its chunk; a duplicate will not be spawned.");
        CompanionEntity companion = ChatCompanionFabric.COMPANION_TYPE.create(owner.serverLevel());
        if (companion == null) throw new IllegalStateException("Entity creation failed");
        companion.owner(owner.getUUID()); companion.setCustomName(Component.literal("Chat")); companion.setCustomNameVisible(true);
        BlockPos pos = owner.blockPosition().offset(2, 0, 0);
        if (!owner.serverLevel().getBlockState(pos).canBeReplaced()) pos = owner.blockPosition().offset(-2, 0, 0);
        companion.moveTo(pos.getX() + 0.5, pos.getY(), pos.getZ() + 0.5, owner.getYRot(), 0);
        if (!owner.serverLevel().addFreshEntity(companion)) throw new IllegalStateException("Entity spawn rejected");
        CompanionRegistry.get(server).set(owner.getUUID(), companion.getUUID(), owner.level().dimension().location().toString());
        return companion;
    }
    private CompletableFuture<Session> session(ServerPlayer owner) {
        assertServer();
        CompanionEntity companion = find(owner.getUUID());
        if (companion == null) return CompletableFuture.failedFuture(new IllegalStateException("Use /chat spawn first; companion must be loaded."));
        Session current = sessions.get(owner.getUUID());
        if (current != null && current.companion.equals(companion.getUUID())) return current.ready.thenApply(v -> current);
        if (sessions.size() >= 32) return CompletableFuture.failedFuture(new IllegalStateException("Companion session capacity reached"));
        CompletableFuture<Session> result = new CompletableFuture<>();
        contextId.whenComplete((id, failure) -> server.execute(() -> {
            if (closed) { result.completeExceptionally(new IllegalStateException("Server stopping")); return; }
            if (failure != null) { result.completeExceptionally(failure); return; }
            Session raced = sessions.get(owner.getUUID());
            if (raced != null && raced.companion.equals(companion.getUUID())) { raced.ready.whenComplete((v, e) -> { if (e != null) result.completeExceptionally(e); else result.complete(raced); }); return; }
            SessionKey key = new SessionKey(owner.getUUID(), id, id, companion.getUUID());
            Bridge bridge = new Bridge(owner.getUUID(), companion.getUUID());
            DurableJournal journal = new DurableJournal(server.getWorldPath(LevelResource.ROOT).resolve("chatcompanion/sessions").resolve(key.storageKey()).resolve("journal.jsonl"));
            CompanionActor actor = new CompanionActor(key, runtimeEpoch, journal, bridge);
            bridge.actor = actor;
            CompletableFuture<Void> ready = actor.start();
            Session created = new Session(owner.getUUID(), companion.getUUID(), bridge, actor, ready);
            created.remoteConsent = companion.remoteConsent();
            bridge.session = created; sessions.put(owner.getUUID(), created);
            ready.whenComplete((v, e) -> { if (e != null) result.completeExceptionally(e); else result.complete(created); });
        }));
        return result;
    }

    public void action(ServerPlayer owner, String name, JsonObject arguments) {
        long sequence = commandSequences.merge(owner.getUUID(), 1L, Long::sum);
        session(owner).thenCompose(s -> {
            CompletableFuture<ToolEntry> result = new CompletableFuture<>();
            server.execute(() -> {
                if (closed || commandSequences.getOrDefault(owner.getUUID(), -1L) != sequence) {
                    result.completeExceptionally(new IllegalStateException("Command superseded by newer control")); return;
                }
                s.actor.admitLocal(name, arguments).whenComplete((entry, failure) -> {
                    if (failure != null) result.completeExceptionally(failure); else result.complete(entry);
                });
            });
            return result;
        }).whenComplete((entry, failure) -> server.execute(() -> {
            if (closed) return;
            if (failure != null) message(owner, "Action rejected: " + safeFailure(failure));
            else if (entry.outcome() != null) message(owner, entry.outcome().status() + ": " + entry.outcome().reasonCode() + " " + entry.outcome().evidence());
        }));
    }
    public void stop(ServerPlayer owner) {
        commandSequences.merge(owner.getUUID(), 1L, Long::sum);
        CompanionEntity companion = find(owner.getUUID());
        if (companion != null) companion.stop("owner_stop");
        Session current = sessions.get(owner.getUUID());
        if (current != null) {
            current.actor.stop().whenComplete((receipt, failure) -> server.execute(() -> { if (!closed && (failure != null || !receipt.durable())) message(owner, "Stopped locally; recording failed. Restored jobs require explicit resume."); }));
            HttpAgentsClient remoteClient = current.remoteClient;
            if (remoteClient != null) current.actor.snapshot().thenAccept(snapshot -> { if (snapshot.remoteSessionId() != null) remoteClient.cancel(snapshot.remoteSessionId()).whenComplete((v, failure) -> { ChatSessionService remote = current.remote; if (remote != null && !closed) remote.reconcile(); }); });
        }
        message(owner, "Stopped locally.");
    }
    public void say(ServerPlayer owner, String text) {
        String normalized = text.strip();
        if (normalized.equalsIgnoreCase("follow me")) { JsonObject args = new JsonObject(); args.addProperty("player_id", owner.getUUID().toString()); args.addProperty("stop_distance", 3); action(owner, "follow_player", args); return; }
        if (normalized.equalsIgnoreCase("stop")) { stop(owner); return; }
        CompanionEntity companion = find(owner.getUUID());
        if (companion == null || !companion.remoteConsent()) { message(owner, "Remote conversation is off. Local follow/stop work. Use /chat remote on to opt in before queueing conversation."); return; }
        session(owner).thenCompose(s -> {
            ChatSessionService remote = enableRemote(s);
            return remote == null ? s.actor.enqueue(normalized, MessagePriority.USER_CHAT) : remote.enqueue(normalized);
        }).whenComplete((record, failure) -> server.execute(() -> {
            if (closed) return;
            if (failure != null) message(owner, "Message rejected: " + safeFailure(failure));
            else message(owner, "Queued " + record.id() + "; " + sessions.get(owner.getUUID()).remoteState);
        }));
    }
    private synchronized ChatSessionService enableRemote(Session session) {
        if (session.remote != null) return session.remote;
        String key = System.getenv("OPENAI_API_KEY");
        if (!session.remoteConsent || closed) { session.remoteState = "OFF: consent withdrawn"; return null; }
        if (key == null || key.isBlank()) { session.remoteState = "OFFLINE: API key NOT DETECTED in Minecraft server process"; return null; }
        HttpAgentsClient client = new HttpAgentsClient(URI.create("https://api.openai.com/v1"), () -> System.getenv("OPENAI_API_KEY"), io);
        ChatSessionService coordinator = new ChatSessionService(session.actor, client, "gpt-6-luna",
                CompanionProfile.instructions(session.owner, session.companion, "Fabric"), CompanionProfile.tools(false));
        session.remoteClient = client; session.remote = coordinator; session.remoteState = "CONNECTING";
        coordinator.start().whenComplete((v, failure) -> server.execute(() -> { if (!closed && session.remote == coordinator && session.remoteConsent) session.remoteState = failure == null ? "READY / RECONCILING" : "DEGRADED: remote access or workflow failed"; }));
        return coordinator;
    }
    public void status(ServerPlayer owner) {
        CompanionEntity companion = find(owner.getUUID());
        if (companion == null) { message(owner, "Companion unavailable. Use /chat spawn or return to its loaded chunk."); return; }
        message(owner, "Fabric 1.21.1 | health " + companion.getHealth() + "/" + companion.getMaxHealth()
                + " | " + companion.jobType() + " " + companion.jobState() + " (" + companion.reason() + ")"
                + " | distance " + String.format(Locale.ROOT, "%.1f", companion.distanceTo(owner))
                + " | navigation " + (!companion.getNavigation().isDone() ? "ACTIVE" : "IDLE")
                + " | world actions UNSUPPORTED" + " | remote consent " + companion.remoteConsent() + " | speech " + companion.speechConsent());
        message(owner, serverCredentialVisibility());
        Session current = sessions.get(owner.getUUID());
        if (current == null) { message(owner, "AI workflow not started; local movement works. Use /chat remote on to start remote conversation."); return; }
        current.actor.snapshot().thenAccept(snapshot -> server.execute(() -> {
            if (closed) return;
            JsonObject protocolStatus = current.remote == null ? null : snapshot.protocolRecords().get("agents/status");
            message(owner, "AI " + (protocolStatus == null ? current.remoteState : protocolStatus) + " | session " + snapshot.remoteSessionId() + " | queue " + snapshot.messages().stream().filter(m -> m.state() == MessageState.QUEUED).count()
                    + " | generation " + snapshot.controlGeneration() + " | storage " + (snapshot.storageHealthy() ? "HEALTHY" : snapshot.storageProblem()));
            snapshot.tools().stream().skip(Math.max(0, snapshot.tools().size() - 3)).forEach(tool -> message(owner, tool.name() + " | execution " + tool.executionState() + " | result " + tool.resultDeliveryState()));
            message(owner, "Speech " + current.speechState + " | client ACKs " + current.utterances.size());
        }));
    }
    public void consent(ServerPlayer owner, String kind, boolean enabled) {
        CompanionEntity companion = find(owner.getUUID());
        if (companion == null) { message(owner, "Use /chat spawn first."); return; }
        switch (kind) {
            case "actions" -> { message(owner, "World actions are unavailable on this Fabric build pending protection integration."); return; }
            case "remote" -> {
                companion.remoteConsent(enabled);
                Session existing = sessions.get(owner.getUUID());
                if (existing != null) existing.remoteConsent = enabled;
                if (!enabled && existing != null) {
                    if (existing.remote != null) existing.remote.close();
                    existing.remote = null; existing.remoteClient = null; existing.remoteState = "OFF: consent withdrawn";
                }
                if (enabled) {
                    message(owner, serverCredentialVisibility());
                    session(owner).thenCompose(s -> {
                        ChatSessionService remote = enableRemote(s);
                        return remote == null ? CompletableFuture.<Void>completedFuture(null) : remote.start();
                    }).whenComplete((v, failure) -> {
                        if (failure != null) server.execute(() -> {
                            if (!closed && companion.remoteConsent()) message(owner, "Remote workflow could not start. Check /chat agent; local movement still works.");
                        });
                    });
                }
            }
            case "speech" -> { companion.speechConsent(enabled); CompanionNetworking.speech(owner, enabled, false); }
            default -> throw new IllegalArgumentException("Unknown consent");
        }
        message(owner, kind + " " + (enabled ? "enabled" : "disabled") + (kind.equals("remote") ? "; addressed conversation is sent to OpenAI only with configured server credentials." : ""));
    }
    public void give(ServerPlayer owner) {
        CompanionEntity companion = find(owner.getUUID());
        if (companion == null || companion.distanceTo(owner) > 4) { message(owner, "Stand within four blocks of your loaded companion."); return; }
        ItemStack held = owner.getMainHandItem();
        if (held.isEmpty()) { message(owner, "Hold the item you want to give Chat."); return; }
        int before = held.getCount(); ItemStack remaining = companion.companionInventory().addItem(held.copy());
        held.shrink(before - remaining.getCount());
        message(owner, "Transferred " + (before - remaining.getCount()) + " item(s) to companion inventory.");
    }
    public void resume(ServerPlayer owner) {
        CompanionEntity companion = find(owner.getUUID());
        if (companion == null || !companion.resume()) message(owner, "No safe suspended movement job to resume.");
        else message(owner, "Movement job resumed: " + companion.jobId());
    }
    void acknowledge(ServerPlayer owner, String itemId, String state) {
        Session current = sessions.get(owner.getUUID());
        if (current != null && current.deliveries.containsKey(itemId) && Set.of("DISPLAYED", "RECEIVED", "ALREADY_ATTEMPTED").contains(state)) {
            current.deliveries.put(itemId, state);
            for (var entry : current.deliverySegments.entrySet()) {
                if (!entry.getValue().contains(itemId)) continue;
                boolean displayed = entry.getValue().stream().allMatch(id -> "DISPLAYED".equals(current.deliveries.get(id)));
                boolean received = entry.getValue().stream().allMatch(id -> Set.of("RECEIVED", "DISPLAYED", "ALREADY_ATTEMPTED").contains(current.deliveries.get(id)));
                if (displayed) current.actor.acknowledgeOutput(entry.getKey(), OutputDeliveryState.DISPLAYED);
                else if (received) current.actor.acknowledgeOutput(entry.getKey(), OutputDeliveryState.RECEIVED);
                break;
            }
        }
    }
    void acknowledgeSpeech(ServerPlayer owner, String utteranceId, String state) {
        Session current = sessions.get(owner.getUUID());
        if (current == null || !current.utterances.containsKey(utteranceId)
                || !Set.of("QUEUED", "STARTING", "PLAYING", "PLAYED", "FAILED", "CANCELLED", "ALREADY_ATTEMPTED").contains(state)) return;
        current.utterances.put(utteranceId, state);
        current.speechState = state;
    }
    public void tick() { /* Movement is ticked by the server entity; no unsafe world jobs. */ }

    private final class Bridge implements GameBridge {
        private final UUID owner;
        private final UUID companionId;
        volatile CompanionActor actor;
        volatile Session session;
        Bridge(UUID owner, UUID companionId) { this.owner = owner; this.companionId = companionId; }
        @Override public CompletionStage<ActionOutcome> execute(ActionRequest request) {
            CompletableFuture<ActionOutcome> result = new CompletableFuture<>();
            server.execute(() -> {
                if (closed || actor == null || !runtimeEpoch.equals(request.runtimeEpoch()) || actor.controlGeneration() != request.controlGeneration()) { result.complete(ActionOutcome.rejected("stale_control_generation")); return; }
                CompanionEntity companion = find(owner); ServerPlayer player = server.getPlayerList().getPlayer(owner);
                if (companion == null || !companionId.equals(companion.getUUID()) || player == null) { result.complete(ActionOutcome.rejected("owner_or_companion_unavailable")); return; }
                if (!request.key().sessionId().startsWith("local:") && !companion.remoteConsent()) { result.complete(ActionOutcome.rejected("remote_consent_revoked")); return; }
                try { result.complete(dispatch(companion, player, request)); }
                catch (RuntimeException invalid) { result.complete(ActionOutcome.rejected("invalid_arguments")); }
            });
            return result;
        }
        private ActionOutcome dispatch(CompanionEntity companion, ServerPlayer owner, ActionRequest request) {
            JsonObject args = request.arguments(); String name = request.name();
            if (name.equals("get_companion_status") || name.equals("get_task_status")) return observed(statusJson(companion), "status_observed");
            if (name.equals("stop_action") || name.equals("cancel_task")) { if (!request.key().sessionId().startsWith("local:")) actor.stop(); companion.stop("cancelled"); return observed(statusJson(companion), "action_stopped"); }
            if (name.equals("follow_player")) {
                if (!UUID.fromString(args.get("player_id").getAsString()).equals(owner.getUUID())) return ActionOutcome.rejected("owner_only");
                double distance = args.get("stop_distance").getAsDouble(); if (!Double.isFinite(distance) || distance < 2 || distance > 8) return ActionOutcome.rejected("invalid_follow_distance");
                return ActionOutcome.accepted(companion.follow(distance).toString());
            }
            if (name.equals("move_to")) {
                BlockPos pos = position(args, companion); double distance = args.has("stop_distance") ? args.get("stop_distance").getAsDouble() : 1.5;
                if (!Double.isFinite(distance) || distance < 1 || distance > 3) return ActionOutcome.rejected("invalid_stop_distance");
                return ActionOutcome.accepted(companion.move(Vec3.atBottomCenterOf(pos), distance).toString());
            }
            if (name.equals("inspect_nearby")) {
                int radius = integer(args, "radius", 1, 16); int max = integer(args, "max_entities", 1, 32);
                JsonObject evidence = statusJson(companion); JsonArray entities = new JsonArray();
                companion.level().getEntities(companion, companion.getBoundingBox().inflate(radius), Entity::isAlive).stream().limit(max).forEach(e -> { JsonObject item = new JsonObject(); item.addProperty("uuid", e.getUUID().toString()); item.addProperty("type", e.getType().toShortString()); item.addProperty("distance", companion.distanceTo(e)); entities.add(item); });
                evidence.add("nearby_entities", entities); return observed(evidence, "world_observed");
            }
            return ActionOutcome.rejected("fabric_world_actions_unavailable");
        }
        @Override public void deliver(String itemId, String text) {
            server.execute(() -> {
                if (closed || session == null) return; ServerPlayer player = server.getPlayerList().getPlayer(owner); if (player == null) return;
                // Preserve stable segment IDs if a model emits more than the packet's bound.
                int parts = Math.max(1, (text.length() + 7999) / 8000);
                List<String> segments = new ArrayList<>();
                for (int i = 0; i < parts; i++) {
                    String id = parts == 1 ? itemId : itemId + "." + i;
                    String segment = text.substring(i * 8000, Math.min(text.length(), (i + 1) * 8000));
                    if (session.deliveries.size() >= 8192 && !session.deliveries.containsKey(id)) break;
                    segments.add(id);
                    session.deliveries.putIfAbsent(id, "SENT"); CompanionNetworking.chat(player, id, segment);
                }
                if (segments.size() == parts) session.deliverySegments.put(itemId, List.copyOf(segments));
                CompanionEntity companion = find(owner);
                if (companion != null && companion.speechConsent() && text.length() <= 4096) {
                    UUID utterance = UUID.nameUUIDFromBytes((itemId + ":" + owner).getBytes(java.nio.charset.StandardCharsets.UTF_8));
                    if (session.utterances.size() >= 64 && !session.utterances.containsKey(utterance.toString())) {
                        String retired = session.utterances.entrySet().stream().filter(entry ->
                                Set.of("PLAYED", "FAILED", "CANCELLED", "ALREADY_ATTEMPTED").contains(entry.getValue()))
                                .map(Map.Entry::getKey).findFirst().orElse(null);
                        if (retired == null) { session.speechState = "QUEUE_LIMIT"; return; }
                        session.utterances.remove(retired);
                    }
                    session.utterances.put(utterance.toString(), "SYNTHESISING");
                    session.speechState = "SYNTHESISING";
                    speech.synthesise(text).whenComplete((wav, failure) -> server.execute(() -> {
                        if (closed || failure != null) {
                            if (!closed) { session.utterances.put(utterance.toString(), "FAILED"); session.speechState = "FAILED"; }
                            return;
                        }
                        ServerPlayer current = server.getPlayerList().getPlayer(owner); CompanionEntity active = find(owner);
                        if (current == null || active == null || !active.speechConsent()) return;
                        session.utterances.put(utterance.toString(), "SENT"); session.speechState = "SENT";
                        int count = (wav.length + 24575) / 24576;
                        for (int i = 0; i < count; i++) ServerPlayNetworking.send(current, new CompanionNetworking.AudioChunk(utterance.toString(), i, count, Arrays.copyOfRange(wav, i * 24576, Math.min(wav.length, (i + 1) * 24576))));
                    }));
                }
            });
        }
        @Override public CompletionStage<JsonObject> snapshot() {
            CompletableFuture<JsonObject> result = new CompletableFuture<>(); server.execute(() -> { CompanionEntity companion = closed ? null : find(owner); result.complete(companion == null ? new JsonObject() : statusJson(companion)); }); return result;
        }
    }
    private BlockPos position(JsonObject args, CompanionEntity companion) {
        String dim = args.get("dimension").getAsString();
        if (!dim.equals(companion.level().dimension().location().toString())) throw new IllegalArgumentException("Dimension mismatch");
        BlockPos pos = new BlockPos(integer(args, "x", -30000000, 30000000), integer(args, "y", companion.level().getMinBuildHeight(), companion.level().getMaxBuildHeight() - 1), integer(args, "z", -30000000, 30000000));
        if (!companion.level().hasChunkAt(pos) || companion.distanceToSqr(Vec3.atCenterOf(pos)) > 64 * 64 || !companion.level().getWorldBorder().isWithinBounds(pos)) throw new IllegalArgumentException("Destination not permitted");
        return pos;
    }
    private static int integer(JsonObject args, String name, int min, int max) {
        double value = args.get(name).getAsDouble(); if (!Double.isFinite(value) || value != Math.rint(value) || value < min || value > max) throw new IllegalArgumentException("Invalid integer"); return (int) value;
    }
    private JsonObject statusJson(CompanionEntity companion) {
        JsonObject status = new JsonObject(); status.addProperty("player_id", companion.owner().toString()); status.addProperty("companion_id", companion.getUUID().toString());
        status.addProperty("dimension", companion.level().dimension().location().toString()); status.addProperty("server_tick", companion.level().getGameTime());
        status.addProperty("x", companion.getX()); status.addProperty("y", companion.getY()); status.addProperty("z", companion.getZ()); status.addProperty("health", companion.getHealth());
        status.addProperty("job_id", companion.jobId() == null ? "" : companion.jobId().toString()); status.addProperty("job_type", companion.jobType().name()); status.addProperty("job_state", companion.jobState().name());
        status.addProperty("reason", companion.reason()); status.addProperty("navigation_active", !companion.getNavigation().isDone()); status.addProperty("world_actions_allowed", false); return status;
    }
    private static ActionOutcome observed(JsonObject evidence, String why) { return new ActionOutcome(true, "observed", why, evidence); }
    static void message(ServerPlayer owner, String text) { owner.sendSystemMessage(Component.literal("[Chat] " + text)); }
    private static String safeFailure(Throwable failure) {
        while (failure instanceof CompletionException && failure.getCause() != null) failure = failure.getCause();
        return failure instanceof IllegalArgumentException || failure instanceof IllegalStateException ? failure.getMessage() : "storage or workflow unavailable";
    }
    private static String serverCredentialVisibility() {
        String key = System.getenv("OPENAI_API_KEY");
        return "API key visibility: " + (key == null || key.isBlank() ? "NOT DETECTED" : "DETECTED") + " (Minecraft server process)";
    }
    private void assertServer() { if (!server.isSameThread()) throw new IllegalStateException("World access outside server thread"); }
    @Override public void close() {
        closed = true;
        sessions.values().forEach(s -> { CompanionEntity companion = find(s.owner); if (companion != null) companion.suspend("server_stopping"); if (s.remote != null) s.remote.close(); s.actor.closeAsync(); });
        io.shutdown();
    }
}
