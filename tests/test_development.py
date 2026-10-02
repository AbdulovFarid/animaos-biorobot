import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from anima_evolution import AdaptiveDevelopment, QUALITY_KEYS
from anima_agent import AnimaAgent, GenomeEncoder, LearningMemory, SafeEvolver, DialogueEngine
from luanti_bridge import handle_event, WorldPlanner


GENES = {k: 0.5 for k in (
    "sociability", "oxytocin_base", "dopamine_decay", "adenosine_rate",
    "cortisol_sensitivity", "initiative_prob", "emotional_range",
)}


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.path = Path(self.temp.name) / "Aya_development.json"
        self.dev = AdaptiveDevelopment(self.path, GENES, clock=lambda: self.now)

    def event(self, kind, **data):
        self.dev.observe(kind, {"world_id": "test", "development_revision": self.dev.runtime()["revision"], **data}, {})

    def baseline(self):
        for _ in range(6):
            self.event("navigation_result", success=False)

    def trial(self, changes=None):
        return self.dev.propose({"name": "test", "reason": "trial", "changes": changes or {"body": {"walk_speed": 1.3}}})

    def test_trial_requires_outcomes_not_command_acceptance(self):
        for _ in range(12):
            self.event("planner_command", success=True)
        self.assertEqual(self.dev.data["episodes"], [])
        self.assertFalse(self.trial()[0])

    def test_improvement_retained_and_human_can_revert_it(self):
        self.baseline()
        self.assertTrue(self.trial()[0])
        for _ in range(8):
            self.event("navigation_result", success=True)
        self.assertIsNone(self.dev.data["trial"])
        self.assertEqual(self.dev.data["history"][-1]["status"], "retained")
        self.assertTrue(self.dev.rollback())
        self.assertEqual(self.dev.runtime()["body"]["walk_speed"], 1.5)

    def test_no_improvement_and_pain_roll_back(self):
        self.baseline()
        self.trial()
        for _ in range(8):
            self.event("navigation_result", success=False)
        self.assertEqual(self.dev.runtime()["body"]["walk_speed"], 1.5)
        self.trial()
        self.event("pain", severity=0.1)
        self.assertIsNone(self.dev.data["trial"])
        self.assertEqual(self.dev.data["history"][-1]["reason"], "pain_during_trial")

    def test_old_revision_cannot_claim_trial_success(self):
        self.baseline()
        self.trial()
        for _ in range(8):
            self.event("navigation_result", success=True, development_revision=0)
        self.assertEqual(self.dev.data["trial"]["results"], [])
        self.now += 901
        self.dev.maintain()
        self.assertEqual(self.dev.runtime()["body"]["walk_speed"], 1.5)

    def test_world_change_aborts_trial_and_does_not_reuse_locations(self):
        self.event("world_observation", resources=[{"name": "apple", "position": {"x": 8, "y": 0, "z": 0}}])
        self.baseline()
        self.trial()
        self.event("world_observation", world_id="another")
        self.assertIsNone(self.dev.data["trial"])
        self.assertEqual(self.dev.summary()["known_resources"], 0)

    def test_memory_location_reused_then_invalidated(self):
        pos = {"x": 8, "y": 1, "z": 0}
        self.event("world_observation", resources=[{"name": "rp_default:apple", "position": pos}])
        context = {"hunger": 12, "position": {"x": 0, "y": 1, "z": 0}, "nodes": ["rp_default:apple"]}
        self.assertEqual(self.dev.recommend(context)["target"], pos)
        self.event("resource_missing", item="rp_default:apple", position=pos)
        self.assertIsNone(self.dev.recommend(context))
        self.assertFalse(self.dev.summary()["resource_memories"][-1]["available"])

    def test_life_needs_precede_mutable_modules(self):
        self.assertEqual(self.dev.recommend({"hunger": 2})["action"], "seek_food")
        self.assertIsNone(self.dev.recommend({"storm": True, "protected": False}))
        self.assertEqual(self.dev.recommend({"biochemistry": {"adenosine": 0.9}})["action"], "rest")

    def test_arbitrary_code_nan_and_large_mutations_rejected(self):
        self.baseline()
        for change in ({"body": {"walk_speed": float("nan")}},
                       {"genes": {"cortisol_sensitivity": 0.0}},
                       {"modules": ["shell"]}, {"code": "import os"},
                       {"body": {"walk_speed": 4}}, {"body": {"collisionbox": []}}):
            self.assertFalse(self.trial(change)[0], change)
        self.assertEqual(self.dev.runtime()["revision"], 0)

    def test_all_evolvable_sections_have_reversible_trials(self):
        for _ in range(6):
            self.event("food_eaten")
            self.event("build_failed")
        changes = [
            {"genes": {"initiative_prob": 0.52}},
            {"gathering": {"prefer_memory": False}},
            {"construction": {"half_size": 3}},
            {"modules": ["exploration", "construction", "learned_actions", "resource_memory"]},
        ]
        for change in changes:
            self.assertTrue(self.trial(change)[0], change)
            self.assertTrue(self.dev.rollback())
        self.assertEqual(self.dev.runtime()["genes"], GENES)

    def test_program_module_order_changes_the_decision(self):
        context = {"position": {"x": 0, "y": 0, "z": 0}, "hunger": 12,
                   "current_goal": "build", "motives": {"curiosity": 0.8}}
        self.event("world_observation", resources=[{"name": "apple", "position": {"x": 8, "y": 0, "z": 0}}])
        for _ in range(6):
            self.event("food_eaten")
        self.assertEqual(self.dev.recommend(context)["reason"], "remembered_resource")
        self.assertTrue(self.trial({"modules": ["exploration", "construction", "learned_actions", "resource_memory"]})[0])
        self.assertEqual(self.dev.recommend(context)["reason"], "curiosity_and_genome")

    def test_model_of_body_and_self_is_grounded_in_results(self):
        self.event("world_observation")
        self.dev.note_decision({"action": "move_to", "reason": "try"})
        self.event("jump_result", success=True, rise=0.85)
        self.event("jump_result", success=False, rise=1.2)
        self.assertEqual(self.dev.summary()["body"]["max_observed_rise"], 0.85)
        self.assertEqual(self.dev.summary()["prediction_error"], 0.25)

    def test_house_without_verified_interior_is_a_failure(self):
        self.event("build_completed")
        self.assertLess(self.dev.data["episodes"][-1]["reward"], 0)
        self.event("build_completed", quality={k: True for k in QUALITY_KEYS})
        small = self.dev.data["episodes"][-1]["reward"]
        self.event("build_completed", quality={k: True for k in QUALITY_KEYS},
                   blueprint={"half_size": 3, "wall_height": 3})
        self.assertGreater(self.dev.data["episodes"][-1]["reward"], small)

    def test_restart_preserves_trial_genome_experience_and_revert(self):
        self.baseline()
        self.trial()
        restored = AdaptiveDevelopment(self.path, GENES, clock=lambda: self.now)
        self.assertEqual(restored.runtime(), self.dev.runtime())
        self.assertEqual(len(restored.data["episodes"]), 6)
        restored.rollback()
        self.assertEqual(restored.runtime()["body"]["walk_speed"], 1.5)

    def test_concurrent_events_leave_readable_memory(self):
        threads = [threading.Thread(target=lambda: self.event("food_eaten")) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        saved = json.loads(self.path.read_text())
        self.assertEqual(len(saved["episodes"]), 12)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agent = AnimaAgent(memory_dir=self.temp.name, start_background=False)
        self.addCleanup(self.agent.stop)

    def test_biochemistry_preserved_by_proposal_and_affected_by_actual_rest(self):
        blood = dict(self.agent.blood)
        evolver = SafeEvolver.__new__(SafeEvolver)
        evolver.agent, evolver.provider = self.agent, "test"
        self.assertFalse(evolver._assimilate(json.dumps({"name": "hack", "code": "agent.blood['cortisol']=0"})))
        self.assertEqual(blood, self.agent.blood)
        self.agent.blood["adenosine"] = 0.8
        handle_event(self.agent, {"kind": "rest_completed", "data": {"seconds": 20}})
        self.assertAlmostEqual(self.agent.blood["adenosine"], 0.62)

    def test_bridge_observes_before_planning_and_includes_development(self):
        planner = WorldPlanner(self.agent, str(Path(self.temp.name) / "command.json"))
        with patch.object(planner, "publish_development"), patch.object(planner, "consider") as consider:
            def check(event):
                self.assertEqual(self.agent.development.summary()["body"]["successful_jumps"], 1)
            consider.side_effect = check
            handle_event(self.agent, {"kind": "jump_result", "data": {"success": True, "rise": 0.9}}, planner)
        snapshot = planner._snapshot("test")
        self.assertIn("development", snapshot)
        self.assertEqual(snapshot["biochemistry"].keys(), self.agent.blood.keys())

    def test_late_planner_command_cannot_replace_human_contact(self):
        path = Path(self.temp.name) / "command.json"
        planner = WorldPlanner(self.agent, str(path))
        planner.human_contact_until = float("inf")
        planner._write_command({"action": "socialize"})
        planner._write_command({"action": "explore"})
        self.assertEqual(json.loads(path.read_text())["action"], "socialize")
        self.assertIsNone(self.agent.development.pending)  # wait for the body's acknowledgement

    def test_host_creates_runtime_before_game_needs_it(self):
        runtime = Path(self.temp.name) / "new-runtime"
        planner = WorldPlanner(self.agent, str(runtime / "command.json"))
        with patch("luanti_bridge.DEVELOPMENT_PATH", str(runtime / "development.json")):
            planner.publish_development(force=True)
        self.assertTrue(runtime.is_dir())
        self.assertEqual(json.loads((runtime / "development.json").read_text())["revision"], 0)
        planner._write_command({"action": "rest"})
        self.assertEqual(json.loads((runtime / "command.json").read_text())["action"], "rest")

    def test_save_restore_memory_and_existing_personality(self):
        self.agent.learning.remember_knowledge("Дом у воды")
        self.agent.blood["oxytocin"] = 0.72
        filename = GenomeEncoder.save_to_disk(self.agent)
        restored = GenomeEncoder.load_from_disk(filename, start_background=False)
        self.addCleanup(restored.stop)
        self.assertAlmostEqual(restored.blood["oxytocin"], 0.72)
        self.assertEqual(restored.learning.recall_knowledge()[0]["statement"], "Дом у воды")
        self.assertEqual(GenomeEncoder.list_vault(self.temp.name), ["Aya_gen0.json"])

    def test_old_memory_corrections_and_consolidation(self):
        memory = self.agent.learning
        memory.remember_knowledge("Яблоки под водой")
        memory.correct_knowledge("Яблоки под водой", "Яблоки на дереве")
        memory = LearningMemory(memory.path)
        self.assertEqual(memory.recall_knowledge()[0]["statement"], "Яблоки на дереве")
        self.assertFalse(memory.recall_knowledge()[0]["verified"])
        for _ in range(3):
            memory.observe("дерево", "обойти", "успех", 0.8)
            memory.observe("дерево", "врезаться", "застряла", -0.8)
        memory.consolidate()
        self.assertEqual(memory.data["concepts"]["дерево"]["preferred_action"], "обойти")

    def test_dialogue_has_experience_and_does_not_claim_unobserved_skill(self):
        dialogue = DialogueEngine.__new__(DialogueEngine)
        dialogue.agent, dialogue.history = self.agent, []
        prompt = dialogue._build_prompt("что ты умеешь?")
        self.assertIn('"competence":{}', prompt)
        self.assertIn("неподтверждённую", prompt)


if __name__ == "__main__":
    unittest.main()
