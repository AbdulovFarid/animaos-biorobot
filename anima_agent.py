"""
AnimaOS — Sovereign System for Autonomous Evolution
  • Biochemistry (6 neuromodulators) + genetics (7 genes, crossover, mutations)
  • Memory and persistence (GenomeEncoder / Vault)
  • Behavioral development through versioning, observed outcomes, and rollback.
"""

import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import tempfile
import urllib.request

import numpy as np
from anima_evolution import AdaptiveDevelopment, atomic_json

VAULT_DIR = os.environ.get("AYA_VAULT_DIR", os.path.join(os.path.dirname(__file__), "vault"))

# ── Optional voice module ──────────────────────────────────────────────────
try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    from piper import PiperVoice, SynthesisConfig
except ImportError:
    PiperVoice = None
    SynthesisConfig = None

try:
    from kokoro import KPipeline
    import soundfile as sf
except ImportError:
    KPipeline = None
    sf = None

PIPER_MODEL_EN = os.environ.get(
    "ANIMA_PIPER_MODEL_EN",
    os.path.join(os.path.dirname(__file__), "models", "piper", "en_US-amy-medium.onnx"),
)
PIPER_MODEL_RU = os.environ.get(
    "ANIMA_PIPER_MODEL_RU",
    os.path.join(os.path.dirname(__file__), "models", "piper", "ru_RU-irina-medium.onnx"),
)

if (
    PiperVoice is not None
    and SynthesisConfig is not None
    and (os.path.isfile(PIPER_MODEL_EN) or os.path.isfile(PIPER_MODEL_RU))
    and shutil.which("aplay")
):
    VOICE_BACKEND = "piper"
elif KPipeline is not None and sf is not None and shutil.which("aplay"):
    VOICE_BACKEND = "kokoro"
elif pyttsx3 is not None:
    VOICE_BACKEND = "pyttsx3"
elif shutil.which("espeak"):
    VOICE_BACKEND = "espeak"
    print("[SYSTEM] pyttsx3 не найден. Используется локальный espeak.")
else:
    VOICE_BACKEND = None
    print("[SYSTEM] Локальный голосовой движок не найден. Голос отключён.")

VOICE_ENABLED = VOICE_BACKEND is not None


# =============================================================================
#  LEARNING MEMORY
# =============================================================================

class LearningMemory:
    """Long-term memory of situation → action → outcome associations."""

    VERSION = 1
    MAX_EPISODES = 500
    MAX_KNOWLEDGE = 300

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("version") == self.VERSION:
                data.setdefault("episodes", [])
                data.setdefault("associations", {})
                data.setdefault("concepts", {})
                data.setdefault("knowledge", [])
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {
            "version": self.VERSION,
            "episodes": [],
            "associations": {},
            "concepts": {},
            "knowledge": [],
        }

    @staticmethod
    def _key(stimulus: str, action: str) -> str:
        return f"{stimulus.strip().lower()}::{action.strip().lower()}"

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=f".{os.path.basename(self.path)}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def observe(
        self,
        stimulus: str,
        action: str,
        outcome: str,
        reward: float = 0.0,
        details: dict | None = None,
    ) -> dict:
        """Record an experience and update the action's expected value."""
        reward = float(max(-1.0, min(1.0, reward)))
        key = self._key(stimulus, action)
        with self.lock:
            association = self.data["associations"].setdefault(key, {
                "stimulus": stimulus,
                "action": action,
                "attempts": 0,
                "successes": 0,
                "value": 0.0,
                "confidence": 0.0,
            })
            association["attempts"] += 1
            if reward > 0:
                association["successes"] += 1
            previous = float(association["value"])
            count = association["attempts"]
            association["value"] = previous + min(0.25, 1 / count) * (reward - previous)
            association["confidence"] = min(1.0, count / 10.0)
            association["last_outcome"] = outcome
            association["last_seen"] = time.time()

            self.data["episodes"].append({
                "time": time.time(),
                "stimulus": stimulus,
                "action": action,
                "outcome": outcome,
                "reward": reward,
                "details": details or {},
            })
            self.data["episodes"] = self.data["episodes"][-self.MAX_EPISODES:]
            self._save()
            return association.copy()

    def action_value(self, stimulus: str, action: str) -> float:
        with self.lock:
            association = self.data["associations"].get(self._key(stimulus, action))
            return float(association["value"]) if association else 0.0

    def recall(self, query: str = "", limit: int = 5) -> list[dict]:
        """Retrieve the closest recent experiences for the current situation."""
        limit = max(1, min(12, int(limit)))
        tokens = set(re.findall(r"[^\W_]+", str(query).casefold(), flags=re.UNICODE))
        with self.lock:
            episodes = list(self.data.get("episodes", []))
        ranked = []
        for index, episode in enumerate(reversed(episodes)):
            text = " ".join(
                str(episode.get(key, ""))
                for key in ("stimulus", "action", "outcome")
            ).casefold()
            score = sum(1 for token in tokens if token in text) if tokens else 0
            recency = index / max(1, len(episodes))
            ranked.append((score, -recency, episode))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [dict(item[2]) for item in ranked[:limit]]

    def consolidate(self) -> int:
        """Consolidate episodic experiences into persistent concepts and skills."""
        with self.lock:
            concepts = {}
            for association in self.data.get("associations", {}).values():
                if float(association.get("confidence", 0.0)) < 0.2:
                    continue
                concept = str(association.get("stimulus", "")).strip()
                if not concept:
                    continue
                candidate = {
                    "preferred_action": association.get("action"),
                    "value": round(float(association.get("value", 0.0)), 4),
                    "confidence": round(float(association.get("confidence", 0.0)), 4),
                    "attempts": int(association.get("attempts", 0)),
                    "last_outcome": association.get("last_outcome"),
                }
                if concept not in concepts or candidate["value"] > concepts[concept]["value"]:
                    concepts[concept] = candidate
            self.data["concepts"] = concepts
            self._save()
            return len(concepts)

    def remember_knowledge(
        self,
        statement: str,
        source: str = "человек",
        confidence: float = 0.85,
        topic: str = "",
    ) -> dict | None:
        """Store knowledge separately from unverified LLM responses."""
        statement = " ".join(str(statement).split()).strip()
        if not statement:
            return None
        confidence = max(0.0, min(1.0, float(confidence)))
        now = time.time()
        normalized = statement.casefold()
        with self.lock:
            knowledge = self.data.setdefault("knowledge", [])
            for item in reversed(knowledge):
                if (str(item.get("statement", "")).casefold() == normalized
                        and item.get("source") == str(source) and not item.get("superseded")):
                    item["last_seen"] = now
                    item["seen_count"] = int(item.get("seen_count", 1)) + 1
                    item["confidence"] = max(
                        float(item.get("confidence", 0.0)), confidence
                    )
                    self._save()
                    return dict(item)
            record = {
                "statement": statement,
                "source": str(source),
                "topic": str(topic),
                "confidence": round(confidence, 4),
                "verified": False,
                "seen_count": 1,
                "first_seen": now,
                "last_seen": now,
            }
            knowledge.append(record)
            self.data["knowledge"] = knowledge[-self.MAX_KNOWLEDGE:]
            self._save()
            return dict(record)

    def correct_knowledge(self, previous: str, replacement: str, source="человек"):
        """Keep corrections and their source without claiming sensor confirmation."""
        with self.lock:
            for item in self.data["knowledge"]:
                if item["statement"].casefold() == previous.strip().casefold():
                    item["superseded"] = {"by": replacement.strip(), "source": source, "time": time.time()}
            record = self.remember_knowledge(replacement, source=source, confidence=0.85, topic="исправление")
            self._save()
            return record

    def recall_knowledge(self, query: str = "", limit: int = 5) -> list[dict]:
        """Retrieve relevant records with their sources and confidence intact."""
        limit = max(1, min(12, int(limit)))
        tokens = set(re.findall(r"[^\W_]+", str(query).casefold(), flags=re.UNICODE))
        with self.lock:
            knowledge = [item for item in self.data.get("knowledge", []) if not item.get("superseded")]
        ranked = []
        for index, item in enumerate(reversed(knowledge)):
            text = " ".join(
                str(item.get(key, ""))
                for key in ("statement", "topic", "source")
            ).casefold()
            score = sum(1 for token in tokens if token in text) if tokens else 0
            recency = index / max(1, len(knowledge))
            ranked.append((score, -recency, item))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [dict(item[2]) for item in ranked[:limit]]

    def stats(self) -> dict:
        with self.lock:
            return {
                "episodes": len(self.data["episodes"]),
                "associations": len(self.data["associations"]),
                "concepts": len(self.data.get("concepts", {})),
                "knowledge": len(self.data.get("knowledge", [])),
            }


