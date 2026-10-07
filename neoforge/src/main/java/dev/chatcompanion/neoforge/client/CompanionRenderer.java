package dev.chatcompanion.neoforge.client;

import dev.chatcompanion.neoforge.CompanionEntity;
import net.minecraft.client.model.HumanoidModel;
import net.minecraft.client.model.geom.ModelLayers;
import net.minecraft.client.renderer.entity.EntityRendererProvider;
import net.minecraft.client.renderer.entity.HumanoidMobRenderer;
import net.minecraft.resources.ResourceLocation;

final class CompanionRenderer extends HumanoidMobRenderer<CompanionEntity, HumanoidModel<CompanionEntity>> {
    private static final ResourceLocation TEXTURE = ResourceLocation.withDefaultNamespace("textures/entity/player/wide/steve.png");
    CompanionRenderer(EntityRendererProvider.Context context) { super(context, new HumanoidModel<>(context.bakeLayer(ModelLayers.ZOMBIE)), 0.5f); }
    @Override public ResourceLocation getTextureLocation(CompanionEntity companion) { return TEXTURE; }
}
