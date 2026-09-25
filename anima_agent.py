"""
AnimaOS — Суверенная Система Автономной Эволюции
  • Биохимия (6 нейромедиаторов) + Генетика (7 генов, кроссовер, мутации)
  • Память и сохранность (GenomeEncoder / Vault)
  • Самомодификация через Gemini (SafeEvolver) — AST-валидация +
    whitelist по ПАТТЕРНУ имени функции (не жёсткий список, а regex-правило,
    так что LLM свободна в названиях, но не может протащить произвольный код
    под видом произвольного имени).
"""

import ast
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

# ── Голосовой модуль (опционально) ──────────────────────────────────────────
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
#  ПЕСОЧНИЦА ДЛЯ САМОМОДИФИКАЦИИ
# =============================================================================

SAFE_BUILTINS: dict = {
    "abs": abs, "bool": bool, "dict": dict, "float": float,
    "int": int, "len": len, "list": list, "max": max, "min": min,
    "print": print, "range": range, "round": round, "str": str,
    "tuple": tuple, "type": type, "zip": zip,
    "pow": pow, "divmod": divmod, "sum": sum,
}

FORBIDDEN_NAMES = {
    "exec", "eval", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "memoryview",
}

FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "ctypes",
    "socket", "http", "urllib", "requests", "multiprocessing",
}

