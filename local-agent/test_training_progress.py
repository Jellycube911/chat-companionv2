"""Persistent, evidence-based curriculum and stage-2 safety regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import training_lab as lab
from training_progress import BASIC_SKILLS, assess_training
from memory_store import MemoryStore
from test_training_lab import FakePhysicalWorld, FakeLocalPlanner


def record_site_trial(db, skill, start, success=True):
    db.record_skill_trial(
        "training." + skill, "physically tested " + skill,
        json.dumps({
            "dimension": "minecraft:overworld", "start": list(start),
            "experiments": [{
                "status": "completed" if success else "failed",
                "plan": {"action": {"observe": "scan_blocks",
                                     "orient": "look_at", "navigate": "move_to",
                                     "mine": "mine", "collect": "collect"}[skill]},
                "result": ({"ok": True, "observed_count": 1} if skill == "observe"
                           else {"ok": True} if skill == "orient"
                           else {"state": "COMPLETED" if success else "FAILED",
                                 "reason": "arrived" if success else "stuck"}),
            }],
        }),
        json.dumps({"verified": success,
                    "outcome": "verified" if success else "physical_failure"}),
        success,
    )


def train_basic_skills(db, successes=10):
    for name in BASIC_SKILLS:
        for i in range(10):
            record_site_trial(
                db, name, (10 + 12 * (i % 3), 64, 11), i < successes)


class TrainingProgressRegressions(unittest.TestCase):
    def test_no_progress_or_unavailable_trial_can_graduate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            for name in BASIC_SKILLS:
                for i in range(15):
                    db.record_skill_trial(
                        "training." + name, "bad JSON", json.dumps({
                            "start": [i * 15, 64, 0],
                            "experiments": [{"status": "rejected",
                                             "reason": "missing_xyz"}]}),
                        json.dumps({"verified": True, "outcome": "imagined"}),
                        True)
            status = assess_training(db, [
                {"task": "mine", "status": "unavailable"},
                {"task": "navigate", "status": "pending"},
            ])
            self.assertEqual(status["graduated_count"], 0)
            self.assertEqual(status["skills"]["observe"]["resolved_trials"], 0)
            self.assertFalse(status["stage_2_unlocked"])
            self.assertEqual(status["skills"]["navigate"]["pending_this_session"], 1)

    def test_genuine_skill_window_graduates_and_failure_demotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            train_basic_skills(db, successes=9)
            status = assess_training(db)
            self.assertTrue(status["stage_2_unlocked"])
            self.assertEqual(status["graduated_count"], 5)
            self.assertEqual(status["skills"]["mine"]["successes"], 9)
            self.assertEqual(status["skills"]["mine"]["distinct_success_sites"], 3)
            record_site_trial(db, "navigate", (100, 64, 0), False)
            record_site_trial(db, "navigate", (120, 64, 0), False)
            degraded = assess_training(db)
            self.assertFalse(degraded["stage_2_unlocked"])
            self.assertEqual(degraded["stage"], 1)
            self.assertEqual(degraded["skills"]["navigate"]["successes"], 7)

    def test_identical_location_successes_do_not_unlock_next_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            for name in BASIC_SKILLS:
                for i in range(10):
                    record_site_trial(db, name, (10.1, 64.0, 11.2))
            status = assess_training(db)
            self.assertFalse(status["stage_2_unlocked"])
            self.assertEqual(status["skills"]["mine"]["distinct_success_sites"], 1)

    def test_pending_session_blocks_graduation_even_with_perfect_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            train_basic_skills(db)
            status = assess_training(db, [{"task": "navigate", "status": "pending"}])
            self.assertFalse(status["stage_2_unlocked"])
            self.assertEqual(status["skills"]["navigate"]["resolved_trials"], 10)

    def test_wood_compound_goal_requires_verified_mining_plus_item_pickup(self):
        target = {"type": "minecraft:oak_log", "pos": [12, 64, 10]}
        task = lab.Challenge("gather_wood", "Gather", target,
                             ("mine", "collect"), "minecraft:overworld")
        before = {"state": {"dimension": "minecraft:overworld"}, "inventory": []}
        after = {"state": {"dimension": "minecraft:overworld"},
                 "inventory": [{"item": "minecraft:oak_log", "count": 1}]}
        collect = {"ok": True, "plan": {"action": "collect"},
                   "result": {"state": "COMPLETED", "reason": "collection_complete:1"}}
        ok, _ = lab._verify(task, before, after, collect, (), [])
        self.assertFalse(ok)
        mined = {"status": "progress",
                 "plan": {"action": "mine", "x": 12, "y": 64, "z": 10},
                 "result": {"state": "COMPLETED",
                            "reason": "block_mined|block=minecraft:oak_log|tool=minecraft:air|break_ticks=8"}}
        ok, _ = lab._verify(task, before, after, collect, [mined], [])
        self.assertTrue(ok)
        altered = {"status": "progress", "plan": dict(mined["plan"], x=13),
                   "result": mined["result"]}
        self.assertFalse(lab._verify(task, before, after, collect, [altered], [])[0])

    def test_stage_two_runs_only_after_auto_graduation_and_saves_report(self):
        class WoodWorld(FakePhysicalWorld):
            def execute(self, plan):
                if plan["action"] == "mine":
                    self.calls.append(dict(plan))
                    self.entities = [{
                        "droppedItem": "minecraft:oak_log", "distance": 1.0,
                        "x": self.pos[0] + .5, "y": self.pos[1], "z": self.pos[2],
                    }]
                    return {"ok": True, "plan": plan,
                            "result": {"state": "COMPLETED",
                                       "reason": "block_mined|block=" + plan["expected_block"]
                                                 + "|tool=minecraft:air|break_ticks=8"}}
                if plan["action"] == "collect":
                    self.calls.append(dict(plan))
                    total = sum(item["count"] for item in self.items
                                if item["item"] == "minecraft:oak_log")
                    self.items = [{"slot": 2, "item": "minecraft:oak_log",
                                   "count": total + 1}]
                    self.entities = []
                    return {"ok": True, "plan": plan,
                            "result": {"state": "COMPLETED",
                                       "reason": "collection_complete:1"}}
                return super().execute(plan)

        class WoodPlanner(FakeLocalPlanner):
            def propose(self, payload):
                p = json.loads(payload)
                if p["task"] == "gather_wood":
                    target = p["target_confirmed_by_minecraft"]
                    if p["previous_attempts_this_trial"]:
                        return {"action": "collect", "intent": "gather one log",
                                "hypothesis": "Collect the real log I just mined"}
                    return {"action": "mine", "intent": "gather one log",
                            "hypothesis": "Mine the world-confirmed exposed trunk",
                            "x": target["pos"][0], "y": target["pos"][1],
                            "z": target["pos"][2], "expected_block": target["type"]}
                return super().propose(payload)

        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            train_basic_skills(db)
            world = WoodWorld()
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                report = lab.run_training(
                    world, WoodPlanner(), rounds=1, max_steps=4,
                    report_dir=Path(tmp) / "reports")
            self.assertTrue(report["graduation"]["stage_2_unlocked"])
            self.assertEqual(report["summary"]["stage_2_wood_gather_successes"], 1)
            self.assertEqual(report["episodes"][-1]["task"], "gather_wood")
            self.assertEqual(report["episodes"][-1]["status"], "passed")
            saved = json.loads(Path(report["report_path"]).read_text())
            self.assertTrue(saved["graduation"]["stage_2_unlocked"])
            self.assertEqual(len(world.calls), 7)


if __name__ == "__main__":
    unittest.main()
