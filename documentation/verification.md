# Verification: 0.1.0-alpha3

Performed locally on **7 October 2026**, Linux x86-64, Java 21.0.12.1, Gradle 9.2.1, Minecraft 1.21.1. NeoForge 21.1.248; Fabric Loader 0.19.5 and Fabric API 0.116.17+1.21.1.

Alpha3 corrects credential-status reporting on both loaders: an unstarted session no longer claims a missing key; `/chat agent` separately reports whether the Minecraft server process detects a nonblank key. Remote-off state takes precedence over persisted remote status, and remote startup failures produce a safe visible message. No credential values are displayed and no live API call was made during verification. NeoForge remains pinned to **21.1.248**. Previous release records are retained in [verification-alpha2.md](verification-alpha2.md) and [verification-alpha1.md](verification-alpha1.md).

## Results

| Check | Result | Evidence |
|---|---|---|
| Journal recovery/integrity tests | 8 passed | `DurableJournalTest`: durability, corruption/truncation handling, ordering and exclusive ownership. |
| Actor tests | 17 passed | `CompanionActorTest`: message identity, immutable results, duplicate calls/intents, control fences, uncertain world recovery and output delivery. |
| Agents HTTP/SSE tests | 14 passed | `HttpAgentsClientTest`: a real loopback HTTP fake service, bounded parsing, function/result correlation, idempotency, errors, streaming and overall body deadlines. |
| Session coordinator tests | 19 passed | `ChatSessionServiceTest`: fake Agents client, uncertain creation, permanent failures, pending results, saved output recovery and idle/turn distinction. |
| Speech tests | 11 passed | WAV validation, queue/admission lifecycle and loopback synthesis transport. |
| NeoForge build | Passed | Compiled and packaged the installable loader JAR, including shared core/common classes. |
| Fabric build | Passed | Compiled client/server source sets and produced the remapped installable JAR. |
| NeoForge headless GameTests | 4 passed | Actual server worlds: movement/stop, continuous follow/holding, permission-gated mining, immediate stop during asynchronous session initialization. |
| Fabric headless GameTests | 3 passed | Actual server worlds: movement/stop, continuous follow/holding, immediate stop during asynchronous session initialization. |
| NeoForge client/native audio | Prior alpha2 check retained | Unchanged audio source reached actual native start and `PLAYED` on NeoForge 21.1.248 in alpha2; alpha3 recompiles the client but does not repeat that audio check. |
| Fabric client/native audio | Prior alpha1 check retained | Unchanged client source previously reached sound-source start and `PLAYED`; the alpha3 Fabric client was recompiled but its native playback check was not repeated. |
| JVM verification | Passed | `-Xverify:all` enabled during alpha3 automated tests and server GameTests. Native audio evidence is retained from alpha2 (NeoForge) and alpha1 (Fabric). |
| JAR inspection | Passed | Correct version/loader metadata, embedded common classes, all classes Java 21, no duplicate entries, no bundled Minecraft or Gson classes. |

Total: **69 automated core/speech tests plus seven server GameTests**. Tests had zero failures, errors or skips in the final run.

The final combined command was:

```bash
JAVA_TOOL_OPTIONS=-Xverify:all ./gradlew -Dorg.gradle.parallel=false \
  :core:test :minecraft-common:test :neoforge:build :fabric:build \
  :neoforge:runGameTestServer :fabric:runGameTestServer --rerun-tasks --console=plain
```

Builds in this environment additionally supplied its network proxy and CA trust-store settings. Those are environment details, not mod configuration. JUnit XML reports are under the modules' `build/test-results/test`. Fabric GameTest XML is under `fabric/test-runs/gametest/reports`; NeoForge's server run logged all four required tests passing.

Client audio checks used Xvfb, Mesa software rendering and OpenAL's **No Output** device. They verify loading, decoding, native source start, sample consumption and completion. They do **not** verify that a person heard sound through physical speakers. The retained alpha2 NeoForge 21.1.248 run and alpha1 Fabric run recorded:

```json
{"test":"native_pcm_wav_client_playback","result":"PLAYED","source_started":true,"last_error":"none","audible_hardware_verified":false}
```

Two test-harness issues were found and corrected before the final pass: Fabric's diagnostic sound initially raced resource/sound-engine startup, and fake HTTP teardown could interrupt a just-released SSE handler. The Fabric XML reporter also required a report path with a parent directory. Final checks use the corrected fixtures.

## Practical limits

No OpenAI credential was provisioned or used. API tests used loopback fixtures and a fake coordinator client. No live Agent turn, real-model action request, paid synthesis request or remote-session deletion was exercised. A later live test must verify beta compatibility and account/model access.

The full Direwolf20 pack, third-party protection combinations, physical audio hardware, production multiplayer connection/reconnection, Windows/macOS runtime behavior and a long compatibility burn-in were not exercised. Client checks reached startup/audio; they were not an automated visual review of the entity in a populated world. Tests use loader development launches; installable JAR contents were separately inspected.

Server GameTests verify observed world behavior, not a crash-atomic transaction between Minecraft saves and the journal. Uncertain outcomes remain explicit and are not automatically replayed. Fabric world-action parity and the stable-release requirements in the engineering specification remain outstanding.

Minecraft startup in this restricted runner emitted nonfatal messages about unavailable Mojang profile/public-key services, initial missing test-server properties and the custom entity's vanilla data-fixer entry. Native clients also emitted narrator/ALSA messages because the runner has no physical sound device. All alpha3 game/server processes completed successfully. Native audio evidence is retained from alpha2 (NeoForge) and alpha1 (Fabric).
