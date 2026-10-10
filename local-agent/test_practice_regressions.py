"""Fast host-side regression tests for real-session failures (2026-10-09)."""
import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import minecraft_agent as agent
import practice_engine as practice


class PracticeRegressions(unittest.TestCase):
    def test_another_wooden_pickaxe_is_a_persistent_user_goal(self):
        with patch.object(agent, "_remember_behavior_feedback"), patch.object(
            agent, "_ensure_user_goal",
            return_value={"id": 31, "title": "Craft a wooden pickaxe"},
        ) as make_goal:
            goal = asyncio.run(agent.fast_task_intent(
                "make another wooden pickaxe now"
            ))
        self.assertEqual(goal["title"], "Craft a wooden pickaxe")
        make_goal.assert_called_once_with(
            "Craft a wooden pickaxe", "make another wooden pickaxe now", 9
        )

    def test_axe_chopping_correction_is_an_actual_user_goal(self):
        msg = "ok so u cant do that, atleast use that wooden axe to cut logs now"
        with patch.object(agent, "_remember_behavior_feedback"), patch.object(
            agent, "_ensure_user_goal",
            return_value={"id": 32, "title": "Chop a log"},
        ) as make_goal:
            goal = asyncio.run(agent.fast_task_intent(msg))
        self.assertEqual(goal["title"], "Chop a log")
        make_goal.assert_called_once_with("Chop a log", msg, 9)

    def test_requested_extra_pickaxe_needs_a_new_verified_craft(self):
        goal = {"id": 31, "source": "user", "title": "Craft a wooden pickaxe"}
        inventory = [{"item": "minecraft:wooden_pickaxe", "count": 1}]
        with patch.object(agent.store, "list_goals", return_value=[goal]), patch.object(
            agent.store, "update_goal"
        ) as update:
            # Carrying a pickaxe before the command must never finish it.
            self.assertEqual(agent._reconcile_user_goals({
                "after": {"inventory": inventory}
            }), [])
            update.assert_not_called()
            completed = agent._reconcile_user_goals({
                "ok": True, "plan": {"action": "craft"},
                "result": {"ok": True, "item": "minecraft:wooden_pickaxe"},
                "after": {"inventory": inventory},
            })
            self.assertTrue(completed)
            update.assert_called_once_with(31, status="completed")

    def test_scans_turn_into_new_axe_experiment_not_same_scan(self):
        now = agent.time.monotonic()
        runtime = {
            "awareness": {"state": {"x": 5.5, "z": 10.5}},
            "invalid_targets": {},
            "last_resource_scan": {
                "at": now, "origin": {"x": 5.5, "z": 10.5},
                "blocks": [
                    {"type": "minecraft:jungle_log",
                     "pos": [8, 70, 10], "distance": 3},
                    {"type": "minecraft:jungle_log",
                     "pos": [9, 70, 11], "distance": 4},
                ],
            },
        }
        goal = {"id": 32, "source": "user", "title": "Chop a log"}
        inventory = [{"item": "minecraft:wooden_axe", "count": 1}]
        selected = agent._material_action_from_scan(runtime, goal, inventory)
        self.assertEqual(selected["action"], "mine")
        self.assertEqual(selected["expected_block"], "minecraft:jungle_log")
        self.assertEqual(selected["tool"], "minecraft:wooden_axe")
        self.assertEqual(selected["goal_id"], 32)
        runtime["invalid_targets"][agent._plan_target_key(selected)] = (
            now, "mining_blocked|block=minecraft:vine"
        )
        alternate = agent._material_action_from_scan(runtime, goal, inventory)
        self.assertEqual(alternate["action"], "mine")
        self.assertNotEqual(
            [alternate["x"], alternate["y"], alternate["z"]],
            [selected["x"], selected["y"], selected["z"]],
        )

    def test_pickaxe_goal_uses_existing_planks_and_sticks_before_scanning(self):
        goal = {"id": 11, "title": "Craft a wooden pickaxe", "source": "user"}
        inventory = [
            {"item": "minecraft:jungle_planks", "count": 3},
            {"item": "minecraft:stick", "count": 3},
            {"item": "minecraft:jungle_log", "count": 18},
        ]
        plan = agent._wooden_pickaxe_goal_action({}, goal, inventory)
        self.assertEqual(plan["action"], "craft")
        self.assertEqual((plan["width"], plan["height"]), (3, 3))
        self.assertEqual(plan["grid"], [
            "minecraft:jungle_planks", "minecraft:jungle_planks",
            "minecraft:jungle_planks", "", "minecraft:stick", "",
            "", "minecraft:stick", "",
        ])
        self.assertEqual(plan["goal_id"], 11)
        self.assertIsNone(agent._wooden_pickaxe_goal_action(
            {}, {"id": 8, "title": "Improve practical capability", "source": "self"},
            inventory,
        ))

    def test_pickaxe_goal_makes_missing_intermediate_ingredients(self):
        goal = {"id": 11, "title": "Craft a wooden pickaxe", "source": "user"}
        no_sticks = [
            {"item": "minecraft:jungle_planks", "count": 3},
            {"item": "minecraft:stick", "count": 0},
            {"item": "minecraft:jungle_log", "count": 5},
        ]
        first = agent._wooden_pickaxe_goal_action({}, goal, no_sticks)
        self.assertEqual(first["grid"], ["minecraft:jungle_planks"] * 2)
        self.assertEqual((first["width"], first["height"]), (1, 2))
        no_planks = [{"item": "minecraft:jungle_log", "count": 5}]
        second = agent._wooden_pickaxe_goal_action({}, goal, no_planks)
        self.assertEqual((second["width"], second["height"]), (1, 1))
        self.assertEqual(second["grid"], ["minecraft:jungle_log"])

    def test_invalid_pickaxe_recipe_is_quarantined_after_real_failure(self):
        goal = {"id": 11, "title": "Craft a wooden pickaxe", "source": "user"}
        inventory = [
            {"item": "minecraft:jungle_planks", "count": 3},
            {"item": "minecraft:stick", "count": 3},
        ]
        plan = agent._wooden_pickaxe_goal_action({}, goal, inventory)
        invalid = {
            agent._plan_target_key(plan): (
                agent.time.monotonic(), "Minecraft server rejected recipe"
            ),
        }
        self.assertIsNone(agent._wooden_pickaxe_goal_action(
            {"invalid_targets": invalid}, goal, inventory,
        ))

    def test_cocoa_obstruction_is_observed_not_blindly_mined(self):
        runtime = {"pending_mining_obstruction": {
            "at": agent.time.monotonic(),
            "original": {
                "action": "mine", "expected_block": "minecraft:jungle_log",
                "x": 208, "y": 70, "z": -112,
            },
            "blocker": {"type": "minecraft:cocoa", "x": 209, "y": 71, "z": -112},
        }}
        plan = agent._mining_obstruction_recovery(runtime, None)
        self.assertEqual(plan["expected_block"], "minecraft:cocoa")
        self.assertEqual([plan["x"], plan["y"], plan["z"]], [209, 71, -112])
        runtime["pending_mining_obstruction"]["blocker"]["type"] = "minecraft:stone"
        self.assertIsNone(agent._mining_obstruction_recovery(runtime, None))

    def test_mining_obstruction_only_clears_observed_vines(self):
        now = agent.time.monotonic()
        original = {"action": "mine", "intent": "chop observed log",
                    "expected_block": "minecraft:jungle_log",
                    "x": 208, "y": 70, "z": -112}
        runtime = {"pending_mining_obstruction": {
            "at": now, "original": original,
            "blocker": {
                "type": "minecraft:vine", "x": 208, "y": 71, "z": -113
            },
        }}
        goal = {"source": "user", "id": 32, "title": "Chop a log"}
        plan = agent._mining_obstruction_recovery(runtime, goal)
        self.assertEqual(plan["action"], "mine")
        self.assertEqual(plan["expected_block"], "minecraft:vine")
        self.assertEqual([plan["x"], plan["y"], plan["z"]], [208, 71, -113])
        self.assertEqual(plan["goal_id"], 32)
        runtime["pending_mining_obstruction"]["blocker"]["type"] = "minecraft:stone"
        self.assertIsNone(agent._mining_obstruction_recovery(runtime, goal))

    def test_chopping_goal_requires_a_confirmed_mined_log(self):
        goal = {"id": 32, "source": "user", "title": "Chop a log"}
        with patch.object(agent.store, "list_goals", return_value=[goal]), patch.object(
            agent.store, "update_goal"
        ) as update:
            self.assertEqual(agent._reconcile_user_goals({
                "ok": False, "plan": {"action": "mine"},
                "result": {"state": "FAILED", "reason": "mining_blocked|block=minecraft:vine"},
            }), [])
            update.assert_not_called()
            msgs = agent._reconcile_user_goals({
                "ok": True, "plan": {"action": "mine"},
                "result": {"state": "COMPLETED", "reason":
                           "block_mined|block=minecraft:jungle_log|tool=minecraft:wooden_axe|break_ticks=14"},
            })
            self.assertEqual(msgs, ["chopped a log, task done"])
            update.assert_called_once_with(32, status="completed")

    def test_brain_console_reports_goal_action_and_verification(self):
        import contextlib
        import io
        stream = io.StringIO()
        runtime = {}
        with patch.object(agent, "log_event"), contextlib.redirect_stdout(stream):
            agent._brain_console_decision(runtime, {
                "action": "mine", "intent": "chop observed log",
                "hypothesis": "mine observed wood", "x": 8, "y": 70, "z": 10,
            }, {"title": "Chop a log"})
            agent._brain_console_result({"action": "mine"}, {
                "ok": True, "result": {"reason": "block_mined"},
            })
        output = stream.getvalue()
        self.assertIn("[BRAIN] GOAL: Chop a log", output)
        self.assertIn("[BRAIN] TEST: mine observed wood", output)
        self.assertIn("[BRAIN] SUCCESS: mine -> block_mined", output)

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

    def test_come_to_me_approaches_once_and_pauses_practice(self):
        async def run():
            with patch.object(agent.store, "cancel_tasks"), patch.object(
                agent, "_get", return_value={"owner": {"x": 5, "y": 64, "z": 8, "distance": 6}}
            ), patch.object(
                agent, "_post", return_value={"ok": True, "action": "move_to"}
            ) as post, patch.object(agent.store, "record_event"):
                runtime = {}
                result = await agent.fast_chat_reflex("come to me", runtime)
                self.assertEqual(result["reply"], "coming")
                self.assertIn("manual_control_until", runtime)
                post.assert_called_once_with("/move-to", {"x": 5, "y": 64, "z": 8, "stop_distance": 2.0})
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
                agent, "_get", return_value={"owner": {"x": 3, "y": 64, "z": 4, "distance": 5}}
            ), patch.object(
                agent, "_post", return_value={"ok": True}
            ) as post, patch.object(agent.store, "record_event"):
                state = {}
                reply = await agent.fast_chat_reflex("come", state)
                self.assertEqual(reply["reply"], "coming")
                self.assertTrue(state["last_move_command"]["accepted"])
                post.assert_called_once_with("/move-to", {"x": 3, "y": 64, "z": 4, "stop_distance": 2.0})
        asyncio.run(check())

    def test_current_activity_is_grounded_not_fake_progress(self):
        self.assertTrue(agent._is_activity_question("so what are u doing"))
        with patch.object(agent.store, "list_tasks", return_value=[]), patch.object(
            agent.store, "list_goals",
            return_value=[{"title": "Obtain an axe", "source": "user", "priority": 9}],
        ):
            reply = agent._current_activity_text({"awareness": {"state": {"jobActive": False}}})
            self.assertIn("working on wooden axe", reply)
            self.assertIn("not crafted yet", reply)

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

    def test_come_correction_routes_to_one_time_approach(self):
        async def check():
            with patch.object(agent.store, "cancel_tasks"), patch.object(
                agent, "_get", return_value={
                    "owner": {"x": 1, "y": 64, "z": 2, "distance": 4.0}
                }
            ), patch.object(agent, "_post", return_value={"ok": True}
            ) as post, patch.object(agent.store, "record_event"):
                reply = await agent.fast_chat_reflex("i didnt say follow i said come", {})
                self.assertEqual(reply["reply"], "coming")
                post.assert_called_once_with(
                    "/move-to", {"x": 1, "y": 64, "z": 2, "stop_distance": 2.0}
                )
        asyncio.run(check())

    def test_craft_rejects_two_ingredients_in_one_cell_without_http(self):
        before = {"inventory": [
            {"item": "minecraft:stick", "count": 5, "slot": 0},
            {"item": "minecraft:wooden_pickaxe", "count": 1, "slot": 1},
        ]}
        with patch.object(practice, "_post") as post:
            result = practice._execute_action({
                "action": "craft", "width": 1, "height": 1,
                "grid": ["minecraft:stick", "minecraft:wooden_pickaxe"],
            }, before)
        self.assertFalse(result["ok"])
        self.assertIn("exactly 1 entries, received 2", result["error"])
        post.assert_not_called()

    def test_craft_valid_3_by_3_reaches_game_bridge(self):
        before = {"inventory": [
            {"item": "minecraft:jungle_planks", "count": 6, "slot": 0},
            {"item": "minecraft:stick", "count": 5, "slot": 1},
        ]}
        planks = "minecraft:jungle_planks"
        grid = [planks, planks, "", planks, "minecraft:stick", "",
                "", "minecraft:stick", ""]
        plan = {"action": "craft", "width": 3, "height": 3,
                "grid": grid, "times": 1}
        with patch.object(practice, "_post", return_value={
            "ok": True, "item": "minecraft:wooden_axe", "count": 1,
        }) as post:
            result = practice._execute_action(plan, before)
        self.assertTrue(result["ok"])
        post.assert_called_once_with("/craft", {
            "width": 3, "height": 3, "grid": grid, "times": 1,
        })

    def test_craft_checks_real_ingredient_counts(self):
        before = {"inventory": [{"item": "minecraft:stick", "count": 1}]}
        with patch.object(practice, "_post") as post:
            result = practice._execute_action({
                "action": "craft", "width": 1, "height": 2,
                "grid": ["minecraft:stick", "minecraft:stick"],
            }, before)
        self.assertIn("craft_ingredients_missing", result["error"])
        post.assert_not_called()

    def test_craft_http_400_preserves_real_server_error(self):
        from unittest.mock import Mock
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.json.return_value = {
            "error": "The supplied grid does not match a crafting recipe."
        }
        with patch.object(practice.requests, "post", return_value=response):
            actual = practice._post("/craft", {"width": 3, "height": 3})
        self.assertFalse(actual["ok"])
        self.assertEqual(actual["status_code"], 400)
        self.assertIn("does not match a crafting recipe", actual["error"])

    def test_craft_rejected_grids_have_distinct_durable_session_keys(self):
        invalid = {"action": "craft", "width": 1, "height": 1,
                   "grid": ["minecraft:stick", "minecraft:wooden_pickaxe"]}
        corrected = {"action": "craft", "width": 3, "height": 3,
                     "grid": ["minecraft:jungle_planks"] * 3 + [""] * 6}
        old_key = agent._plan_target_key(invalid)
        self.assertTrue(old_key.startswith("craft@"))
        self.assertNotEqual(old_key, agent._plan_target_key(corrected))
        self.assertEqual(agent._invalid_target_ttl(old_key), 1800)
        self.assertTrue(agent._target_failure_needs_replan({
            "ok": False, "plan": invalid,
            "result": {"ok": False, "error": "craft_grid_size_mismatch"},
        }))
        self.assertFalse(agent._target_failure_needs_replan({
            "ok": True, "plan": corrected,
            "result": {"ok": True, "item": "minecraft:wooden_axe"},
        }))

    def test_crafting_teacher_only_after_three_observed_failures(self):
        runtime = {}
        plan = {"action": "craft", "width": 1, "height": 1,
                "grid": ["minecraft:stick", "minecraft:wooden_pickaxe"]}
        goal = {"id": 9, "title": "Obtain an axe"}
        failed = {"ok": False, "result": {
            "ok": False, "error": "craft_grid_size_mismatch: grid requires 1 entry"
        }}
        with patch.object(agent, "_cloud_teacher_enabled", return_value=True), patch.object(
            agent.store, "request_learning", return_value={"ok": True}
        ) as teacher, patch.object(agent, "log_event"):
            agent._handle_craft_learning(runtime, plan, failed, goal, [])
            agent._handle_craft_learning(runtime, plan, failed, goal, [])
            teacher.assert_not_called()
            agent._handle_craft_learning(runtime, plan, failed, goal, [])
            teacher.assert_called_once()
            self.assertEqual(runtime["failed_craft_attempts"], 0)
            self.assertEqual(teacher.call_args.kwargs["cooldown_seconds"], 1800)
            agent._handle_craft_learning(runtime, plan, {"ok": True}, goal, [])
            self.assertEqual(runtime["failed_craft_attempts"], 0)

    def test_nearby_place_probes_real_support_before_trying(self):
        tried = []
        def post(path, payload=None):
            tried.append((path, payload))
            if path == "/block-at":
                if payload["y"] == 65:
                    return {"ok": True, "air": True, "replaceable": True}
                return {"ok": True, "air": False, "solid_support_up": True}
            return {"ok": True}
        with patch.object(practice, "_get", return_value={
            "x": 10.5, "y": 65.0, "z": 20.5
        }), patch.object(practice, "_post", side_effect=post), patch.object(
            practice, "_wait_for_job", return_value={
                "state": "COMPLETED", "reason": "block_placed"
            }
        ):
            done = practice._place_nearby(7)
        self.assertEqual(done["state"], "COMPLETED")
        self.assertEqual(done["placed_at"], [11, 65, 20])
        placed = [payload for path, payload in tried if path == "/place-block"]
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["inventory_slot"], 7)

    def test_no_solid_support_never_attempts_placement(self):
        attempts = []
        def post(path, payload=None):
            attempts.append(path)
            if payload["y"] == 65:
                return {"ok": True, "air": True, "replaceable": True}
            return {"ok": True, "air": True, "solid_support_up": False}
        with patch.object(practice, "_get", return_value={
            "x": 10.5, "y": 65.0, "z": 20.5
        }), patch.object(practice, "_post", side_effect=post):
            result = practice._place_nearby(7)
        self.assertFalse(result["ok"])
        self.assertIn("no empty target with solid upper support", result["error"])
        self.assertNotIn("/place-block", attempts)

    def test_failed_place_preserves_actual_job_reason(self):
        def post(path, payload=None):
            if path == "/block-at":
                if payload["y"] == 65:
                    return {"ok": True, "air": True, "replaceable": True}
                return {"ok": True, "air": False, "solid_support_up": True}
            return {"ok": True}
        with patch.object(practice, "_get", return_value={
            "x": 10.5, "y": 65.0, "z": 20.5
        }), patch.object(practice, "_post", side_effect=post), patch.object(
            practice, "_wait_for_job", return_value={
                "state": "FAILED", "reason": "placement_alignment_timeout"
            }
        ):
            result = practice._place_nearby(7)
        self.assertFalse(result["ok"])
        self.assertIn("placement_alignment_timeout", result["error"])
        self.assertTrue(result["failures"])

    def test_crafting_table_placement_unlocks_previously_rejected_recipe(self):
        axe_plan = {
            "action": "craft", "width": 3, "height": 3,
            "grid": ["minecraft:jungle_planks"] * 3 + [""] * 6,
        }
        recipe_key = agent._plan_target_key(axe_plan)
        malformed = agent._plan_target_key({
            "action": "craft", "width": 1, "height": 1,
            "grid": ["minecraft:stick", "minecraft:stick"],
        })
        runtime = {"invalid_targets": {
            recipe_key: (100.0, "A 3x3 recipe requires a crafting table within reach."),
            malformed: (100.0, "craft_grid_size_mismatch: requires 1 item"),
        }}
        place = {"action": "place", "item": "minecraft:crafting_table"}
        near_key = agent._plan_target_key(place)
        runtime["invalid_targets"][near_key] = (100.0, "no_valid_placement")
        cleared = agent._clear_resolved_placement_blockers(
            runtime, place, {"ok": True}
        )
        self.assertIn(recipe_key, cleared)
        self.assertNotIn(recipe_key, runtime["invalid_targets"])
        self.assertNotIn(near_key, runtime["invalid_targets"])
        self.assertIn(malformed, runtime["invalid_targets"])
        self.assertEqual(agent._invalid_target_ttl(near_key), 90.0)
        self.assertTrue(agent._target_failure_needs_replan({
            "ok": False, "plan": place,
            "result": {"ok": False, "error": "no_valid_placement"},
        }))

    def test_blocked_axe_plan_recovers_from_persistent_trial_after_restart(self):
        # Mirrors the real sequence: axe request rejected for missing table,
        # followed by a successful placement and a fresh planning tick.
        grid = ["minecraft:jungle_planks", "minecraft:jungle_planks", "",
                "minecraft:jungle_planks", "minecraft:stick", "",
                "", "minecraft:stick", ""]
        saved = {"action": "craft", "intent": "craft wooden axe",
                 "hypothesis": "try axe", "width": 3, "height": 3, "grid": grid,
                 "goal_id": 9, "goal_reason": "this completes the requested axe"}
        trial = {
            "actions": __import__("json").dumps(saved),
            "outcome": __import__("json").dumps({
                "result": {"ok": False,
                           "error": "A 3x3 recipe requires a crafting table within reach."}
            }),
            "success": False,
        }
        inventory = [
            {"item": "minecraft:jungle_planks", "count": 6},
            {"item": "minecraft:stick", "count": 5},
        ]
        goal = {"source": "user", "id": 9, "title": "Obtain an axe"}
        runtime = {}
        with patch.object(agent.store, "recent_trials", return_value=[trial]), patch.object(
            agent, "log_event"
        ), patch.object(agent, "_post", return_value={
            "blocks": [{"type": "minecraft:crafting_table", "distance": 2.2,
                        "x": 211, "y": 70, "z": -108}]
        }) as post:
            result = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertEqual(result, saved)
        self.assertEqual(runtime["pending_craft"]["goal_id"], 9)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "/find-blocks")

    def test_chat_and_body_share_the_same_verified_action_record(self):
        import body_truth
        runtime = {"awareness": {"inventory": [
            {"item": "minecraft:stick", "count": 2},
        ]}}
        scan = {
            "action": "scan_blocks", "intent": "locate jungle logs"
        }
        body_truth.record_action(runtime, scan, {
            "ok": True, "result": {"ok": True, "blocks": [
                {"type": "minecraft:jungle_log", "pos": [12, 70, 13]}
            ]},
        })
        self.assertIsNone(runtime.get("body_last_verified"))
        self.assertIn("scanned blocks", body_truth.summarize_action(
            runtime["body_last_action"]
        ))
        self.assertIn("haven't", body_truth.grounded_chat(
            "i got the wooden hoe", "did you craft it?", runtime,
            [{"id": 55, "source": "user", "title": "Craft minecraft:wooden_hoe"}]
        ))

        plan = {"action": "craft", "intent": "craft wooden pickaxe", "goal_id": 9}
        body_truth.record_action(runtime, plan, {
            "ok": True, "result": {"item": "minecraft:wooden_pickaxe", "count": 1},
        })
        self.assertNotEqual(
            body_truth.grounded_chat(
                "i got the hoe", "is it done?", runtime,
                [{"id": 55, "source": "user", "title": "Craft minecraft:wooden_hoe"}],
            ),
            "i got the hoe",
        )

    def test_implicit_craft_claim_is_rejected_without_world_evidence(self):
        import body_truth
        goal = {"id": 31, "source": "user", "title": "Craft minecraft:wooden_hoe"}
        runtime = {"body_last_action": {
            "action": "scan_blocks", "ok": True,
            "result": {"blocks": [{"type": "minecraft:jungle_log"}]},
        }}
        for answer in ("already got the hoe", "it's ready", "i got the hoe"):
            result = body_truth.grounded_chat(answer, "make a wooden hoe", runtime, [goal])
            self.assertNotEqual(result, answer)

    def test_chat_accepts_only_verified_matching_goal_craft(self):
        import body_truth
        goal = {"id": 55, "source": "user", "title": "Craft minecraft:wooden_hoe"}
        runtime = {"awareness": {"inventory": [
            {"item": "minecraft:wooden_hoe", "count": 1}
        ]}}
        self.assertFalse(body_truth.verified_craft_for_goal(runtime, goal))
        # An existing item in inventory is not evidence of a NEW craft.
        self.assertNotEqual(body_truth.grounded_chat(
            "i crafted a wooden hoe", "is it ready?", runtime, [goal]
        ), "i crafted a wooden hoe")
        body_truth.record_action(runtime, {
            "action": "craft", "intent": "craft wooden hoe", "goal_id": 55
        }, {"ok": True, "result": {
            "item": "minecraft:wooden_hoe", "count": 1,
        }})
        self.assertTrue(body_truth.verified_craft_for_goal(runtime, goal))
        self.assertEqual(body_truth.grounded_chat(
            "i crafted a wooden hoe", "is it ready?", runtime, [goal]
        ), "i crafted a wooden hoe")

    def test_user_feedback_about_scan_loop_stays_factual(self):
        import body_truth
        runtime = {"body_recent_actions": [
            {"action": "scan_blocks"} for _ in range(6)
        ]}
        response = body_truth.grounded_chat(
            "i got the hoe, but the log is gone.",
            "ur task is putting u in a loop", runtime, [],
        )
        self.assertEqual(response, "yea, stuck rescanning. no progress yet")

    def test_recognize_question_about_current_task(self):
        self.assertTrue(agent._is_activity_question("what is ur task"))
        self.assertTrue(agent._is_activity_question("what's your task"))

    def test_another_axe_is_a_new_crafting_goal_not_old_inventory(self):
        with patch.object(agent, "_remember_behavior_feedback"), patch.object(
            agent, "_ensure_user_goal",
            return_value={"id": 57, "title": "Craft minecraft:wooden_axe"},
        ) as make_goal:
            goal = asyncio.run(agent.fast_task_intent("now make another axe"))
        self.assertEqual(goal["title"], "Craft minecraft:wooden_axe")
        make_goal.assert_called_once_with(
            "Craft minecraft:wooden_axe", "now make another axe", 9
        )

    def test_wooden_hoe_command_registers_a_generic_crafting_goal(self):
        with patch.object(agent, "_remember_behavior_feedback"), patch.object(
            agent, "_ensure_user_goal",
            return_value={"id": 55, "title": "Craft minecraft:wooden_hoe"},
        ) as make_goal:
            goal = asyncio.run(agent.fast_task_intent("Make a wooden hoe"))
        self.assertEqual(goal["title"], "Craft minecraft:wooden_hoe")
        make_goal.assert_called_once_with(
            "Craft minecraft:wooden_hoe", "Make a wooden hoe", 9
        )

    def test_chat_context_includes_authoritative_physical_status(self):
        import body_truth
        runtime = {
            "awareness": {
                "state": {"x": 10, "y": 70, "z": 15},
                "inventory": [
                    {"item": "minecraft:jungle_planks", "count": 6}
                ],
            },
            "body_last_action": {
                "action": "mine", "ok": False,
                "result": {"reason": "mining_blocked|block=minecraft:oak_leaves"}
            },
        }
        with patch.object(agent.store, "list_goals", return_value=[
            {"id": 55, "source": "user", "title": "Craft minecraft:wooden_hoe"}
        ]), patch.object(agent.store, "recent_events", return_value=[]):
            context = agent._chat_awareness_text(runtime)
        self.assertIn("active player task=", context)
        self.assertIn("mining_blocked", context)
        self.assertIn("minecraft:jungle_planksx6", context)
        self.assertIn("not crafted yet", context)

    def test_generic_crafting_goal_without_handwritten_item_rule(self):
        with patch.object(agent, "_remember_behavior_feedback"), patch.object(
            agent, "_ensure_user_goal",
            return_value={"id": 51, "title": "Craft minecraft:furnace"},
        ) as goal:
            request = asyncio.run(agent.fast_task_intent("craft a furnace"))
        self.assertEqual(request["title"], "Craft minecraft:furnace")
        goal.assert_called_once_with("Craft minecraft:furnace", "craft a furnace", 9)

    def test_recipe_knowledge_handles_inventory_alternatives_and_modded_items(self):
        import knowledge_engine as knowledge
        inventory = [
            {"item": "minecraft:jungle_planks", "count": 2},
            {"item": "minecraft:jungle_planks", "count": 1},
            {"item": "minecraft:stick", "count": 2},
        ]
        recipes = [{
            "recipe_id": "examplemod:axe_like_item",
            "output": "examplemod:axe_like_item",
            "count": 1, "width": 3, "height": 3,
            "ingredients": [
                ["minecraft:oak_planks", "minecraft:jungle_planks"],
                ["minecraft:oak_planks", "minecraft:jungle_planks"],
                [], ["minecraft:oak_planks", "minecraft:jungle_planks"],
                ["minecraft:stick"], [], [], ["minecraft:stick"], [],
            ],
        }]
        goal = {"id": 51, "title": "Craft examplemod:axe_like_item"}
        plan, reason, trace = knowledge.decide_recipe(
            goal, inventory, lambda _: {"ok": True, "recipes": recipes},
        )
        self.assertEqual(reason, "ready")
        self.assertEqual(plan["grid"][0], "minecraft:jungle_planks")
        self.assertEqual(plan["expected_output"], "examplemod:axe_like_item")
        self.assertEqual(plan["recipe_id"], "examplemod:axe_like_item")
        self.assertEqual(trace[0]["missing"], {})

    def test_general_recipe_dependencies_make_intermediate_items_first(self):
        import knowledge_engine as knowledge
        items = {
            "minecraft:torch": [{
                "recipe_id": "minecraft:torch", "output": "minecraft:torch",
                "width": 1, "height": 2,
                "ingredients": [["minecraft:coal"], ["minecraft:stick"]],
            }],
            "minecraft:stick": [{
                "recipe_id": "minecraft:stick", "output": "minecraft:stick",
                "width": 1, "height": 2,
                "ingredients": [["minecraft:jungle_planks"], ["minecraft:jungle_planks"]],
            }],
        }
        inventory = [
            {"item": "minecraft:coal", "count": 2},
            {"item": "minecraft:jungle_planks", "count": 3},
        ]
        plan, reason, chain = knowledge.decide_recipe(
            {"id": 72, "title": "Craft minecraft:torch"}, inventory,
            lambda output: {"ok": True, "recipes": items.get(output, [])},
        )
        self.assertEqual(plan["expected_output"], "minecraft:stick")
        self.assertEqual(plan["grid"], ["minecraft:jungle_planks"] * 2)
        self.assertTrue(reason.startswith("intermediate:"))

    def test_recipe_bridge_failure_does_not_invent_a_knowledge_result(self):
        import knowledge_engine as knowledge
        action, reason, chain = knowledge.decide_recipe(
            {"id": 51, "title": "Craft minecraft:furnace"}, [],
            lambda _: {"ok": False, "error": "404"},
        )
        self.assertIsNone(action)
        self.assertEqual(reason, "recipe_service_unavailable")
        self.assertFalse(chain)

    def test_generic_craft_goal_requires_authoritative_result(self):
        goal = {"id": 71, "title": "Craft minecraft:furnace", "source": "user"}
        with patch.object(agent.store, "list_goals", return_value=[goal]), patch.object(
            agent.store, "update_goal"
        ) as update:
            self.assertEqual(agent._reconcile_user_goals({
                "after": {"inventory": [{"item": "minecraft:furnace", "count": 1}]}
            }), [])
            update.assert_not_called()
            result = agent._reconcile_user_goals({
                "ok": True, "plan": {"action": "craft"},
                "result": {"ok": True, "item": "minecraft:furnace"},
            })
            self.assertTrue(result)
            update.assert_called_once_with(71, status="completed")

    def test_static_http_routes_exist_in_neoforge_bridge(self):
        # A previous regression invented /scan-blocks, and the test suite
        # accidentally reinforced the typo. Check against real Java routes.
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        java = (root / "neoforge" / "src" / "main" / "java" /
                "dev" / "chatcompanion" / "neoforge" / "client" /
                "LocalAgentBridge.java").read_text(encoding="utf-8")
        supported = set(re.findall(
            r'createContext\("(/[a-z-]+)"', java
        ))
        self.assertIn("/find-blocks", supported)
        for name in ("minecraft_agent.py", "practice_engine.py", "mcp_server.py"):
            source = (root / "local-agent" / name).read_text(encoding="utf-8")
            calls = set(re.findall(
                r'''_(?:post|get)\(\s*["'](/[a-z-]+)["']''', source
            ))
            self.assertFalse(calls - supported, (
                f"{name} references nonexistent bridge routes: "
                f"{sorted(calls - supported)}"
            ))

    def test_workstation_probe_uses_real_bridge_route_and_checks_result(self):
        with patch.object(agent, "_post", return_value={
            "ok": True, "blocks": [
                {"type": "minecraft:crafting_table", "distance": 2.5},
                {"type": "minecraft:jungle_log", "distance": 2.0},
            ],
        }) as post:
            blocks = agent._workstation_probe()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "minecraft:crafting_table")
        post.assert_called_once_with("/find-blocks", {
            "exact": ["minecraft:crafting_table"], "radius": 8, "limit": 8,
        })

    def test_failed_workstation_probe_is_not_treated_as_missing_table(self):
        goal = {"source": "user", "id": 9, "title": "Obtain an axe"}
        recipe = {
            "action": "craft", "intent": "craft wooden axe", "width": 3,
            "height": 3,
            "grid": ["minecraft:jungle_planks"] * 3 +
                    ["minecraft:stick"] * 2 + [""] * 4,
        }
        inventory = [
            {"item": "minecraft:crafting_table", "count": 1},
            {"item": "minecraft:jungle_planks", "count": 6},
            {"item": "minecraft:stick", "count": 5},
        ]
        runtime = {"pending_craft": {"goal_id": 9, "plan": recipe}}
        with patch.object(agent, "_post", return_value={
            "ok": False, "error": "server unavailable", "status_code": 500
        }) as post, patch.object(agent, "log_event"):
            result = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertIsNone(result)
        self.assertGreater(runtime["planner_backoff_until"], 0)
        post.assert_called_once()

    def test_http_404_workstation_probe_recovers_without_main_loop_crash(self):
        import requests
        goal = {"source": "user", "id": 9, "title": "Obtain an axe"}
        grid = ["minecraft:jungle_planks"] * 3 + [
            "minecraft:stick"] * 2 + [""] * 4
        runtime = {"pending_craft": {"goal_id": 9, "plan": {
            "action": "craft", "intent": "craft wooden axe",
            "width": 3, "height": 3, "grid": grid,
        }}}
        inventory = [
            {"item": "minecraft:crafting_table", "count": 2},
            {"item": "minecraft:jungle_planks", "count": 6},
            {"item": "minecraft:stick", "count": 5},
        ]
        with patch.object(agent, "_post", side_effect=requests.HTTPError(
            "404 Client Error: Not Found for url: http://127.0.0.1:8765/find-blocks"
        )) as post, patch.object(agent, "log_event"):
            plan = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertIsNone(plan)
        self.assertGreater(runtime["planner_backoff_until"], 0)
        post.assert_called_once()

    def test_missing_workstation_places_one_table_then_retries_recipe(self):
        craft = {"action": "craft", "intent": "craft wooden axe",
                 "width": 3, "height": 3,
                 "grid": ["minecraft:jungle_planks"] * 3 +
                         ["minecraft:stick"] * 2 + [""] * 4}
        goal = {"source": "user", "id": 9, "title": "Obtain an axe"}
        inventory = [
            {"item": "minecraft:crafting_table", "count": 2},
            {"item": "minecraft:jungle_planks", "count": 6},
            {"item": "minecraft:stick", "count": 5},
        ]
        runtime = {"pending_craft": {"goal_id": 9, "plan": craft}}
        scan = {"blocks": []}
        with patch.object(agent, "_post", return_value=scan) as post:
            action = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertEqual(action["action"], "place")
        self.assertEqual(action["item"], "minecraft:crafting_table")
        self.assertEqual(post.call_count, 1)

        # The next planning tick sees the actual placed table and reuses
        # the identical recipe, without placing another one.
        with patch.object(agent, "_post", return_value={
            "blocks": [{"type": "minecraft:crafting_table", "distance": 2.1}]
        }):
            action2 = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertEqual(action2, craft)

    def test_placed_but_distant_workstation_does_not_waste_another_table(self):
        goal = {"source": "user", "id": 9, "title": "Obtain an axe"}
        craft = {"action": "craft", "intent": "craft wooden axe",
                 "width": 3, "height": 3,
                 "grid": ["minecraft:jungle_planks"] * 3 +
                         ["minecraft:stick"] * 2 + [""] * 4}
        runtime = {"pending_craft": {"goal_id": 9, "plan": craft}}
        inventory = [
            {"item": "minecraft:crafting_table", "count": 2},
            {"item": "minecraft:jungle_planks", "count": 6},
            {"item": "minecraft:stick", "count": 5},
        ]
        with patch.object(agent, "_post", return_value={
            "blocks": [{"type": "minecraft:crafting_table",
                        "distance": 5.5, "x": 200, "y": 70, "z": -100}]
        }), patch.object(agent, "log_event"):
            action = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertIsNone(action)
        self.assertEqual(runtime["known_workstation"]["x"], 200)

    def test_distant_workstation_makes_companion_walk_over_not_build_another(self):
        goal = {"source": "user", "id": 9, "title": "Obtain an axe"}
        recipe = {"action": "craft", "intent": "craft wooden axe",
                  "width": 3, "height": 3,
                  "grid": ["minecraft:jungle_planks"] * 3 +
                          ["minecraft:stick"] * 2 + [""] * 4}
        runtime = {
            "pending_craft": {"goal_id": 9, "plan": recipe},
            "awareness": {"state": {"x": 200.5, "y": 70, "z": -102.5}},
        }
        inventory = [
            {"item": "minecraft:jungle_planks", "count": 6},
            {"item": "minecraft:stick", "count": 5},
            {"item": "minecraft:crafting_table", "count": 1},
        ]
        calls = []
        def post(path, payload=None):
            calls.append((path, payload))
            if path == "/find-blocks":
                return {"ok": True, "blocks": [{
                    "x": 204, "y": 70, "z": -102,
                    "distance": 5.0, "type": "minecraft:crafting_table"
                }]}
            if path == "/block-at":
                if payload["y"] == 69:
                    return {"ok": True, "air": False,
                            "solid_support_up": True}
                return {"ok": True, "air": True}
            raise AssertionError("unexpected bridge call " + path)
        with patch.object(agent, "_post", side_effect=post), patch.object(
            agent, "log_event"
        ):
            movement = agent._resume_blocked_crafting(runtime, goal, inventory)
        self.assertEqual(movement["action"], "move_to")
        self.assertIn((movement["x"], movement["z"]), {
            (205, -102), (203, -102), (204, -101), (204, -103),
            (205, -101), (205, -103), (203, -101), (203, -103),
        })
        self.assertEqual(movement["goal_id"], 9)
        self.assertEqual(len([v for v in calls if v[0] == "/find-blocks"]), 1)
        self.assertFalse(any(path == "/place-block" for path, _ in calls))

    def test_chat_cannot_claim_to_be_shaping_an_unmade_axe(self):
        runtime = {"awareness": {"inventory": [
            {"item": "minecraft:wooden_pickaxe", "count": 1}
        ]}}
        goals = [{"source": "user", "id": 9, "title": "Obtain an axe"}]
        with patch.object(agent.store, "list_goals", return_value=goals):
            grounded = agent._ground_chat_reply(
                "nearly done, just need to shape the head.", runtime
            )
        self.assertEqual(grounded, "not yet, still haven't crafted the axe")

    def test_recipe_capability_question_is_not_inventory_table_question(self):
        response = agent._inventory_fact_reply(
            "do you have access to recipes? to use crafting table?",
            {"awareness": {"inventory": []}}
        )
        self.assertIn("test crafting recipes", response)

    def test_target_key_ignores_hypothesis_wording(self):
        a = {"action": "mine", "x": 205, "y": 71, "z": -120, "hypothesis": "A"}
        b = dict(a, hypothesis="B", tool="minecraft:string")
        self.assertEqual(agent._plan_target_key(a), agent._plan_target_key(b))


