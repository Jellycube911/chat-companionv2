"""Offline tests for the uncoached five-part Stage 1 practical exam.

All world actions here are mocked. Passing these tests does not claim a live
Minecraft exam passed. The real exam must run with Ollama and Minecraft.
"""
import json
import tempfile
import unittest
from pathlib import Path

import training_lab as lab
from stage1_exam import run_exam
from memory_store import MemoryStore
from test_training_lab import FakePhysicalWorld
from test_training_progress import train_basic_skills
from training_progress import assess_training


class RealisticExamWorld(FakePhysicalWorld):
    def execute(self, plan):
        if plan["action"] == "scan_blocks":
            self.calls.append(dict(plan))
            return {"ok": True, "plan": plan, "result": {
                "ok": True, "blocks": [{"type": "minecraft:oak_log",
                                        "pos": [16, 64, 10], "distance": 6.0},
                                       {"type": "minecraft:birch_log",
                                        "pos": [16, 64, 12], "distance": 6.3}]}}
        if plan["action"] == "mine":
            self.calls.append(dict(plan))
            self.entities = [{"droppedItem": "minecraft:oak_log",
                              "x": 16.5, "y": 64.5, "z": 10.5,
                              "distance": 1.5}]
            return {"ok": True, "plan": plan, "result": {
                "state": "COMPLETED",
                "reason": "block_mined|block=minecraft:oak_log|"
                          "tool=minecraft:air|break_ticks=61"}}
        if plan["action"] == "collect":
            self.calls.append(dict(plan))
            self.items.append({"item": "minecraft:oak_log", "count": 1})
            self.entities = []
            return {"ok": True, "plan": plan, "result": {
                "state": "COMPLETED", "reason": "collection_complete:1"}}
        return super().execute(plan)


class ExamPlanner:
    def propose(self, prompt):
        p = json.loads(prompt)
        resources = p["own_observed_resources"]
        if not resources:
            return {"action": "scan_blocks", "contains": "_log",
                    "radius": 10, "limit": 24,
                    "hypothesis": "Look for actual trees nearby"}
        target = resources[0]
        progress = p["confirmed_progress"]
        if not progress["approached"]:
            stand = target["standable_candidates"][0]
            return {"action": "move_to", "position": stand,
                    "hypothesis": "Physically walk to the trunk"}
        if not progress["faced"]:
            return {"action": "look_at", "pos": target["pos"],
                    "hypothesis": "Face the tree"}
        if not progress["mined"]:
            return {"action": "mine", "mine": {"x": target["pos"][0],
                       "y": target["pos"][1], "z": target["pos"][2]},
                    "expected_contains": "_log",
                    "hypothesis": "Mine confirmed trunk at the chosen coordinate"}
        return {"action": "collect", "hypothesis": "Pick up the physical drop"}