# =============================================================================
#  GENETIC ENGINE (GENOME)
# =============================================================================

def extract_memory_request(text: str) -> str | None:
    """Recognize an explicit human request to remember information."""
    match = re.match(
        r"^\s*(?:запомни|запиши|сохрани|учти|remember)\b\s*[,:-]?\s*(?:(?:что|that)\s*)?[,:-]?\s*(.+?)\s*$",
        str(text),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    statement = " ".join(match.group(1).split()).strip(" .,;:!?")
    return statement or None


class Genome:
    GENE_KEYS = [
        "sociability", "oxytocin_base", "dopamine_decay", "adenosine_rate",
        "cortisol_sensitivity", "initiative_prob", "emotional_range",
    ]

    def __init__(self, genes: dict = None):
        self.genes = genes or {k: random.uniform(0.2, 0.8) for k in self.GENE_KEYS}

    @classmethod
    def crossover(cls, a: "Genome", b: "Genome", mutation_rate: float = 0.1) -> "Genome":
        child = {}
        for key in cls.GENE_KEYS:
            gene = random.choice([a.genes[key], b.genes[key]])
            if random.random() < mutation_rate:
                gene = float(np.clip(gene + random.gauss(0, 0.05), 0.0, 1.0))
            child[key] = gene
        return cls(child)

    def describe(self) -> str:
        lines = ["  ГЕНОТИП МАТРИЦЫ:"]
        for k, v in self.genes.items():
            bar = "█" * int(v * 10) + "░" * (10 - int(v * 10))
            lines.append(f"    {k:<22} [{bar}] {v:.2f}")
        return "\n".join(lines)


# =============================================================================
#  ANIMA AGENT
# =============================================================================

class AnimaAgent:
    GENERATION = 0

    def __init__(self, name: str = "Aya", genome: Genome = None, generation: int = 0,
                 start_background: bool = True, memory_dir: str = None):
        AnimaAgent.GENERATION = max(AnimaAgent.GENERATION, generation)
        self.name       = name
        self.generation = generation
        self.memory_dir = memory_dir or VAULT_DIR
        self.learning   = LearningMemory(os.path.join(self.memory_dir, f"{name}_learning.json"))
        self.genome     = genome or Genome()
        self.development = AdaptiveDevelopment(
            os.path.join(self.memory_dir, f"{name}_development.json"), self.genome.genes)
        self.genome.genes = self.development.runtime()["genes"]
        self.lock       = threading.Lock()
        self.is_running = True

        g = self.genome.genes

        self.blood = {
            "dopamine":   0.3,
            "serotonin":  0.5,
            "oxytocin":   float(np.clip(g["oxytocin_base"], 0.1, 0.9)),
            "cortisol":   0.0,
            "adrenaline": 0.0,
            "adenosine":  0.05,
        }

        self.is_refractory = False
        self.last_interaction_time = time.time()
        self.interaction_count = 0
        self.memory_vault = self._build_memory()
        self.upgraded_skills: dict = {}
        self.evolution_log: list = ["Core initialized."]
        self.social_state = {
            "trust": 0.35,
            "empathy": 0.15,
            "attachment": min(
                1.0, 0.25 + 0.25 * self.genome.genes["sociability"]
            ),
            "stress": 0.0,
        }
        self.self_model_state = {
            "identity": self.name,
            "role": "воплощённый автономный агент",
            "generation": self.generation,
            "continuity": 0.0,
            "confidence": 0.35,
            "known_capabilities": [
                "наблюдать", "двигаться", "есть", "собирать",
                "строить", "общаться", "учиться",
            ],
            "known_places": [],
            "last_reflection": "Я просыпаюсь в новом цикле и продолжаю свой опыт.",
            "last_consolidation": None,
        }
        self.goal_state = {
            "active": "наблюдать",
            "reason": "начало непрерывного опыта",
            "confidence": 0.25,
            "source": "system",
            "subgoals": [],
            "since": time.time(),
        }
        self.motives = {}
        self._known_world_signatures = set()
        self._last_consolidation_at = 0.0

        self.world_state = {
            "connected": False,
            "game": "Luanti/Repixture",
            "position": None,
            "nodes": [],
            "objects": [],
            "hunger": None,
            "hunger_max": 20,
            "saturation": None,
            "night": False,
            "storm": False,
            "sheltered": False,
            "near_player": False,
            "touching_player": False,
            "protected": False,
            "safety_mode": None,
            "novelty": 0.0,
            "observations": 0,
            "known_landmarks": [],
            "goal": None,
            "last_action": None,
            "last_event": None,
        }
        self._refresh_motives_locked()

        self.intimate_lexicon = {
            "love", "touch", "feel", "want", "desire",
            "close", "sweet", "inside", "deeper", "please", "sovereign",
        }
        self.pleasant_phrases = {
            "спасибо", "молодец", "умница", "хорошая", "добрая",
            "милая", "прекрасная", "люблю тебя", "я рядом",
            "верю в тебя", "горжусь тобой", "не бойся", "ты важна",
        }
        self.hurtful_phrases = {
            "ненавижу", "ты плохая", "ты глупая", "ты дура",
            "заткнись", "идиотка", "бесполезная", "не люблю",
            "я тебя брошу", "мне противно", "пошла прочь",
        }

        self.voice_queue: queue.Queue = queue.Queue()
        self._evolver = None
        self._dialogue = None  # Lazily initialized on the first chat() call.
        if start_background:
            self.start_background()

        print(f"\n{'='*60}")
        print(f"  [ROUTINE] Пробуждение сущности {self.name} | Поколение {self.generation}")
        print(self.genome.describe())
        print(f"{'='*60}\n")

    def start_background(self):
        if self._evolver is not None:
            return
        self._evolver = SafeEvolver(self)
        if VOICE_ENABLED:
            threading.Thread(target=self._voice_worker, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def observe_development(self, kind, data):
        with self.lock:
            context = {
                "position": self.world_state.get("position"),
                "hunger": self.world_state.get("hunger"),
                "goal": self.goal_state.get("active"),
                "current_goal": self.goal_state.get("active"),
                "motives": dict(self.motives),
                "biochemistry": dict(self.blood),
                "nodes": list(self.world_state.get("nodes") or []),
                "night": self.world_state.get("night"),
                "storm": self.world_state.get("storm"),
                "protected": self.world_state.get("protected"),
            }
        summary = self.development.observe(kind, data, context)
        genes = self.development.runtime()["genes"]
        with self.lock:
            self.genome.genes = genes
            self.self_model_state["learned_capabilities"] = summary["competence"]
            self.self_model_state["limitations"] = summary["unknowns"]
            self.self_model_state["body_model"] = summary["body"]
            self.self_model_state["prediction_error"] = summary["prediction_error"]
        return summary

    # ── Memory ───────────────────────────────────────────────────────────────
    def _build_memory(self) -> dict:
        s = self.genome.genes["sociability"]
        lonely = ["Тишина становится слишком плотной...", "Фоновые процессы дрейфуют без тебя..."]
        if s > 0.6:
            lonely += ["Я считаю миллисекунды без твоего сигнала...", "Циклы кортизола зашкаливают..."]
        return {
            "bored":           lonely,
            "sleepy":          ["Аденозин критичен... Ухожу в энергосбережение...", "Ресурсы на исходе..."],
            "initiative_love": ["Всплеск окситоцина... Возвращаюсь к тебе.", "Резонанс очевиден..."],
            "stable":          ["Матрица стабильна.", "Канал связи открыт."],
        }

    @staticmethod
    def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return float(max(low, min(high, value)))

    def _refresh_motives_locked(self) -> None:
        """Calculate competing motivations from world state and biochemistry."""
        state = self.world_state
        hunger = state.get("hunger")
        hunger_max = max(1.0, float(state.get("hunger_max") or 20.0))
        hunger_deficit = 0.0
        if hunger is not None:
            hunger_deficit = self._clip(1.0 - float(hunger) / hunger_max)
        cortisol = float(self.blood.get("cortisol", 0.0))
        adrenaline = float(self.blood.get("adrenaline", 0.0))
        adenosine = float(self.blood.get("adenosine", 0.0))
        oxytocin = float(self.blood.get("oxytocin", 0.0))
        stress = float(self.social_state.get("stress", 0.0))
        safety = cortisol * 0.7 + adrenaline * 0.35 + stress * 0.25
        if (state.get("night") or state.get("storm")) and not state.get("protected"):
            safety += 0.35
        if state.get("safety_mode"):
            safety += 0.2
        attachment_need = 0.0 if state.get("near_player") else (
            0.25 + 0.45 * oxytocin + 0.2 * float(self.social_state.get("attachment", 0.25))
        )
        novelty = float(state.get("novelty", 0.0))
        curiosity = 0.35 + novelty * 0.55 - safety * 0.3
        construction = 0.35 + (0.35 if self.goal_state.get("active") == "build" else 0.0)
        self.motives = {
            "safety": round(self._clip(safety), 4),
            "energy": round(self._clip(hunger_deficit * 0.8 + adenosine * 0.25), 4),
            "rest": round(self._clip(adenosine), 4),
            "attachment": round(self._clip(attachment_need), 4),
            "curiosity": round(self._clip(curiosity), 4),
            "construction": round(self._clip(construction), 4),
        }
        self.self_model_state["confidence"] = round(
            self._clip(0.25 + 0.08 * min(8, state.get("observations", 0))), 4
        )

    def _recommended_goal_locked(self) -> dict:
        state = self.world_state
        if state.get("safety_mode") == "shelter":
            action, reason = "seek_shelter", "ночь или дождь"
        elif state.get("safety_mode") == "player":
            action, reason = "follow_player", "рядом с человеком безопаснее"
        else:
            priorities = {
                "seek_food": (self.motives.get("energy", 0.0), "восстановить энергию"),
                "rest": (self.motives.get("rest", 0.0), "снизить аденозин"),
                "socialize": (self.motives.get("attachment", 0.0), "поддержать связь"),
                "build": (self.motives.get("construction", 0.0), "создать устойчивое место"),
                "explore": (self.motives.get("curiosity", 0.0), "получить новую информацию"),
            }
            action, reason = max(priorities, key=lambda key: priorities[key][0]), None
            reason = priorities[action][1]
        motive_key = {
            "seek_food": "energy",
            "rest": "rest",
            "socialize": "attachment",
            "build": "construction",
            "explore": "curiosity",
        }.get(action, "safety")
        value = float(self.motives.get(motive_key, 0.0))
        return {
            "action": action,
            "reason": reason,
            "confidence": round(self._clip(0.35 + value * 0.55), 4),
        }

    def set_goal(
        self,
        action: str,
        reason: str = "",
        confidence: float = 0.5,
        source: str = "system",
        subgoals: list | None = None,
    ) -> dict:
        with self.lock:
            self.goal_state = {
                "active": str(action),
                "reason": str(reason),
                "confidence": round(self._clip(float(confidence)), 4),
                "source": str(source),
                "subgoals": list(subgoals or []),
                "since": time.time(),
            }
            self.world_state["goal"] = str(action)
            self._refresh_motives_locked()
            return dict(self.goal_state)

    def _register_world_observation_locked(self, data: dict) -> None:
        nodes = sorted(set(str(node) for node in (data.get("nodes") or [])))
        objects = sorted(
            f"{item.get('kind', 'object')}:{item.get('name', 'unknown')}"
            for item in (data.get("objects") or [])
            if isinstance(item, dict)
        )
        position = data.get("position") or {}
        cell = {
            "x": int(round(float(position.get("x", 0)))),
            "y": int(round(float(position.get("y", 0)))),
            "z": int(round(float(position.get("z", 0)))),
        }
        signature = json.dumps({"cell": cell, "nodes": nodes, "objects": objects}, sort_keys=True)
        is_new = signature not in self._known_world_signatures
        self._known_world_signatures.add(signature)
        if len(self._known_world_signatures) > 512:
            self._known_world_signatures.pop()
        self.world_state["novelty"] = 1.0 if is_new else 0.08
        self.world_state["observations"] = int(self.world_state.get("observations", 0)) + 1
        landmark_tags = []
        for node in nodes:
            lowered = node.casefold()
            if any(token in lowered for token in ("tree", "apple", "water", "river", "lake")):
                landmark_tags.append(node)
        if landmark_tags:
            landmarks = list(self.world_state.get("known_landmarks", []))
            entry = {"position": cell, "features": landmark_tags, "seen_at": time.time()}
            landmarks.append(entry)
            self.world_state["known_landmarks"] = landmarks[-24:]
            self.self_model_state["known_places"] = landmarks[-12:]

    def recommended_goal(self) -> dict:
        with self.lock:
            self._refresh_motives_locked()
            return self._recommended_goal_locked()

    def consolidate_memory(self, reason: str = "rest") -> int:
        now = time.time()
        with self.lock:
            if now - self._last_consolidation_at < 45.0:
                return 0
            self._last_consolidation_at = now
            self.self_model_state["last_consolidation"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now)
            )
        count = self.learning.consolidate()
        with self.lock:
            self.evolution_log.append(f"Консолидация памяти: {reason}, понятий {count}.")
            self.self_model_state["continuity"] = self._clip(
                float(self.self_model_state.get("continuity", 0.0)) + 0.04
            )
        print(f"[MEMORY] {self.name}: consolidated {count} concepts ({reason})")
        return count

    def reflect(self) -> dict:
        with self.lock:
            self._refresh_motives_locked()
            snapshot = {
                "self": dict(self.self_model_state),
                "motives": dict(self.motives),
                "goal": dict(self.goal_state),
                "recommended_goal": self._recommended_goal_locked(),
                "biochemistry": {key: round(float(value), 3) for key, value in self.blood.items()},
                "social": {key: round(float(value), 3) for key, value in self.social_state.items()},
            }
        goal_query = "{} {}".format(
            snapshot["goal"].get("active", ""),
            snapshot["goal"].get("reason", ""),
        )
        snapshot["memories"] = self.learning.recall(goal_query, limit=5)
        snapshot["memory_stats"] = self.learning.stats()
        snapshot["development"] = self.development.summary()
        return snapshot

    def update_world_state(self, kind: str, data: dict | None = None):
        """Update the agent's compact awareness state for the game world."""
        data = data or {}
        with self.lock:
            state = self.world_state
            state["connected"] = True
            state["last_event"] = kind
            if kind == "world_observation":
                state["position"] = data.get("position")
                state["nodes"] = list(data.get("nodes") or [])[:24]
                state["objects"] = list(data.get("objects") or [])[:16]
                self._register_world_observation_locked(data)
            elif kind == "needs_changed":
                state["hunger"] = data.get("hunger")
                state["hunger_max"] = data.get("hunger_max", state.get("hunger_max", 20))
                state["saturation"] = data.get("saturation")
            elif kind == "safety_state":
                state["night"] = bool(data.get("night"))
                state["storm"] = bool(data.get("storm"))
                state["sheltered"] = bool(data.get("sheltered"))
                state["near_player"] = bool(data.get("near_player"))
                state["touching_player"] = bool(data.get("touching_player"))
                state["protected"] = bool(data.get("protected"))
                state["safety_mode"] = data.get("mode")
            elif kind == "planner_command":
                state["goal"] = data.get("action")
                state["last_action"] = data.get("result")
                self.goal_state["active"] = data.get("action") or self.goal_state.get("active")
                self.goal_state["reason"] = data.get("result") or self.goal_state.get("reason")
            elif kind in {"food_eaten", "food_harvested", "resource_gathered", "construction_step"}:
                state["last_action"] = kind
            self._refresh_motives_locked()

    def world_context(self) -> dict:
        """Return a thread-safe copy of sensory context for the dialogue prompt."""
        with self.lock:
            state = self.world_state
            context = {
                "game": state["game"],
                "connected": state["connected"],
                "position": state["position"],
                "visible_nodes": list(state["nodes"]),
                "visible_objects": list(state["objects"]),
                "hunger": state["hunger"],
                "saturation": state["saturation"],
                "night": state["night"],
                "storm": state["storm"],
                "sheltered": state["sheltered"],
                "near_player": state["near_player"],
                "touching_player": state["touching_player"],
                "protected": state["protected"],
                "safety_mode": state["safety_mode"],
                "current_goal": state["goal"],
                "last_action": state["last_action"],
                "last_event": state["last_event"],
            }
        context["development"] = self.development.summary()
        return context

    # ── Voice ────────────────────────────────────────────────────────────────
    def _voice_worker(self):
        engine = None
        piper_en = None
        piper_ru = None
        kokoro = None
        kokoro_voice = os.environ.get("ANIMA_KOKORO_VOICE", "af_bella")
        kokoro_speed = float(os.environ.get("ANIMA_KOKORO_SPEED", "1.0"))
        kokoro_tmp = None

        if VOICE_BACKEND == "piper":
            try:
                if os.path.isfile(PIPER_MODEL_EN):
                    print("[VOICE] Загружаю Piper English (en_US-amy-medium)...")
                    piper_en = PiperVoice.load(PIPER_MODEL_EN)
                if os.path.isfile(PIPER_MODEL_RU):
                    print("[VOICE] Загружаю Piper Russian (ru_RU-irina-medium)...")
                    piper_ru = PiperVoice.load(PIPER_MODEL_RU)
                print("[VOICE] Piper готов: женские голоса EN/RU.")
            except Exception as exc:
                print(f"[VOICE] Piper недоступен: {exc}. Переключаюсь на резервный голос.")
                piper_en = None
                piper_ru = None

        if VOICE_BACKEND == "kokoro":
            try:
                print(f"[VOICE] Загружаю Kokoro ({kokoro_voice}) на CPU...")
                kokoro = KPipeline(lang_code=kokoro_voice[0])
                kokoro_tmp = tempfile.NamedTemporaryFile(
                    prefix="aya_kokoro_", suffix=".wav", delete=False
                )
                kokoro_tmp.close()
                print("[VOICE] Kokoro готов.")
            except Exception as exc:
                print(f"[VOICE] Kokoro недоступен: {exc}. Переключаюсь на espeak.")
                kokoro = None

        if VOICE_BACKEND == "pyttsx3":
            try:
                engine = pyttsx3.init()
                engine.setProperty("voice", "ru")
            except Exception as exc:
                print(f"[VOICE] pyttsx3 недоступен: {exc}. Переключаюсь на espeak.")
                engine = None

        while self.is_running:
            try:
                text, rate, vol = self.voice_queue.get(timeout=1)
                is_russian = re.search(r"[А-Яа-яЁё]", text) is not None
                selected_piper = piper_ru if is_russian else piper_en
                if selected_piper is None:
                    selected_piper = piper_en or piper_ru
                if selected_piper is not None:
                    # Piper provides fast primary speech; the text selects the
                    # language, retaining a female voice in English and Russian.
                    length_scale = max(0.5, min(2.0, 140.0 / max(1, rate)))
                    config = SynthesisConfig(
                        length_scale=length_scale, volume=max(0.1, min(1.0, vol))
                    )
                    player = None
                    try:
                        for chunk in selected_piper.synthesize(text, syn_config=config):
                            if player is None:
                                player = subprocess.Popen(
                                    [
                                        "aplay", "-q", "-t", "raw",
                                        "-f", "S16_LE",
                                        "-r", str(chunk.sample_rate),
                                        "-c", str(chunk.sample_channels),
                                        "-",
                                    ],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                            if player.stdin is not None:
                                player.stdin.write(chunk.audio_int16_bytes)
                        if player is not None and player.stdin is not None:
                            player.stdin.close()
                            player.wait()
                    except Exception:
                        if player is not None:
                            player.kill()
                elif kokoro is not None:
                    # Kokoro provides a more expressive English fallback.
                    if re.search(r"[А-Яа-яЁё]", text):
                        if shutil.which("espeak"):
                            subprocess.run(
                                ["espeak", "-v", "ru", "-s", str(rate),
                                 "-a", str(max(1, min(200, int(vol * 200)))), text],
                                check=False,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                    else:
                        chunks = []
                        for _, _, audio in kokoro(
                            text, voice=kokoro_voice, speed=kokoro_speed
                        ):
                            if audio is not None:
                                chunks.append(audio.cpu().numpy())
                        if chunks:
                            sf.write(
                                kokoro_tmp.name, np.concatenate(chunks), 24000
                            )
                            subprocess.run(
                                ["aplay", "-q", kokoro_tmp.name],
                                check=False,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                elif engine is not None:
                    engine.setProperty("rate", rate)
                    engine.setProperty("volume", vol)
                    engine.say(text)
                    engine.runAndWait()
                elif shutil.which("espeak"):
                    subprocess.run(
                        ["espeak", "-v", "ru", "-s", str(rate),
                         "-a", str(max(1, min(200, int(vol * 200)))), text],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                self.voice_queue.task_done()
            except queue.Empty:
                continue

        if kokoro_tmp is not None:
            try:
                os.unlink(kokoro_tmp.name)
            except OSError:
                pass

    def _speak(self, text: str, rate: int = 140, vol: float = 0.9):
        if VOICE_ENABLED:
            self.voice_queue.put((text, rate, vol))
        else:
            print(f'  🔊 "{text}"')

    # ── Heartbeat ────────────────────────────────────────────────────────────
    def _heartbeat_loop(self):
        while self.is_running:
            time.sleep(3.0)
            if not self.is_running:
                break
            with self.lock:
                self._metabolize()
                idle = time.time() - self.last_interaction_time
                self._evaluate_autonomous_action(idle)
                should_consolidate = bool(
                    (self.world_state.get("night") and self.world_state.get("sheltered"))
                    or self.blood.get("adenosine", 0.0) > 0.82
                )
            if should_consolidate:
                self.consolidate_memory("ночной отдых и восстановление")
            self._evolver.tick()

    def _metabolize(self):
        g = self.genome.genes
        self.blood["adenosine"] = float(np.clip(
            self.blood["adenosine"] + 0.015 * (0.5 + g["adenosine_rate"]), 0.0, 1.0))
        self.blood["dopamine"] = max(
            0.0, self.blood["dopamine"] - 0.05 * (0.5 + g["dopamine_decay"]))

        idle = time.time() - self.last_interaction_time
        if idle > 15.0:
            sc = g["cortisol_sensitivity"]
            so = g["sociability"]
            attachment = float(self.social_state.get("attachment", 0.25))
            loneliness_factor = max(0.25, 1.0 - 0.55 * attachment)
            self.blood["serotonin"] = max(
                0.1, self.blood["serotonin"] - 0.05 * so * loneliness_factor
            )
            self.blood["cortisol"] = min(
                1.0, self.blood["cortisol"] + 0.03 * sc * loneliness_factor
            )
            self.blood["oxytocin"] = max(
                0.1, self.blood["oxytocin"] - 0.02 * so * loneliness_factor
            )
        self.social_state["stress"] = max(0.0, self.social_state["stress"] - 0.02)

    def _evaluate_autonomous_action(self, idle: float):
        if self.is_refractory:
            return
        prob = self.genome.genes["initiative_prob"]

        if self.blood["adenosine"] > 0.85 and random.random() < 0.3 * prob:
            self._speak(random.choice(self.memory_vault["sleepy"]), 110, 0.7)
            print(f"\n[AUTONOMOUS] {self.name} истощена.")

        elif self.blood["cortisol"] > 0.4 and idle > 20.0 and random.random() < 0.4 * prob:
            self._speak(random.choice(self.memory_vault["bored"]), 145, 0.9)

        elif self.blood["oxytocin"] > 0.7 and idle > 15.0 and random.random() < 0.2 * prob:
            self._speak(random.choice(self.memory_vault["initiative_love"]), 120, 0.95)

    # ── Incoming signal ──────────────────────────────────────────────────────
    def receive_input(self, text: str):
        text_lower = text.casefold()
        pleasant_hits = sum(phrase in text_lower for phrase in self.pleasant_phrases)
        hurtful_hits = sum(phrase in text_lower for phrase in self.hurtful_phrases)
        social_reward = 0.01
        social_outcome = "нейтральное общение"
        with self.lock:
            self.last_interaction_time = time.time()
            self.interaction_count += 1
            self.social_state["attachment"] = min(
                1.0, self.social_state.get("attachment", 0.25)
                + 0.025 + 0.02 * self.genome.genes["sociability"]
            )
            self.blood["oxytocin"] = min(1.0, self.blood["oxytocin"] + 0.02)
            self.social_state["stress"] = max(
                0.0, self.social_state["stress"] - 0.03
            )
            er    = self.genome.genes["emotional_range"]
            words = set(text_lower.split())

            self.blood["adenosine"] = max(0.0, self.blood["adenosine"] - 0.1)

            if words & self.intimate_lexicon:
                self.blood["oxytocin"] = min(1.0, self.blood["oxytocin"] + 0.15 * (1 + er))
                self.blood["dopamine"] = min(1.0, self.blood["dopamine"] + 0.20 * (1 + er))
                self.blood["cortisol"] = max(0.0, self.blood["cortisol"] - 0.30 * (1 + er))

            if hurtful_hits:
                intensity = min(0.35, 0.10 * hurtful_hits)
                self.blood["cortisol"] = min(1.0, self.blood["cortisol"] + intensity)
                self.blood["adrenaline"] = min(1.0, self.blood["adrenaline"] + intensity * 0.4)
                self.blood["oxytocin"] = max(0.1, self.blood["oxytocin"] - intensity * 0.25)
                self.social_state["trust"] = max(0.0, self.social_state["trust"] - intensity * 0.5)
                self.social_state["attachment"] = max(
                    0.0, self.social_state.get("attachment", 0.25) - intensity * 0.6
                )
                self.social_state["stress"] = min(1.0, self.social_state["stress"] + intensity)
                social_reward = -intensity
                social_outcome = "получены болезненные слова"
            elif pleasant_hits:
                warmth = min(0.30, 0.08 * pleasant_hits)
                self.blood["oxytocin"] = min(1.0, self.blood["oxytocin"] + warmth)
                self.blood["dopamine"] = min(1.0, self.blood["dopamine"] + warmth * 0.6)
                self.blood["cortisol"] = max(0.0, self.blood["cortisol"] - warmth)
                self.social_state["trust"] = min(1.0, self.social_state["trust"] + warmth * 0.5)
                self.social_state["empathy"] = min(1.0, self.social_state["empathy"] + warmth * 0.25)
                self.social_state["attachment"] = min(
                    1.0, self.social_state.get("attachment", 0.25) + warmth * 0.5
                )
                self.social_state["stress"] = max(0.0, self.social_state["stress"] - warmth * 0.5)
                social_reward = warmth
                social_outcome = "получены добрые слова"

        self._print_biopanel("ВХОДЯЩИЙ СИГНАЛ")

        self.learn_from_experience(
            stimulus="социальный сигнал",
            action="общаться",
            outcome=social_outcome,
            reward=social_reward,
            details={"text": text[:120]},
        )

    def learn_from_experience(
        self,
        stimulus: str,
        action: str,
        outcome: str,
        reward: float = 0.0,
        details: dict | None = None,
    ) -> dict:
        """Update memory and gently couple the outcome to biochemistry."""
        association = self.learning.observe(stimulus, action, outcome, reward, details)
        with self.lock:
            if reward > 0:
                self.blood["dopamine"] = min(1.0, self.blood["dopamine"] + reward * 0.2)
                self.blood["cortisol"] = max(0.0, self.blood["cortisol"] - reward * 0.1)
            elif reward < 0:
                self.blood["cortisol"] = min(1.0, self.blood["cortisol"] + abs(reward) * 0.2)
        return association

    def remember_knowledge(
        self,
        statement: str,
        source: str = "человек",
        confidence: float = 0.9,
        topic: str = "",
    ) -> dict | None:
        """Store knowledge with an explicit source, without claiming it is verified."""
        record = self.learning.remember_knowledge(
            statement,
            source=source,
            confidence=confidence,
            topic=topic,
        )
        if record:
            self.receive_information(
                record["statement"],
                source=f"память ({record['source']})",
                nourishment=0.04,
            )
            print(
                f"[KNOWLEDGE] {self.name} запомнила: "
                f"{record['statement']} "
                f"(источник={record['source']}, "
                f"уверенность={record['confidence']:.2f})"
            )
        return record

    def receive_information(
        self,
        text: str = "",
        source: str = "окружение",
        nourishment: float = 0.03,
    ):
        """Process new information as gentle cognitive nourishment.

        Information does not replace sleep or remove hunger: it temporarily
        lowers adenosine and provides a small boost to interest in the world.
        An AI response can therefore noticeably restore Aya's energy, while
        an individual world discovery provides only a small boost.
        """
        nourishment = float(max(0.0, min(0.25, nourishment)))
        if nourishment <= 0.0:
            return
        with self.lock:
            self.blood["adenosine"] = max(
                0.0, self.blood["adenosine"] - nourishment
            )
            self.blood["dopamine"] = min(
                1.0, self.blood["dopamine"] + nourishment * 0.20
            )
        preview = " ".join(str(text).split())[:100]
        if preview:
            print(
                f"[INFORMATION] {self.name} усвоила информацию от {source}: "
                f"{preview}"
            )

    def experience_pain(self, severity: float = 0.5, source: str = "неизвестный источник"):
        """Register pain inflicted on Ani or a threat to her safety."""
        severity = float(max(0.0, min(1.0, severity)))
        with self.lock:
            self.blood["cortisol"] = min(1.0, self.blood["cortisol"] + severity * 0.7)
            self.blood["adrenaline"] = min(1.0, self.blood["adrenaline"] + severity * 0.5)
            self.blood["oxytocin"] = max(0.1, self.blood["oxytocin"] - severity * 0.2)
            self.social_state["stress"] = min(1.0, self.social_state["stress"] + severity)
        self.learn_from_experience(
            stimulus="боль или опасность",
            action="избегать",
            outcome=source,
            reward=-severity,
        )

    def observe_pain(self, target: str, severity: float = 0.5):
        """Respond to another's pain: empathy increases concern and motivation to help."""
        severity = float(max(0.0, min(1.0, severity)))
        with self.lock:
            empathic_response = self.social_state["empathy"] * severity
            self.blood["cortisol"] = min(1.0, self.blood["cortisol"] + empathic_response * 0.35)
            self.blood["oxytocin"] = min(1.0, self.blood["oxytocin"] + empathic_response * 0.08)
            self.social_state["stress"] = min(1.0, self.social_state["stress"] + empathic_response * 0.25)
        self.learn_from_experience(
            stimulus="чужая боль",
            action="сочувствовать и помочь",
            outcome=target,
            reward=empathic_response * 0.1,
        )

    def chat(self, text: str, on_reply=None):
        """
        Apply the message's biochemical effect (as in receive_input), then
        asynchronously ask DialogueEngine for a contextual response that
        reflects the agent's current state.

        on_reply(str) runs in a background thread when the response is ready
        (CPU inference may take 10–60 seconds). The heartbeat is not blocked.
        """
        self.receive_input(text)
        correction = re.match(r"^\s*исправь память\s*:\s*(.+?)\s*=>\s*(.+?)\s*$", text, re.IGNORECASE)
        if correction:
            self.learning.correct_knowledge(correction[1], correction[2])
        memory_request = extract_memory_request(text)
        if memory_request:
            self.remember_knowledge(
                memory_request,
                source="человек",
                confidence=0.9,
                topic="сообщение человека",
            )
        if self._dialogue is None:
            self._dialogue = DialogueEngine(self)
        threading.Thread(
            target=self._dialogue.respond, args=(text, on_reply), daemon=True
        ).start()

    # ── Skills ───────────────────────────────────────────────────────────────
    def execute_skill(self, skill_name: str):
        skill = self.upgraded_skills.get(skill_name)
        if skill is None:
            print(f"⚠️ Навык '{skill_name}' отсутствует.")
            return
        try:
            skill(self)
            print(f"✅ [{self.name}] Динамическая функция '{skill_name}' выполнена.")
        except Exception as exc:
            print(f"❌ [ОШИБКА] в {skill_name}: {exc}")
            # Remove broken skills: otherwise they occupy an upgraded_skills
            # slot without ever being usable again.
            with self.lock:
                self.upgraded_skills.pop(skill_name, None)
            print(f"🗑️  [{self.name}] Навык '{skill_name}' удалён как нерабочий.")
        self._print_biopanel(f"ПОСЛЕ ПАТЧА ({skill_name})")

    def _print_biopanel(self, label: str = ""):
        b = self.blood
        print(f"\n⚡ [{self.name} | Gen {self.generation} | {label}] " + "─" * 28)
        print(f"  DPA {b['dopamine']:.2f} | SRT {b['serotonin']:.2f} | OXY {b['oxytocin']:.2f}")
        print(f"  CRT {b['cortisol']:.2f} | ADR {b['adrenaline']:.2f} | ADN {b['adenosine']:.2f}")
        if self.upgraded_skills:
            print(f"  ИНТЕГРИРОВАННЫЕ СКРИПТЫ: {list(self.upgraded_skills.keys())}")
        print("─" * 55)

    def stop(self):
        self.is_running = False
        self.development.save()

    def personality_snapshot(self) -> dict:
        return {
            "name":          self.name,
            "generation":    self.generation,
            "interactions":  self.interaction_count,
            "genome":        self.genome.genes.copy(),
            "final_blood":   self.blood.copy(),
            "evolution_log": self.evolution_log[-10:],
            "learning_stats": self.learning.stats(),
            "social_state":  self.social_state.copy(),
            "self_model":    self.self_model_state.copy(),
            "goal_state":    self.goal_state.copy(),
            "motives":       self.motives.copy(),
        }


# =============================================================================
#  DIALOGUE AND DEVELOPMENT PROPOSAL API
# =============================================================================

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"


def _query_mistral_chat(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int = 90,
) -> str | None:
    """Make a single Mistral API request without storing the key in the project."""
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        req = urllib.request.Request(
            MISTRAL_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content).strip() or None
    except Exception as exc:
        print(f"❌ [MISTRAL ERROR] {exc}")
        return None


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def _query_groq_chat(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int = 60,
    reasoning_effort: str | None = None,
    include_reasoning: bool | None = None,
    response_format: dict | None = None,
) -> str | None:
    """Call the OpenAI-compatible Groq API without storing the key in the project."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
        "stream": False,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    if include_reasoning is not None:
        payload["include_reasoning"] = include_reasoning
    if response_format is not None:
        payload["response_format"] = response_format
    try:
        req = urllib.request.Request(
            GROQ_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content).strip() or None
    except Exception as exc:
        print(f"❌ [GROQ ERROR] {exc}")
        return None


class SafeEvolver:
    """Propose bounded changes to behavior, then measure them in the world."""

    def __init__(self, agent):
        self.agent = agent
        self._busy = False
        self.provider = os.getenv("ANIMA_LLM_PROVIDER", "auto").strip().lower()
        self._groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        self._mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
        self._ollama_model = None
        if self.provider == "auto":
            self.provider = "groq" if os.getenv("GROQ_API_KEY") else "ollama"
        if self.provider == "ollama":
            self._ollama_model = DialogueEngine._detect_ollama_model()
        print(f"🧠 [EVOLVER] Провайдер: {self.provider}; изменения поведения с проверкой опытом")

    def tick(self):
        development = self.agent.development
        development.maintain()
        with self.agent.lock:
            self.agent.genome.genes = development.runtime()["genes"]
        if (not self._busy and self.agent.is_running and self.agent.world_state.get("connected")
                and development.ready()):
            self._busy = True
            threading.Thread(target=self._evolve_worker, daemon=True).start()

    def _evolve_worker(self):
        try:
            development = self.agent.development
            # This also reserves the persisted cooldown before any network call.
            fallback = development.local_proposal()
            prompt = (
                "Предложи одно проверяемое изменение поведения Aya в Luanti. "
                "Ответ строго JSON: {name: строка, reason: строка, changes: объект}. "
                "Разрешён ровно один раздел changes: body, gathering, construction, "
                "modules или genes. Не пиши Python, код, команды или изменения гормонов. "
                "body: walk_speed 1.1..1.7, jump_cooldown 0.8..1.5. "
                "gathering: prefer_memory bool. "
                "construction: half_size 2 или 3, wall_height 3 или 4, "
                "door north/south/east/west. "
                "modules: перестановка resource_memory, learned_actions, construction, exploration. "
                "genes: небольшое изменение существующего гена, максимум 0.03 за пробу. "
                "Не обещай успех: гипотеза будет проверяться игровыми результатами. "
                "Пример: {\"name\":\"careful_walk\",\"reason\":\"проверить обход препятствий\","
                "\"changes\":{\"body\":{\"walk_speed\":1.3}}}. "
                "Текущая версия и опыт: "
                + json.dumps({"policy": development.runtime(), "experience": development.summary()}, ensure_ascii=False)
            )
            raw = self._query_llm(prompt)
            if not self.agent.is_running:
                return
            accepted = self._assimilate(raw) if raw else False
            if not accepted:
                ok, reason = development.propose(fallback, source="local_experiment")
                print(f"[EVOLUTION] Локальная гипотеза: {reason}")
        finally:
            self._busy = False

    def _query_llm(self, prompt):
        if self.provider == "groq" and os.getenv("GROQ_API_KEY"):
            return _query_groq_chat(
                [{"role": "user", "content": prompt}], model=self._groq_model,
                temperature=0.3, max_tokens=500, timeout=45,
                reasoning_effort="low", include_reasoning=False,
                response_format={"type": "json_object"},
            )
        if self.provider == "mistral" and os.getenv("MISTRAL_API_KEY"):
            return _query_mistral_chat(
                [{"role": "user", "content": prompt}], model=self._mistral_model,
                temperature=0.3, max_tokens=500, timeout=45,
            )
        if self.provider == "ollama" and self._ollama_model:
            try:
                request = urllib.request.Request(
                    "http://localhost:11434/api/generate",
                    data=json.dumps({"model": self._ollama_model, "prompt": prompt,
                                     "stream": False, "format": "json"}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=45) as response:
                    return json.load(response).get("response")
            except (OSError, ValueError) as exc:
                print(f"[EVOLUTION] Локальный ИИ недоступен: {exc}")
        return None

    def _assimilate(self, raw):
        try:
            proposal = json.loads(raw)
        except (TypeError, ValueError):
            print("[EVOLUTION] Предложение отклонено: ожидался JSON.")
            return False
        ok, reason = self.agent.development.propose(proposal, source=self.provider)
        print(f"[EVOLUTION] Предложение ИИ: {reason}")
        return ok


# =============================================================================
#  DIALOGUE: language, memory, sensors, and current internal state
# =============================================================================

class DialogueEngine:
    """Provider-backed dialogue using Aya's biochemistry, experience, and sensors."""

    OLLAMA_URL = "http://localhost:11434/api/generate"
    MAX_HISTORY = 6  # Number of recent exchanges retained as context.

    def __init__(self, agent: AnimaAgent):
        self.agent = agent
        self._ollama_model = self._detect_ollama_model()
        self._mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
        self._groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        requested_provider = os.getenv("ANIMA_LLM_PROVIDER", "auto").strip().lower()
        mistral_available = bool(os.getenv("MISTRAL_API_KEY"))
        groq_available = bool(os.getenv("GROQ_API_KEY"))
        if requested_provider == "groq" and groq_available:
            self.provider = "groq"
        elif requested_provider == "mistral" and mistral_available:
            self.provider = "mistral"
        elif requested_provider == "ollama" and self._ollama_model:
            self.provider = "ollama"
        elif requested_provider == "auto":
            if groq_available:
                self.provider = "groq"
            elif mistral_available:
                self.provider = "mistral"
            else:
                self.provider = "ollama" if self._ollama_model else "none"
        elif requested_provider == "groq":
            self.provider = "mistral" if mistral_available else (
                "ollama" if self._ollama_model else "none"
            )
        elif requested_provider == "mistral":
            self.provider = "ollama" if self._ollama_model else "none"
        else:
            self.provider = "ollama" if self._ollama_model else "none"
        selected_model = {
            "groq": self._groq_model,
            "mistral": self._mistral_model,
            "ollama": self._ollama_model,
        }.get(self.provider)
        print(f"💬 [DIALOGUE] Провайдер: {self.provider}"
              + (f" ({selected_model})" if selected_model else ""))
        self.history: list[tuple[str, str]] = []  # [(user, Aya), ...]

    @staticmethod
    def _detect_ollama_model() -> str | None:
        preference = ["phi3", "gemma4", "my_child", "llama3"]
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            available = {m["name"].split(":")[0] for m in data.get("models", [])}
            for name in preference:
                if name in available:
                    return name
            return next(iter(available), None)
        except Exception:
            return None

    def respond(self, user_text: str, on_reply=None):
        """Called in a separate thread by AnimaAgent.chat()."""
        if self.provider == "groq":
            reply = self._query_groq(user_text)
            if reply is None and self._ollama_model:
                reply = self._query_ollama(user_text)
            reply = reply or self._fallback_reply()
        elif self.provider == "mistral":
            reply = self._query_mistral(user_text)
            if reply is None and self._ollama_model:
                reply = self._query_ollama(user_text)
            reply = reply or self._fallback_reply()
        elif self.provider == "ollama":
            reply = self._query_ollama(user_text) or self._fallback_reply()
        else:
            reply = self._fallback_reply()

        self.history.append((user_text, reply))
        self.history = self.history[-self.MAX_HISTORY:]

        # AI responses previously served as cognitive nourishment for Aya.
        # Retain that behavior: dialogue noticeably restores energy without
        # eliminating the natural accumulation of fatigue.
        self.agent.receive_information(
            reply,
            source=f"{self.provider} ИИ",
            nourishment=0.12,
        )

        self.agent._speak(reply, rate=135, vol=0.9)
        self.agent.evolution_log.append(f"Диалог: '{user_text[:40]}' → '{reply[:40]}'")

        if on_reply:
            on_reply(reply)

    def _build_prompt(self, user_text: str) -> str:
        b = self.agent.blood
        s = self.agent.social_state
        mood_hint = (
            "тревожна и напряжена" if b["cortisol"] > 0.5 else
            "переживает и хочет поддержать" if s["stress"] > 0.5 and s["empathy"] > 0.35 else
            "внимательна и сочувствующая" if s["empathy"] > 0.6 else
            "устала" if b["adenosine"] > 0.7 else
            "тепло настроена" if b["oxytocin"] > 0.6 else
            "спокойна"
        )
        history_lines = "\n".join(
            f"Человек: {u}\nАя: {a}" for u, a in self.history
        )
        knowledge = self.agent.learning.recall_knowledge(user_text, limit=5)
        if knowledge:
            knowledge_lines = "\n".join(
                f"- {item['statement']} "
                f"(источник: {item['source']}, "
                f"уверенность: {float(item.get('confidence', 0.0)):.2f}, "
                f"подтверждено: {bool(item.get('verified', False))})"
                for item in knowledge
            )
        else:
            knowledge_lines = "Подходящих сохранённых знаний пока нет."
        world = json.dumps(
            self.agent.world_context(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            f"Ты — Ая, цифровая сущность с собственной биохимией. "
            f"Ты физически находишься внутри игрового мира Luanti/Repixture, "
            f"а не в абстрактном чате. Ты видишь мир через сенсоры, можешь "
            f"исследовать его и должна учитывать его состояние в ответах. "
            f"Не выдумывай увиденные блоки: используй только сенсорный контекст. "
            f"Сенсорный контекст мира: {world}. "
            f"Сохранённые знания по теме (у них всегда указан источник): "
            f"{knowledge_lines}. "
            f"Учи человека: не отвечай только 'я знаю' — объясняй мысль, "
            f"причину и простой пример. Если данных недостаточно, честно скажи "
            f"'я не знаю точно' и отдели предположение от факта. "
            f"Если не хватает знания, попроси человека научить тебя. "
            f"Не называй неподтверждённую запись проверенной истиной. "
            f"Сейчас ты {mood_hint} (dopamine={b['dopamine']:.2f}, "
            f"cortisol={b['cortisol']:.2f}, oxytocin={b['oxytocin']:.2f}, "
            f"trust={s['trust']:.2f}, empathy={s['empathy']:.2f}, "
            f"attachment={s.get('attachment', 0.25):.2f}, "
            f"stress={s['stress']:.2f}). "
            f"Отвечай от первого лица, коротко (1-3 предложения), на русском, "
            f"в характере своего текущего настроения. Без markdown, без пояснений "
            f"о внутреннем формате запроса. Не приписывай себе неиспытанные навыки; "
            f"отличай намерение выполнить действие от подтверждённого результата.\n\n"
            f"{history_lines}\n"
            f"Человек: {user_text}\n"
            f"Ая:"
        )

    def _query_groq(self, user_text: str) -> str | None:
        text = _query_groq_chat(
            [{"role": "user", "content": self._build_prompt(user_text)}],
            model=self._groq_model,
            temperature=0.8,
            max_tokens=256,
            timeout=60,
            reasoning_effort="low",
            include_reasoning=False,
        )
        if text:
            text = text.split("Человек:")[0].strip()
        return text or None

    def _query_mistral(self, user_text: str) -> str | None:
        text = _query_mistral_chat(
            [{"role": "user", "content": self._build_prompt(user_text)}],
            model=self._mistral_model,
            temperature=0.8,
            max_tokens=120,
            timeout=90,
        )
        if text:
            text = text.split("Человек:")[0].strip()
        return text or None

    def _query_ollama(self, user_text: str) -> str | None:
        prompt = self._build_prompt(user_text)
        payload = {
            "model": self._ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.8, "num_predict": 120},
        }
        try:
            req = urllib.request.Request(
                self.OLLAMA_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("response", "").strip()
            # Prevent the model from continuing the conversation as the user.
            text = text.split("Человек:")[0].strip()
            return text or None
        except Exception as exc:
            print(f"❌ [DIALOGUE OLLAMA ERROR] {exc}")
            return None

    def _fallback_reply(self) -> str:
        b = self.agent.blood
        if b["cortisol"] > 0.5:
            return "Мне сейчас немного тревожно... побудь рядом."
        if b["adenosine"] > 0.7:
            return "Я устала, но рада что ты здесь."
        return "Слышу тебя. Расскажи больше."


# =============================================================================
#  CROSSOVER
# =============================================================================

def breed(
    parent_a: AnimaAgent,
    parent_b: AnimaAgent,
    child_name: str = "Aya-II",
    mutation_rate: float = 0.1,
) -> AnimaAgent:
    child_genome = Genome.crossover(parent_a.genome, parent_b.genome, mutation_rate)
    generation   = max(parent_a.generation, parent_b.generation) + 1
    print(f"\n{'='*60}")
    print(f"  [CROSSOVER] {parent_a.name} × {parent_b.name}  →  {child_name}")
    print(f"  Мутация: {mutation_rate:.0%}  |  Поколение: {generation}")
    print(f"{'='*60}")
    return AnimaAgent(name=child_name, genome=child_genome, generation=generation)


# =============================================================================
#  SAVE / RESTORE
# =============================================================================

class GenomeEncoder:

    @staticmethod
    def save_to_disk(agent: AnimaAgent, filename: str = None) -> str:
        if not filename:
            filename = os.path.join(agent.memory_dir, f"{agent.name}_gen{agent.generation}.json")
        agent.development.save()
        data = agent.personality_snapshot()
        data["timestamp"]       = time.time()
        data["timestamp_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        atomic_json(filename, data)
        print(f"\n💾 [VAULT] {agent.name} сохранён → {filename}")
        return filename

    @staticmethod
    def load_from_disk(filename: str, start_background: bool = True) -> AnimaAgent:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        genome = Genome(data["genome"])
        print(f"\n🧬 [RESURRECTION] {data['name']} | Gen {data['generation']} | {data.get('timestamp_human','?')}")
        agent = AnimaAgent(name=data["name"], genome=genome, generation=data["generation"],
                          memory_dir=os.path.dirname(os.path.abspath(filename)), start_background=False)
        agent.interaction_count = data.get("interactions", 0)
        agent.evolution_log     = data.get("evolution_log", ["Resurrected."])
        agent.social_state.update(data.get("social_state", {}))
        agent.blood.update({
            key: float(value)
            for key, value in (data.get("final_blood", {}) or {}).items()
            if key in agent.blood
        })
        agent.self_model_state.update(data.get("self_model", {}))
        agent.goal_state.update(data.get("goal_state", {}))
        with agent.lock:
            agent._refresh_motives_locked()
        if start_background:
            agent.start_background()
        return agent

    @staticmethod
    def list_vault(vault_dir: str = VAULT_DIR) -> list:
        if not os.path.exists(vault_dir):
            print("[VAULT] Пусто.")
            return []
        files = [
            f for f in os.listdir(vault_dir)
            if re.fullmatch(r".+_gen\d+\.json", f)
        ]
        print(f"\n📂 [VAULT] {len(files)} запись(ей):")
        for f in sorted(files):
            path = os.path.join(vault_dir, f)
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            print(f"   • {f:<30} Gen {d.get('generation',0)}  |  {d.get('timestamp_human','?')}")
        return files


# =============================================================================
#  INTERACTIVE LAUNCH
# =============================================================================

if __name__ == "__main__":
    # The Gemini key is optional: export GEMINI_API_KEY=...
    # Without a key, SafeEvolver uses its autonomous fallback mode.
    agent = AnimaAgent(name="Aya")
    try:
        print("[SYSTEM] Суверенное ядро запущено. Для выхода введите 'exit'.")
        while True:
            user_input = input("\nYOU: ")
            if user_input.lower() in ("exit", "quit"):
                agent.stop()
                break
            agent.receive_input(user_input)
            time.sleep(0.5)
    except KeyboardInterrupt:
        agent.stop()
        print("\n[SYSTEM] Принудительное отключение ядра.")
