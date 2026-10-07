package dev.chatcompanion.fabric;

import com.google.gson.JsonObject;
import java.util.UUID;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.phys.Vec3;
import net.fabricmc.fabric.api.gametest.v1.FabricGameTest;

/** Real server-world checks, independent of OpenAI access. */
public final class CompanionGameTests implements FabricGameTest {
    public CompanionGameTests() {}
    private static void floor(GameTestHelper helper) {
        for (int x = 0; x < 12; x++) for (int z = 0; z < 12; z++) helper.setBlock(x, 1, z, Blocks.STONE);
    }
    @GameTest(template = "chatcompanion:empty", timeoutTicks = 250)
    public void movementChangesPositionAndStops(GameTestHelper helper) {
        floor(helper);
        CompanionEntity companion = helper.spawn(ChatCompanionFabric.COMPANION_TYPE, new BlockPos(2, 2, 2));
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
    @GameTest(template = "chatcompanion:empty", timeoutTicks = 300)
    public void followHoldsAndFollowsAgain(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 target = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(8, 2, 2))); owner.setPos(target.x, target.y, target.z);
        CompanionEntity companion = helper.spawn(ChatCompanionFabric.COMPANION_TYPE, new BlockPos(2, 2, 2)); companion.owner(owner.getUUID()); companion.follow(2);
        helper.runAfterDelay(80, () -> {
            helper.assertTrue(companion.distanceTo(owner) < 2.5, "Follow should reduce distance to the real server player");
            helper.assertTrue(companion.jobState() == CompanionEntity.JobState.RUNNING && companion.reason().equals("holding"), "Arrival must hold a continuous follow job");
            Vec3 next = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 8))); owner.setPos(next.x, next.y, next.z);
            helper.runAfterDelay(100, () -> { helper.assertTrue(companion.distanceTo(owner) < 2.5, "Follow should resume when the owner moves"); companion.stop("test_end"); owner.connection.disconnect(net.minecraft.network.chat.Component.literal("test complete")); helper.succeed(); });
        });
    }

    @GameTest(template = "chatcompanion:empty", timeoutTicks = 150)
    public void immediateStopFencesSessionInitialization(GameTestHelper helper) {
        floor(helper);
        ServerPlayer owner = helper.makeMockServerPlayerInLevel();
        Vec3 origin = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(8, 2, 2)));
        owner.setPos(origin.x, origin.y, origin.z);
        CompanionEntity companion = ChatCompanionFabric.service.spawn(owner);
        Vec3 started = companion.position();
        Vec3 target = Vec3.atBottomCenterOf(helper.absolutePos(new BlockPos(2, 2, 2)));
        owner.setPos(target.x, target.y, target.z);
        JsonObject args = new JsonObject();
        args.addProperty("player_id", owner.getUUID().toString()); args.addProperty("stop_distance", 2);
        ChatCompanionFabric.service.action(owner, "follow_player", args);
        ChatCompanionFabric.service.stop(owner);
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
