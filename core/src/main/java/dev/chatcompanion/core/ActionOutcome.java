package dev.chatcompanion.core;

import com.google.gson.JsonObject;
import java.util.Objects;
import java.util.Set;

/** An observed/local job outcome, never a claim of atomic Minecraft save durability. */
public record ActionOutcome(boolean success, String status, String reasonCode, JsonObject evidence) {
    public ActionOutcome {
        Objects.requireNonNull(status); Objects.requireNonNull(reasonCode);
        if (!Set.of("accepted", "observed", "rejected", "cancelled", "unknown").contains(status))
            throw new IllegalArgumentException("Unsupported action status: " + status);
        if ((status.equals("unknown") || status.equals("rejected") || status.equals("cancelled")) && success)
            throw new IllegalArgumentException("Failure outcome cannot claim success");
        evidence = evidence == null ? new JsonObject() : evidence.deepCopy();
    }
    @Override public JsonObject evidence() { return evidence.deepCopy(); }
    public JsonObject toJson() {
        JsonObject object = new JsonObject();
        object.addProperty("schema_version", 1); object.addProperty("success", success);
        object.addProperty("status", status); object.addProperty("reason_code", reasonCode);
        object.add("evidence", evidence()); return object;
    }
    public static ActionOutcome accepted(String jobId) {
        JsonObject evidence = new JsonObject(); evidence.addProperty("job_id", jobId);
        return new ActionOutcome(true, "accepted", "job_installed", evidence);
    }
    public static ActionOutcome rejected(String reason) { return new ActionOutcome(false, "rejected", reason, null); }
    public static ActionOutcome unknown(String reason) { return new ActionOutcome(false, "unknown", reason, null); }
}
