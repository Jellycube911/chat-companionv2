package dev.chatcompanion.neoforge;

import java.util.UUID;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.MobCategory;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.entity.EntityAttributeCreationEvent;
import net.neoforged.neoforge.event.server.ServerStartingEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import net.neoforged.neoforge.registries.DeferredHolder;
import net.neoforged.neoforge.registries.DeferredRegister;

@Mod(ChatCompanion.MOD_ID)
public final class ChatCompanion {
    public static final String MOD_ID = "chatcompanion";
    public static final DeferredRegister<EntityType<?>> ENTITIES = DeferredRegister.create(Registries.ENTITY_TYPE, MOD_ID);
    public static final DeferredHolder<EntityType<?>, EntityType<CompanionEntity>> COMPANION = ENTITIES.register("companion", () ->
            EntityType.Builder.of(CompanionEntity::new, MobCategory.CREATURE).sized(0.6f, 1.8f)
                    .clientTrackingRange(10).build(ResourceLocation.fromNamespaceAndPath(MOD_ID, "companion").toString()));
    static volatile CompanionService service;

    /** Local integrations may inspect the active logical-server companion service. */
    public static CompanionService service() { return service; }

    public ChatCompanion(IEventBus modBus) {
        ENTITIES.register(modBus);
        modBus.addListener(this::attributes);
        modBus.addListener(CompanionNetworking::register);
        NeoForge.EVENT_BUS.addListener((net.neoforged.neoforge.event.RegisterCommandsEvent event) -> CompanionCommands.register(event));
        NeoForge.EVENT_BUS.addListener(CompanionCommands::chat);
        NeoForge.EVENT_BUS.addListener(this::start);
        NeoForge.EVENT_BUS.addListener(this::stop);
        NeoForge.EVENT_BUS.addListener((net.neoforged.neoforge.event.tick.ServerTickEvent.Post event) -> { if (service != null) service.tick(); });
        NeoForge.EVENT_BUS.addListener((net.neoforged.neoforge.event.entity.living.LivingDeathEvent event) -> {
            if (event.getEntity() instanceof CompanionEntity companion && companion.level() instanceof net.minecraft.server.level.ServerLevel world && companion.owner() != null)
                CompanionRegistry.get(world.getServer()).remove(companion.owner());
        });
    }
    private void attributes(EntityAttributeCreationEvent event) { event.put(COMPANION.get(), CompanionEntity.attributes().build()); }
    private void start(ServerStartingEvent event) { service = new CompanionService(event.getServer(), UUID.randomUUID()); }
    private void stop(ServerStoppingEvent event) {
        CompanionService current = service;
        service = null;
        if (current != null) current.close();
    }
}
