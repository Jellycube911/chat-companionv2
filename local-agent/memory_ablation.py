"""Live, memory-on versus memory-withheld Minecraft practical experiments.

This is an A/B *outcome* comparison, not neural-weight training or a
claim of causality. Trials use real Minecraft and the same uncoached Qwen
policy, with the prior verified SQLite memories frozen at session start.
Only the prompt's past-experience fields change. Runs are balanced in
random order; world positions and natural resources can still differ.

NEVER reset/delete SQLite or teleport the companion to repeat a trial.
"""
import argparse
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memory_store import store
from stage1_exam import exam_memory_context, run_exam
from training_lab import (
    REPORT_DIR, RealWorld, OllamaPlanner, TRAINING_BUILD,
    _atomic_checkpoint, exclusive_controller,
)


def _arm_schedule(trials, seed):
    if not 2 <= int(trials) <= 20 or int(trials) % 2:
        raise ValueError("Use an even number of trials between 2 and 20.")
    rng = random.Random(seed)
    schedule = []
    # Block randomization reduces (but cannot eliminate) time/location bias.
    for _ in range(int(trials) // 2):
        pair = ["memory_on", "memory_off"]
        rng.shuffle(pair)
        schedule.extend(pair)
    return schedule


class _VisiblePlanner:
    """Expose the otherwise silent, potentially slow Ollama proposal calls.

    This wrapper changes no instructions, prompts, model options or outputs.
    """
    def __init__(self, planner):
        self.planner = planner
        self.trial_index = 0
        self.total_trials = 0
        self.condition = "unknown"
        self.step = 0

    def start_trial(self, index, total, condition):
        self.trial_index = index
        self.total_trials = total
        self.condition = condition
        self.step = 0

    def propose(self, prompt):
        self.step += 1
        label = ("trial %d/%d %s, proposal %d" %
                 (self.trial_index, self.total_trials, self.condition, self.step))
        print("[MEMORY TEST] %s: Qwen is choosing an action..." % label,
              flush=True)
        started = time.monotonic()
        try:
            answer = self.planner.propose(prompt)
        except BaseException as error:
            print("[MEMORY TEST] %s: Qwen request failed after %.1fs (%s)" %
                  (label, time.monotonic() - started, type(error).__name__),
                  flush=True)
            raise
        action = answer.get("action") if isinstance(answer, dict) else "invalid"
        print("[MEMORY TEST] %s: Qwen returned %s after %.1fs" %
              (label, action, time.monotonic() - started), flush=True)
        return answer


def _summarize(trials):
    arms = {}
    for arm in ("memory_on", "memory_off"):
        cases = [t for t in trials if t["condition"] == arm]
        passed = sum(t.get("status") == "passed" for t in cases)
        resolved = [t for t in cases if t.get("status") in {"passed", "failed"}]
        actions = [
            a for t in cases for a in t.get("experiments", [])
            if a.get("result") is not None
        ]
        arms[arm] = {
            "trials": len(cases),
            "passed": passed,
            "failed": sum(t.get("status") == "failed" for t in cases),
            "unavailable": sum(t.get("status") == "unavailable" for t in cases),
            "pending": sum(t.get("status") == "pending" for t in cases),
            "resolved_pass_rate": round(passed / len(resolved), 3)
            if resolved else None,
            "verified_physical_steps": sum(
                a.get("verified") is True
                and a.get("action") in {"move_to", "look_at", "mine", "collect"}
                for a in actions),
            "mean_seconds": round(
                sum(t["seconds"] for t in cases) / len(cases), 2)
            if cases else None,
        }
    return {
        "arms": arms,
        "memory_minus_baseline_pass_rate": round(
            arms["memory_on"]["resolved_pass_rate"] -
            arms["memory_off"]["resolved_pass_rate"], 3)
        if (arms["memory_on"]["resolved_pass_rate"] is not None and
            arms["memory_off"]["resolved_pass_rate"] is not None) else None,
        "interpretation": (
            "Descriptive real-world comparison only. Minecraft conditions "
            "change between trials, and sample size is small. A higher "
            "memory-on score suggests an advantage but does NOT establish "
            "causal learning without replicated, comparable novel scenarios."
        ),
    }


def run_comparison(world, planner, memory, *, trials=6, max_steps=18,
                   seed=17, report_dir=REPORT_DIR, max_minutes=60):
    schedule = _arm_schedule(trials, seed)
    print("[MEMORY TEST] Reading verified learning evidence from SQLite...",
          flush=True)
    context = exam_memory_context(memory)  # Freeze historical context.
    exposed = (len(context["previous_learned_experiences"]) +
               len(context["learned_procedures"]))
    print("[MEMORY TEST] Historical evidence: %d experiences, %d learned "
          "procedures." % (len(context["previous_learned_experiences"]),
                            len(context["learned_procedures"])), flush=True)
    visible_planner = _VisiblePlanner(planner)
    path = Path(report_dir) / (
        "memory-comparison-" +
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") +
        "-" + uuid.uuid4().hex[:8] + ".json")
    report = {
        "version": TRAINING_BUILD,
        "kind": "live_stage1_memory_ablation",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "report_path": str(path),
        "local_model_same_in_both_arms": True,
        "prior_memory_frozen": True,
        "comparison_no_exam_graduation_credit": True,
        "schedule": schedule,
        "memory_available": context,
        "experiences_available": exposed,
        "random_seed": seed,
        "trials": [], "summary": {},
        "important_limitation": (
            "Natural Minecraft terrain, available drops, and location differ "
            "between sequential trials. Randomized balanced order reduces "
            "but does not eliminate this confound."
        ),
    }

    def checkpoint():
        report["completed_trials"] = len(report["trials"])
        report["summary"] = _summarize(report["trials"])
        _atomic_checkpoint(report, path)

    checkpoint()
    print("[MEMORY TEST] Saving progress to: %s" % path, flush=True)
    if not exposed:
        report["status"] = "not_testable_no_verified_memories"
        checkpoint()
        print("[MEMORY TEST] No verified experience to compare. "
              "Run train.bat first.", flush=True)
        return report

    start = time.monotonic()
    no_physical_trials = 0
    try:
        for index, arm in enumerate(schedule, start=1):
            if time.monotonic() - start > float(max_minutes) * 60:
                report["status"] = "stopped_early_time_limit"
                break
            before = world.snapshot()
            state = before["state"]
            report["active_trial"] = {
                "index": index, "condition": arm,
                "status": "incomplete_unverified",
                "start": [float(state[k]) for k in ("x", "y", "z")],
                "dimension": state.get("dimension"),
                "experiments": [],
            }
            checkpoint()
            print("[MEMORY TEST] Beginning trial %d/%d (%s) at %s" %
                  (index, trials, arm, report["active_trial"]["start"]),
                  flush=True)
            visible_planner.start_trial(index, trials, arm)

            def progress(steps):
                report["active_trial"]["experiments"] = steps
                checkpoint()
                if steps and steps[-1].get("status") != "in_flight":
                    latest = steps[-1]
                    print("[MEMORY TEST] Trial %d/%d step %d: %s, %s (%s)" %
                          (index, trials, len(steps),
                           latest.get("action", "unknown"),
                           "verified" if latest.get("verified") else "not verified",
                           latest.get("reason", "unknown")), flush=True)

            trial = run_exam(
                world, visible_planner, memory,
                max_steps=max_steps, checkpoint=progress,
                memory_context=(context if arm == "memory_on" else {
                    "previous_learned_experiences": [],
                    "learned_procedures": [],
                }),
                record_trial=False,
            )
            trial["trial"] = index
            trial["condition"] = arm
            trial["starting_position"] = report["active_trial"]["start"]
            trial["dimension"] = report["active_trial"]["dimension"]
            # A/B results stay in this audit report, and cannot unlock Stage 2.
            report["trials"].append(trial)
            report.pop("active_trial", None)
            checkpoint()
            print("[MEMORY TEST] %d/%d %-10s %-11s %d step(s)" %
                  (index, trials, arm, trial["status"], trial["attempts"]),
                  flush=True)
            physical_attempts = [
                step for step in trial["experiments"]
                if step.get("action") in {"move_to", "look_at", "mine", "collect"}
                and step.get("result") is not None
            ]
            print("[MEMORY TEST] Trial %d: %d actual body-action receipt(s)." %
                  (index, len(physical_attempts)), flush=True)
            no_physical_trials = (0 if physical_attempts
                                  else no_physical_trials + 1)
            if trial["status"] == "pending":
                report["status"] = "stopped_early_pending_job"
                break
            if no_physical_trials >= 2:
                report["status"] = "stopped_no_physical_actions"
                report["stop_reason"] = (
                    "Two consecutive trials performed no physical actions. "
                    "Inspect experiments and failure reasons; this is not "
                    "a valid memory comparison."
                )
                checkpoint()
                print("[MEMORY TEST] STOP: no physical actions in two trials. "
                      "See report experiments; no learning benefit can be inferred.",
                      flush=True)
                break
    except BaseException as error:
        report["status"] = "interrupted" if isinstance(
            error, (KeyboardInterrupt, SystemExit)) else "error"
        report["stop_reason"] = type(error).__name__
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint()
        raise

    if report["status"] == "running":
        report["status"] = "completed"
    report["ended_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Live physical Minecraft memory-on/off comparison")
    parser.add_argument("--trials", type=int, default=6,
                        help="Even number 2-20, balanced between both conditions")
    parser.add_argument("--steps", type=int, default=18)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    _arm_schedule(args.trials, args.seed)
    os.environ["COMPANION_MODEL_PROVIDER"] = "local"
    os.environ["COMPANION_CLOUD_TEACHER"] = "off"
    os.environ["COMPANION_TRAINING_MODE"] = "on"
    print("[MEMORY TEST] Real Minecraft only; preserves all existing memory.")
    print("[MEMORY TEST] Stop the regular brain and training before starting.",
          flush=True)
    print("[MEMORY TEST] Acquiring exclusive companion control...", flush=True)
    with exclusive_controller():
        print("[MEMORY TEST] Checking Minecraft connection and Chat body...",
              flush=True)
        world = RealWorld()
        world.snapshot()
        print("[MEMORY TEST] Minecraft connected. Checking local Ollama...",
              flush=True)
        planner = OllamaPlanner()
        print("[MEMORY TEST] Ollama ready (model: %s)." % planner.model,
              flush=True)
        result = run_comparison(
            world, planner, store, trials=args.trials, max_steps=args.steps,
            seed=args.seed, max_minutes=args.minutes)
    print("[MEMORY TEST] " + json.dumps(result["summary"], indent=2))
    print("[MEMORY TEST] Report: " + result["report_path"])
    if not result["experiences_available"]:
        print("[MEMORY TEST] No verified prior memories were available. "
              "Comparison not run.")


if __name__ == "__main__":
    main()
