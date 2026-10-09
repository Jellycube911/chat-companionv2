package dev.chatcompanion.neoforge;

import com.google.gson.JsonObject;
import dev.chatcompanion.core.ActionOutcome;
import java.util.UUID;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.phys.Vec3;
import net.neoforged.neoforge.gametest.GameTestHolder;
import net.neoforged.neoforge.gametest.PrefixGameTestTemplate;

/** Real server-world checks, independent of OpenAI access. */
@GameTestHolder(ChatCompanion.MOD_ID)
@PrefixGameTestTemplate(false)
public final class CompanionGameTests {
    private CompanionGameTests() {}
    private static void floor(GameTestHelper helper) {
        for (int x = 0; x < 12; x++) for (int z = 0; z < 12; z++) helper.setBlock(x, 1, z, Blocks.STONE);
    }
    @GameTest(template = "empty", timeoutTicks = 250)
    public static void movementChangesPositionAndStops(GameTestHelper helper) {
        floor(helper);
        CompanionEntity companion = helper.spawn(ChatCompanion.COMPANION.get(), new BlockPos(2, 2, 2));
        companion.owner(UUID.randomUUID());
        Vec3 start = companion.position(); Vec3 target = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(8, 2, 2)));
        companion.move(target, 1);
        helper.runAfterDelay(80, () -> {
            helper.assertTrue(companion.position().distanceTo(start) > 2, "Companion must physically move, not only report a path");
            helper.assertTrue(companion.position().distanceTo(target) < 1.5, "Companion should approach its destination");
            companion.stop("test_stop"); Vec3 stopped = companion.position();
            helper.runAfterDelay(10, () -> { helper.assertTrue(companion.position().distanceTo(stopped) < 0.5, "Stopping must end navigation"); helper.succeed(); });
        });
    }
    @GameTest(template = "empty", timeoutTicks = 300)
    public static void followHoldsAndFollowsAgain(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 target = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(8, 2, 2))); owner.setPos(target.x, target.y, target.z);
        CompanionEntity companion = helper.spawn(ChatCompanion.COMPANION.get(), new BlockPos(2, 2, 2)); companion.owner(owner.getUUID()); companion.follow(2);
        helper.runAfterDelay(80, () -> {
            helper.assertTrue(companion.distanceTo(owner) < 2.5, "Follow should reduce distance to the real server player");
            helper.assertTrue(companion.jobState() == CompanionEntity.JobState.RUNNING && companion.reason().equals("holding"), "Arrival must hold a continuous follow job");
            Vec3 next = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 8))); owner.setPos(next.x, next.y, next.z);
            helper.runAfterDelay(100, () -> { helper.assertTrue(companion.distanceTo(owner) < 2.5, "Follow should resume when the owner moves"); companion.stop("test_end"); owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete")); helper.succeed(); });
        });
    }
    @GameTest(template = "empty", timeoutTicks = 260)
    public static void miningApproachesAndBreaksWithoutApprovalGate(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 pos = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 2)));
        owner.setPos(pos.x, pos.y, pos.z);

        CompanionEntity companion = ChatCompanion.service.spawn(owner);
        companion.companionInventory().setItem(0, new ItemStack(Items.DIAMOND_PICKAXE));
        Vec3 start = companion.position();

        BlockPos target = companion.blockPosition().offset(6, 0, 0);
        helper.getLevel().setBlockAndUpdate(target, Blocks.STONE.defaultBlockState());

        JsonObject args = new JsonObject();
        args.addProperty("dimension", owner.level().dimension().location().toString());
        args.addProperty("x", target.getX());
        args.addProperty("y", target.getY());
        args.addProperty("z", target.getZ());

        ChatCompanion.service.action(owner, "mine_block", args);
        helper.runAfterDelay(160, () -> {
            helper.assertTrue(companion.position().distanceTo(start) > 1.0,
                    "Mining a distant block must physically approach it");
            helper.assertTrue(!helper.getLevel().getBlockState(target).is(Blocks.STONE),
                    "Mining must break the actual target block without an approval toggle");
            helper.assertTrue(companion.jobState() == CompanionEntity.JobState.COMPLETED,
                    "Mining should complete only after the block is actually changed");
            owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete"));
            helper.succeed();
        });
    }

    @GameTest(template = "empty", timeoutTicks = 260)
    public static void localMcpMiningNeedsNoLegacySession(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 pos = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 2)));
        owner.setPos(pos.x, pos.y, pos.z);

        CompanionEntity companion = ChatCompanion.service.spawn(owner);
        companion.companionInventory().setItem(0, new ItemStack(Items.DIAMOND_PICKAXE));

        BlockPos target = companion.blockPosition().offset(5, 0, 0);
        helper.getLevel().setBlockAndUpdate(target, Blocks.STONE.defaultBlockState());

        JsonObject args = new JsonObject();
        args.addProperty("dimension", owner.level().dimension().location().toString());
        args.addProperty("x", target.getX());
        args.addProperty("y", target.getY());
        args.addProperty("z", target.getZ());

        ActionOutcome outcome = ChatCompanion.service.localAction(owner, "mine_block", args);
        helper.assertTrue(outcome.success(), "Local MCP action should be admitted without starting a legacy remote session");

        helper.runAfterDelay(160, () -> {
            helper.assertTrue(!helper.getLevel().getBlockState(target).is(Blocks.STONE),
                    "Direct local MCP mining must complete without /chat remote on");
            helper.assertTrue(companion.jobState() == CompanionEntity.JobState.COMPLETED,
                    "Direct local MCP job should reach a terminal completed state");
            owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete"));
            helper.succeed();
        });
    }

    @GameTest(template = "empty", timeoutTicks = 160)
    public static void miningHasObservableProgressBeforeCompletion(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 pos = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 2)));
        owner.setPos(pos.x, pos.y, pos.z);
        CompanionEntity companion = ChatCompanion.service.spawn(owner);
        companion.companionInventory().setItem(0, new ItemStack(Items.WOODEN_PICKAXE));
        BlockPos target = companion.blockPosition().offset(2, 0, 0);
        helper.getLevel().setBlockAndUpdate(target, Blocks.STONE.defaultBlockState());
        JsonObject args = new JsonObject();
        args.addProperty("dimension", owner.level().dimension().location().toString());
        args.addProperty("x", target.getX());
        args.addProperty("y", target.getY());
        args.addProperty("z", target.getZ());
        helper.assertTrue(ChatCompanion.service.localAction(owner, "mine_block", args).success(),
                "Progressive mining must be admitted");
        helper.runAfterDelay(8, () -> {
            helper.assertTrue(companion.jobProgress() > 0.0F && companion.jobProgress() < 1.0F,
                    "Mining must expose partial progress before the block breaks");
            helper.assertTrue(helper.getLevel().getBlockState(target).is(Blocks.STONE),
                    "Partial progress must not immediately remove the block");
            helper.runAfterDelay(85, () -> {
                helper.assertTrue(companion.jobState() == CompanionEntity.JobState.COMPLETED,
                        "Progressive mining must eventually finish");
                helper.assertTrue(!helper.getLevel().getBlockState(target).is(Blocks.STONE),
                        "Completed mining must change the physical world");
                helper.assertTrue(companion.reason().contains("break_ticks="),
                        "Successful mining must report observed break timing");
                owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete"));
                helper.succeed();
            });
        });
    }

    @GameTest(template = "empty", timeoutTicks = 150)
    public static void miningReportsObstructionWithoutStalling(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 pos = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 2)));
        owner.setPos(pos.x, pos.y, pos.z);
        CompanionEntity companion = ChatCompanion.service.spawn(owner);
        BlockPos target = companion.blockPosition().offset(3, 0, 0);
        BlockPos blocker = companion.blockPosition().offset(1, 0, 0);
        helper.getLevel().setBlockAndUpdate(target, Blocks.STONE.defaultBlockState());
        helper.getLevel().setBlockAndUpdate(blocker, Blocks.STONE.defaultBlockState());
        helper.getLevel().setBlockAndUpdate(blocker.above(), Blocks.STONE.defaultBlockState());
        JsonObject args = new JsonObject();
        args.addProperty("dimension", owner.level().dimension().location().toString());
        args.addProperty("x", target.getX());
        args.addProperty("y", target.getY());
        args.addProperty("z", target.getZ());
        ActionOutcome admitted = ChatCompanion.service.localAction(owner, "mine_block", args);
        helper.assertTrue(admitted.success(), "Blocked mining experiment must be admitted");
        helper.runAfterDelay(18, () -> {
            helper.assertTrue(companion.jobState() == CompanionEntity.JobState.FAILED,
                    "Occluded mining must fail promptly, not wait indefinitely");
            helper.assertTrue(companion.reason().contains("mining_blocked|block=minecraft:stone|at="),
                    "The obstruction must be reported for local experimentation");
            helper.assertTrue(helper.getLevel().getBlockState(target).is(Blocks.STONE),
                    "Failed mining must not mutate the target");
            owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete"));
            helper.succeed();
        });
    }

    @GameTest(template = "empty", timeoutTicks = 150)
    public static void immediateStopFencesSessionInitialization(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 origin = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(8, 2, 2)));
        owner.setPos(origin.x, origin.y, origin.z);
        CompanionEntity companion = ChatCompanion.service.spawn(owner);
        Vec3 started = companion.position();
        Vec3 target = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 2)));
        owner.setPos(target.x, target.y, target.z);
        JsonObject args = new JsonObject();
        args.addProperty("player_id", owner.getUUID().toString()); args.addProperty("stop_distance", 2);
        ChatCompanion.service.action(owner, "follow_player", args);
        ChatCompanion.service.stop(owner);
        helper.runAfterDelay(60, () -> {
            helper.assertTrue(companion.position().distanceTo(started) < 0.5,
                    "A delayed durable admission must not restart movement after an immediate stop");
            helper.assertTrue(companion.jobState() != CompanionEntity.JobState.RUNNING,
                    "Session initialization must preserve the latest local control");
            owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete"));
            helper.succeed();
        });
    }
}
