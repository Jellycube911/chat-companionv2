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

    def test_context_budget_does_not_dump_unrelated_memories(self):
        self.store.remember("fact", "irrelevant.secret", "Unrelated text.", 5)
        self.store.record_episode("hello", "hey")
        context = load_context(self.store, query="where is diamond", goals=[])
        self.assertEqual(context["memories"], [])
        self.assertEqual(len(context["episodes"]), 1)


if __name__ == "__main__":
    unittest.main()
