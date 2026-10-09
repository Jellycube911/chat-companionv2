"""Fast host-side regression tests for real-session failures (2026-10-09)."""
import asyncio
import unittest
from unittest.mock import patch

import minecraft_agent as agent
import practice_engine as practice


class PracticeRegressions(unittest.TestCase):
    def test_near_standable_destination_is_allowed(self):
        before = {"state": {"pos": [0.0, 64.0, 0.0]}, "inventory": []}
        calls = []
        def post(path, payload=None):
            calls.append(path)
            if path == "/block-at":
                return {"ok": True, "air": True, "type": "minecraft:air"}
            return {"ok": True}
        with patch.object(practice, "_post", side_effect=post), patch.object(
            practice, "_wait_for_job", return_value={"state": "COMPLETED", "reason": "arrived"}
        ):
            result = practice._execute_action(
                {"action": "move_to", "x": 2, "y": 64, "z": 0}, before
            )
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(calls, ["/block-at", "/move-to"])

    def test_near_occupied_destination_is_rejected(self):
        before = {"state": {"pos": [0.0, 64.0, 0.0]}, "inventory": []}
        with patch.object(practice, "_post", return_value={
            "ok": True, "air": False, "type": "minecraft:oak_log"
        }) as post:
            result = practice._execute_action(
                {"action": "move_to", "x": 2, "y": 64, "z": 0}, before
            )
        self.assertEqual(result["error"], "destination_occupied")
        self.assertEqual(post.call_count, 1)

    def test_tool_selection_uses_experiment_not_block_recipe(self):
        inventory = [
            {"item": "minecraft:string", "slot": 0, "count": 2},
            {"item": "minecraft:wooden_pickaxe", "slot": 1, "count": 1},
            {"item": "minecraft:wooden_axe", "slot": 2, "count": 1},
        ]
        with patch.object(practice.store, "list_efficiency", return_value=[]):
            first = practice._choose_mining_tool("minecraft:jungle_log", inventory, "minecraft:string")
        self.assertEqual(first, "minecraft:wooden_pickaxe")
        history = [
            {"option": "minecraft:wooden_pickaxe", "attempts": 2, "successes": 2, "avg_reward": 1.2},
            {"option": "minecraft:wooden_axe", "attempts": 3, "successes": 3, "avg_reward": 4.1},
        ]
        with patch.object(practice.store, "list_efficiency", return_value=history):
            best = practice._choose_mining_tool("minecraft:jungle_log", inventory)
        self.assertEqual(best, "minecraft:wooden_axe")

    def test_no_false_skill_after_two_successes_amid_failures(self):
        plan = {"intent": "test direct mining action", "hypothesis": "try", "action": "mine"}
        successful = {
            "state": "COMPLETED",
            "reason": "block_mined|block=minecraft:jungle_log|tool=minecraft:string|break_ticks=301",
        }
        trials = [
            {"success": True, "actions": '{"action":"mine"}', "outcome": '{"result":' + __import__("json").dumps(successful) + "}", "hypothesis": "try"},
            {"success": True, "actions": '{"action":"mine"}', "outcome": '{"result":' + __import__("json").dumps(successful) + "}", "hypothesis": "try"},
        ] + [
            {"success": False, "actions": '{"action":"mine"}', "outcome": '{"result":{"error":"target_block_mismatch"}}', "hypothesis": "same"}
            for _ in range(6)
        ]
        with patch.object(practice.store, "record_skill_trial", return_value={"skill": None}), patch.object(
            practice.store, "recent_skill_trials", return_value=trials
        ), patch.object(practice.store, "save_learned_skill") as promote, patch.object(
            practice.store, "request_learning", return_value={"deferred": True}
        ):
            result = practice._record_learning(plan, successful, {}, {}, True)
        self.assertIsNone(result["promoted_skill"])
        promote.assert_not_called()

    def test_axe_request_creates_goal_and_discovers_completion(self):
        async def check():
            with patch.object(agent, "_ensure_user_goal", return_value={"id": 44, "title": "Obtain an axe"}) as ensure:
                goal = await agent.fast_task_intent("sorry make an axe")
                self.assertEqual(goal["id"], 44)
                ensure.assert_called_once()
        asyncio.run(check())

    def test_target_key_ignores_hypothesis_wording(self):
        a = {"action": "mine", "x": 205, "y": 71, "z": -120, "hypothesis": "A"}
        b = dict(a, hypothesis="B", tool="minecraft:string")
        self.assertEqual(agent._plan_target_key(a), agent._plan_target_key(b))


if __name__ == "__main__":
    unittest.main()
