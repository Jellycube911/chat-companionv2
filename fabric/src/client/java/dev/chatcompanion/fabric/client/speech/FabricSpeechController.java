package dev.chatcompanion.fabric.client.speech;

import dev.chatcompanion.game.speech.DemoWav;
import dev.chatcompanion.game.speech.PcmAudio;
import dev.chatcompanion.game.speech.SpeechQueue;
import dev.chatcompanion.game.speech.SpeechAdmissionStore;
import dev.chatcompanion.game.speech.WavDecoder;
import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
import com.google.gson.JsonObject;
import java.util.UUID;
import java.util.function.BiConsumer;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import net.minecraft.client.Minecraft;
import net.minecraft.client.sounds.SoundManager;
import net.minecraft.sounds.SoundSource;

/** Client-only audio actor. Load exclusively from physical-client initialization. */
public final class FabricSpeechController {
    public record Snapshot(boolean enabled, float volume, SpeechQueue.Snapshot queue, String lastError) {}
    private static final FabricSpeechController INSTANCE = new FabricSpeechController();
    private final SpeechQueue queue = new SpeechQueue(8, 512);
    private final ExecutorService decoder = new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(8), runnable -> {
                Thread thread = new Thread(runnable, "chatcompanion-audio-decode");
                thread.setDaemon(true);
                return thread;
            }, new ThreadPoolExecutor.AbortPolicy());
    private boolean enabled;
    private float volume = 1;
    private SpeechSound active;
    private int startWaitTicks;
    private int playingTicks;
    private int expectedTicks;
    private String lastError = "none";
    private Path admissionJournal;
    private volatile SpeechAdmissionStore admissions;
    private volatile BiConsumer<UUID, String> acknowledgements = (id, state) -> {};
    private final boolean smokeMode = Boolean.getBoolean("chatcompanion.speechSmoke");
    private int smokeTicks;
    private boolean smokeRequested;
    private boolean smokeReported;
    private UUID smokeId;
    private boolean smokeSourceStarted;

    private FabricSpeechController() {}
    public static FabricSpeechController instance() { return INSTANCE; }

    /** Optional store path. Configure before audio is accepted; default is gameDirectory/chatcompanion/speech-admissions.log. */
    public void configureAdmissionStore(Path journal) {
        requireClientThread();
        if (active != null || queue.snapshot().queued() > 0 || admissions != null) {
            throw new IllegalStateException("Cannot change an initialized speech admission store");
        }
        admissionJournal = journal;
    }

    /** Configuration may run during client bootstrap; callbacks run on the client main thread. */
    public void setAcknowledgementListener(BiConsumer<UUID, String> listener) {
        acknowledgements = java.util.Objects.requireNonNull(listener);
    }

    /** Must be called on the client main thread. */
    public void configure(boolean enabled, float volume) {
        requireClientThread();
        if (!Float.isFinite(volume) || volume < 0 || volume > 1) {
            throw new IllegalArgumentException("Speech volume must be within 0–1");
        }
        this.enabled = enabled;
        this.volume = volume;
        if (!enabled || volume == 0) stop();
        else if (active != null) {
            active.setSpeechVolume(volume);
            Minecraft minecraft = Minecraft.getInstance();
            minecraft.getSoundManager().updateSourceVolume(SoundSource.VOICE,
                    minecraft.options.getSoundSourceVolume(SoundSource.VOICE));
        }
    }

    /** Decoding stays off the render thread. Completion indicates enqueue acceptance, never playback. */
    public CompletableFuture<SpeechQueue.EnqueueResult> accept(UUID id, byte[] wav) {
        if (id == null || wav == null || wav.length > WavDecoder.MAX_BYTES) {
            return CompletableFuture.failedFuture(new IllegalArgumentException("Invalid audio payload size"));
        }
        byte[] stable = wav.clone();
        CompletableFuture<SpeechQueue.EnqueueResult> result = new CompletableFuture<>();
        try {
            decoder.execute(() -> {
                try {
                    PcmAudio audio = WavDecoder.decode(stable);
                    Minecraft.getInstance().execute(() -> {
                        if (!enabled) {
                            result.completeExceptionally(new IllegalStateException("Client speech is disabled"));
                        } else {
                            SpeechQueue.EnqueueResult accepted = queue.enqueue(id, audio);
                            if (accepted == SpeechQueue.EnqueueResult.ACCEPTED) acknowledge(id, "QUEUED");
                            result.complete(accepted);
                        }
                    });
                } catch (RuntimeException failure) {
                    Minecraft.getInstance().execute(() -> lastError = "Invalid or unsupported WAV audio");
                    result.completeExceptionally(failure);
                }
            });
        } catch (RuntimeException full) {
            result.completeExceptionally(new IllegalStateException("Audio decoder queue is full"));
        }
        return result;
    }

    public CompletableFuture<SpeechQueue.EnqueueResult> playDemo(UUID id) { return accept(id, DemoWav.create()); }

    /** Invoke once after each client tick. */
    public void tick() {
        requireClientThread();
        Minecraft minecraft = Minecraft.getInstance();
        if (smokeMode) smokeTick(minecraft);
        if (!enabled || (minecraft.level == null && !smokeMode)) {
            if (active != null || queue.snapshot().queued() > 0) stop();
            return;
        }
        if (minecraft.isPaused()) return;
        SoundManager sounds = minecraft.getSoundManager();
        if (active != null) {
            SpeechQueue.State state = queue.snapshot().active().state();
            if (state == SpeechQueue.State.STARTING && active.sourceStarted()) {
                queue.started(active.utteranceId());
                if (active.utteranceId().equals(smokeId)) smokeSourceStarted = true;
                acknowledge(active.utteranceId(), "PLAYING");
                playingTicks = 0;
                state = SpeechQueue.State.PLAYING;
            }
            if (state == SpeechQueue.State.STARTING && ++startWaitTicks > 100) {
                failActive("Minecraft did not start an audio source (check audio device and volume)");
            } else if (state == SpeechQueue.State.PLAYING) {
                playingTicks++;
                if (!sounds.isActive(active)) {
                    if (active.samplesExhausted() && playingTicks + 2 >= expectedTicks) {
                        queue.completed(active.utteranceId());
                        acknowledge(active.utteranceId(), "PLAYED");
                        active = null;
                    } else {
                        failActive("Audio source stopped before playback completed");
                    }
                } else if (playingTicks > expectedTicks + 200) {
                    failActive("Audio playback exceeded its deadline");
                }
            }
        }
        if (active == null && minecraft.options.getSoundSourceVolume(SoundSource.MASTER) > 0
                && minecraft.options.getSoundSourceVolume(SoundSource.VOICE) > 0 && volume > 0) {
            queue.beginNext().ifPresent(next -> {
                active = new SpeechSound(next.utteranceId(), next.audio(), volume);
                startWaitTicks = 0;
                playingTicks = 0;
                expectedTicks = Math.max(1, (int) Math.ceil(next.audio().duration().toNanos() / 50_000_000.0));
                acknowledge(active.utteranceId(), "STARTING");
                persistAndPlay(active);
            });
        }
    }

    /** Cancels queued work and the current source; it never reports cancellation as successful playback. */
    public void stop() {
        requireClientThread();
        if (active != null) {
            Minecraft.getInstance().getSoundManager().stop(active);
        }
        active = null;
        queue.cancelAll("Client playback stopped").forEach(status -> acknowledge(status.utteranceId(), "CANCELLED"));
    }

    public Snapshot snapshot() { requireClientThread(); return new Snapshot(enabled, volume, queue.snapshot(), lastError); }

    private void failActive(String reason) {
        Minecraft.getInstance().getSoundManager().stop(active);
        queue.failed(active.utteranceId(), reason);
        acknowledge(active.utteranceId(), "FAILED");
        lastError = reason;
        active = null;
    }

    private void persistAndPlay(SpeechSound intended) {
        Path path = admissionJournal == null ? Minecraft.getInstance().gameDirectory.toPath()
                .resolve("chatcompanion/speech-admissions.log") : admissionJournal;
        try {
            decoder.execute(() -> {
                String error = null;
                try {
                    if (admissions == null) {
                        admissions = new SpeechAdmissionStore(path);
                    }
                    if (!admissions.admit(intended.utteranceId())) {
                        error = "Utterance already admitted before restart; replay suppressed";
                    }
                } catch (IOException failure) {
                    error = "Speech admission journal unavailable or uncertain; playback suppressed";
                }
                String finalError = error;
                Minecraft.getInstance().execute(() -> {
                    if (active != intended) return;
                    if (finalError != null) failActive(finalError);
                    else if (enabled) Minecraft.getInstance().getSoundManager().play(intended);
                });
            });
        } catch (RuntimeException full) {
            failActive("Audio worker queue is full");
        }
    }

    private void acknowledge(UUID id, String state) {
        try { acknowledgements.accept(id, state); }
        catch (RuntimeException ignored) { lastError = "Playback acknowledgement could not be sent"; }
    }

    /** Explicit CI-only switch; inert in normal installations and never calls a remote service. */
    private void smokeTick(Minecraft minecraft) {
        if (smokeReported) return;
        if (minecraft.getOverlay() != null) return;
        smokeTicks++;
        if (!smokeRequested && smokeTicks >= 40) {
            smokeRequested = true;
            smokeId = UUID.randomUUID();
            configure(true, 0.25f);
            playDemo(smokeId).exceptionally(failure -> {
                minecraft.execute(() -> lastError = "Diagnostic audio enqueue failed");
                return null;
            });
        }
        SpeechQueue.Snapshot snapshot = queue.snapshot();
        SpeechQueue.SpeechStatus last = snapshot.last();
        boolean finished = last != null && last.utteranceId().equals(smokeId)
                && (last.state() == SpeechQueue.State.PLAYED || last.state() == SpeechQueue.State.FAILED
                        || last.state() == SpeechQueue.State.CANCELLED);
        if (!finished && smokeTicks < 400) return;
        smokeReported = true;
        JsonObject report = new JsonObject();
        report.addProperty("test", "native_pcm_wav_client_playback");
        report.addProperty("result", finished ? last.state().name() : "TIMED_OUT");
        report.addProperty("source_started", smokeSourceStarted);
        report.addProperty("last_error", lastError);
        report.addProperty("audible_hardware_verified", false);
        String selected = System.getProperty("chatcompanion.speechSmokeReport");
        Path reportPath = selected == null ? minecraft.gameDirectory.toPath().resolve("speech-smoke-report.json")
                : Path.of(selected);
        decoder.execute(() -> {
            try {
                Files.createDirectories(reportPath.toAbsolutePath().getParent());
                Files.writeString(reportPath, report.toString(), StandardCharsets.UTF_8);
            } catch (IOException failure) {
                System.err.println("Chat Companion speech smoke report could not be written");
            } finally { minecraft.execute(minecraft::stop); }
        });
    }

    private static void requireClientThread() {
        if (!Minecraft.getInstance().isSameThread()) {
            throw new IllegalStateException("Client speech state accessed outside the client main thread");
        }
    }
}
