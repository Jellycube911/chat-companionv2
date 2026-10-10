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
        available = ([block_id] if isinstance(block_id, str)
                     else list(block_id))
        found = "minecraft:dirt" if "minecraft:dirt" in available else available[0]
        return [{"type": found, "x": math.floor(self.pos[0]) + 2,
                 "y": 64, "z": math.floor(self.pos[2]),
                 "distance": 2.4}]

    def execute(self, plan):
        self.calls.append(dict(plan))
        action = plan["action"]
        if action == "scan_blocks":
            selection = plan.get("exact") or plan.get("contains") or []
            block_id = selection if isinstance(selection, str) else next(iter(selection), "")
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

    def test_actual_uploaded_report_action_shapes_are_canonicalized(self):
        """Two user reports: nested scan/look/move, target_pos and target."""
        import practice_engine as practice
        examples = [
            ({"action": "scan_blocks",
              "scan_blocks": {"contains": "minecraft:dandelion",
                              "radius": 10, "limit": 100, "exposed_only": True}},
             {"contains": "minecraft:dandelion", "radius": 10}),
            ({"action": "scan_blocks",
              "scan_blocks": {"contains": "minecraft:dirt",
                              "radius": 5, "limit": 50}},
             {"contains": "minecraft:dirt", "radius": 5}),
            ({"action": "look_at",
              "look_at": {"x": 58, "y": 83, "z": 174}},
             {"x": 58, "y": 83, "z": 174}),
            ({"action": "look_at", "target": [60, 87, 183]},
             {"x": 60, "y": 87, "z": 183}),
            ({"action": "move_to",
              "move_to": {"x": 61, "y": 87, "z": 180}},
             {"x": 61, "y": 87, "z": 180}),
            ({"action": "move_to", "target_pos": [54, 87, 173]},
             {"x": 54, "y": 87, "z": 173}),
        ]
        for original, expected in examples:
            with self.subTest(original=original):
                plan = practice.parse_plan(json.dumps(original))
                self.assertIsNotNone(plan)
                for key, value in expected.items():
                    self.assertEqual(plan.get(key), value)
                if plan["action"] == "scan_blocks":
                    self.assertEqual(lab._plan_allowed(
                        plan, lab.Challenge("observe", "observe",
                            {"type": "minecraft:dirt"},
                            ("scan_blocks",), "minecraft:overworld"))[0], True)
                else:
                    known = [expected[k] for k in ("x", "y", "z")]
                    self.assertEqual(lab._plan_allowed(
                        plan, lab.Challenge(
                            "orient" if plan["action"] == "look_at" else "navigate",
                            "test", {"pos": known},
                            (plan["action"],), "minecraft:overworld")), (True, "ok"))

    def test_old_schema_only_memories_are_ignored_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            db.record_skill_trial(
                "training.observe", "find dirt", json.dumps({
                    "experiments": [{"status": "rejected",
                                     "reason": "empty_scan_query"}]}),
                "old interface rejection", False)
            db.record_skill_trial(
                "training.observe", "scan stone", json.dumps({
                    "experiments": [{"status": "completed",
                                     "verified": True}]}),
                "real observed result", True)
            with patch.object(lab, "store", db):
                filtered = lab._verified_history("training.observe")
            self.assertEqual(len(filtered), 1)
            self.assertTrue(filtered[0]["success"])
            self.assertEqual(len(db.recent_skill_trials("training.observe")), 2)

    def test_live_training_runner_accepts_wrapped_qwen_actions(self):
        """Replay all five stages through the real normalizing parser."""
        import practice_engine as practice

        class WrappedPlanner(FakeLocalPlanner):
            def propose(self, payload):
                p = super().propose(payload)
                action = p["action"]
                if action == "scan_blocks":
                    return practice.parse_plan(json.dumps({
                        "action": action,
                        "scan_blocks": {"contains": p["exact"][0],
                                        "radius": 7, "limit": 50}
                    }))
                if action == "look_at":
                    return practice.parse_plan(json.dumps({
                        "action": action,
                        "target_pos": [p["x"], p["y"], p["z"]]
                    }))
                if action == "move_to":
                    return practice.parse_plan(json.dumps({
                        "action": action,
                        "move_to": {"x": p["x"], "y": p["y"], "z": p["z"]}
                    }))
                return p
        world = FakePhysicalWorld()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                result = lab.run_training(
                    world, WrappedPlanner(), rounds=1, max_steps=3,
                    report_dir=Path(tmp))
            self.assertEqual(result["summary"]["rejected_proposals"], 0)
            self.assertEqual(result["summary"]["passed"], 5)
            self.assertEqual(result["summary"]["executed_primitives"], 5)

    def test_no_progress_stop_prevents_permanent_json_rejection_loop(self):
        class InvalidPlanner:
            def propose(self, payload):
                return {"action": "look_at", "intent": "wrong"}
        world = FakePhysicalWorld()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                result = lab.run_training(
                    world, InvalidPlanner(), rounds=12, max_steps=1,
                    report_dir=Path(tmp))
            self.assertTrue(result["summary"]["stopped_due_to_no_actions"])
            self.assertEqual(result["summary"]["physical_actions"], 0)
            self.assertLessEqual(len(result["episodes"]), 15)
            self.assertFalse(db.recent_skill_trials("training.observe"))
            self.assertFalse(db.recent_skill_trials("training.orient"))
            self.assertFalse(db.recent_skill_trials("training.navigate"))

    def test_live_report_exact_textual_target_is_grounded_not_invented(self):
        """Navigation proposals in uploaded report provide ONLY hypothesis XYZ."""
        challenge = lab.Challenge(
            "navigate", "Walk to confirmed location",
            {"pos": [54, 87, 180]}, ("move_to",), "minecraft:overworld")
        proposal = {
            "action": "move_to", "intent": "navigate to target",
            "hypothesis": (
                "The target position [54, 87, 180] is reachable from "
                "the current position [58.06738186390909, 87.0, 177.98960466651576]."
            ),
        }
        bound = lab._ground_confirmed_coordinates(proposal, challenge)
        self.assertEqual([bound[k] for k in ("x", "y", "z")], [54, 87, 180])
        self.assertEqual(lab._plan_allowed(bound, challenge), (True, "ok"))
        self.assertNotIn("x", proposal)  # do not mutate original LLM output
        self.assertEqual(bound["_schema_binding"],
                         "exact_triple_confirmed_in_world_objective")

    def test_untrusted_textual_position_cannot_override_observed_target(self):
        challenge = lab.Challenge(
            "navigate", "Walk to confirmed location",
            {"pos": [54, 87, 180]}, ("move_to",), "minecraft:overworld")
        for text in ("walk to [999, 87, 999]",
                     "I think I should move closer",
                     "Target might be [54, 87, 181]"):
            with self.subTest(text=text):
                proposal = {"action": "move_to", "hypothesis": text}
                bound = lab._ground_confirmed_coordinates(proposal, challenge)
                self.assertNotIn("x", bound)
                self.assertEqual(lab._plan_allowed(bound, challenge)[1], "missing_xyz")

    def test_text_only_goal_coordinates_execute_once_when_server_confirms(self):
        class TextualTargetPlanner(FakeLocalPlanner):
            def propose(self, payload):
                plan = super().propose(payload)
                if plan["action"] == "move_to":
                    target = json.loads(payload)["target_confirmed_by_minecraft"]["pos"]
                    return {
                        "action": "move_to", "intent": "navigate",
                        "hypothesis": (
                            "The target position [%d, %d, %d] is reachable "
                            "from my current position." % tuple(target)
                        ),
                    }
                return plan
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            world = FakePhysicalWorld()
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                report = lab.run_training(
                    world, TextualTargetPlanner(), rounds=1, max_steps=1,
                    report_dir=Path(tmp))
            self.assertEqual(report["summary"]["passed"], 5)
            self.assertEqual(report["summary"]["rejected_proposals"], 0)
            self.assertIn("move_to", [p["action"] for p in world.calls])

    def test_mining_eligibility_uses_observed_exposed_block_beyond_five_blocks(self):
        class TreeSixBlocksAway(FakePhysicalWorld):
            def find(self, block_id, radius=7):
                if not isinstance(block_id, (list, tuple)):
                    return []
                self.calls.append({"probe": "/find-blocks", "radius": radius})
                return [{
                    "type": "minecraft:birch_log",
                    "x": 14, "y": 64, "z": 15, "distance": 6.4
                }]
        world = TreeSixBlocksAway()
        challenge = lab._select_challenge(
            "mine", world, world.snapshot(), __import__("random").Random(0))
        self.assertIsNotNone(challenge)
        self.assertEqual(challenge.target["type"], "minecraft:birch_log")
        self.assertEqual(challenge.target["pos"], [14, 64, 15])
        self.assertTrue(challenge.target["standable_candidates"])
        self.assertEqual(world.calls[0]["radius"], 10)

    def test_dense_ground_blocks_do_not_hide_a_confirmed_standable_tree(self):
        class DenseGroundWithTree(FakePhysicalWorld):
            def find(self, block_id, radius=7):
                ids = list(block_id)
                if "minecraft:birch_log" in ids:
                    return [{"type": "minecraft:birch_log",
                             "x": 16, "y": 64, "z": 10,
                             "distance": 5.6}]
                return [{
                    "type": "minecraft:grass_block",
                    "x": 11, "y": 63, "z": 11, "distance": 1.5,
                } for _ in range(64)]
        world = DenseGroundWithTree()
        found = lab._select_challenge(
            "mine", world, world.snapshot(),
            __import__("random").Random(5))
        self.assertIsNotNone(found)
        self.assertEqual(found.target["type"], "minecraft:birch_log")
        self.assertEqual(found.target["pos"], [16, 64, 10])

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
            def find(self, block_id, radius=7):
                return [{"type": "minecraft:diamond_ore", "x": 12,
                         "y": 64, "z": 12, "distance": 2}]
        world = ValuableOnly()
        self.assertIsNone(lab._select_challenge(
            "mine", world, world.snapshot(), __import__("random").Random(1)))

    def test_pending_navigation_completes_from_real_terminal_and_proceeds(self):
        """Only a terminal MOVE + observed new position earns navigation credit."""
        class DelayedMoveWorld(FakePhysicalWorld):
            def __init__(self):
                super().__init__()
                self.destination = None
                self.poll_count = 0

            def execute(self, plan):
                if plan["action"] == "move_to":
                    self.calls.append(dict(plan))
                    self.destination = [float(plan["x"]) + .5, float(plan["y"]),
                                        float(plan["z"]) + .5]
                    self.busy = True
                    return {"ok": None, "status": "pending", "plan": plan,
                            "result": {"state": "RUNNING", "reason": "repath_pending"}}
                return super().execute(plan)

            def job_state(self):
                self.poll_count += 1
                if self.poll_count >= 2:
                    self.busy = False
                    self.pos = list(self.destination)
                    return {"jobActive": False, "lastJob": {
                        "type": "MOVE", "state": "COMPLETED", "reason": "arrived"}}
                return {"jobActive": True, "jobType": "MOVE",
                        "jobState": "RUNNING", "jobReason": "repath_pending"}

        world = DelayedMoveWorld()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"), \
                 patch.object(lab.time, "sleep"):
                result = lab.run_training(world, FakeLocalPlanner(),
                    rounds=1, max_steps=1, report_dir=Path(tmp))
        self.assertEqual(result["summary"]["passed"], 5)
        self.assertEqual(result["summary"]["pending"], 0)
        self.assertEqual([e["status"] for e in result["episodes"]],
                         ["passed"] * 5)
        self.assertEqual(len([p for p in world.calls if p["action"] == "move_to"]), 1)

    def test_pending_navigation_server_failed_is_not_claimed_as_success(self):
        class FailedMove(FakePhysicalWorld):
            def execute(self, plan):
                if plan["action"] == "move_to":
                    self.calls.append(dict(plan))
                    return {"ok": None, "status": "pending", "plan": plan,
                            "result": {"state": "RUNNING", "reason": "repath_pending"}}
                return super().execute(plan)
            def job_state(self):
                return {"jobActive": False, "lastJob": {
                    "type": "MOVE", "state": "FAILED", "reason": "stuck"}}
        world = FailedMove()
        with tempfile.TemporaryDirectory() as tmp:
            db = MemoryStore(Path(tmp) / "memory.sqlite3")
            with patch.object(lab, "store", db), patch.object(lab, "log_event"):
                report = lab.run_training(world, FakeLocalPlanner(),
                    rounds=1, max_steps=1, report_dir=Path(tmp))
        self.assertEqual(report["episodes"][2]["status"], "failed")
        self.assertIn("stuck", report["episodes"][2]["reason"])
        self.assertEqual(report["summary"]["pending"], 0)
        self.assertTrue(any(p["action"] == "mine" for p in world.calls))
        self.assertTrue(any(p["action"] == "collect" for p in world.calls))

    def test_pending_job_timeout_cancels_only_matching_training_job(self):
        class StalledMove(FakePhysicalWorld):
            def __init__(self):
                super().__init__()
                self.busy = True
                self.stopped = False
            def job_state(self):
                if self.stopped:
                    return {"jobActive": False, "lastJob": {
                        "type": "MOVE", "state": "CANCELLED",
                        "reason": "owner_stop"}}
                return {"jobActive": True, "jobType": "MOVE",
                        "jobState": "RUNNING", "jobReason": "repath_pending"}
            def stop_job(self):
                self.stopped = True
                self.busy = False
                return {"ok": True}
        world = StalledMove()
        action = {"ok": None, "status": "pending",
                  "plan": {"action": "move_to"},
                  "result": {"state": "RUNNING", "reason": "repath_pending"}}
        with patch.object(lab, "log_event"):
            result = lab._settle_pending_job(
                world, action, timeout_seconds=0.01, poll_seconds=0.001)
        self.assertTrue(world.stopped)
        self.assertEqual(result["status"], "settled")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"]["state"], "CANCELLED")
        self.assertTrue(result["result"]["training_timeout"])

    def test_pending_terminal_wrong_job_type_never_claimed_as_ours(self):
        class ForeignJob(FakePhysicalWorld):
            def job_state(self):
                return {"jobActive": False, "lastJob": {
                    "type": "MINE", "state": "COMPLETED",
                    "reason": "block_mined|block=minecraft:dirt"}}
        action = {"ok": None, "status": "pending",
                  "plan": {"action": "move_to"},
                  "result": {"state": "RUNNING", "reason": "moving"}}
        with patch.object(lab, "log_event"):
            result = lab._settle_pending_job(ForeignJob(), action, 0.01)
        self.assertEqual(result["status"], "pending")

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
            with patch.object(lab, "store", db), patch.object(lab, "log_event"), \
                 patch.object(lab, "_settle_pending_job",
                              side_effect=lambda world, execution: execution):
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
