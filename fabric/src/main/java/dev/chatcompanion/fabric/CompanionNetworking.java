package dev.chatcompanion.fabric;

import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import net.minecraft.network.RegistryFriendlyByteBuf;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.server.level.ServerPlayer;

/** Bound codecs and authenticated server receivers; no client classes enter this source set. */
public final class CompanionNetworking {
    private CompanionNetworking() {}

    public record ChatPayload(String itemId, String text) implements CustomPacketPayload {
        public static final Type<ChatPayload> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(
                ChatCompanionFabric.MOD_ID, "chat_output"));
        public static final StreamCodec<RegistryFriendlyByteBuf, ChatPayload> CODEC = StreamCodec.of(
                (buffer, packet) -> { buffer.writeUtf(packet.itemId, 256); buffer.writeUtf(packet.text, 8192); },
                buffer -> new ChatPayload(buffer.readUtf(256), buffer.readUtf(8192)));
        @Override public Type<? extends CustomPacketPayload> type() { return TYPE; }
    }

    public record OutputAck(String itemId, String state) implements CustomPacketPayload {
        public static final Type<OutputAck> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(
                ChatCompanionFabric.MOD_ID, "output_ack"));
        public static final StreamCodec<RegistryFriendlyByteBuf, OutputAck> CODEC = StreamCodec.of(
                (buffer, packet) -> { buffer.writeUtf(packet.itemId, 256); buffer.writeUtf(packet.state, 32); },
                buffer -> new OutputAck(buffer.readUtf(256), buffer.readUtf(32)));
        @Override public Type<? extends CustomPacketPayload> type() { return TYPE; }
    }

    public record SpeechPayload(boolean enabled, boolean demo, String message) implements CustomPacketPayload {
        public static final Type<SpeechPayload> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(ChatCompanionFabric.MOD_ID, "speech_control"));
        public static final StreamCodec<RegistryFriendlyByteBuf, SpeechPayload> CODEC = StreamCodec.of(
                (buffer, packet) -> { buffer.writeBoolean(packet.enabled); buffer.writeBoolean(packet.demo); buffer.writeUtf(packet.message, 512); },
                buffer -> new SpeechPayload(buffer.readBoolean(), buffer.readBoolean(), buffer.readUtf(512)));
        @Override public Type<? extends CustomPacketPayload> type() { return TYPE; }
    }
    public record AudioChunk(String utteranceId, int index, int count, byte[] bytes) implements CustomPacketPayload {
        public AudioChunk { if (count < 1 || count > 350 || index < 0 || index >= count || bytes.length > 24576) throw new IllegalArgumentException("Invalid audio chunk"); bytes = bytes.clone(); }
        @Override public byte[] bytes() { return bytes.clone(); }
        public static final Type<AudioChunk> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(ChatCompanionFabric.MOD_ID, "audio_chunk"));
        public static final StreamCodec<RegistryFriendlyByteBuf, AudioChunk> CODEC = StreamCodec.of(
                (buffer, packet) -> { buffer.writeUtf(packet.utteranceId, 128); buffer.writeVarInt(packet.index); buffer.writeVarInt(packet.count); buffer.writeByteArray(packet.bytes); },
                buffer -> new AudioChunk(buffer.readUtf(128), buffer.readVarInt(), buffer.readVarInt(), buffer.readByteArray(24576)));
        @Override public Type<? extends CustomPacketPayload> type() { return TYPE; }
    }
    public record SpeechAck(String utteranceId, String state) implements CustomPacketPayload {
        public static final Type<SpeechAck> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(
                ChatCompanionFabric.MOD_ID, "speech_ack"));
        public static final StreamCodec<RegistryFriendlyByteBuf, SpeechAck> CODEC = StreamCodec.of(
                (buffer, packet) -> { buffer.writeUtf(packet.utteranceId, 128); buffer.writeUtf(packet.state, 32); },
                buffer -> new SpeechAck(buffer.readUtf(128), buffer.readUtf(32)));
        @Override public Type<? extends CustomPacketPayload> type() { return TYPE; }
    }

    static void register() {
        PayloadTypeRegistry.playS2C().register(ChatPayload.TYPE, ChatPayload.CODEC);
        PayloadTypeRegistry.playC2S().register(OutputAck.TYPE, OutputAck.CODEC);
        PayloadTypeRegistry.playS2C().register(SpeechPayload.TYPE, SpeechPayload.CODEC);
        PayloadTypeRegistry.playS2C().register(AudioChunk.TYPE, AudioChunk.CODEC);
        PayloadTypeRegistry.playC2S().register(SpeechAck.TYPE, SpeechAck.CODEC);
        ServerPlayNetworking.registerGlobalReceiver(SpeechAck.TYPE, (packet, context) -> {
            CompanionService current = ChatCompanionFabric.service;
            if (current != null) current.acknowledgeSpeech(context.player(), packet.utteranceId(), packet.state());
        });
        ServerPlayNetworking.registerGlobalReceiver(OutputAck.TYPE, (packet, context) -> {
            CompanionService current = ChatCompanionFabric.service;
            if (current != null) current.acknowledge(context.player(), packet.itemId(), packet.state());
        });
    }

    static void chat(ServerPlayer player, String itemId, String text) {
        if (ServerPlayNetworking.canSend(player, ChatPayload.TYPE))
            ServerPlayNetworking.send(player, new ChatPayload(itemId, text));
    }
    static void speech(ServerPlayer player, boolean enabled, boolean demo) {
        if (ServerPlayNetworking.canSend(player, SpeechPayload.TYPE))
            ServerPlayNetworking.send(player, new SpeechPayload(enabled, demo, "Generated companion speech is artificial."));
    }

}