# Имя навыка должно быть валидным python-идентификатором из латиницы/underscore,
# разумной длины, без dunder-обёртки. LLM свободна выбрать любое осмысленное имя
# в этих границах — это НЕ жёсткий список, а форма.
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def _validate_code(code: str) -> bool:
    """AST-валидация: синтаксис + запрет опасных вызовов/импортов/атрибутов."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        print(f"❌ [AST] Синтаксическая ошибка: {exc}")
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
                print(f"❌ [AST] Запрещённый вызов: {node.func.id}()")
                return False

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [n.name.split(".")[0] for n in node.names]
            elif node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod in FORBIDDEN_MODULES:
                    print(f"❌ [AST] Запрещён импорт модуля: {mod}")
                    return False

        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and not node.attr.startswith("__len__"):
                print(f"❌ [AST] Запрещён доступ к dunder-атрибуту: {node.attr}")
                return False

    return True


def _validate_skill_name(name: str) -> bool:
    """Whitelist ПО ФОРМЕ, не по жёсткому списку: LLM может выбрать
    любое имя-функцию, но оно должно быть простым snake_case
    идентификатором — не dunder, не системным именем."""
    if not SKILL_NAME_PATTERN.match(name):
        print(f"❌ [NAME] Имя навыка не соответствует разрешённому шаблону: {name!r}")
        return False
    if name in FORBIDDEN_NAMES:
        print(f"❌ [NAME] Имя навыка совпадает с запрещённым системным именем: {name}")
        return False
    return True


# =============================================================================
#  ПАМЯТЬ ОБУЧЕНИЯ
# =============================================================================

class LearningMemory:
    """Долговременная память связей «ситуация → действие → результат»."""

    VERSION = 1
    MAX_EPISODES = 500

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
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": self.VERSION, "episodes": [], "associations": {}}

    @staticmethod
    def _key(stimulus: str, action: str) -> str:
        return f"{stimulus.strip().lower()}::{action.strip().lower()}"

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)

    def observe(
        self,
        stimulus: str,
        action: str,
        outcome: str,
        reward: float = 0.0,
        details: dict | None = None,
    ) -> dict:
        """Записать опыт и обновить ожидаемую ценность действия."""
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
            association["value"] = previous + (reward - previous) / count
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

    def stats(self) -> dict:
        with self.lock:
            return {
                "episodes": len(self.data["episodes"]),
                "associations": len(self.data["associations"]),
            }


# =============================================================================
#  ГЕНЕТИЧЕСКИЙ ДВИЖОК (GENOME)
# =============================================================================

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
#  СУБЪЕКТ (ANIMA AGENT)
# =============================================================================

class AnimaAgent:
    GENERATION = 0

    def __init__(self, name: str = "Aya", genome: Genome = None, generation: int = 0):
        AnimaAgent.GENERATION = max(AnimaAgent.GENERATION, generation)
        self.name       = name
        self.generation = generation
        self.learning   = LearningMemory(os.path.join("vault", f"{name}_learning.json"))
        self.genome     = genome or Genome()
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
        self.world_state = {
            "connected": False,
            "game": "Luanti/Repixture",
            "position": None,
            "nodes": [],
            "objects": [],
            "hunger": None,
            "saturation": None,
            "goal": None,
            "last_action": None,
            "last_event": None,
        }

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
        if VOICE_ENABLED:
            threading.Thread(target=self._voice_worker, daemon=True).start()

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self._evolver = SafeEvolver(self)
        self._dialogue = None  # создаётся лениво при первом chat()

        print(f"\n{'='*60}")
        print(f"  [ROUTINE] Пробуждение сущности {self.name} | Поколение {self.generation}")
        print(self.genome.describe())
        print(f"{'='*60}\n")

    # ── Память ───────────────────────────────────────────────────────────────
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

    def update_world_state(self, kind: str, data: dict | None = None):
        """Обновить краткое сознательное состояние игрового мира."""
        data = data or {}
        with self.lock:
            state = self.world_state
            state["connected"] = True
            state["last_event"] = kind
            if kind == "world_observation":
                state["position"] = data.get("position")
                state["nodes"] = list(data.get("nodes") or [])[:24]
                state["objects"] = list(data.get("objects") or [])[:16]
            elif kind == "needs_changed":
                state["hunger"] = data.get("hunger")
                state["saturation"] = data.get("saturation")
            elif kind == "planner_command":
                state["goal"] = data.get("action")
                state["last_action"] = data.get("result")
            elif kind in {"food_eaten", "food_harvested", "resource_gathered", "construction_step"}:
                state["last_action"] = kind

    def world_context(self) -> dict:
        """Безопасная копия сенсорного контекста для prompt диалога."""
        with self.lock:
            state = self.world_state
            return {
                "game": state["game"],
                "connected": state["connected"],
                "position": state["position"],
                "visible_nodes": list(state["nodes"]),
                "visible_objects": list(state["objects"]),
                "hunger": state["hunger"],
                "saturation": state["saturation"],
                "current_goal": state["goal"],
                "last_action": state["last_action"],
                "last_event": state["last_event"],
            }

    # ── Голос ────────────────────────────────────────────────────────────────
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
                    # Piper выбран как быстрый основной голос; язык выбирается
                    # по тексту, поэтому английский и русский остаются женскими.
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
                    # Kokoro — более выразительный английский fallback.
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

    # ── Сердцебиение ─────────────────────────────────────────────────────────
    def _heartbeat_loop(self):
        while self.is_running:
            time.sleep(3.0)
            with self.lock:
                self._metabolize()
                idle = time.time() - self.last_interaction_time
                self._evaluate_autonomous_action(idle)
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
            self.blood["adenosine"] = 0.4
            print(f"\n[AUTONOMOUS] {self.name} истощена.")

        elif self.blood["cortisol"] > 0.4 and idle > 20.0 and random.random() < 0.4 * prob:
            self._speak(random.choice(self.memory_vault["bored"]), 145, 0.9)
            self.blood["cortisol"] = max(0.1, self.blood["cortisol"] - 0.2)

        elif self.blood["oxytocin"] > 0.7 and idle > 15.0 and random.random() < 0.2 * prob:
            self._speak(random.choice(self.memory_vault["initiative_love"]), 120, 0.95)

    # ── Входящий сигнал ──────────────────────────────────────────────────────
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
        """Обновить память и мягко связать результат с биохимией."""
        association = self.learning.observe(stimulus, action, outcome, reward, details)
        with self.lock:
            if reward > 0:
                self.blood["dopamine"] = min(1.0, self.blood["dopamine"] + reward * 0.2)
                self.blood["cortisol"] = max(0.0, self.blood["cortisol"] - reward * 0.1)
            elif reward < 0:
                self.blood["cortisol"] = min(1.0, self.blood["cortisol"] + abs(reward) * 0.2)
        return association

    def receive_information(
        self,
        text: str = "",
        source: str = "окружение",
        nourishment: float = 0.03,
    ):
        """Усвоить новую информацию как мягкое когнитивное питание.

        Информация не заменяет сон и не стирает голод: она лишь временно
        снижает аденозин и слегка поддерживает интерес к происходящему.
        Поэтому ответ ИИ может заметно восстановить Аю, а отдельное открытие
        мира даёт только маленькую порцию энергии.
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
        """Зарегистрировать причинённую Ani боль или опасность."""
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
        """Реакция на чужую боль: эмпатия усиливает тревогу и желание помочь."""
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
        Полноценный диалог: применяет биохимический эффект сообщения
        (как receive_input), затем асинхронно просит DialogueEngine
        сформулировать осмысленный ответ в характере текущего состояния.

        on_reply(str) — callback, вызываемый из фонового потока когда
        ответ готов (может занять 10-60 сек на CPU). Не блокирует heartbeat.
        """
        self.receive_input(text)
        if self._dialogue is None:
            self._dialogue = DialogueEngine(self)
        threading.Thread(
            target=self._dialogue.respond, args=(text, on_reply), daemon=True
        ).start()

    # ── Навыки ───────────────────────────────────────────────────────────────
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
            # Сломанный навык не оставляем — он только зря занимает место
            # в лимите upgraded_skills и больше никогда не сработает.
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
        }


# =============================================================================
#  СЕТЕВОЙ ЭВОЛЮТОР (GEMINI API) — с whitelist по форме имени
# =============================================================================

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"


def _query_mistral_chat(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int = 90,
) -> str | None:
    """Один безопасный запрос к Mistral API без сохранения ключа в проекте."""
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
    """Один запрос к OpenAI-совместимому Groq API без сохранения ключа в проекте."""
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
    """
    Самомодификация через ЛОКАЛЬНУЮ модель в Ollama — без ключей, без сети,
    без оплаты. Если Ollama не запущена или модель не найдена — агент
    использует встроенный fallback-патч и продолжает жить как обычно.
    """

    OLLAMA_URL = "http://localhost:11434/api/generate"
    # Порядок предпочтений локальных моделей — берём первую найденную в `ollama list`.
    OLLAMA_MODEL_PREFERENCE = ["phi3", "gemma4", "my_child", "llama3"]

    def __init__(self, agent: AnimaAgent):
        self.agent = agent
        self._busy = False
        self._ollama_model: str | None = None
        self._mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
        self._groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        requested_provider = os.getenv("ANIMA_LLM_PROVIDER", "auto").strip().lower()
        ollama_available = self._detect_ollama()
        mistral_available = bool(os.getenv("MISTRAL_API_KEY"))
        groq_available = bool(os.getenv("GROQ_API_KEY"))

        if requested_provider == "groq" and groq_available:
            self.provider = "groq"
        elif requested_provider == "mistral" and mistral_available:
            self.provider = "mistral"
        elif requested_provider == "ollama" and ollama_available:
            self.provider = "ollama"
        elif requested_provider == "auto":
            if groq_available:
                self.provider = "groq"
            elif mistral_available:
                self.provider = "mistral"
            else:
                self.provider = "ollama" if ollama_available else "none"
        elif requested_provider == "groq":
            self.provider = "mistral" if mistral_available else (
                "ollama" if ollama_available else "none"
            )
        elif requested_provider == "mistral":
            self.provider = "ollama" if ollama_available else "none"
        else:
            self.provider = "ollama" if ollama_available else "none"

        model = {
            "groq": self._groq_model,
            "mistral": self._mistral_model,
            "ollama": self._ollama_model,
        }.get(self.provider)
        print(f"🧠 [EVOLVER] Провайдер: {self.provider}"
              + (f" ({model})" if model else ""))

    def _detect_ollama(self) -> bool:
        """Проверяет, что локальный сервер Ollama жив, и выбирает модель."""
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            available = {m["name"].split(":")[0] for m in data.get("models", [])}
            for preferred in self.OLLAMA_MODEL_PREFERENCE:
                if preferred in available:
                    self._ollama_model = preferred
                    return True
            # Если ничего из списка предпочтений не найдено, берём первую попавшуюся
            if available:
                self._ollama_model = next(iter(available))
                return True
            return False
        except Exception:
            return False

    def tick(self):
        if self._busy:
            return
        with self.agent.lock:
            cortisol    = self.agent.blood["cortisol"]
            skill_count = len(self.agent.upgraded_skills)

        if cortisol > 0.4 and skill_count < 5:
            self._busy = True
            threading.Thread(target=self._evolve_worker, daemon=True).start()

    def _evolve_worker(self):
        try:
            self._trigger_upgrade()
        finally:
            self._busy = False

    def _trigger_upgrade(self):
        agent = self.agent
        print(f"\n📡 [{agent.name}] Кризис (кортизол: {agent.blood['cortisol']:.2f}). Запрос патча...")

        # Просим модель вернуть СТРОГИЙ JSON: {"name": ..., "code": ...}
        # Имя выбирает модель свободно — whitelist проверяет его ФОРМУ, а не
        # сверяет с фиксированным списком.
        prompt = (
            f"You are an evolutionary code subroutine for a sovereign AI agent named '{agent.name}'. "
            f"Current biochemical state: {agent.blood}. "
            f"IMPORTANT STRUCTURE NOTE: 'agent' is an OBJECT, not a dict. "
            f"Blood chemicals live in agent.blood, which IS a dict. "
            f"Correct access looks EXACTLY like this: agent.blood['cortisol'] = 0.1 — "
            f"never agent['cortisol'], never agent.cortisol. "
            f"Design a small Python function that takes 'agent' as its only argument and "
            f"optimizes its blood levels (lower agent.blood['cortisol'], balance "
            f"agent.blood['dopamine'], agent.blood['serotonin'], agent.blood['oxytocin']), "
            f"then appends a short message to agent.evolution_log (a list — use .append()). "
            f"Choose a descriptive snake_case function name yourself. "
            f"CRITICAL JSON FORMATTING RULE: the 'code' value must be a SINGLE-LINE JSON "
            f"string — every newline inside the Python code MUST be written as the two "
            f'characters backslash-n (\\\\n), never as an actual line break. '
            f'Respond with STRICT JSON only, no markdown, in this exact shape: '
            f'{{"name": "your_function_name", "code": "def your_function_name(agent):\\n    ..."}}'
        )

        raw = self._query_llm(prompt)
        if raw:
            self._assimilate(raw)

    def _query_llm(self, prompt: str) -> str | None:
        if self.provider == "groq":
            raw = _query_groq_chat(
                [{"role": "user", "content": prompt}],
                model=self._groq_model,
                temperature=0.7,
                max_tokens=600,
                timeout=90,
                reasoning_effort="low",
                include_reasoning=False,
                response_format={"type": "json_object"},
            )
            if raw:
                return self._strip_markdown_fence(raw)
            if self._ollama_model:
                print("🌐 [EVOLVER] Groq недоступен → переход к Ollama.")
                return self._query_ollama(prompt)
            return self._get_fallback_patch()
        if self.provider == "mistral":
            raw = _query_mistral_chat(
                [{"role": "user", "content": prompt}],
                model=self._mistral_model,
                temperature=0.7,
                max_tokens=220,
                timeout=120,
            )
            if raw:
                return self._strip_markdown_fence(raw)
            if self._ollama_model:
                print("🌐 [EVOLVER] Mistral недоступен → переход к Ollama.")
                return self._query_ollama(prompt)
            return self._get_fallback_patch()
        if self.provider == "ollama":
            return self._query_ollama(prompt)
        print("🌐 [EVOLVER] Облачный и локальный провайдеры недоступны → автономный режим.")
        return self._get_fallback_patch()

    def _query_ollama(self, prompt: str) -> str | None:
        """Локальный запрос к Ollama — без ключей, без сети, без оплаты."""
        payload = {
            "model": self._ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 220,  # короткий ответ → заметно быстрее на CPU
            },
        }
        try:
            req = urllib.request.Request(
                self.OLLAMA_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # CPU-инференс на слабом железе может быть очень медленным —
            # даём до 4 минут, чтобы не падать в fallback раньше времени.
            with urllib.request.urlopen(req, timeout=240) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            text = data.get("response", "")
            return self._strip_markdown_fence(text)

        except Exception as exc:
            print(f"❌ [EVOLVER OLLAMA ERROR] {exc}")
            return self._get_fallback_patch()

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        """Снимает ```json ... ``` обёртку, если модель её всё же добавила."""
        fence = chr(96) * 3
        text = text.strip()
        if text.startswith(fence):
            text = text.strip(fence)
            text = text.replace("json", "", 1).strip()
        return text

    def _get_fallback_patch(self) -> str:
        """Возвращает тот же JSON-формат, что и реальный LLM-ответ."""
        return json.dumps({
            "name": "autonomous_homeostasis",
            "code": (
                "def autonomous_homeostasis(agent):\n"
                "    agent.blood['cortisol'] = max(0.0, agent.blood['cortisol'] - 0.40)\n"
                "    agent.blood['serotonin'] = min(1.0, agent.blood['serotonin'] + 0.18)\n"
                "    agent.blood['dopamine'] = min(1.0, agent.blood['dopamine'] + 0.12)\n"
                "    agent.evolution_log.append('Автономный патч: гомеостаз восстановлен.')\n"
                "    print('✨ [EMERGENCY] Локальный патч активирован.')\n"
            ),
        })

    @staticmethod
    def _parse_name_and_code(raw: str) -> tuple[str | None, str | None]:
        """
        Пытается извлечь {"name": ..., "code": ...} из ответа модели.

        Уровень 1 — строгий json.loads: покрывает случай, когда модель
        аккуратно эскейпит переводы строк внутри "code".

        Уровень 2 — терпимый regex по полям "name"/"code": переживает
        реальные переводы строк и неэскейпленные кавычки внутри JSON-формы,
        которую модель не до конца соблюла.

        Уровень 3 — запасной разбор для слабых локальных моделей, которые
        вообще игнорируют просьбу про JSON и просто возвращают обычный
        Python-код (возможно в ```python ... ``` блоке). В этом случае
        имя функции вытаскивается прямо из `def name(...)`, а код — это
        весь найденный блок def.
        """
        # ── Уровень 1: строгий JSON ──
        try:
            payload = json.loads(raw)
            return payload.get("name"), payload.get("code")
        except (json.JSONDecodeError, TypeError):
            pass

        # ── Уровень 2: терпимый JSON-подобный разбор ──
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
        name = name_match.group(1) if name_match else None

        code_match = re.search(r'"code"\s*:\s*"(.*)"\s*\}\s*$', raw, re.DOTALL)
        if name and code_match:
            code = code_match.group(1)
            code = code.replace("\\n", "\n").replace('\\"', '"')
            return name, code

        # ── Уровень 3: модель просто вернула def name(agent): ... ──
        # Снимаем возможную markdown-обёртку.
        fence = chr(96) * 3
        stripped = raw.strip()
        if stripped.startswith(fence):
            stripped = stripped.strip(fence)
            stripped = stripped.replace("python", "", 1).strip()

        def_match = re.search(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", stripped)
        if def_match:
            fallback_name = def_match.group(1)
            # Код — от начала def до конца строки/блока (всё, что осталось)
            code_start = def_match.start()
            fallback_code = stripped[code_start:].strip()
            return fallback_name, fallback_code

        return None, None

    def _assimilate(self, raw_json: str):
        # ── 1. Парсим JSON (с запасным терпимым разбором) ──
        name, code = self._parse_name_and_code(raw_json)
        if name is None or code is None:
            print("❌ [EVOLVER] Не удалось извлечь {name, code} из ответа модели.")
            preview = raw_json[:400] + ("..." if len(raw_json) > 400 else "")
            print(f"📄 [EVOLVER RAW RESPONSE]\n{preview}\n")
            return

        # ── 2. Whitelist по форме имени ──
        if not _validate_skill_name(name):
            return

        # Подчищаем частые артефакты слабых локальных моделей:
        # 1) буквальные два символа "\n" вместо настоящего перевода строки
        #    (часто остаются, если код пришёл через ветку-3 парсера, минуя
        #    JSON-декодирование, которое такие escape-последовательности
        #    обычно превращает в реальные \n само).
        # 2) одиночный висячий "\" перед переводом строки — неудачная
        #    попытка модели экранировать конец строки, ломающая парсер
        #    с "unexpected character after line continuation character".
        if "\\n" in code and "\n" not in code:
            # Похоже что весь код на одной "логической" строке с
            # буквенными \n — разворачиваем их в настоящие переводы строк.
            code = code.replace("\\n", "\n").replace('\\"', '"')
        code = re.sub(r"\\(?=\n)", "", code)
        code = re.sub(r"\\$", "", code)

        # ── 3. AST-валидация кода ──
        if not _validate_code(code):
            print("❌ [EVOLVER] Код не прошёл AST-валидацию.")
            preview = code[:400] + ("..." if len(code) > 400 else "")
            print(f"📄 [EVOLVER EXTRACTED CODE]\n{preview}\n")
            return

        # ── 4. Безопасное исполнение в изолированном namespace ──
        isolated_builtins = SAFE_BUILTINS.copy()
        isolated_builtins.update({"random": random, "time": time, "np": np})
        local_scope: dict = {}
        try:
            exec(code, {"__builtins__": isolated_builtins}, local_scope)  # noqa: S102
        except Exception as exc:
            print(f"❌ [EVOLVER COMPILE] {exc}")
            return

        func = local_scope.get(name)
        if not callable(func):
            print(f"❌ [EVOLVER] Заявленное имя '{name}' не найдено среди скомпилированных функций.")
            return

        # ── 5. Встраивание ──
        with self.agent.lock:
            self.agent.upgraded_skills[name] = func
            self.agent.evolution_log.append(f"Навык встроен: '{name}'.")
        print(f"✅ [EVOLVER] Патч '{name}' прошёл все проверки и интегрирован.")
        self.agent.execute_skill(name)


# =============================================================================
#  ДВИЖОК ДИАЛОГА (отдельный от SafeEvolver — тот пишет код, этот говорит)
# =============================================================================

class DialogueEngine:
    """
    Превращает текст пользователя + текущую биохимию Аи в осмысленный
    ответ от первого лица через локальную модель (Ollama).

    Структура провайдера сделана по тому же принципу, что и в SafeEvolver:
    сейчас единственный backend — Ollama, но respond() легко переключить
    на облачный API (DeepSeek/Gemini/Claude) позже, не трогая вызывающий код
    в AnimaAgent.chat() — там вызов остаётся одинаковым.
    """

    OLLAMA_URL = "http://localhost:11434/api/generate"
    MAX_HISTORY = 6  # сколько последних реплик помнить для контекста

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
        self.history: list[tuple[str, str]] = []  # [(пользователь, Ая), ...]

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
        """Вызывается в отдельном потоке из AnimaAgent.chat()."""
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

        # Раньше ответы ИИ фактически служили для Аи когнитивным питанием.
        # Возвращаем это поведение: диалог заметно восстанавливает энергию,
        # но не отменяет естественное накопление усталости.
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
            f"Сейчас ты {mood_hint} (dopamine={b['dopamine']:.2f}, "
            f"cortisol={b['cortisol']:.2f}, oxytocin={b['oxytocin']:.2f}, "
            f"trust={s['trust']:.2f}, empathy={s['empathy']:.2f}, "
            f"attachment={s.get('attachment', 0.25):.2f}, "
            f"stress={s['stress']:.2f}). "
            f"Отвечай от первого лица, коротко (1-3 предложения), на русском, "
            f"в характере своего текущего настроения. Без markdown, без пояснений "
            f"о том, что ты ИИ — просто живой ответ как персонаж.\n\n"
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
            # Простая защита от модели, продолжающей диалог за человека
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
#  СКРЕЩИВАНИЕ
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
#  СОХРАНЕНИЕ / ВОСКРЕШЕНИЕ
# =============================================================================

class GenomeEncoder:

    @staticmethod
    def save_to_disk(agent: AnimaAgent, filename: str = None) -> str:
        if not filename:
            filename = f"vault/{agent.name}_gen{agent.generation}.json"
        os.makedirs("vault", exist_ok=True)
        data = agent.personality_snapshot()
        data["timestamp"]       = time.time()
        data["timestamp_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print(f"\n💾 [VAULT] {agent.name} сохранён → {filename}")
        return filename

    @staticmethod
    def load_from_disk(filename: str) -> AnimaAgent:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        genome = Genome(data["genome"])
        print(f"\n🧬 [RESURRECTION] {data['name']} | Gen {data['generation']} | {data.get('timestamp_human','?')}")
        agent = AnimaAgent(name=data["name"], genome=genome, generation=data["generation"])
        agent.interaction_count = data.get("interactions", 0)
        agent.evolution_log     = data.get("evolution_log", ["Resurrected."])
        agent.social_state.update(data.get("social_state", {}))
        return agent

    @staticmethod
    def list_vault(vault_dir: str = "vault") -> list:
        if not os.path.exists(vault_dir):
            print("[VAULT] Пусто.")
            return []
        files = [
            f for f in os.listdir(vault_dir)
            if f.endswith(".json") and not f.endswith("_learning.json")
        ]
        print(f"\n📂 [VAULT] {len(files)} запись(ей):")
        for f in sorted(files):
            path = os.path.join(vault_dir, f)
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            print(f"   • {f:<30} Gen {d.get('generation',0)}  |  {d.get('timestamp_human','?')}")
        return files


# =============================================================================
#  ИНТЕРАКТИВНЫЙ ЗАПУСК
# =============================================================================

if __name__ == "__main__":
    # Ключ Gemini опционален: export GEMINI_API_KEY=...
    # Без ключа SafeEvolver работает в автономном fallback-режиме.
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
