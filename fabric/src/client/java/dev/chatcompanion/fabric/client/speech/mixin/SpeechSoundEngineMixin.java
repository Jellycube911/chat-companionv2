package dev.chatcompanion.fabric.client.speech.mixin;

import dev.chatcompanion.fabric.client.speech.SpeechSound;
import java.util.Map;
import net.minecraft.client.resources.sounds.SoundInstance;
import net.minecraft.client.sounds.ChannelAccess;
import net.minecraft.client.sounds.SoundEngine;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Minecraft 1.21.1 only. Confirms actual source playback for companion audio on the sound executor.
 * Stream sourcing uses the public FabricSoundInstance API, avoiding conflicting stream redirects.
 * Client-only config prevents this mixin or any sound classes from loading on a dedicated server.
 */
@Mixin(SoundEngine.class)
abstract class SpeechSoundEngineMixin {
    @Shadow @Final private Map<SoundInstance, ChannelAccess.ChannelHandle> instanceToChannel;

    @Inject(method = "tick(Z)V", at = @At("TAIL"), require = 1)
    private void chatcompanion$confirmAudioSource(boolean paused, CallbackInfo callback) {
        if (paused) return;
        // Fabric can attach a stream asynchronously after play() returns. Probe retained sources
        // on sound ticks so an early probe cannot permanently miss actual native playback.
        instanceToChannel.forEach((source, handle) -> {
            if (source instanceof SpeechSound speech && !speech.sourceStarted()) {
                handle.execute(channel -> {
                if (channel.playing()) speech.confirmSourceStarted();
                });
            }
        });
    }
}
