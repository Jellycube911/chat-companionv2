package dev.chatcompanion.fabric.client.speech;

import dev.chatcompanion.game.speech.PcmAudio;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicBoolean;
import net.minecraft.client.resources.sounds.AbstractSoundInstance;
import net.minecraft.client.resources.sounds.Sound;
import net.minecraft.client.resources.sounds.SoundInstance;
import net.minecraft.client.sounds.AudioStream;
import net.minecraft.client.sounds.SoundBufferLibrary;
import net.minecraft.client.sounds.SoundManager;
import net.minecraft.client.sounds.WeighedSoundEvents;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.sounds.SoundSource;
import net.minecraft.util.valueproviders.ConstantFloat;

/** A streaming sound resolved in memory through the narrowly scoped Fabric client mixin. */
public final class SpeechSound extends AbstractSoundInstance {
    private static final ResourceLocation LOCATION = ResourceLocation.fromNamespaceAndPath("chatcompanion", "speech");
    private final UUID utteranceId;
    private final PcmAudioStream stream;
    private final AtomicBoolean sourceStarted = new AtomicBoolean();

    SpeechSound(UUID utteranceId, PcmAudio audio, float speechVolume) {
        super(LOCATION, SoundSource.VOICE, SoundInstance.createUnseededRandom());
        this.utteranceId = utteranceId;
        this.stream = new PcmAudioStream(audio);
        volume = speechVolume;
        pitch = 1;
        relative = true;
        attenuation = SoundInstance.Attenuation.NONE;
        sound = new Sound(LOCATION, ConstantFloat.of(1), ConstantFloat.of(1), 1,
                Sound.Type.FILE, true, false, 16);
    }

    @Override public WeighedSoundEvents resolve(SoundManager manager) {
        WeighedSoundEvents events = new WeighedSoundEvents(LOCATION, null);
        events.addSound(sound);
        return events;
    }

    @Override public CompletableFuture<AudioStream> getAudioStream(
            SoundBufferLibrary buffers, ResourceLocation path, boolean looping) {
        return CompletableFuture.completedFuture(stream);
    }

    UUID utteranceId() { return utteranceId; }
    public void confirmSourceStarted() { sourceStarted.set(true); }
    public boolean sourceStarted() { return sourceStarted.get(); }
    boolean samplesExhausted() { return stream.exhausted(); }
    void setSpeechVolume(float volume) { this.volume = volume; }
}
