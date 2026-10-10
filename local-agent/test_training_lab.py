"""Offline tests for training lab. No simulated success is persisted as real play."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import training_lab as lab
from memory_store import MemoryStore


class FakePhysicalWorld:
    """Deterministic stand-in for testing the *evaluator*, not a training run."""
    def __init__(self):
        self.pos = [10.5, 64.0, 10.5]
        self.yaw = 0.0
        self.items = []
        self.entities = []
        self.calls = []
        self.busy = False

    def snapshot(self, allow_busy=False):
        if self.busy and not allow_busy:
            raise RuntimeError("busy physical action")
        x, y, z = self.pos
        return {
            "state": {
                "x": x, "y": y, "z": z, "yaw": self.yaw,
                "jobActive": self.busy, "dimension": "minecraft:overworld",
                "serverState": "running",
            },
            "inventory": [dict(v) for v in self.items],
            "blocks": [
                {"type": "minecraft:dirt", "count": 30,
                 "nearestX": math.floor(x) + 2, "nearestY": int(y),
                 "nearestZ": math.floor(z), "nearestDistance": 2.4},
                {"type": "minecraft:stone", "count": 20,
                 "nearestX": math.floor(x) + 2, "nearestY": int(y),
                 "nearestZ": math.floor(z) + 1, "nearestDistance": 2.8},
            ],
            "entities": [dict(v) for v in self.entities],
        }

    def at(self, x, y, z):
        # Simulate flat ordinary terrain for evaluating safe candidate selection.
        return {"ok": True, "air": y >= 64, "solid_support_up": y == 63}

    def find(self, block_id, radius=7):
        return [{"type": block_id, "x": math.floor(self.pos[0]) + 2,
                 "y": 64, "z": math.floor(self.pos[2]),
                 "distance": 2.4}]

    def execute(self, plan):
        self.calls.append(dict(plan))
        action = plan["action"]
        if action == "scan_blocks":
            block_id = next(iter(plan.get("exact") or plan.get("contains") or []), "")
            return {"ok": True, "plan": plan,
                    "result": {"ok": True, "blocks": [
                        {"type": block_id, "pos": [
                            math.floor(self.pos[0]) + 2, 64, math.floor(self.pos[2])
                        ]}]}}
        if action == "look_at":
            dx = float(plan["x"]) + .5 - self.pos[0]
            dz = float(plan["z"]) + .5 - self.pos[2]
            self.yaw = math.degrees(math.atan2(-dx, dz))
            return {"ok": True, "plan": plan, "result": {"ok": True}}
        if action == "move_to":
            self.pos = [float(plan["x"]) + .5, float(plan["y"]),
                        float(plan["z"]) + .5]
            return {"ok": True, "plan": plan,
                    "result": {"state": "COMPLETED", "reason": "arrived"}}
        if action == "mine":
            self.entities = [{
                "droppedItem": "minecraft:dirt", "distance": 2.0,
                "x": self.pos[0] + 1, "y": self.pos[1], "z": self.pos[2],
            }]
            return {"ok": True, "plan": plan,
                    "result": {"state": "COMPLETED",
                               "reason": "block_mined|block=" + plan["expected_block"] +
                                         "|tool=minecraft:air|break_ticks=8"}}
        if action == "collect":
            self.items = [{"slot": 2, "item": "minecraft:dirt", "count": 1}]
            self.entities = []
            return {"ok": True, "plan": plan,
                    "result": {"state": "COMPLETED", "reason": "collection_complete:1"}}
        raise AssertionError("unexpected primitive: " + action)


class FakeLocalPlanner:
    """Uses the task description to propose actions; not hardcoded in host."""
    def propose(self, payload):
        p = json.loads(payload)
        target = p["target_confirmed_by_minecraft"]
        task = p["task"]
        base = {"intent": "practice " + task,
                "hypothesis": "use a world-observed primitive for " + task}
        if task == "observe":
            return {**base, "action": "scan_blocks", "exact": [target["type"]],
                    "radius": 7, "limit": 8}
        if task in {"orient", "navigate", "mine"}:
            a = {"orient": "look_at", "navigate": "move_to", "mine": "mine"}[task]
            plan = {**base, "action": a,
                    **dict(zip(("x", "y", "z"), target["pos"]))}
            if task == "mine":
                plan["expected_block"] = target["type"]
            return plan
        if task == "collect":
            return {**base, "action": "collect"}
        raise AssertionError(task)


class TrainingLabRegressions(unittest.TestCase):
    def test_successful_five_stage_training_is_verified_and_saved(self):
        world = FakePhysicalWorld()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "remember.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                report = lab.run_training(
                    world, FakeLocalPlanner(), rounds=1, max_steps=2,
                    report_dir=Path(tmp) / "reports")
                self.assertEqual(report["summary"]["passed"], 5)
                self.assertEqual(report["summary"]["physical_actions"], 4)
                self.assertEqual(report["summary"]["observation_actions"], 1)
                self.assertEqual(report["summary"]["executed_primitives"], 5)
                self.assertEqual(report["summary"]["teacher_calls"], 0)
                self.assertEqual(len(world.calls), 5)
                self.assertEqual(
                    [p["action"] for p in world.calls],
                    ["scan_blocks", "look_at", "move_to", "mine", "collect"])
                self.assertEqual(len(db.recent_skill_trials("training.mine", 6)), 1)
                self.assertEqual(len(db.recent_skill_trials("training.collect", 6)), 1)
                disk = json.loads(Path(report["report_path"]).read_text())
                self.assertEqual(disk["summary"]["passed"], 5)

    def test_inventory_is_required_for_verified_collection(self):
        task = lab.Challenge("collect", "collect", {
            "item": "minecraft:dirt", "pos": [11, 64, 10]
        }, ("collect",), "minecraft:overworld")
        before = {"state": {"dimension": "minecraft:overworld"},
                  "inventory": []}
        after = {"state": {"dimension": "minecraft:overworld"},
                 "inventory": []}
        success, reason = lab._verify(task, before, after, {
            "ok": True, "plan": {"action": "collect"},
            "result": {"state": "COMPLETED", "reason": "collection_complete:0"},
        })
        self.assertFalse(success)
        self.assertIn("after=0", reason)

    def test_wrong_mining_target_is_never_counted(self):
        task = lab.Challenge("mine", "mine", {
            "type": "minecraft:dirt", "pos": [10, 64, 12],
        }, ("mine",), "minecraft:overworld")
        sample = {"state": {"dimension": "minecraft:overworld"}, "inventory": []}
        passed, _ = lab._verify(task, sample, sample, {
            "ok": True, "plan": {"action": "mine", "x": 50, "y": 64, "z": 12},
            "result": {"state": "COMPLETED",
                       "reason": "block_mined|block=minecraft:dirt"},
        })
        self.assertFalse(passed)

    def test_unobserved_movement_and_mining_coordinates_rejected(self):
        challenge = lab.Challenge("mine", "mine", {
            "type": "minecraft:stone", "pos": [12, 64, 12]
        }, ("mine", "move_to"), "minecraft:overworld")
        invented = {"action": "mine", "x": 100, "y": 64, "z": 100,
                    "expected_block": "minecraft:stone"}
        self.assertEqual(lab._plan_allowed(invented, challenge)[1],
                         "unobserved_coordinates")
        genuine = dict(invented, x=12, z=12)
        self.assertEqual(lab._plan_allowed(genuine, challenge), (True, "ok"))
        self.assertEqual(lab._plan_allowed(
            dict(genuine, expected_block="minecraft:chest"), challenge
        )[1], "block_identity_mismatch")

    def test_world_observed_blocker_allows_one_grounded_experiment(self):
        challenge = lab.Challenge("mine", "mine", {
            "type": "minecraft:stone", "pos": [12, 64, 12]
        }, ("mine",), "minecraft:overworld")
        ray = {"result": {"state": "FAILED",
                          "reason": "mining_blocked|block=minecraft:dirt|at=11,64,12"}}
        blocker = lab._observed_obstruction(ray)
        self.assertEqual(blocker, {"type": "minecraft:dirt", "pos": [11, 64, 12]})
        plan = {"action": "mine", "expected_block": "minecraft:dirt",
                "x": 11, "y": 64, "z": 12}
        self.assertEqual(lab._plan_allowed(plan, challenge, [blocker]),
                         (True, "ok"))
        self.assertFalse(lab._plan_allowed(dict(plan, x=40), challenge, [blocker])[0])

    def test_valuable_block_is_not_a_training_mining_target(self):
        class ValuableOnly(FakePhysicalWorld):
            def snapshot(self, allow_busy=False):
                original = super().snapshot(allow_busy)
                original["blocks"] = [{
                    "type": "minecraft:diamond_ore", "nearestX": 12,
                    "nearestY": 64, "nearestZ": 12, "nearestDistance": 2,
                }]
                return original
        world = ValuableOnly()
        self.assertIsNone(lab._select_challenge(
            "mine", world, world.snapshot(), __import__("random").Random(1)))

    def test_pending_job_never_becomes_a_failure_or_a_second_action(self):
        class PendingWorld(FakePhysicalWorld):
            def execute(self, plan):
                if plan["action"] == "mine":
                    self.calls.append(dict(plan))
                    self.busy = True
                    return {"ok": None, "status": "pending", "plan": plan,
                            "result": {"state": "RUNNING",
                                       "reason": "approaching_block"}}
                return super().execute(plan)
        world = PendingWorld()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                report = lab.run_training(
                    world, FakeLocalPlanner(), rounds=2, max_steps=1,
                    report_dir=Path(tmp))
            self.assertEqual(report["summary"]["pending"], 1)
            self.assertEqual(len(db.recent_skill_trials("training.mine")), 0)
            self.assertFalse(any(p["action"] == "collect" for p in world.calls))
            self.assertTrue(Path(report["report_path"]).is_file())

    def test_rejection_never_executes_invented_game_action(self):
        class InventingPlanner(FakeLocalPlanner):
            def propose(self, payload):
                task = json.loads(payload)["task"]
                if task == "observe":
                    return {"action": "mine", "intent": "oops", "x": 999,
                            "y": 64, "z": 999, "expected_block": "minecraft:dirt"}
                return super().propose(payload)
        world = FakePhysicalWorld()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                report = lab.run_training(
                    world, InventingPlanner(), rounds=1, max_steps=1,
                    report_dir=Path(tmp))
            self.assertEqual(report["episodes"][0]["status"], "failed")
            self.assertEqual(report["episodes"][0]["reason"], "unsupported_action")
            self.assertFalse(any(p.get("x") == 999 for p in world.calls))


if __name__ == "__main__":
    unittest.main()
