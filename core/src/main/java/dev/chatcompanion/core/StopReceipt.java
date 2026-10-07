package dev.chatcompanion.core;
/** Local fencing has already happened even when durable=false. */
public record StopReceipt(long controlGeneration, boolean durable, String reason) {}
