package dev.chatcompanion.neoforge;

import com.google.gson.JsonObject;
import dev.chatcompanion.core.ActionOutcome;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.BoolArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.commands.arguments.EntityArgument;
import net.minecraft.commands.arguments.coordinates.BlockPosArgument;
import net.minecraft.core.BlockPos;
import net.minecraft.network.chat.Component;
import net.minecraft.server.level.ServerPlayer;
import net.neoforged.neoforge.event.RegisterCommandsEvent;
import net.neoforged.neoforge.event.ServerChatEvent;

final class CompanionCommands {
    private CompanionCommands() {}
    static void register(RegisterCommandsEvent event) { register(event.getDispatcher()); }
    static void register(CommandDispatcher<CommandSourceStack> dispatcher) {
        var root = Commands.literal("chat").executes(context -> {
            context.getSource().sendSuccess(() -> Component.literal("Chat Companion: spawn, follow, stop, move, say, agent, speech, give, mine, place, collect, defend, resume, inventory. Normal chat and autonomous behavior use the same local MCP brain."), false); return 1;
        });
        root.then(Commands.literal("spawn").executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); try { CompanionEntity companion = service().spawn(owner); CompanionService.message(owner, "Companion ready: " + companion.getUUID()); } catch (IllegalStateException error) { CompanionService.message(owner, error.getMessage()); } return 1; }));
        root.then(Commands.literal("follow").executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); JsonObject args = new JsonObject(); args.addProperty("player_id", owner.getUUID().toString()); args.addProperty("stop_distance", 3); local(owner, "follow_player", args); return 1; }));
        root.then(Commands.literal("stop").executes(context -> { service().stop(context.getSource().getPlayerOrException()); return 1; }));
        root.then(Commands.literal("cancel").executes(context -> { service().stop(context.getSource().getPlayerOrException()); return 1; }));
        root.then(Commands.literal("resume").executes(context -> { service().resume(context.getSource().getPlayerOrException()); return 1; }));
        root.then(Commands.literal("agent").executes(context -> { service().status(context.getSource().getPlayerOrException()); return 1; }));
        root.then(Commands.literal("tasks").executes(context -> { service().status(context.getSource().getPlayerOrException()); return 1; }));
        root.then(Commands.literal("say").then(Commands.argument("message", StringArgumentType.greedyString()).executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); LocalAgentInbox.offer(owner.getUUID(), StringArgumentType.getString(context, "message")); return 1; })));
        root.then(Commands.literal("move").then(Commands.argument("position", BlockPosArgument.blockPos()).executes(context -> {
            ServerPlayer owner = context.getSource().getPlayerOrException(); JsonObject args = position(owner, BlockPosArgument.getBlockPos(context, "position")); args.addProperty("stop_distance", 1.5); local(owner, "move_to", args); return 1;
        })));
        var speech = Commands.literal("speech");
        speech.then(Commands.literal("on").executes(context -> { service().consent(context.getSource().getPlayerOrException(), "speech", true); return 1; }));
        speech.then(Commands.literal("off").executes(context -> { service().consent(context.getSource().getPlayerOrException(), "speech", false); return 1; }));
        speech.then(Commands.literal("test").executes(context -> { CompanionNetworking.speech(context.getSource().getPlayerOrException(), true, true); return 1; }));
        root.then(speech);

        var remote = Commands.literal("remote");
        remote.then(Commands.literal("on").executes(context -> {
            CompanionService.message(context.getSource().getPlayerOrException(),
                    "Legacy remote workflow is disabled. The local MCP Python agent is the only AI brain now.");
            return 1;
        }));
        remote.then(Commands.literal("off").executes(context -> {
            CompanionService.message(context.getSource().getPlayerOrException(),
                    "Legacy remote workflow is already disabled.");
            return 1;
        }));
        root.then(remote);
        root.then(Commands.literal("give").executes(context -> { service().give(context.getSource().getPlayerOrException()); return 1; }));
        root.then(Commands.literal("inventory").executes(context -> {
            ServerPlayer owner = context.getSource().getPlayerOrException(); CompanionEntity companion = service().find(owner.getUUID());
            if (companion == null) CompanionService.message(owner, "Companion unavailable.");
            else { for (int i = 0; i < companion.companionInventory().getContainerSize(); i++) if (!companion.companionInventory().getItem(i).isEmpty()) CompanionService.message(owner, "Slot " + i + ": " + companion.companionInventory().getItem(i).getHoverName().getString() + " x" + companion.companionInventory().getItem(i).getCount()); }
            return 1;
        }));
        root.then(Commands.literal("mine").then(Commands.argument("position", BlockPosArgument.blockPos()).executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); local(owner, "mine_block", position(owner, BlockPosArgument.getBlockPos(context, "position"))); return 1; })));
        root.then(Commands.literal("place").then(Commands.argument("position", BlockPosArgument.blockPos()).executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); JsonObject args = position(owner, BlockPosArgument.getBlockPos(context, "position")); args.addProperty("inventory_slot", 0); args.addProperty("face", "up"); local(owner, "place_block", args); return 1; })));
        root.then(Commands.literal("collect").executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); JsonObject args = new JsonObject(); args.addProperty("radius", 8); args.addProperty("max_items", 16); local(owner, "collect_items", args); return 1; }));
        root.then(Commands.literal("defend").then(Commands.argument("target", EntityArgument.entity()).executes(context -> { ServerPlayer owner = context.getSource().getPlayerOrException(); JsonObject args = new JsonObject(); args.addProperty("entity_id", EntityArgument.getEntity(context, "target").getUUID().toString()); local(owner, "attack_entity", args); return 1; })));
        root.then(Commands.literal("privacy").executes(context -> { context.getSource().sendSuccess(() -> Component.literal("Conversation and autonomy run through the local MCP Python agent. No microphone or hidden telemetry. /chat remote is legacy and disabled; /chat speech controls generated voice only."), false); return 1; }));
        dispatcher.register(root);
    }
    static void chat(ServerChatEvent event) {
        if (event.isCanceled() || ChatCompanion.service == null) return;
        String text = event.getRawText().strip();
        if (!text.isEmpty()) LocalAgentInbox.offer(event.getPlayer().getUUID(), text);
    }
    private static JsonObject position(ServerPlayer player, BlockPos pos) { JsonObject args = new JsonObject(); args.addProperty("dimension", player.level().dimension().location().toString()); args.addProperty("x", pos.getX()); args.addProperty("y", pos.getY()); args.addProperty("z", pos.getZ()); return args; }
    private static void local(ServerPlayer owner, String name, JsonObject args) {
        ActionOutcome outcome = service().localAction(owner, name, args);
        if (!outcome.success()) CompanionService.message(owner, "Action rejected: " + outcome.reasonCode());
    }
    private static CompanionService service() { if (ChatCompanion.service == null) throw new IllegalStateException("Companion server not ready"); return ChatCompanion.service; }
}
