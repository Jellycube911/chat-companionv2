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

    def test_background_status_rate_limit_and_goal_priority(self):
        from unittest.mock import AsyncMock
        async def check():
            with patch.object(agent, "_send_ingame", new_callable=AsyncMock) as send, patch.object(
                agent.time, "monotonic", return_value=1000.0
            ), patch.object(agent, "log_event"):
                runtime = {}
                self.assertTrue(await agent._notify_once(runtime, "blocked:a", "blocked a"))
                self.assertFalse(await agent._notify_once(runtime, "blocked:b", "blocked b"))
                self.assertTrue(await agent._notify_once(runtime, "goal_complete:1", "task done"))
                self.assertEqual(send.await_count, 2)
        asyncio.run(check())

    def test_deferred_teacher_does_not_claim_new_consultation(self):
        from unittest.mock import AsyncMock
        async def check():
            execution = {
                "ok": True,
                "plan": {"intent": "mining", "action": "mine"},
                "learning": {"teacher_request": {"deferred": True, "ok": False}},
            }
            with patch.object(agent, "_reconcile_user_goals", return_value=[]), patch.object(
                agent, "_notify_once", new_callable=AsyncMock
            ) as notify:
                await agent._report_planned_outcome({}, execution, "practice")
                notify.assert_not_awaited()
        asyncio.run(check())

    def test_user_goal_rejects_unrelated_mining(self):
        goal = {"title": "Obtain an axe", "source": "user"}
        unrelated = {
            "action": "mine", "intent": "test direct mining action",
            "hypothesis": "the mining primitive breaks a log",
        }
        related = dict(unrelated, intent="gather material for axe")
        self.assertFalse(agent._plan_matches_user_goal(unrelated, goal))
        self.assertTrue(agent._plan_matches_user_goal(related, goal))

    def test_come_to_me_follows_and_pauses_practice(self):
        from unittest.mock import AsyncMock
        async def run():
            with patch.object(agent.store, "cancel_tasks"), patch.object(
                agent, "_post", return_value={"ok": True, "action": "follow"}
            ) as post, patch.object(agent.store, "record_event"):
                runtime = {}
                result = await agent.fast_chat_reflex("come to me", runtime)
                self.assertEqual(result["reply"], "coming")
                self.assertIn("manual_control_until", runtime)
                post.assert_called_once_with("/follow-owner")
        asyncio.run(run())

    def test_natural_user_instruction_is_actionable(self):
        text = "which spot? u need to place a crafting table and craft it"
        self.assertTrue(agent._looks_like_action_request(text))
        goal = {"title": "Obtain an axe"}
        plan = {"action": "place", "intent": "place crafting table", "hypothesis": "test placement"}
        self.assertTrue(agent._plan_matches_user_goal(plan, goal))
        self.assertTrue(agent._plan_matches_user_goal(plan, goal, text))

    def test_mining_occlusion_is_cached_but_not_falsely_successful(self):
        blocked = {"ok": False, "result": {
            "state": "FAILED",
            "reason": "mining_blocked|block=minecraft:vine|at=225,71,-81",
        }}
        self.assertTrue(agent._target_failure_needs_replan(blocked))
        self.assertTrue(agent._target_failure_needs_replan({
            "ok": False, "result": {"state": "FAILED", "reason": "mining_alignment_timeout"}
        }))
        self.assertFalse(agent._target_failure_needs_replan({
            "ok": True, "result": {
                "state": "COMPLETED",
                "reason": "block_mined|block=minecraft:jungle_log|tool=minecraft:wooden_axe|break_ticks=6",
            }
        }))

    def test_continuous_follow_does_not_starve_goal_practice(self):
        state = {"jobActive": True, "jobType": "FOLLOW", "jobReason": "holding"}
        with patch.object(agent.time, "monotonic", return_value=500.0):
            self.assertFalse(agent._autonomy_job_blocks_practice(state, {}, True))
            self.assertTrue(agent._autonomy_job_blocks_practice(state, {}, False))
            self.assertTrue(agent._autonomy_job_blocks_practice(
                state, {"manual_control_until": 550.0}, True))
            self.assertTrue(agent._autonomy_job_blocks_practice(
                dict(state, jobType="MINE"), {}, True))

    def test_short_come_command_is_physical(self):
        async def check():
            with patch.object(agent.store, "cancel_tasks"), patch.object(
                agent, "_post", return_value={"ok": True}
            ) as post, patch.object(agent.store, "record_event"):
                state = {}
                reply = await agent.fast_chat_reflex("come", state)
                self.assertEqual(reply["reply"], "coming")
                self.assertTrue(state["last_move_command"]["accepted"])
                post.assert_called_once_with("/follow-owner")
        asyncio.run(check())

    def test_current_activity_is_grounded_not_fake_progress(self):
        self.assertTrue(agent._is_activity_question("so what are u doing"))
        with patch.object(agent.store, "list_tasks", return_value=[]), patch.object(
            agent.store, "list_goals",
            return_value=[{"title": "Obtain an axe", "source": "user", "priority": 9}],
        ):
            reply = agent._current_activity_text({"awareness": {"state": {"jobActive": False}}})
            self.assertIn("planning", reply)
            self.assertNotIn("working on", reply)

    def test_unverified_chat_and_movement_corrections(self):
        self.assertTrue(agent._is_movement_feedback("u didnt freaking move"))
        self.assertEqual(
            agent._motion_evidence_reply({}),
            "you're right, no movement attempt recorded",
        )
        self.assertEqual(
            agent._ground_chat_reply("i need 14 jungle logs and 5 sticks to craft an axe", {}),
            "haven't checked the recipe yet",
        )
        self.assertEqual(
            agent._ground_chat_reply("i tried moving but got stuck", {}),
            "haven't confirmed any movement yet",
        )

    def test_multistep_goal_allows_linked_prerequisites(self):
        goal = {"id": 9, "title": "Obtain an axe", "source": "user"}
        step = {
            "action": "craft",
            "intent": "make wooden planks",
            "hypothesis": "turn available wood into a usable material",
            "goal_id": 9,
            "goal_reason": "preparing an intermediate material for the active user goal",
        }
        self.assertTrue(agent._plan_matches_user_goal(step, goal))
        self.assertFalse(agent._plan_matches_user_goal(dict(step, goal_id=42), goal))

    def test_repeated_navigation_budget_resets_when_inventory_changes(self):
        runtime = {}
        goal = {"id": 9, "title": "Obtain an axe"}
        inv = [{"item": "minecraft:stick", "count": 5}]
        plan = {"action": "move_to", "x": 216, "y": 70, "z": -105}
        agent._record_goal_navigation(runtime, goal, inv, plan)
        agent._record_goal_navigation(runtime, goal, inv, plan)
        self.assertEqual(agent._goal_navigation_count(runtime, goal, inv), 2)
        self.assertEqual(agent._goal_navigation_count(runtime, goal, inv), 2)
        self.assertEqual(agent._goal_navigation_count(
            runtime, goal, [{"item": "minecraft:stick", "count": 4}]
        ), 0)

    def test_inventory_crafting_is_valid_goal_prerequisite(self):
        goal = {"title": "Obtain an axe", "source": "user"}
        self.assertTrue(agent._plan_matches_user_goal(
            {"action": "craft", "intent": "try planks"}, goal
        ))
        self.assertFalse(agent._plan_matches_user_goal(
            {"action": "mine", "intent": "test random stone"}, goal
        ))

    def test_target_key_ignores_hypothesis_wording(self):
        a = {"action": "mine", "x": 205, "y": 71, "z": -120, "hypothesis": "A"}
        b = dict(a, hypothesis="B", tool="minecraft:string")
        self.assertEqual(agent._plan_target_key(a), agent._plan_target_key(b))


if __name__ == "__main__":
    unittest.main()
