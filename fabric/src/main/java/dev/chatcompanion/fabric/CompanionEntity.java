package dev.chatcompanion.fabric;

import java.util.UUID;
import net.minecraft.core.BlockPos;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.nbt.ListTag;
import net.minecraft.nbt.Tag;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.SimpleContainer;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.PathfinderMob;
import net.minecraft.world.entity.ai.attributes.AttributeSupplier;
import net.minecraft.world.entity.ai.attributes.Attributes;
import net.minecraft.world.entity.ai.goal.FloatGoal;
import net.minecraft.world.entity.ai.goal.LookAtPlayerGoal;
import net.minecraft.world.entity.ai.goal.RandomLookAroundGoal;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.Level;
import net.minecraft.world.phys.Vec3;

/** Server-owned goals keep ticking while the Agent is disconnected or busy. */
public final class CompanionEntity extends PathfinderMob {
    public enum JobState { SUSPENDED, RUNNING, COMPLETED, CANCELLED, FAILED, UNKNOWN }
    public enum JobType { NONE, FOLLOW, MOVE, MINE, COLLECT, DEFEND }

    private UUID owner;
    private UUID jobId;
    private JobType jobType = JobType.NONE;
    private JobState jobState = JobState.CANCELLED;
    private String reason = "idle";
    private Vec3 destination;
    private double stopDistance = 3;
    private long repathTick;
    private long lastProgressTick;
    private Vec3 progressPosition = Vec3.ZERO;
    private boolean recoveryTried;
    private boolean actionsAllowed;
    private boolean remoteConsent;
    private boolean speechConsent;
    private final SimpleContainer inventory = new SimpleContainer(36);

    public CompanionEntity(EntityType<? extends PathfinderMob> type, Level level) {
        super(type, level);
        setPersistenceRequired();
        setCanPickUpLoot(false);
    }

    public static AttributeSupplier.Builder attributes() {
        return createMobAttributes().add(Attributes.MAX_HEALTH, 20)
                .add(Attributes.MOVEMENT_SPEED, 0.28).add(Attributes.FOLLOW_RANGE, 48)
                .add(Attributes.ATTACK_DAMAGE, 3);
    }

    @Override protected void registerGoals() {
        goalSelector.addGoal(0, new FloatGoal(this));
        goalSelector.addGoal(7, new LookAtPlayerGoal(this, Player.class, 8));
        goalSelector.addGoal(8, new RandomLookAroundGoal(this));
    }

    public UUID owner() { return owner; }
    public void owner(UUID id) { owner = id; }
    public UUID jobId() { return jobId; }
    public JobType jobType() { return jobType; }
    public JobState jobState() { return jobState; }
    public String reason() { return reason; }
    public SimpleContainer companionInventory() { return inventory; }
    public boolean actionsAllowed() { return actionsAllowed; }
    public void actionsAllowed(boolean value) { actionsAllowed = value; if (!value && jobType != JobType.FOLLOW && jobType != JobType.MOVE) stop("permission_revoked"); }
    public boolean remoteConsent() { return remoteConsent; }
    public void remoteConsent(boolean value) { remoteConsent = value; }
    public boolean speechConsent() { return speechConsent; }
    public void speechConsent(boolean value) { speechConsent = value; }
    public double stopDistance() { return stopDistance; }

    public UUID follow(double radius) {
        begin(JobType.FOLLOW, null, radius);
        return jobId;
    }

    public UUID move(Vec3 target, double radius) {
        begin(JobType.MOVE, target, radius);
        return jobId;
    }

    public void suspend(String why) {
        getNavigation().stop();
        if (jobState == JobState.RUNNING) jobState = JobState.SUSPENDED;
        reason = why;
    }

    public void stop(String why) {
        getNavigation().stop();
        jobState = JobState.CANCELLED;
        reason = why;
    }

    public boolean resume() {
        if (jobState != JobState.SUSPENDED || (jobType != JobType.FOLLOW && jobType != JobType.MOVE)) return false;
        jobState = JobState.RUNNING;
        reason = "resumed";
        resetProgress();
        return true;
    }

    public UUID externalJob(JobType type) { begin(type, null, 2); return jobId; }
    public void complete(String why) { getNavigation().stop(); jobState = JobState.COMPLETED; reason = why; }
    public void failJob(String why) { fail(why); }

    private void begin(JobType type, Vec3 target, double radius) {
        getNavigation().stop();
        jobId = UUID.randomUUID();
        jobType = type;
        jobState = JobState.RUNNING;
        reason = "moving";
        destination = target;
        stopDistance = radius;
        resetProgress();
    }

    private void resetProgress() {
        repathTick = Long.MIN_VALUE / 2;
        lastProgressTick = level().getGameTime();
        progressPosition = position();
        recoveryTried = false;
    }

