"""Unit checks for live memory-ablation runner. World here is MOCKED.

Only an actual run with Ollama and Minecraft supplies gameplay evidence.
"""
import json
import tempfile
import unittest
from pathlib import Path

from memory_ablation import _arm_schedule, _summarize, run_comparison
from memory_store import MemoryStore
from stage1_exam import exam_memory_context
from test_stage1_exam import ExamPlanner, RealisticExamWorld
from test_training_progress import train_basic_skills


class ExposedPlanner(ExamPlanner):
    def __init__(self):
        self.exposures = []

    def propose(self, prompt):
        data = json.loads(prompt)
        self.exposures.append((
            len(data["previous_learned_experiences"]),
            len(data["learned_procedures"]),
        ))
        return super().propose(prompt)


class InterruptedPlanner(ExposedPlanner):
    def propose(self, prompt):
        raise KeyboardInterrupt("stop mid comparison")


class MemoryComparisonRegressions(unittest.TestCase):
    def test_randomized_balanced_conditions_and_invalid_schedule(self):
        for n in (2, 4, 6, 12):
            schedule = _arm_schedule(n, seed=18)
            self.assertEqual(schedule.count("memory_on"), n // 2)
            self.assertEqual(schedule.count("memory_off"), n // 2)
            for first in range(0, n, 2):
                self.assertEqual(set(schedule[first:first + 2]),
                                 {"memory_on", "memory_off"})
        for n in (0, 1, 3, 21):
            with self.assertRaises(ValueError):
                _arm_schedule(n, seed=1)

    def test_uses_same_model_and_frozen_experiences_but_no_graduation_credit(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MemoryStore(Path(folder) / "memory.sqlite3")
            train_basic_skills(db)
            self.assertTrue(exam_memory_context(db)["previous_learned_experiences"])
            planner = ExposedPlanner()
            report = run_comparison(
                RealisticExamWorld(), planner, db,
                trials=2, max_steps=8, seed=6,
                report_dir=Path(folder) / "reports")
            self.assertEqual(report["status"], "completed")
            self.assertTrue(report["prior_memory_frozen"])
            self.assertTrue(report["comparison_no_exam_graduation_credit"])
            self.assertEqual(len(report["trials"]), 2)
            self.assertEqual({x["condition"] for x in report["trials"]},
                             {"memory_off", "memory_on"})
            for x in report["trials"]:
                expected = x["condition"] == "memory_on"
                self.assertEqual(bool(x["memory_exposure"]["verified_experiences"]),
                                 expected)
                self.assertNotIn("unverified", x.get("status", ""))
            self.assertTrue(any(a[0] > 0 for a in planner.exposures))
            self.assertTrue(any(a[0] == 0 for a in planner.exposures))
            self.assertFalse(db.recent_skill_trials("training.stage1_exam"))
            self.assertTrue(db.recent_skill_trials("training.mine"))
            saved = json.loads(Path(report["report_path"]).read_text())
            self.assertEqual(saved["completed_trials"], 2)
            self.assertEqual(saved["status"], "completed")

    def test_empty_memory_stops_without_fabricated_progress_or_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MemoryStore(Path(folder) / "memory.sqlite3")
            planner = ExposedPlanner()
            world = RealisticExamWorld()
            report = run_comparison(
                world, planner, db, trials=2,
                report_dir=Path(folder) / "reports")
            self.assertEqual(report["status"], "not_testable_no_verified_memories")
            self.assertEqual(report["trials"], [])
            self.assertEqual(world.calls, [])
            self.assertEqual(planner.exposures, [])
            self.assertTrue(Path(report["report_path"]).exists())

    def test_interrupt_preserves_unverified_trial_report(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MemoryStore(Path(folder) / "memory.sqlite3")
            train_basic_skills(db)
            reports = Path(folder) / "reports"
            with self.assertRaises(KeyboardInterrupt):
                run_comparison(
                    RealisticExamWorld(), InterruptedPlanner(), db, trials=2,
                    report_dir=reports)
            files = list(reports.glob("memory-comparison-*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text())
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(saved["stop_reason"], "KeyboardInterrupt")
            self.assertEqual(saved["completed_trials"], 0)
            self.assertEqual(saved["active_trial"]["status"],
                             "incomplete_unverified")
            self.assertFalse(db.recent_skill_trials("training.stage1_exam"))

    def test_ablation_stops_after_two_scanning_only_trials(self):
        class ScansOnlyPlanner:
            def propose(self, prompt):
                return {"action": "scan_blocks", "exact": ["minecraft:oak_log"],
                        "radius": 7, "limit": 16}

        class EmptyScanWorld(RealisticExamWorld):
            def execute(self, plan):
                if plan["action"] == "scan_blocks":
                    self.calls.append(dict(plan))
                    return {"ok": True, "plan": plan,
                            "result": {"ok": True, "blocks": []}}
                return super().execute(plan)

        with tempfile.TemporaryDirectory() as folder:
            db = MemoryStore(Path(folder) / "memory.sqlite3")
            train_basic_skills(db)
            world = EmptyScanWorld()
            report = run_comparison(
                world, ScansOnlyPlanner(), db, trials=6, max_steps=1,
                report_dir=Path(folder) / "reports")
            self.assertEqual(report["status"], "stopped_no_physical_actions")
            self.assertEqual(len(report["trials"]), 2)
            self.assertTrue(all(x["attempts"] == 1 for x in report["trials"]))
            self.assertFalse(db.recent_skill_trials("training.stage1_exam"))

    def test_descriptive_statistics_do_not_claim_learning(self):
        summary = _summarize([
            {"condition": "memory_on", "status": "passed",
             "experiments": [{"verified": True, "result": {"state": "COMPLETED"}}],
             "seconds": 12},
            {"condition": "memory_off", "status": "failed",
             "experiments": [], "seconds": 30},
        ])
        self.assertEqual(summary["memory_minus_baseline_pass_rate"], 1.0)
        self.assertIn("does NOT establish", summary["interpretation"])


if __name__ == "__main__":
    unittest.main()
