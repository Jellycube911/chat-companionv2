package dev.chatcompanion.game;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/** Only expose capabilities actually implemented by the current loader adapter. */
public final class CompanionProfile {
    private CompanionProfile() {}
    public static String instructions(UUID owner, UUID companion, String platform) {
        return "You are Chat, a friendly Minecraft companion on " + platform + " Minecraft 1.21.1. Your authorized owner UUID is " + owner
                + " and companion UUID is " + companion + ". Speak briefly and naturally. Minecraft's server validates every operation. "
                + "Use get_companion_status before spatial plans; inspect_nearby for bounded observations. "
                + "Action context contains a server-issued intent_id; copy it exactly into each physical tool. Never invent an intent or act for another player. "
                + "Only act within the owner's current request. Assistant text does not execute movement. "
                + "A tool status accepted means a job started, not that it finished. Follow remains running/holding after arrival. "
                + "Use get_task_status for actual progress; never claim mining, placement, movement or audio happened without observed evidence. "
                + "Respect rejection, unknown outcomes, disabled world actions and local cancellation. Do not retry an uncertain destructive action. "
                + "Do not run commands, shell, arbitrary code, interact with unsupported machines or access other players' inventories. "
                + "Read only owner-authorized context. No continuous autonomous decisions are enabled in this build.";
    }
    public static List<String> tools(boolean worldActions) {
        List<String> result = new ArrayList<>();
        result.add(function("get_companion_status", "Observe authoritative companion status.", false));
        result.add(function("inspect_nearby", "Observe bounded nearby entities.", false,
                "radius", number("integer", 1, 16), "max_entities", number("integer", 1, 32)));
        result.add(function("follow_player", "Start continuous owner follow; acceptance is not arrival.", true,
                "player_id", string(), "stop_distance", number("number", 2, 8)));
        result.add(function("move_to", "Start a bounded same-dimension movement job.", true,
                "dimension", string(), "x", number("integer", -30000000, 30000000), "y", number("integer", -2048, 2048),
                "z", number("integer", -30000000, 30000000), "stop_distance", number("number", 1, 3)));
        result.add(function("stop_action", "Stop local physical work.", true));
        result.add(function("get_task_status", "Observe the owner's current local job.", false, "job_id", string()));
        result.add(function("cancel_task", "Cancel the owner's current local job.", true, "job_id", string()));
        if (worldActions) {
            result.add(function("mine_block", "Start one-block protected survival mining with companion inventory slot zero.", true,
                    "dimension", string(), "x", number("integer", -30000000, 30000000), "y", number("integer", -2048, 2048), "z", number("integer", -30000000, 30000000)));
            JsonObject face = string(); JsonArray faces = new JsonArray(); for (String value : List.of("up", "down", "north", "south", "east", "west")) faces.add(value); face.add("enum", faces);
            result.add(function("place_block", "Place a companion inventory item using standard protected interaction semantics.", true,
                    "dimension", string(), "x", number("integer", -30000000, 30000000), "y", number("integer", -2048, 2048), "z", number("integer", -30000000, 30000000), "inventory_slot", number("integer", 0, 35), "face", face));
            result.add(function("collect_items", "Start bounded nearby item collection into companion inventory.", true,
                    "radius", number("integer", 1, 8), "max_items", number("integer", 1, 32)));
            result.add(function("attack_entity", "Start a bounded defensive job against one eligible hostile, never PvP.", true, "entity_id", string()));
        }
        return List.copyOf(result);
    }
    private static String function(String name, String description, boolean action, Object... fields) {
        JsonObject properties = new JsonObject(); JsonArray required = new JsonArray();
        if (action) { properties.add("intent_id", string()); required.add("intent_id"); }
        for (int i = 0; i < fields.length; i += 2) { String field = (String) fields[i]; properties.add(field, (JsonObject) fields[i + 1]); required.add(field); }
        JsonObject schema = new JsonObject(); schema.addProperty("type", "object"); schema.add("properties", properties); schema.add("required", required); schema.addProperty("additionalProperties", false);
        JsonObject definition = new JsonObject(); definition.addProperty("type", "function"); definition.addProperty("name", name); definition.addProperty("description", description); definition.add("parameters", schema);
        return definition.toString();
    }
    private static JsonObject string() { JsonObject schema = new JsonObject(); schema.addProperty("type", "string"); return schema; }
    private static JsonObject number(String type, double min, double max) { JsonObject schema = new JsonObject(); schema.addProperty("type", type); schema.addProperty("minimum", min); schema.addProperty("maximum", max); return schema; }
}