class StageOneExamRegressions(unittest.TestCase):
    def test_uncoached_exam_proves_all_five_actions_from_real_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            train_basic_skills(db)
            self.assertFalse(assess_training(db)["stage_2_unlocked"])
            world = RealisticExamWorld()
            exam = run_exam(world, ExamPlanner(), db, max_steps=8)
            self.assertEqual(exam["status"], "passed", exam)
            self.assertEqual([x["action"] for x in exam["experiments"]],
                             ["scan_blocks", "move_to", "look_at", "mine",
                              "collect"])
            self.assertTrue(exam["proof"]["ordered_actions"])
            self.assertEqual(exam["proof"]["inventory_after"], 1)
            self.assertEqual(db.recent_skill_trials(
                "training.stage1_exam", 1)[0]["evidence_status"], "verified")
            self.assertTrue(assess_training(db)["stage_2_unlocked"])
            self.assertEqual([v["action"] for v in world.calls],
                             ["scan_blocks", "move_to", "look_at", "mine",
                              "collect"])

    def test_exam_rejects_claimed_success_without_physical_mining(self):
        class FakeClaim:
            def propose(self, prompt):
                return {"action": "collect", "hypothesis": "I am finished"}
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            train_basic_skills(db)
            result = run_exam(RealisticExamWorld(), FakeClaim(), db, max_steps=4)
            self.assertNotEqual(result["status"], "passed")
            self.assertFalse(assess_training(db)["stage_2_unlocked"])

    def test_no_resources_does_not_forge_a_failed_skill_or_graduation(self):
        class EmptyWorld(RealisticExamWorld):
            def execute(self, plan):
                if plan["action"] == "scan_blocks":
                    self.calls.append(plan)
                    return {"ok": True, "plan": plan,
                            "result": {"ok": True, "blocks": []}}
                return super().execute(plan)
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            result = run_exam(EmptyWorld(), ExamPlanner(), db, max_steps=3)
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(db.recent_skill_trials("training.stage1_exam"))

    def test_empty_scan_can_trigger_real_exploration_without_exam_credit(self):
        class EmptyWorld(RealisticExamWorld):
            def execute(self, plan):
                if plan["action"] == "scan_blocks":
                    self.calls.append(dict(plan))
                    return {"ok": True, "plan": plan,
                            "result": {"ok": True, "blocks": []}}
                return super().execute(plan)

        class ExploringPlanner:
            def propose(self, prompt):
                p = json.loads(prompt)
                options = p["exploration_standable_positions"]
                if options:
                    return {"action": "move_to", "position": options[0],
                            "hypothesis": "Explore confirmed open ground"}
                return {"action": "scan_blocks", "exact": ["minecraft:oak_log"],
                        "radius": 10, "limit": 32}

        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            world = EmptyWorld()
            result = run_exam(world, ExploringPlanner(), db, max_steps=3)
            self.assertEqual([r["action"] for r in world.calls],
                             ["scan_blocks", "move_to", "scan_blocks"])
            self.assertEqual(result["experiments"][1]["reason"],
                             "explored_new_area")
            self.assertTrue(result["experiments"][1]["verified"])
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(db.recent_skill_trials("training.stage1_exam"))

    def test_interrupted_training_writes_readable_progress_before_end(self):
        class InterruptedWorld(FakePhysicalWorld):
            def execute(self, plan):
                if plan["action"] == "move_to":
                    raise KeyboardInterrupt("simulate sudden shutdown")
                return super().execute(plan)
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            from unittest.mock import patch
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                with self.assertRaises(KeyboardInterrupt):
                    lab.run_training(InterruptedWorld(), ExamPlannerForRegular(),
                                     rounds=12, max_steps=2,
                                     report_dir=Path(tmp) / "reports")
            reports = list((Path(tmp) / "reports").glob("training-*.json"))
            self.assertEqual(len(reports), 1)
            data = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "interrupted")
            self.assertEqual(data["stop_reason"], "KeyboardInterrupt")
            self.assertEqual(data["episodes"][0]["status"], "passed")
            self.assertEqual(data["checkpoint"]["completed_exercises"], 1)
            self.assertEqual(data["active_exercise"]["task"], "navigate")
            self.assertEqual(data["active_exercise"]["in_flight_action"]["status"],
                             "issued_unverified")
            self.assertEqual(len(db.recent_skill_trials("training.observe")), 1)
            self.assertFalse(db.recent_skill_trials("training.navigate"))


class ExamPlannerForRegular:
    def propose(self, prompt):
        p = json.loads(prompt)
        target = p["target_confirmed_by_minecraft"]
        a = {"observe": "scan_blocks", "navigate": "move_to",
             "orient": "look_at", "mine": "mine", "collect": "collect"}[p["task"]]
        if a == "scan_blocks":
            return {"action": a, "exact": [target["type"]], "radius": 7}
        proposal = {"action": a}
        if a in {"move_to", "look_at", "mine"}:
            proposal.update(dict(zip(("x", "y", "z"), target["pos"])))
        if a == "mine":
            proposal["expected_block"] = target["type"]
        return proposal


if __name__ == "__main__":
    unittest.main()
