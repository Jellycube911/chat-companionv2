package dev.chatcompanion.neoforge.client;

import dev.chatcompanion.neoforge.ChatCompanion;
import dev.chatcompanion.neoforge.CompanionNetworking;
import dev.chatcompanion.neoforge.client.speech.ClientSpeechController;
import java.io.ByteArrayOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.neoforge.client.event.EntityRenderersEvent;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.network.PacketDistributor;

@EventBusSubscriber(modid = ChatCompanion.MOD_ID, value = Dist.CLIENT, bus = EventBusSubscriber.Bus.MOD)
public final class ChatCompanionClient {
    private static final ExecutorService STORE = Executors.newSingleThreadExecutor(r -> { Thread t = new Thread(r, "chat-companion-client-delivery"); t.setDaemon(true); return t; });
    private static final Set<String> ATTEMPTED = new HashSet<>();
    private static final Map<String, Assembly> AUDIO = new HashMap<>();
    private static boolean loaded;
    private record Assembly(long created, byte[][] parts) {}
    private ChatCompanionClient() {}
    @SubscribeEvent public static void renderers(EntityRenderersEvent.RegisterRenderers event) {
        event.registerEntityRenderer(ChatCompanion.COMPANION.get(), CompanionRenderer::new);
        CompanionNetworking.clientChat = ChatCompanionClient::chat;
        CompanionNetworking.clientSpeech = packet -> {
            ClientSpeechController.instance().configure(packet.enabled(), 1f);
            Minecraft.getInstance().gui.getChat().addMessage(Component.literal("[Chat] " + packet.message()));
            if (packet.demo()) ClientSpeechController.instance().playDemo(UUID.randomUUID());
        };
        CompanionNetworking.clientAudio = ChatCompanionClient::audio;
        NeoForge.EVENT_BUS.addListener(ClientSpeechController::onSourceStarted);
        ClientSpeechController.instance().setAcknowledgementListener((id, state) -> {
            if (Minecraft.getInstance().getConnection() != null) PacketDistributor.sendToServer(new CompanionNetworking.SpeechAck(id.toString(), state));
        });
        NeoForge.EVENT_BUS.addListener(ChatCompanionClient::tick);
    }
    private static void chat(CompanionNetworking.ChatPayload packet) {
        if (!packet.itemId().matches("[A-Za-z0-9_:.-]{1,256}") || packet.text().length() > 8192) return;
        STORE.execute(() -> {
            try {
                Path path = Minecraft.getInstance().gameDirectory.toPath().resolve("config/chatcompanion-delivery.log");
                if (!loaded) { loadDeliveryHistory(path); loaded = true; }
                boolean first = !ATTEMPTED.contains(packet.itemId());
                if (first) {
                    if (ATTEMPTED.size() >= 100_000) throw new java.io.IOException("Delivery history full");
                    Files.createDirectories(path.getParent());
                    Files.writeString(path, packet.itemId() + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND, StandardOpenOption.SYNC);
                    ATTEMPTED.add(packet.itemId());
                }
                Minecraft.getInstance().execute(() -> {
                    if (first) Minecraft.getInstance().gui.getChat().addMessage(Component.literal("<Chat> " + packet.text()));
                    PacketDistributor.sendToServer(new CompanionNetworking.OutputAck(packet.itemId(), first ? "DISPLAYED" : "ALREADY_ATTEMPTED"));
                });
            } catch (Exception failure) {
                Minecraft.getInstance().execute(() -> Minecraft.getInstance().gui.getChat().addMessage(Component.literal("[Chat] Could not persist output delivery; see companion status.")));
            }
        });
    }
    private static void audio(CompanionNetworking.AudioChunk packet) {
        long now = System.currentTimeMillis();
        AUDIO.entrySet().removeIf(entry -> now - entry.getValue().created() > 30000);
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
        try { ClientSpeechController.instance().accept(UUID.fromString(packet.utteranceId()), bytes.toByteArray()); }
        catch (IllegalArgumentException ignored) { }
    }
    private static void tick(ClientTickEvent.Post event) { ClientSpeechController.instance().tick(); }

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
