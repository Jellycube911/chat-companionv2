package dev.chatcompanion.fabric.client;

import dev.chatcompanion.fabric.ChatCompanionFabric;
import dev.chatcompanion.fabric.CompanionEntity;
import dev.chatcompanion.fabric.CompanionNetworking;
import dev.chatcompanion.fabric.client.speech.FabricSpeechController;
import dev.chatcompanion.game.speech.SpeechQueue;
import java.io.ByteArrayOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.HashSet;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.rendering.v1.EntityRendererRegistry;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.model.HumanoidModel;
import net.minecraft.client.model.geom.ModelLayers;
import net.minecraft.client.renderer.entity.EntityRendererProvider;
import net.minecraft.client.renderer.entity.HumanoidMobRenderer;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.network.chat.Component;

/** All rendering references are confined to the client source set. */
public final class ChatCompanionFabricClient implements ClientModInitializer {
    private static final ExecutorService STORE = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "chat-companion-fabric-delivery"); thread.setDaemon(true); return thread;
    });
    private static final Set<String> ATTEMPTED = new HashSet<>();
    private static boolean loaded;
    private static final Map<String, Assembly> AUDIO = new HashMap<>();
    private static final Set<UUID> DEMOS = new HashSet<>();
    private record Assembly(long created, byte[][] parts) {}

    @Override public void onInitializeClient() {
        EntityRendererRegistry.register(ChatCompanionFabric.COMPANION_TYPE, CompanionRenderer::new);
        FabricSpeechController speech = FabricSpeechController.instance();
        speech.setAcknowledgementListener((id, state) -> Minecraft.getInstance().execute(() -> {
            if (ClientPlayNetworking.canSend(CompanionNetworking.SpeechAck.TYPE))
                ClientPlayNetworking.send(new CompanionNetworking.SpeechAck(id.toString(), state));
            if (Set.of("PLAYED", "FAILED", "CANCELLED").contains(state) && DEMOS.remove(id))
                Minecraft.getInstance().gui.getChat().addMessage(Component.literal("[Chat] Speech test " + state));
        }));
        ClientTickEvents.END_CLIENT_TICK.register(client -> speech.tick());
        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> client.execute(() -> {
            speech.stop(); AUDIO.clear(); DEMOS.clear();
        }));
        ClientPlayNetworking.registerGlobalReceiver(CompanionNetworking.SpeechPayload.TYPE, (packet, context) -> {
            speech.configure(packet.enabled(), 1.0F);
            context.client().gui.getChat().addMessage(Component.literal("[Chat] " + packet.message()));
            if (packet.demo() && DEMOS.size() < 4) {
                UUID id = UUID.randomUUID(); DEMOS.add(id);
                speech.playDemo(id).whenComplete((result, failure) -> context.client().execute(() -> {
                    if (failure != null || result == SpeechQueue.EnqueueResult.FULL) {
                        DEMOS.remove(id);
                        context.client().gui.getChat().addMessage(Component.literal("[Chat] Speech test could not enter playback queue."));
                    }
                }));
            }
        });
        ClientPlayNetworking.registerGlobalReceiver(CompanionNetworking.AudioChunk.TYPE, (packet, context) -> {
            long now = System.currentTimeMillis();
            AUDIO.entrySet().removeIf(entry -> now - entry.getValue().created() > 30_000);
            if (AUDIO.size() >= 4 && !AUDIO.containsKey(packet.utteranceId())) return;
            Assembly assembly = AUDIO.computeIfAbsent(packet.utteranceId(), id -> new Assembly(now, new byte[packet.count()][]));
            if (assembly.parts().length != packet.count()) { AUDIO.remove(packet.utteranceId()); return; }
            assembly.parts()[packet.index()] = packet.bytes();
            int total = 0;
            for (byte[] part : assembly.parts()) { if (part == null) return; total += part.length; }
            if (total > dev.chatcompanion.game.speech.WavDecoder.MAX_BYTES) { AUDIO.remove(packet.utteranceId()); return; }
            ByteArrayOutputStream bytes = new ByteArrayOutputStream(total);
            for (byte[] part : assembly.parts()) bytes.writeBytes(part);
            AUDIO.remove(packet.utteranceId());
            try {
                UUID id = UUID.fromString(packet.utteranceId());
                speech.accept(id, bytes.toByteArray()).whenComplete((result, failure) -> context.client().execute(() -> {
                    if (!ClientPlayNetworking.canSend(CompanionNetworking.SpeechAck.TYPE)) return;
                    if (failure != null || result == SpeechQueue.EnqueueResult.FULL)
                        ClientPlayNetworking.send(new CompanionNetworking.SpeechAck(id.toString(), "FAILED"));
                    else if (result == SpeechQueue.EnqueueResult.DUPLICATE)
                        ClientPlayNetworking.send(new CompanionNetworking.SpeechAck(id.toString(), "ALREADY_ATTEMPTED"));
                }));
            } catch (IllegalArgumentException ignored) { /* Invalid identities cannot enter speech history. */ }
        });
        ClientPlayNetworking.registerGlobalReceiver(CompanionNetworking.ChatPayload.TYPE, (packet, context) -> {
            if (!packet.itemId().matches("[A-Za-z0-9_:.-]{1,256}") || packet.text().length() > 8192) return;
            Path file = context.client().gameDirectory.toPath().resolve("config/chatcompanion-delivery.log");
            STORE.execute(() -> {
                try {
                    if (!loaded) { loadDeliveryHistory(file); loaded = true; }
                    boolean first = !ATTEMPTED.contains(packet.itemId());
                    if (first) {
                        if (ATTEMPTED.size() >= 100_000) throw new java.io.IOException("Delivery history full");
                        Files.createDirectories(file.getParent());
                        Files.writeString(file, packet.itemId() + "\n", StandardOpenOption.CREATE,
                                StandardOpenOption.APPEND, StandardOpenOption.SYNC);
                        ATTEMPTED.add(packet.itemId());
                    }
                    context.client().execute(() -> {
                        if (first) context.client().gui.getChat().addMessage(Component.literal("<Chat> " + packet.text()));
                        if (ClientPlayNetworking.canSend(CompanionNetworking.OutputAck.TYPE))
                            ClientPlayNetworking.send(new CompanionNetworking.OutputAck(packet.itemId(),
                                    first ? "DISPLAYED" : "ALREADY_ATTEMPTED"));
                    });
                } catch (Exception failure) {
                    context.client().execute(() -> context.client().gui.getChat().addMessage(Component.literal(
                            "[Chat] Could not persist output delivery; inspect /chat agent.")));
                }
            });
        });
    }

    private static final class CompanionRenderer
            extends HumanoidMobRenderer<CompanionEntity, HumanoidModel<CompanionEntity>> {
        private static final ResourceLocation TEXTURE = ResourceLocation.withDefaultNamespace(
                "textures/entity/player/wide/steve.png");
        private CompanionRenderer(EntityRendererProvider.Context context) {
            super(context, new HumanoidModel<>(context.bakeLayer(ModelLayers.ZOMBIE)), 0.5F);
        }
        @Override public ResourceLocation getTextureLocation(CompanionEntity entity) { return TEXTURE; }
    }

    private static void loadDeliveryHistory(Path path) throws java.io.IOException {
        if (!Files.exists(path)) return;
        if (Files.size(path) > 16 * 1024 * 1024) throw new java.io.IOException("Delivery history too large");
        try (var reader = Files.newBufferedReader(path)) {
            String id;
            int count = 0;
            while ((id = reader.readLine()) != null) {
                if (++count > 100_000 || !id.matches("[A-Za-z0-9_:.-]{1,256}"))
                    throw new java.io.IOException("Delivery history corrupt or full");
                ATTEMPTED.add(id);
            }
        }
    }
}
