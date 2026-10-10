"""Offline regressions for the shared local-agent memory context."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_builder import load_context
from memory_store import MemoryStore
import minecraft_agent as agent


class SharedContextTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MemoryStore(Path(self.folder.name) / "memory.sqlite3")
        self.runtime = {
            "awareness": {
                "state": {"x": 0, "y": 64, "z": 0, "owner": {}},
                "inventory": [],
            },
            "invalid_targets": {},
        }

    def test_older_lesson_is_not_lost_after_one_thousand_newer_memories(self):
        self.store.remember(
            "lesson", "ancient.brickshine",
            "Brickshine is a user-taught furnace experiment.", 9,
        )
        for index in range(1005):
            self.store.remember(
                "fact", f"irrelevant.{index}",
                f"Unrelated filler entry number {index}.",
            )
        recalled = self.store.recall("brickshine", 3)
        self.assertEqual(recalled[0]["key"], "ancient.brickshine")

    def test_both_chat_and_physical_planner_receive_saved_lesson(self):
        self.store.remember(
            "lesson", "taught.birch",
            "Birch tools are an experimental hypothesis, not verified fact.", 9,
        )
        self.store.record_episode("Remember the birch lesson", "I'll remember it.")
        with patch.object(agent, "store", self.store):
            chat = agent.build_turn_input(
                "What was the birch lesson?", "minecraft", self.runtime
            )
            planner = agent.build_practice_plan_input(
                self.runtime, directive="birch tools"
            )
        self.assertIn("Birch tools are an experimental hypothesis", chat)
        self.assertIn("Birch tools are an experimental hypothesis", planner)
        self.assertIn("Remember the birch lesson", chat)

    def test_context_reloads_from_same_database_after_restart(self):
        path = Path(self.folder.name) / "memory.sqlite3"
        self.store.remember(
            "lesson", "taught.respawn", "Respawn knowledge persists.", 8
        )
        restarted = MemoryStore(path)
        snapshot = load_context(restarted, query="respawn", goals=[])
        self.assertEqual(snapshot["memories"][0]["key"], "taught.respawn")

    def test_redundant_positive_scan_at_same_position_is_rejected(self):
        now = agent.time.monotonic()
        self.runtime["awareness"]["state"]["dimension"] = "minecraft:overworld"
        self.runtime["last_resource_scan"] = {
            "at": now, "origin": {"x": 0, "y": 64, "z": 0},
            "dimension": "minecraft:overworld",
            "query": {"contains": ["minecraft:stone", "minecraft:coal_ore"],
                      "exact": [], "radius": 16, "limit": 15, "exposed_only": True},
            "blocks": [{"type": "minecraft:stone", "pos": [3, 64, 0]}],
        }
        repeat = {"action": "scan_blocks", "contains": ["minecraft:stone"],
                  "radius": 15, "limit": 10, "exposed_only": True}
        self.assertIsNotNone(agent._covered_positive_scan(self.runtime, repeat))
        self.runtime["awareness"]["state"]["x"] = 8
        self.assertIsNone(agent._covered_positive_scan(self.runtime, repeat))
        self.runtime["awareness"]["state"]["x"] = 0
        fresh = dict(repeat, contains=["minecraft:diamond_ore"])
        self.assertIsNone(agent._covered_positive_scan(self.runtime, fresh))
        expanded = dict(repeat, radius=19)
        self.assertIsNone(agent._covered_positive_scan(self.runtime, expanded))

    def test_positive_scan_targets_are_included_in_planning_prompt(self):
        self.runtime["last_resource_scan"] = {
            "at": agent.time.monotonic(),
            "origin": {"x": 0, "y": 64, "z": 0},
            "dimension": "minecraft:overworld",
            "query": {"contains": ["minecraft:stone"], "exact": [],
                      "radius": 20, "limit": 15, "exposed_only": True},
            "blocks": [{"type": "minecraft:stone", "pos": [3, 64, 0]}],
        }
        with patch.object(agent, "store", self.store):
            prompt = agent.build_practice_plan_input(self.runtime)
        self.assertIn("FRESH OBSERVED BLOCK TARGETS", prompt)
        self.assertIn("minecraft:stone", prompt)
        self.assertIn("[3, 64, 0]", prompt)

    def test_empty_collection_receipt_is_not_gameplay_success(self):
        from practice_engine import _objective_success
        before = {"inventory": [{"item": "minecraft:dirt", "count": 1}]}
        unchanged = {"inventory": [{"item": "minecraft:dirt", "count": 1}]}
        acquired = {"inventory": [{"item": "minecraft:dirt", "count": 2}]}
        receipt = {"state": "COMPLETED", "reason": "collection_complete:0"}
        self.assertFalse(_objective_success({"action": "collect"}, before, unchanged, receipt))
        self.assertTrue(_objective_success({"action": "collect"}, before, acquired, receipt))

    def test_unverified_claims_do_not_upgrade_skills(self):
        import json
        skill = self.store.save_learned_skill("Pickup", "pickup", "Collect real items")
        action = '{"action":"collect"}'
        for state in ("RUNNING", "COMPLETED"):
            outcome = json.dumps({
                "result": {"state": state, "reason": "collection_complete:0"},
                "before": {"inventory": [{"item": "minecraft:dirt", "count": 2}]},
                "after": {"inventory": [{"item": "minecraft:dirt", "count": 2}]},
            })
            claim = self.store.record_skill_trial(
                "pickup", "try", action, outcome, True, skill_id=skill["id"])
            self.assertFalse(claim["success"])
            self.assertIn(claim["evidence_status"], {"unresolved", "unverified"})
        self.assertEqual(self.store.learned_skill(skill["id"])["successes"], 0)
        proof = json.dumps({
            "result": {"state": "COMPLETED", "reason": "collection_complete:1"},
            "before": {"inventory": [{"item": "minecraft:dirt", "count": 2}]},
            "after": {"inventory": [{"item": "minecraft:dirt", "count": 3}]},
        })
        result = self.store.record_skill_trial(
            "pickup", "tested", action, proof, True, skill_id=skill["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.learned_skill(skill["id"])["successes"], 1)

    def test_repeated_failure_digest_preserves_raw_trials(self):
        import json
        evidence = json.dumps({"result": {
            "state": "FAILED",
            "reason": "mining_blocked|block=minecraft:oak_leaves|at=6,7,8"}})
        for _ in range(18):
            self.store.record_skill_trial("mine test", "blocked",
                                          '{"action":"mine"}', evidence, False)
        self.assertEqual(len(self.store.recent_skill_trials("mine test", 30)), 18)
        digest = self.store.recent_trials_digest(8)
        group = next(x for x in digest if x["intent"] == "mine test")
        self.assertEqual(group["repetitions"], 18)
        with patch.object(agent, "store", self.store):
            text = agent.build_practice_plan_input(self.runtime, directive="mine test")
        self.assertIn("repeats=18", text)

    def test_explicit_user_teaching_survives_restart(self):
        path = Path(self.folder.name) / "memory.sqlite3"
        self.assertFalse(self.store.remember_explicit_teaching("Hello there"))
        for _ in range(2):
            self.assertTrue(self.store.remember_explicit_teaching(
                "Remember that the base camp is near the birch forest"))
        restarted = MemoryStore(path)
        remembered = restarted.recall("base camp birch forest", 5)
        self.assertEqual(sum(x["kind"] == "user_taught" for x in remembered), 1)
        with patch.object(agent, "store", restarted):
            chat = agent.build_turn_input("Where is base camp?", "minecraft", self.runtime)
        self.assertIn("birch forest", chat)

    def test_context_budget_does_not_dump_unrelated_memories(self):
        self.store.remember("fact", "irrelevant.secret", "Unrelated text.", 5)
        self.store.record_episode("hello", "hey")
        context = load_context(self.store, query="where is diamond", goals=[])
        self.assertEqual(context["memories"], [])
        self.assertEqual(len(context["episodes"]), 1)


if __name__ == "__main__":
    unittest.main()
