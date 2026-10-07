# Chat Companion 0.1.0-alpha3

A source-built companion for **Minecraft 1.21.1 and Java 21**. Local movement works without an OpenAI key. This alpha targets the replacement architecture, with a shared Java core and separate NeoForge and Fabric adapters.

Alpha3 targets **NeoForge 21.1.248** and corrects misleading credential-status messages. Replace an older Chat Companion JAR; keep only one Chat Companion JAR per installation. The local storage and networking formats are unchanged.

## Install

Choose **one** JAR for your loader:

| Platform | Required dependencies | JAR |
|---|---|---|
| NeoForge, primary | NeoForge **21.1.248 or newer**, Minecraft 1.21.1, Java 21 | `chat-companion-neoforge-1.21.1-0.1.0-alpha3.jar` |
| Fabric, preview | Fabric Loader **0.19.5 or newer**, Fabric API **0.116.17+1.21.1 or newer for 1.21.1**, Java 21 | `chat-companion-fabric-1.21.1-0.1.0-alpha3.jar` |

Put the JAR in the installation's `mods` folder. Single-player uses the integrated server. Multiplayer requires the matching companion JAR on the server and each participating client. The current networking protocol requires compatible modded clients.

For Direwolf20, use NeoForge and first check the pack's loader version against the requirement above. **The complete Direwolf20 pack has not been tested.** This build does not certify compatibility with an older pack loader. Back up a world before trying an alpha.

Start Minecraft, enter a world, and run:

```text
/chat spawn
/chat follow
/chat stop
/chat agent
```

The companion uses a vanilla humanoid model and Steve texture. It has health, an owner, saved inventory, navigation and local job state. One companion is registered per owner in a server/world context.

## Commands

| Command | Behavior |
|---|---|
| `/chat spawn` | Spawn your companion, or return the existing loaded companion. An unloaded companion does not cause a duplicate spawn. |
| `/chat follow` | Follow your player, hold at approximately three blocks, and follow again when you move. |
| `/chat move <x> <y> <z>` | Navigate to a loaded destination within 64 blocks in the same dimension. Relative coordinates work. |
| `/chat stop` or `/chat cancel` | Immediately stop local work and fence pending older requests. |
| `/chat resume` | Resume a safely suspended follow/move job after restart, disconnect or unloaded chunks. |
| `/chat agent` or `/chat tasks` | Show physical job/navigation state, consent, queue, storage and remote workflow state separately. |
| `/chat give` | Transfer your held stack into the companion's saved inventory. |
| `/chat inventory` | List occupied inventory slots. |
| `/chat speech test` | Play a local two-tone sound through Minecraft's audio system; no key or speech API is used. Master and Voice volume must be audible. |
| `/chat remote on` / `off` | Opt in/out of remote conversation processing. Off by default. |
| `/chat say <message>` | Queue conversation when remote processing is enabled. Unambiguous `follow me`, `follow` and `stop` requests dispatch locally. |
| `Chat: <message>`, `Chat, <message>` or `Chat <message>` | Address the companion in normal player chat. |
| `/chat speech on` / `off` | Enable/disable client playback and optional remote speech synthesis separately from conversation. Off by default. |
| `/chat privacy` | Show the remote-processing boundary. |

NeoForge additionally implements bounded world actions, disabled until `/chat actions on`:

| Command | Behavior |
|---|---|
| `/chat mine <x> <y> <z>` | Mine one block within four blocks of the companion using its inventory slot 0. Give it a suitable tool first. |
| `/chat place <x> <y> <z>` | Place a block from slot 0 onto a supporting block below the target. |
| `/chat collect` | Collect nearby dropped items into companion inventory, with a radius, item count and time limit. |
| `/chat defend <entity>` | Engage a hostile, non-allied mob. Player targets are rejected. |
| `/chat actions off` | Revoke world-action permission; running physical jobs stop on their next server tick. |

Mining and placement use NeoForge's fake-player/normal interaction paths. Claim/protection behavior depends on the other mods installed and still needs compatibility testing. Fabric's preview supports movement and conversation/speech plumbing; mining, placement, collection and combat are deliberately unavailable there and are absent from its advertised AI tools.

## Optional AI connection, later

No credential is needed to install or test local commands. Without a key, remote conversation remains visibly offline; accepted conversation is durably queued after remote opt-in. It does not produce a simulated AI reply.

