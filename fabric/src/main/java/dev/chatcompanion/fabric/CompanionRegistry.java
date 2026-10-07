package dev.chatcompanion.fabric;

import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import net.minecraft.core.HolderLookup;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.nbt.ListTag;
import net.minecraft.nbt.Tag;
import net.minecraft.server.MinecraftServer;
import net.minecraft.world.level.saveddata.SavedData;
import net.minecraft.util.datafix.DataFixTypes;

/** Prevents duplicate summons while an existing owned companion is unloaded. */
final class CompanionRegistry extends SavedData {
    record Reference(UUID entity, String dimension) {}
    private final Map<UUID, Reference> companions = new HashMap<>();
    static CompanionRegistry get(MinecraftServer server) {
        return server.overworld().getDataStorage().computeIfAbsent(new Factory<>(CompanionRegistry::new,
                CompanionRegistry::load, DataFixTypes.LEVEL), "chatcompanion_registry");
    }
    Reference get(UUID owner) { return companions.get(owner); }
    void set(UUID owner, UUID entity, String dimension) { companions.put(owner, new Reference(entity, dimension)); setDirty(); }
    void remove(UUID owner) { companions.remove(owner); setDirty(); }
    @Override public CompoundTag save(CompoundTag tag, HolderLookup.Provider registries) {
        ListTag list = new ListTag();
        companions.forEach((owner, ref) -> { CompoundTag e = new CompoundTag(); e.putUUID("Owner", owner); e.putUUID("Entity", ref.entity()); e.putString("Dimension", ref.dimension()); list.add(e); });
        tag.putInt("Schema", 1); tag.put("Companions", list); return tag;
    }
    private static CompanionRegistry load(CompoundTag tag, HolderLookup.Provider registries) {
        CompanionRegistry result = new CompanionRegistry();
        ListTag list = tag.getList("Companions", Tag.TAG_COMPOUND);
        for (int i = 0; i < list.size(); i++) { CompoundTag e = list.getCompound(i); if (e.hasUUID("Owner") && e.hasUUID("Entity")) result.companions.put(e.getUUID("Owner"), new Reference(e.getUUID("Entity"), e.getString("Dimension"))); }
        return result;
    }
}
