package dev.chatcompanion.core;

import com.google.gson.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.TreeMap;

public final class CanonicalJson {
    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
    private CanonicalJson() {}
    public static String encode(JsonElement element) { return GSON.toJson(sorted(element)); }
    private static JsonElement sorted(JsonElement value) {
        if (value == null || value.isJsonNull()) return JsonNull.INSTANCE;
        if (value.isJsonObject()) {
            JsonObject result = new JsonObject();
            new TreeMap<>(value.getAsJsonObject().asMap()).forEach((key, child) -> result.add(key, sorted(child)));
            return result;
        }
        if (value.isJsonArray()) {
            JsonArray result = new JsonArray(); value.getAsJsonArray().forEach(child -> result.add(sorted(child))); return result;
        }
        return value.deepCopy();
    }
    public static String hash(JsonElement element) { return hash(encode(element)); }
    public static String hash(String input) {
        try {
            return java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(input.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException impossible) { throw new IllegalStateException(impossible); }
    }
}