The remote adapter reads `OPENAI_API_KEY` from the **Minecraft server process** environment. In single-player that is the game process. After setting it, fully close and restart both the launcher and Minecraft, then use `/chat remote on`. On Windows a full computer restart rules out a launcher retaining its old environment. Run `/chat agent` to check `API key visibility: DETECTED` or `NOT DETECTED` in the Minecraft server process. Detection confirms presence only; it does not prove authentication or API access. `Remote workflow not started` means a session has not started, independently of key presence. Do not paste a key into game chat, source code, the JAR or client packets. There is no in-game key provisioning UI in this alpha.

The default conversation model is `gpt-6-luna` with low reasoning and the Agents `agents=v1` beta contract. Synthesis uses `gpt-4o-mini-tts`, `alloy`, WAV. These adapters were tested against local fixtures, **not a live OpenAI account**. Actual account access, current beta compatibility and real model/voice output require a later live test. Generated speech is identified as artificial in the UI.

Remote conversation and remote speech are separate opt-ins. Only addressed owner conversation and bounded tool observations enter the configured remote workflow. There is no microphone recording or telemetry uploader. Turning remote processing off cancels/closes the local connection; it does not delete existing remote application state or retract a request already sent.

## Recovery and storage

Server state is stored beneath `<world>/chatcompanion/`. The server keeps a stable context identity and a checksummed, sequenced, append-only journal with fsynced checkpoints. Each accepted message has its own durable ID, even if its text repeats. Tool results, submission acknowledgements, saved-result confirmation and output delivery are separate records.

Retrying a recorded tool result reuses that result and never invokes the action executor. A crash after admission or an unverifiable restored world outcome becomes `UNKNOWN`; destructive work is not automatically repeated. The journal and Minecraft chunk/inventory saves are not one atomic transaction. A recorded job acceptance means a job started, not that its eventual world changes were crash-atomically saved.

Saved follow/move jobs suspend on restart and can be resumed explicitly. Other interrupted world jobs require review or a fresh user command. Local navigation runs independently of remote inference.

Clients persist display admission IDs in `config/chatcompanion-delivery.log` and speech admission IDs in `chatcompanion/speech-admissions.log`. Speech acknowledgement distinguishes queuing, actual native source start and completion. Admission before presentation favors suppressing repeats: a crash after admission can leave an uncertain display/playback attempt rather than replaying it automatically.

This alpha fails visibly when storage is corrupt or capacity is reached. Limits include 128 pending messages per actor, 32 active owner sessions, a 64 MiB journal and bounded client histories. Automatic journal compaction, storage migration, session migration, privacy deletion UI and legacy 0.19.x import are future work. Preserve the world and companion directory together when making backups; do not delete a ledger to retry an uncertain physical action.

## Build and test

Install Java 21, then use the included Gradle wrapper:

```bash
./gradlew :core:test :minecraft-common:test :neoforge:build :fabric:build
./gradlew :neoforge:runGameTestServer :fabric:runGameTestServer
```

On Windows use `gradlew.bat`. Loader JARs are written to each loader's `build/libs` directory; shared core/common classes are embedded, and Minecraft-owned Gson is not bundled.

Development clients: `./gradlew :neoforge:runClient` or `./gradlew :fabric:runClient`. Development servers use the corresponding `runServer` task. Minecraft downloads and loader dependencies require network access on the first build.

The explicit CI switch `-Dchatcompanion.speechSmoke=true` runs a local PCM playback check and then exits a client; it is inert in ordinary installations. `chatcompanion.speechSmokeReport` selects its report path. The verification record in [documentation/verification.md](documentation/verification.md) distinguishes fake-service tests, real GameTests, native audio and untested production environments.

## Current scope

Implemented: server-owned entity, local follow/stop/move, saved inventory and jobs, durable message/action records, generation fences, Agents transport/coordinator, streaming plus saved-item reconciliation, bounded retries, client speech pipeline, and operational commands. NeoForge has the physical actions above; Fabric is a smaller preview.

The larger [engineering specification](documentation/engineering-specification.md) remains the target for a stable release. This alpha does not complete continuous AI autonomy, complex task planning, modded machine allowlists, configurable budgets/models/voices, a management HUD, automatic migration/compaction, full protection integration or full modpack burn-in. Existing Chat Companion 0.19.x state is not imported.
