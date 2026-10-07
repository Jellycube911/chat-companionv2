package dev.chatcompanion.fabric;

import java.util.UUID;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.message.v1.ServerMessageEvents;
import net.fabricmc.fabric.api.object.builder.v1.entity.FabricDefaultAttributeRegistry;
import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.MobCategory;

/** Fabric loader entrypoint; shared workflow logic contains no loader imports. */
public final class ChatCompanionFabric implements ModInitializer {
    public static final String MOD_ID = "chatcompanion";
    public static final EntityType<CompanionEntity> COMPANION_TYPE = Registry.register(
            BuiltInRegistries.ENTITY_TYPE, ResourceLocation.fromNamespaceAndPath(MOD_ID, "companion"),
            EntityType.Builder.of(CompanionEntity::new, MobCategory.CREATURE).sized(0.6F, 1.8F)
                    .clientTrackingRange(10).build(MOD_ID + ":companion"));
    static volatile CompanionService service;

    @Override public void onInitialize() {
        FabricDefaultAttributeRegistry.register(COMPANION_TYPE, CompanionEntity.attributes());
        CompanionNetworking.register();
        CommandRegistrationCallback.EVENT.register((dispatcher, context, environment) ->
                CompanionCommands.register(dispatcher));
        ServerMessageEvents.CHAT_MESSAGE.register((message, sender, parameters) ->
                CompanionCommands.chat(sender, message.signedContent()));
        ServerLifecycleEvents.SERVER_STARTING.register(server -> service = new CompanionService(server, UUID.randomUUID()));
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> {
            CompanionService current = service;
            service = null;
            if (current != null) current.close();
        });
    }
}