class EmptySearchRegressions(unittest.TestCase):
    """Replay the v15 repeated empty-search failure without a live model."""

    def setUp(self):
        self.runtime = {"awareness": {
            "state": {"x": 10.5, "y": 70.0, "z": -10.5,
                      "dimension": "minecraft:overworld", "health": 20,
                      "maxHealth": 20},
            "inventory": [],
        }}
        self.plan = {"action": "scan_blocks", "intent": "re-observe target block",
                     "hypothesis": "confirm target before mining",
                     "contains": ["minecraft:jungle_log"], "radius": 5, "limit": 1}
        self.empty = {"ok": True, "result": {"ok": True, "blocks": []}}

    def remember(self):
        agent._remember_scan_evidence(self.runtime, self.plan, self.empty)

    def test_wording_limit_and_smaller_radius_do_not_bypass_empty_evidence(self):
        self.remember()
        for radius in (4, 5):
            revised = dict(self.plan, radius=radius, limit=32,
                           contains="minecraft:jungle_log", hypothesis="a new wording")
            self.assertIsNotNone(agent._covered_empty_scan(self.runtime, revised))
        self.assertIsNone(agent._covered_empty_scan(
            self.runtime, dict(self.plan, radius=6)))
        self.assertIsNone(agent._covered_empty_scan(
            self.runtime, dict(self.plan, contains=["_log"])))

    def test_new_area_dimension_and_expired_observations_allow_retry(self):
        self.remember()
        self.runtime["awareness"]["state"]["x"] += 2
        self.assertIsNone(agent._covered_empty_scan(self.runtime, self.plan))
        self.runtime["awareness"]["state"]["x"] -= 2
        self.runtime["empty_resource_scans"][0]["at"] -= 121
        self.assertIsNone(agent._covered_empty_scan(self.runtime, self.plan))
        self.remember()
        self.runtime["awareness"]["state"]["dimension"] = "minecraft:the_nether"
        self.assertIsNone(agent._covered_empty_scan(self.runtime, self.plan))

    def test_transport_failure_is_not_negative_resource_evidence(self):
        agent._remember_scan_evidence(self.runtime, self.plan, {
            "ok": False, "result": {"ok": False, "error": "connection failed"}
        })
        self.assertIsNone(agent._covered_empty_scan(self.runtime, self.plan))

    def test_empty_scan_replaces_old_positive_target_cache(self):
        self.runtime["last_resource_scan"] = {"blocks": [{"pos": [1, 2, 3]}]}
        self.remember()
        self.assertEqual(self.runtime["last_resource_scan"]["blocks"], [])

    def test_negative_evidence_survives_sqlite_reopen_without_learning_success(self):
        from memory_store import MemoryStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.sqlite3"
            db = MemoryStore(path)
            snapshot = {"state": {"pos": [10.5, 70, -10.5],
                                   "dimension": "minecraft:overworld"}, "inventory": []}
            with patch.object(practice, "store", db), patch.object(
                practice, "_snapshot", return_value=snapshot
            ), patch.object(practice, "_post", return_value={"ok": True, "blocks": []}):
                result = practice.execute_plan(self.plan)
            self.assertTrue(result["ok"])  # query succeeded, no resource acquired
            self.assertFalse(result["observation"]["target_found"])
            reopened = MemoryStore(path)
            event = next(e for e in reopened.recent_events(20)
                         if e["kind"] == "practice_observation")
            evidence = json.loads(event["summary"])
            self.assertEqual(evidence["query"]["contains"], ["minecraft:jungle_log"])
            self.assertEqual(evidence["origin"], snapshot["state"]["pos"])
            self.assertEqual(evidence["count"], 0)
            self.assertEqual(reopened.recent_trials(), [])

    def planner_environment(self, stack, completion, execution=None):
        from memory_store import MemoryStore
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(agent, "store", MemoryStore(Path(tmp) / "memory.sqlite3")))
        stack.enter_context(patch.object(agent, "update_awareness"))
        stack.enter_context(patch.object(agent, "_notify_once"))
        stack.enter_context(patch.object(agent, "_report_planned_outcome"))
        stack.enter_context(patch.object(agent, "_local_fast_completion", side_effect=completion))
        return stack.enter_context(patch.object(agent, "execute_plan", side_effect=execution or (
            lambda plan: dict(self.empty, plan=plan)
        )))

    def test_fifteen_repeated_plans_do_not_execute_fifteen_empty_scans(self):
        async def completion(*args, **kwargs):
            return json.dumps(self.plan)
        with ExitStack() as stack:
            execute = self.planner_environment(stack, completion)
            first = asyncio.run(agent.run_planned_action(None, self.runtime))
            self.assertTrue(first["ok"])
            for index in range(14):
                self.runtime.pop("planner_backoff_until", None)
                self.plan["radius"] = 4 if index % 2 else 5
                result = asyncio.run(agent.run_planned_action(None, self.runtime))
                self.assertEqual(result["status"], "scan_stalled")
            self.assertEqual(execute.call_count, 1)
            prompt = agent.build_practice_plan_input(self.runtime)
            self.assertIn("RECENT NEGATIVE SEARCH EVIDENCE", prompt)
            self.assertIn("ZERO targets", prompt)
            self.assertEqual(agent.store.recent_trials(), [])

    def test_replanning_can_execute_a_wider_search(self):
        self.remember()
        wider = dict(self.plan, radius=12)
        with ExitStack() as stack:
            execute = self.planner_environment(stack, [json.dumps(self.plan), json.dumps(wider)])
            result = asyncio.run(agent.run_planned_action(None, self.runtime))
            self.assertTrue(result["ok"])
            self.assertEqual(execute.call_args.args[0]["radius"], 12)

    def test_replanning_cannot_execute_known_failed_target(self):
        self.remember()
        invalid = {"action": "mine", "x": 11, "y": 70, "z": -11,
                   "intent": "mine log", "hypothesis": "retry old target"}
        self.runtime["invalid_targets"] = {
            agent._plan_target_key(invalid): (agent.time.monotonic(), "target_block_mismatch")
        }
        with ExitStack() as stack:
            execute = self.planner_environment(stack, [json.dumps(self.plan), json.dumps(invalid)])
            result = asyncio.run(agent.run_planned_action(None, self.runtime))
            self.assertEqual(result["status"], "scan_stalled")
            execute.assert_not_called()

    def test_verified_world_change_invalidates_old_empty_evidence(self):
        self.remember()
        mine = {"action": "mine", "x": 11, "y": 70, "z": -11,
                "expected_block": "minecraft:stone", "intent": "test stone",
                "hypothesis": "mine observed stone"}
        with ExitStack() as stack:
            self.planner_environment(stack, [json.dumps(mine)], lambda plan: {
                "ok": True, "plan": plan,
                "result": {"state": "COMPLETED", "reason": "block_mined"},
            })
            asyncio.run(agent.run_planned_action(None, self.runtime))
            self.assertIsNone(agent._covered_empty_scan(self.runtime, self.plan))


if __name__ == "__main__":
    unittest.main()
