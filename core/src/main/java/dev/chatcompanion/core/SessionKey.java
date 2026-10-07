package dev.chatcompanion.core;

import java.util.Objects;
import java.util.UUID;

public record SessionKey(UUID playerUuid, String serverContextId, String worldId, UUID companionUuid) {
    public SessionKey { Objects.requireNonNull(playerUuid); Objects.requireNonNull(serverContextId);
        Objects.requireNonNull(worldId); Objects.requireNonNull(companionUuid); }
    public String storageKey() { return CanonicalJson.hash(toString()); }
}