    @Override public void tick() {
        super.tick();
        if (!(level() instanceof ServerLevel world) || jobState != JobState.RUNNING) return;
        if (jobType != JobType.FOLLOW && jobType != JobType.MOVE) return;
        Vec3 target = destination;
        ServerPlayer player = owner == null ? null : world.getServer().getPlayerList().getPlayer(owner);
        if (jobType == JobType.FOLLOW) {
            if (player == null) { suspend("owner_disconnected"); return; }
            if (player.level() != world) { fail("target_dimension_changed"); return; }
            target = player.position();
        }
        if (target == null || !world.hasChunkAt(BlockPos.containing(target))) { suspend("chunk_unloaded"); return; }
        if (position().distanceTo(target) <= stopDistance) {
            getNavigation().stop();
            if (jobType == JobType.MOVE) { jobState = JobState.COMPLETED; reason = "arrived"; }
            else { reason = "holding"; lastProgressTick = world.getGameTime(); progressPosition = position(); }
            return;
        }
        // Avoid lava/fire and drop-offs at the next immediate step.
        Vec3 step = target.subtract(position()).normalize();
        BlockPos next = BlockPos.containing(position().add(step.x, 0, step.z));
        if (world.getBlockState(next).getFluidState().is(net.minecraft.tags.FluidTags.LAVA)
                || world.getBlockState(next).is(net.minecraft.tags.BlockTags.FIRE)) {
            fail("hazard"); return;
        }
        long tick = world.getGameTime();
        if (tick - repathTick >= 15) {
            boolean path = jobType == JobType.FOLLOW
                    ? getNavigation().moveTo(player, 1.1)
                    : getNavigation().moveTo(target.x, target.y, target.z, 1.1);
            repathTick = tick;
            reason = path ? "moving" : "repath_pending";
        }
        if (position().distanceToSqr(progressPosition) >= 0.25) {
            progressPosition = position(); lastProgressTick = tick;
        } else if (tick - lastProgressTick >= (recoveryTried ? 200 : 100)) {
            if (recoveryTried) { fail("stuck"); }
            else { getNavigation().stop(); repathTick = Long.MIN_VALUE / 2; lastProgressTick = tick; recoveryTried = true; }
        }
    }

    private void fail(String why) { getNavigation().stop(); jobState = JobState.FAILED; reason = why; }

    @Override public void addAdditionalSaveData(CompoundTag tag) {
        super.addAdditionalSaveData(tag);
        tag.putInt("ChatCompanionSchema", 1);
        if (owner != null) tag.putUUID("Owner", owner);
        if (jobId != null) tag.putUUID("JobId", jobId);
        tag.putString("JobType", jobType.name()); tag.putString("JobState", jobState.name());
        tag.putString("JobReason", reason); tag.putDouble("StopDistance", stopDistance);
        tag.putBoolean("ActionsAllowed", actionsAllowed); tag.putBoolean("RemoteConsent", remoteConsent);
        tag.putBoolean("SpeechConsent", speechConsent);
        if (destination != null) { tag.putDouble("TargetX", destination.x); tag.putDouble("TargetY", destination.y); tag.putDouble("TargetZ", destination.z); }
        ListTag items = new ListTag();
        for (int i = 0; i < inventory.getContainerSize(); i++) {
            ItemStack stack = inventory.getItem(i);
            if (!stack.isEmpty()) { CompoundTag entry = new CompoundTag(); entry.putInt("Slot", i); entry.put("Item", stack.save(level().registryAccess())); items.add(entry); }
        }
        tag.put("CompanionItems", items);
    }

    @Override public void readAdditionalSaveData(CompoundTag tag) {
        super.readAdditionalSaveData(tag);
        if (tag.hasUUID("Owner")) owner = tag.getUUID("Owner");
        if (tag.hasUUID("JobId")) jobId = tag.getUUID("JobId");
        try { jobType = JobType.valueOf(tag.getString("JobType")); } catch (IllegalArgumentException e) { jobType = JobType.NONE; }
        try { jobState = JobState.valueOf(tag.getString("JobState")); } catch (IllegalArgumentException e) { jobState = JobState.UNKNOWN; }
        if (jobState == JobState.RUNNING) {
            jobState = jobType == JobType.FOLLOW || jobType == JobType.MOVE ? JobState.SUSPENDED : JobState.UNKNOWN;
            reason = jobState == JobState.UNKNOWN ? "restart_outcome_unknown" : "restart_requires_resume";
        }
        else reason = tag.getString("JobReason");
        stopDistance = Math.clamp(tag.getDouble("StopDistance"), 1, 8);
        if (tag.contains("TargetX")) destination = new Vec3(tag.getDouble("TargetX"), tag.getDouble("TargetY"), tag.getDouble("TargetZ"));
        actionsAllowed = tag.getBoolean("ActionsAllowed"); remoteConsent = tag.getBoolean("RemoteConsent"); speechConsent = tag.getBoolean("SpeechConsent");
        inventory.clearContent();
        ListTag items = tag.getList("CompanionItems", Tag.TAG_COMPOUND);
        for (int i = 0; i < items.size(); i++) {
            CompoundTag entry = items.getCompound(i); int slot = entry.getInt("Slot");
            if (slot >= 0 && slot < inventory.getContainerSize()) inventory.setItem(slot, ItemStack.parseOptional(level().registryAccess(), entry.getCompound("Item")));
        }
    }
}
