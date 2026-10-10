"""Regressions for real user priority, Qwen schema and world-verified progress."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import minecraft_agent as agent
import practice_engine as practice
from memory_store import MemoryStore


class GoalControlRegressions(unittest.TestCase):
    def test_filters_alias_preserves_model_intent_and_never_guesses_target(self):
        proposal = {
            "action": "scan_blocks", "filters": ["minecraft:oak_log"],
            "goal_id": "mine:minecraft:oak_log",
        }
        raw = json.dumps(proposal)
        plan = practice.parse_plan(raw)
        self.assertEqual(plan["contains"], ["minecraft:oak_log"])
        self.assertEqual(plan["goal_id"], proposal["goal_id"])
        self.assertIsNone(agent._planner_shape_error(plan))
        self.assertNotIn("x", plan)
        nested = agent._normalize_planner_scan(dict(proposal))
        self.assertEqual(nested["contains"], ["minecraft:oak_log"])
        self.assertEqual(nested["action"], "scan_blocks")
        self.assertEqual(agent._planner_shape_error(nested), None)
        self.assertIsNotNone(agent._planner_shape_error(
            {"action": "scan_blocks", "filters": ["???"]}))

    def test_symbolic_goal_id_with_relevant_logs_is_normalized_not_rejected(self):
        goal = {"id": 31, "source": "user", "title": "Gather 8 logs"}
        raw = '{"action":"scan_blocks","filters":["minecraft:oak_log"],' \
              '"goal_id":"mine:minecraft:oak_log","intent":"scan for logs"}'
        parsed = practice.parse_plan(raw)
        bound = agent._bind_planner_goal(parsed, goal)
        self.assertEqual(bound["goal_id"], 31)
        self.assertTrue(agent._plan_matches_user_goal(bound, goal))
        self.assertIsNone(agent._planner_shape_error(bound))
        unrelated = practice.parse_plan(raw.replace("minecraft:oak_log",
                                                       "minecraft:grass_block"))
        denied = agent._bind_planner_goal(unrelated, goal)
        self.assertFalse(agent._plan_matches_user_goal(denied, goal))

    def test_direct_user_order_pauses_old_self_and_old_user_goals(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MemoryStore(Path(folder) / "memory.sqlite3")
            vague = db.create_goal("Improve practical capability", "vague", 4, "self")
            stale = db.create_goal("Chop a log", "stale order", 9, "user")
            runtime = {"awareness": {"inventory": [
                {"item": "minecraft:oak_log", "count": 61}]}}
            with patch.object(agent, "store", db):
                goal = asyncio.run(agent.fast_task_intent("gather some wood", runtime))
            self.assertEqual(goal["title"], "Gather 8 logs")
            self.assertEqual(db.goal_progress(goal["id"]), {
                "metric": "logs", "baseline": 61, "target_delta": 8})
            active = db.list_goals("active", 20)
            self.assertEqual([g["id"] for g in active if g["source"] in {"user","self"}],
                             [goal["id"]])
            paused_ids = {g["id"] for g in db.list_goals("paused", 20)}
            self.assertTrue({vague["id"], stale["id"]}.issubset(paused_ids))

    def test_inventory_at_goal_creation_does_not_complete_gathering(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "memory.sqlite3"
            db = MemoryStore(path)
            goal = db.create_goal("Gather 8 logs", "gather some wood", 8, "user")
            db.set_goal_progress(goal["id"], "logs", 61, 8)
            before = {"after": {"inventory": [
                {"item": "minecraft:oak_log", "count": 61}]}}
            with patch.object(agent, "store", db):
                self.assertEqual(agent._reconcile_user_goals(before), [])
                self.assertEqual(
                    [g["id"] for g in db.list_goals("active")
                     if g.get("source") == "user"], [goal["id"]])
                self.assertEqual(agent._reconcile_user_goals({
                    "after": {"inventory": [
                        {"item": "minecraft:oak_log", "count": 68}]} }), [])
            restarted = MemoryStore(path)
            with patch.object(agent, "store", restarted):
                self.assertEqual(agent._reconcile_user_goals({
                    "after": {"inventory": [
                        {"item": "minecraft:oak_log", "count": 69}]} }),
                    ["gathered 8 new logs, task done"])
                self.assertFalse([
                    g for g in restarted.list_goals("active")
                    if g.get("source") == "user"])

    def test_stationary_movement_is_not_a_verified_success(self):
        before = {"state": {"pos": [10, 64, 10]}}
        unchanged = {"state": {"pos": [10, 64, 10]}}
        moved = {"state": {"pos": [12, 64, 10]}}
        plan = {"action": "move_to", "x": 12, "y": 64, "z": 10}
        self.assertFalse(practice._objective_success(
            plan, before, unchanged, {"state": "COMPLETED", "reason": "arrived"}))
        self.assertTrue(practice._objective_success(
            plan, before, moved, {"state": "COMPLETED", "reason": "arrived"}))
        self.assertTrue(agent._target_failure_needs_replan({
            "ok": False, "plan": plan,
            "before": before, "after": unchanged,
            "result": {"state": "COMPLETED", "reason": "already_at_destination"},
        }))

    def test_more_logs_keeps_active_user_task_not_vague_goal(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MemoryStore(Path(folder) / "memory.sqlite3")
            db.create_goal("Improve practical capability", "old", 4, "self")
            first = db.create_goal("Gather 8 logs", "gather", 8, "user")
            db.set_goal_progress(first["id"], "logs", 20, 8)
            runtime = {"awareness": {"inventory": [
                {"item":"minecraft:oak_log", "count": 24}]}}
            with patch.object(agent, "store", db):
                again = asyncio.run(agent.fast_task_intent("get more wood", runtime))
            self.assertEqual(again["id"], first["id"])
            self.assertEqual(db.goal_progress(first["id"])["baseline"], 24)
            self.assertEqual([g["title"] for g in db.list_goals("active")
                              if g.get("source") in {"user","self"}],
                             ["Gather 8 logs"])


if __name__ == "__main__":
    unittest.main()
