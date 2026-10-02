"""Local bridge between the Luanti log and Ani's cognitive core.

Usage:
    python3 luanti_bridge.py

The bridge reads only lines marked [anima_event] and forwards them to
AnimaAgent. The world and its Lua logic remain autonomous.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

from anima_agent import AnimaAgent, GenomeEncoder, VAULT_DIR, _query_groq_chat
from anima_evolution import atomic_json, QUALITY_KEYS


DEFAULT_LOG = "/home/fargo/.var/app/org.luanti.luanti/.minetest/debug.txt"
EVENT_MARKER = "[anima_event]"
RUNTIME_DIR = os.environ.get("AYA_RUNTIME_DIR", os.path.join(
    os.path.dirname(DEFAULT_LOG), "mods", "anima_bridge", "runtime"))
COMMAND_PATH = os.environ.get("AYA_COMMAND_PATH", os.path.join(RUNTIME_DIR, "command.json"))
DEVELOPMENT_PATH = os.environ.get("AYA_DEVELOPMENT_PATH", os.path.join(RUNTIME_DIR, "development.json"))
PLANNER_INTERVAL = 12.0
HUMAN_CONTACT_DURATION = 20.0
SOCIAL_COMMAND_INTERVAL = 3.0
PLANNER_ACTIONS = {"explore", "seek_food", "build", "rest", "move_to"}

BRIDGE_LOCK_PATH = os.environ.get("AYA_BRIDGE_LOCK_PATH", "/tmp/aya_bridge.lock")
GAME_HEARTBEAT_PATH = os.environ.get(
    "AYA_GAME_HEARTBEAT_PATH", os.path.join(RUNTIME_DIR, "heartbeat")
)

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
MIC_RECORD_SECONDS = float(os.environ.get("AYA_MIC_RECORD_SECONDS", "8"))
VOSK_MODEL_PATH = os.environ.get(
    "AYA_VOSK_MODEL_PATH",
    os.path.join(os.path.dirname(__file__), "models", "vosk-model-small-ru-0.22"),
)
MICROPHONE_LOCK = threading.Lock()
VOSK_MODEL = None


def _multipart_audio_body(audio_bytes: bytes, filename: str, fields: dict[str, str]):
    boundary = "----AyaVoice" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(audio_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return boundary, b"".join(chunks)


def transcribe_vosk(audio_path: str) -> str:
    """Local Russian-language fallback when Groq Whisper is unavailable."""
    global VOSK_MODEL
    try:
        from vosk import KaldiRecognizer, Model
    except ImportError:
        print("[VOICE ERROR] Пакет Vosk не установлен.")
        return ""
    if not os.path.isdir(VOSK_MODEL_PATH):
        print(f"[VOICE ERROR] Модель Vosk не найдена: {VOSK_MODEL_PATH}")
        return ""

    try:
        if VOSK_MODEL is None:
            print("[VOICE] Загружаю локальную модель Vosk...")
            VOSK_MODEL = Model(VOSK_MODEL_PATH)
        with wave.open(audio_path, "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                print("[VOICE ERROR] Для Vosk нужен моно WAV PCM16.")
                return ""
            recognizer = KaldiRecognizer(VOSK_MODEL, audio.getframerate())
            while True:
                chunk = audio.readframes(4000)
                if not chunk:
                    break
                recognizer.AcceptWaveform(chunk)
        result = json.loads(recognizer.FinalResult())
        return str(result.get("text", "")).strip()
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"[VOICE ERROR] Локальное распознавание Vosk не удалось: {exc}")
        return ""


def transcribe_microphone(seconds: float = MIC_RECORD_SECONDS) -> str:
    """Record a short phrase and transcribe it with Groq or local Vosk."""
    if not shutil.which("arecord"):
        print("[VOICE ERROR] Команда arecord не найдена.")
        return ""

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    record_seconds = max(2, min(15, int(float(seconds))))
    device = os.environ.get("AYA_MIC_DEVICE", "plughw:1,0")
    audio_path = ""
    with MICROPHONE_LOCK:
        try:
            fd, audio_path = tempfile.mkstemp(prefix="aya_mic_", suffix=".wav")
            os.close(fd)
            print(
                f"[VOICE] Слушаю микрофон {record_seconds} сек. "
                "Говорите после сигнала игры."
            )
            result = subprocess.run(
                [
                    "arecord",
                    "-q",
                    "-D",
                    device,
                    "-f",
                    "S16_LE",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-d",
                    str(record_seconds),
                    audio_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=record_seconds + 8,
            )
            if result.returncode != 0:
                details = (result.stderr or "").strip().replace("\n", " ")
                print(
                    f"[VOICE ERROR] arecord завершился с кодом "
                    f"{result.returncode}: {details[:240]}"
                )
                return ""

            with open(audio_path, "rb") as audio_file:
                audio_bytes = audio_file.read()
            if not audio_bytes:
                print("[VOICE ERROR] Микрофон не вернул аудиоданные.")
                return ""

            if api_key:
                try:
                    boundary, body = _multipart_audio_body(
                        audio_bytes,
                        "aya_voice.wav",
                        {
                            "model": GROQ_STT_MODEL,
                            "response_format": "json",
                            "prompt": "Разговор с Aya на русском или английском языке.",
                        },
                    )
                    request = urllib.request.Request(
                        GROQ_STT_URL,
                        data=body,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": f"multipart/form-data; boundary={boundary}",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=35) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    text = str(payload.get("text", "")).strip()
                    if text:
                        return text
                    print("[VOICE] Groq не распознал фразу; пробую локальный Vosk.")
                except urllib.error.HTTPError as exc:
                    details = exc.read().decode(
                        "utf-8", errors="replace"
                    ).replace("\n", " ")
                    print(
                        f"[VOICE] Groq STT HTTP {exc.code}; "
                        f"переключаюсь на Vosk: {details[:240]}"
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"[VOICE] Groq STT недоступен; переключаюсь на Vosk: {exc}")
            else:
                print("[VOICE] GROQ_API_KEY не загружен; использую локальный Vosk.")

            return transcribe_vosk(audio_path)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[VOICE ERROR] Не удалось записать речь: {exc}")
            return ""
        finally:
            if audio_path:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass


def acquire_bridge_lock():
    """Prevent duplicate bridge processes and overlapping speech output."""
    try:
        import fcntl
        handle = open(BRIDGE_LOCK_PATH, "w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        try:
            handle.close()
        except UnboundLocalError:
            pass
        print("[BRIDGE] Уже запущен другой экземпляр; второй запуск отменён.")
        return None
    except OSError as exc:
        print(f"[BRIDGE] Не удалось создать lock-файл: {exc}")
        return None


def release_bridge_lock(handle):
    if handle is None:
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()



PLANNER_EVENTS = {
    "world_observation",
    "needs_changed",
    "obstacle",
    "food_eaten",
    "food_harvested",
    "resource_gathered",
    "build_completed",
    "build_failed",
}


def load_agent() -> AnimaAgent:
    vault_files = GenomeEncoder.list_vault()
    if vault_files:
        latest = sorted(vault_files)[-1]
        return GenomeEncoder.load_from_disk(os.path.join(VAULT_DIR, latest))
    return AnimaAgent(name="Aya")


def parse_event(line: str) -> dict | None:
    marker_pos = line.find(EVENT_MARKER)
    if marker_pos < 0:
        return None
    payload = line[marker_pos + len(EVENT_MARKER):].strip()
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


class WorldPlanner:
    """Select one safe in-game goal and pass it to the Lua mod."""

    def __init__(self, agent: AnimaAgent, command_path: str = COMMAND_PATH):
        self.agent = agent
        self.command_path = command_path
        self.lock = threading.Lock()
        self.busy = False
        self.last_plan_at = 0.0
        self.last_action = None
        self.last_action_at = 0.0
        self.human_contact_until = 0.0
        self.last_social_command_at = 0.0
        self.human_name = None
        self.last_development_publish = 0.0
        self.state = {
            "position": None,
            "nodes": [],
            "objects": [],
            "hunger": None,
            "saturation": None,
            "night": False,
            "storm": False,
            "sheltered": False,
            "near_player": False,
            "touching_player": False,
            "protected": False,
            "safety_mode": None,
            "last_event": None,
            "last_event_data": {},
            "human_name": None,
            "human_position": None,
            "human_contact_until": 0.0,
        }

    @staticmethod
    def _normalize_position(position: object) -> dict | None:
        if not isinstance(position, dict):
            return None
        try:
            return {
                "x": int(round(float(position["x"]))),
                "y": int(round(float(position["y"]))),
                "z": int(round(float(position["z"]))),
            }
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _player_target(cls, objects: list, player_name: str | None = None) -> dict | None:
        candidates = [
            item for item in (objects or [])
            if isinstance(item, dict) and item.get("kind") == "player"
        ]
        if player_name:
            candidates = (
                [item for item in candidates if item.get("name") == player_name]
                + [item for item in candidates if item.get("name") != player_name]
            )
        for item in candidates:
            target = cls._normalize_position(item.get("position"))
            if target:
                return target
        return None

    def consider(self, event: dict) -> None:
        kind = event.get("kind")
        data = event.get("data") or {}
        now = time.time()
        social_command = None

        with self.lock:
            self.state["last_event"] = kind
            self.state["last_event_data"] = data
            if kind == "world_observation":
                self.state["position"] = data.get("position")
                self.state["nodes"] = list(data.get("nodes") or [])[:24]
                self.state["objects"] = list(data.get("objects") or [])[:16]
                if self.human_contact_until > now:
                    target = self._player_target(
                        self.state["objects"], self.human_name
                    )
                    if target:
                        self.state["human_position"] = target
                        if now - self.last_social_command_at >= SOCIAL_COMMAND_INTERVAL:
                            social_command = {
                                "action": "socialize",
                                "player": self.human_name or "singleplayer",
                                "target": target,
                                "duration": HUMAN_CONTACT_DURATION,
                                "reason": "human_contact_priority",
                            }
            elif kind == "needs_changed":
                self.state["hunger"] = data.get("hunger")
                self.state["saturation"] = data.get("saturation")
            elif kind == "safety_state":
                self.state["night"] = bool(data.get("night"))
                self.state["storm"] = bool(data.get("storm"))
                self.state["sheltered"] = bool(data.get("sheltered"))
                self.state["near_player"] = bool(data.get("near_player"))
                self.state["touching_player"] = bool(data.get("touching_player"))
                self.state["protected"] = bool(data.get("protected"))
                self.state["safety_mode"] = data.get("mode")
            elif kind == "chat":
                self.human_name = str(data.get("name") or "singleplayer")
                self.human_contact_until = now + HUMAN_CONTACT_DURATION
                self.state["human_name"] = self.human_name
                self.state["human_contact_until"] = self.human_contact_until
                target = (
                    self._normalize_position(data.get("position"))
                    or self._player_target(self.state["objects"], self.human_name)
                )
                self.state["human_position"] = target
                if target:
                    social_command = {
                        "action": "socialize",
                        "player": self.human_name,
                        "target": target,
                        "duration": HUMAN_CONTACT_DURATION,
                        "reason": "human_contact_priority",
                    }

        if social_command:
            self.last_social_command_at = now
            self.last_plan_at = now
            self._write_command(social_command)
            return

        # Chat must not trigger the usual explore/build selection: human contact
        # takes priority even when the LLM planner is busy or ran recently.
        if kind == "chat":
            return

        if kind not in PLANNER_EVENTS or self.busy:
            return
        if now - self.last_plan_at < PLANNER_INTERVAL:
            return
        if self.last_action and now - self.last_action_at < PLANNER_INTERVAL:
            return
        self.last_plan_at = now
        self.busy = True
        threading.Thread(
            target=self._plan_worker,
            args=(kind,),
            daemon=True,
        ).start()

    def _plan_worker(self, trigger: str) -> None:
        try:
            snapshot = self._snapshot(trigger)
            if snapshot.get("safety_mode"):
                return  # The embodied shelter/player controller is already resolving danger.
            command = self.agent.development.recommend(snapshot)
            if command is None:
                command = self._ask_model(snapshot)
            if command is None:
                command = self._fallback(snapshot)
            if command is not None:
                self._write_command(command)
        finally:
            self.busy = False

    def _snapshot(self, trigger: str) -> dict:
        with self.lock:
            snapshot = dict(self.state)
            snapshot["nodes"] = list(self.state.get("nodes") or [])
            snapshot["last_event_data"] = dict(self.state.get("last_event_data") or {})
        with self.agent.lock:
            snapshot["biochemistry"] = {
                key: round(float(value), 3)
                for key, value in self.agent.blood.items()
            }
            snapshot["social_state"] = {
                key: round(float(value), 3)
                for key, value in self.agent.social_state.items()
            }
            snapshot["memory_stats"] = self.agent.learning.stats()
            snapshot["motives"] = dict(self.agent.motives)
            snapshot["current_goal"] = self.agent.goal_state.get("active")
        snapshot["development"] = self.agent.development.summary()
        snapshot["visible_objects"] = snapshot.get("objects", [])
        snapshot["trigger"] = trigger
        snapshot["recent_action"] = self.last_action
        return snapshot

    def _ask_model(self, snapshot: dict) -> dict | None:
        if os.getenv("ANIMA_LLM_PROVIDER", "").strip().lower() != "groq":
            return None
        if not os.getenv("GROQ_API_KEY"):
            return None
        system = (
            "Ты — планировщик действий Aya, которая действительно находится "
            "в физическом мире Luanti/Repixture и получает сенсорные данные. "
            "Выбери ровно одну следующую цель. Не пиши код и не объясняй решение. "
            "Разрешены только JSON-действия: "
            "{\"action\":\"explore\"}, "
            "{\"action\":\"seek_food\"}, "
            "{\"action\":\"build\"}, "
            "{\"action\":\"rest\"} или "
            "{\"action\":\"move_to\",\"target\":{\"x\":0,\"y\":0,\"z\":0}}. "
            "Цель: сначала поддерживать жизнь и исследовать мир, "
            "затем собирать ресурсы и постепенно строить жильё. "
            "Учитывай visible_objects: player — человек, entity — животное или другой моб. "
            "Не путай видимые объекты с блоками и не утверждай, что видишь кого-то, если список пуст. "
            "Если данных мало, выбирай explore. "
            "Ответь строго одним JSON-объектом."
        )
        user = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        raw = _query_groq_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            temperature=0.2,
            max_tokens=256,
            timeout=45,
            reasoning_effort="low",
            include_reasoning=False,
            response_format={"type": "json_object"},
        )
        return self._parse_command(raw, snapshot) if raw else None

    @staticmethod
    def _parse_command(raw: str | None, snapshot: dict) -> dict | None:
        if not raw:
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            command = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(command, dict):
            return None
        action = command.get("action")
        if action not in PLANNER_ACTIONS:
            return None
        if action != "move_to":
            return {"action": action, "reason": "groq_plan"}

        target = command.get("target")
        position = snapshot.get("position") or {}
        if not isinstance(target, dict) or not isinstance(position, dict):
            return None
        try:
            target = {
                "x": int(round(float(target["x"]))),
                "y": int(round(float(target["y"]))),
                "z": int(round(float(target["z"]))),
            }
            current = {
                "x": float(position["x"]),
                "y": float(position["y"]),
                "z": float(position["z"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
        distance = ((target["x"] - current["x"]) ** 2
                    + (target["y"] - current["y"]) ** 2
                    + (target["z"] - current["z"]) ** 2) ** 0.5
        if distance > 16.0:
            return None
        return {"action": "move_to", "target": target, "reason": "groq_plan"}

    def _fallback(self, snapshot: dict) -> dict:
        hunger = snapshot.get("hunger")
        nodes = set(snapshot.get("nodes") or [])
        contact_until = float(snapshot.get("human_contact_until") or 0.0)
        human_position = snapshot.get("human_position")
        internal_recommendation = self.agent.recommended_goal()
        if contact_until > time.time() and human_position:
            return {
                "action": "socialize",
                "player": snapshot.get("human_name") or "singleplayer",
                "target": human_position,
                "duration": HUMAN_CONTACT_DURATION,
                "reason": "human_contact_priority",
            }
        if hunger is not None and float(hunger) < 8:
            return {"action": "seek_food", "reason": "low_hunger_fallback"}
        recommended_action = internal_recommendation.get("action")
        if recommended_action == "seek_food":
            return {"action": "seek_food", "reason": "biochemistry_energy_motive"}
        if recommended_action == "build":
            return {"action": "build", "reason": "construction_motive"}
        if recommended_action == "follow_player" and human_position:
            return {
                "action": "socialize",
                "player": snapshot.get("human_name") or "singleplayer",
                "target": human_position,
                "duration": HUMAN_CONTACT_DURATION,
                "reason": "attachment_motive",
            }
        if recommended_action == "rest":
            return {"action": "rest", "reason": "adenosine_rest_motive"}
        if snapshot.get("last_event") == "obstacle":
            return {"action": "explore", "reason": "obstacle_fallback"}
        if any("tree" in node for node in nodes):
            return {"action": "build", "reason": "tree_seen_fallback"}
        return {"action": "explore", "reason": "default_fallback"}

    def _write_command(self, command: dict) -> None:
        # A planner worker can finish after a chat event. Never let that
        # stale result overwrite the active human-contact command.
        with self.lock:
            contact_active = self.human_contact_until > time.time()
            if contact_active and command.get("action") != "socialize":
                print("[PLANNER] action skipped: human_contact_priority")
                return
            if not self.agent.is_running:
                return
            try:
                atomic_json(self.command_path, command)
                self.last_action = command.get("action")
                self.last_action_at = time.time()
                print(f"[PLANNER] action={self.last_action} reason={command.get('reason', '')}")
            except OSError as exc:
                print(f"❌ [PLANNER] Не удалось передать команду Lua: {exc}")

    def publish_development(self, force=False):
        now = time.time()
        if force or now - self.last_development_publish >= 3:
            state = self.agent.development.runtime()
            state["summary"] = self.agent.development.summary()
            atomic_json(DEVELOPMENT_PATH, state)
            self.last_development_publish = now


def _handle_event(agent: AnimaAgent, event: dict) -> None:
    kind = event.get("kind")
    data = event.get("data") or {}
    agent.update_world_state(kind, data)

    if kind == "development_request":
        if data.get("action") == "rollback":
            agent.development.rollback()
        print("[EVOLUTION] " + json.dumps(agent.development.summary(), ensure_ascii=False))
    elif kind == "rest_completed":
        with agent.lock:
            agent.blood["adenosine"] = max(0.0, agent.blood["adenosine"] - 0.18)
            if data.get("protected"):
                agent.blood["cortisol"] = max(0.0, agent.blood["cortisol"] - 0.03)
        agent.consolidate_memory("подтверждённый отдых")
    elif kind == "reflection_request":
        reflection = agent.reflect()
        print("[REFLECTION] " + json.dumps(reflection, ensure_ascii=False, separators=(",", ":")))
    elif kind == "chat":
        text = str(data.get("text", "")).strip()
        if text:
            agent.set_goal("human_contact", "человек обратился к Aya", 0.9, "human")
            agent.chat(text)
    elif kind == "voice_request":
        seconds = data.get("seconds", MIC_RECORD_SECONDS)
        speaker = str(data.get("name", "человек"))
        print(f"[VOICE] Запрос на разговор от {speaker}.")
        text = transcribe_microphone(seconds)
        if text:
            print(f"[VOICE] Распознано: {text}")
            agent.set_goal("human_contact", "человек обратился к Aya голосом", 0.95, "human")
            agent.chat(text)
        else:
            print("[VOICE] Речь не распознана.")
    elif kind == "food_eaten":
        agent.set_goal("recover_energy", "еда доступна и усвоена", 0.85, "experience")
        agent.learn_from_experience(
            stimulus="еда в игровом мире",
            action="есть",
            outcome="голод уменьшился",
            reward=0.8,
            details=data,
        )
    elif kind == "food_harvested":
        agent.set_goal("seek_food", "найден съедобный ресурс", 0.8, "experience")
        agent.learn_from_experience(
            stimulus="яблоко растёт на дереве",
            action="смотреть вверх и собирать",
            outcome="яблоко добыто",
            reward=0.9,
            details=data,
        )
    elif kind == "safety_state":
        night = bool(data.get("night"))
        storm = bool(data.get("storm"))
        sheltered = bool(data.get("sheltered"))
        near_player = bool(data.get("near_player"))
        touching_player = bool(data.get("touching_player"))
        protected = bool(data.get("protected"))
        mode = data.get("mode")
        if mode == "shelter":
            agent.set_goal("seek_shelter", "ночь или дождь требуют защиты", 0.9, "homeostasis")
        elif mode == "player":
            agent.set_goal("follow_player", "рядом с игроком безопаснее", 0.85, "attachment")
        elif touching_player:
            agent.set_goal("socialize", "контакт с игроком поддерживает связь", 0.8, "attachment")
        safety_key = (
            night, storm, sheltered, near_player, touching_player,
            protected, mode,
        )
        previous_key = getattr(agent, "_last_safety_state_key", None)
        agent._last_safety_state_key = safety_key

        with agent.lock:
            if protected:
                calm = 0.07 if sheltered else 0.05
                agent.blood["cortisol"] = max(
                    0.0, float(agent.blood.get("cortisol", 0.0)) - calm
                )
                agent.social_state["stress"] = max(
                    0.0, float(agent.social_state.get("stress", 0.0)) - calm * 0.6
                )
                agent.blood["serotonin"] = min(
                    1.0, float(agent.blood.get("serotonin", 0.0)) + 0.025
                )
            elif night or storm:
                threat = 0.035 if storm else 0.03
                agent.blood["cortisol"] = min(
                    1.0, float(agent.blood.get("cortisol", 0.0)) + threat
                )
                agent.social_state["stress"] = min(
                    1.0, float(agent.social_state.get("stress", 0.0)) + threat * 0.7
                )

            if near_player:
                agent.blood["oxytocin"] = min(
                    1.0, float(agent.blood.get("oxytocin", 0.0)) + 0.025
                )
                agent.blood["dopamine"] = min(
                    1.0, float(agent.blood.get("dopamine", 0.0)) + 0.02
                )

            if touching_player:
                agent.blood["oxytocin"] = min(
                    1.0, float(agent.blood.get("oxytocin", 0.0)) + 0.08
                )
                agent.blood["dopamine"] = min(
                    1.0, float(agent.blood.get("dopamine", 0.0)) + 0.08
                )
                agent.blood["serotonin"] = min(
                    1.0, float(agent.blood.get("serotonin", 0.0)) + 0.04
                )
                agent.blood["cortisol"] = max(
                    0.0, float(agent.blood.get("cortisol", 0.0)) - 0.12
                )
                agent.social_state["attachment"] = min(
                    1.0, float(agent.social_state.get("attachment", 0.0)) + 0.04
                )
                agent.social_state["stress"] = max(
                    0.0, float(agent.social_state.get("stress", 0.0)) - 0.08
                )

        if previous_key != safety_key:
            if sheltered:
                action = "оставаться в укрытии"
                outcome = "дом защищает от ночи или дождя"
                reward = 0.25
            elif touching_player:
                action = "приблизиться к игроку"
                outcome = "контакт с игроком успокоил Aya"
                reward = 0.35
            elif near_player:
                action = "держаться рядом с игроком"
                outcome = "рядом с игроком безопаснее"
                reward = 0.2
            elif mode == "shelter":
                action = "искать укрытие"
                outcome = "ночь или дождь требуют защиты"
                reward = -0.1
            else:
                action = "исследовать безопасную зону"
                outcome = "условия спокойные"
                reward = 0.05
            agent.learn_from_experience(
                stimulus="ночь, дождь и близость к человеку",
                action=action,
                outcome=outcome,
                reward=reward,
                details=data,
            )
    elif kind == "needs_changed":
        hunger = float(data.get("hunger", 0.0))
        hunger_max = max(1.0, float(data.get("hunger_max", 20.0)))
        deficit = max(0.0, min(1.0, 1.0 - hunger / hunger_max))
        if deficit > 0.0:
            if deficit > 0.35:
                agent.set_goal("seek_food", "энергия снижается", min(0.95, 0.45 + deficit), "homeostasis")
            with agent.lock:
                agent.blood["cortisol"] = min(
                    1.0, agent.blood["cortisol"] + 0.04 * deficit
                )
                agent.social_state["stress"] = min(
                    1.0, agent.social_state["stress"] + 0.02 * deficit
                )
            agent.learn_from_experience(
                stimulus="снижение запасов энергии",
                action="искать еду",
                outcome=f"голод {hunger:.0f}/{hunger_max:.0f}",
                reward=-0.05 * deficit,
                details=data,
            )
    elif kind == "pain":
        agent.experience_pain(
            severity=float(data.get("severity", 0.5)),
            source=str(data.get("source", "игровой мир")),
        )
    elif kind == "obstacle":
        agent.set_goal("replan", "препятствие нарушило маршрут", 0.75, "experience")
        agent.learn_from_experience(
            stimulus="препятствие на маршруте",
            action="обойти",
            outcome="маршрут пришлось перестроить",
            reward=-0.15,
            details=data,
        )
    elif kind == "planner_command":
        if data.get("success"):
            agent.development.note_decision(data)
        agent.set_goal(
            str(data.get("action", "unknown")),
            str(data.get("reason", data.get("result", "planner"))),
            0.65 if data.get("success") else 0.35,
            "planner",
        )
        agent.learn_from_experience(
            stimulus="планировщик выбрал действие",
            action=str(data.get("action", "неизвестно")),
            outcome=str(data.get("result", data.get("reason", "команда принята"))),
            reward=0.0,
            details=data,
        )
    elif kind == "resource_gathered":
        agent.set_goal("build", "ресурс добыт для будущего дома", 0.8, "construction")
        agent.learn_from_experience(
            stimulus="найден строительный ресурс",
            action="собирать и возвращаться к плану",
            outcome="ресурс добавлен в запас",
            reward=0.7,
            details=data,
        )
    elif kind == "construction_step":
        agent.set_goal("build", "дом строится по плану", 0.9, "construction")
        agent.learn_from_experience(
            stimulus="строительный план",
            action="строить следующий блок",
            outcome="блок установлен",
            reward=0.25,
            details=data,
        )
    elif kind == "build_started":
        agent.set_goal("build", "план и место выбраны", 0.75, "construction")
    elif kind == "build_completed":
        if all((data.get("quality") or {}).get(k) is True for k in QUALITY_KEYS):
            agent.set_goal("rest", "дом проверен, можно восстановиться", 0.75, "construction")
            agent.learn_from_experience("строительный план", "проверить готовый дом", "дом пригоден", 0.8, data)
            agent.consolidate_memory("завершение строительства")
    elif kind == "build_failed":
        agent.learn_from_experience("строительный план", "проверить готовый дом", str(data.get("reason")), -0.4, data)
    elif kind == "world_observation":
        if agent.world_state.get("novelty", 0.0) < 0.5:
            return
        nodes = data.get("nodes") or []
        objects = data.get("objects") or []
        node_summary = ", ".join(str(node) for node in nodes[:12])
        object_summary = ", ".join(
            f"{item.get('kind', 'object')}:{item.get('name', 'unknown')} "
            f"на расстоянии {item.get('distance', '?')}"
            for item in objects[:8]
            if isinstance(item, dict)
        )
        summaries = [part for part in (node_summary, object_summary) if part]
        agent.learn_from_experience(
            stimulus="новая клетка мира",
            action="наблюдать и запоминать",
            outcome="; ".join(summaries) or "окружение осмотрено",
            reward=0.05,
            details=data,
        )
        if node_summary:
            agent.receive_information(
                node_summary,
                source="исследование мира",
                nourishment=0.02,
            )
        if object_summary:
            agent.receive_information(
                object_summary,
                source="наблюдение за объектами мира",
                nourishment=0.03,
            )


def handle_event(agent: AnimaAgent, event: dict, planner: WorldPlanner | None = None) -> None:
    if not isinstance(event.get("data", {}), dict):
        return
    _handle_event(agent, event)
    agent.observe_development(event.get("kind"), event.get("data") or {})
    if planner is not None:
        planner.publish_development(force=event.get("kind") == "development_request")
        planner.consider(event)


def run(log_path: str) -> None:
    agent = load_agent()
    planner = WorldPlanner(agent)
    planner.publish_development(force=True)
    stopping = False
    bridge_started_at = time.time()
    last_heartbeat_seen_at = bridge_started_at
    last_heartbeat_mtime = None
    heartbeat_grace_period = float(
        os.environ.get("AYA_HEARTBEAT_GRACE_PERIOD", "30")
    )
    heartbeat_timeout = float(
        os.environ.get("AYA_HEARTBEAT_TIMEOUT", "15")
    )

    def stop_bridge(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_bridge)
    signal.signal(signal.SIGTERM, stop_bridge)

    print(f"[BRIDGE] Ani подключена к Luanti: {log_path}")
    print("[BRIDGE] События: chat, food_eaten, food_harvested, needs_changed, pain, obstacle, world_observation")

    with open(log_path, "r", encoding="utf-8", errors="replace") as log:
        log.seek(0, os.SEEK_END)
        while not stopping:
            now = time.time()
            planner.publish_development()
            try:
                heartbeat_mtime = os.path.getmtime(GAME_HEARTBEAT_PATH)
            except OSError:
                heartbeat_mtime = None
            if (
                heartbeat_mtime is not None
                and (
                    last_heartbeat_mtime is None
                    or heartbeat_mtime > last_heartbeat_mtime
                )
            ):
                last_heartbeat_mtime = heartbeat_mtime
                last_heartbeat_seen_at = now
            if (
                now - bridge_started_at > heartbeat_grace_period
                and now - last_heartbeat_seen_at > heartbeat_timeout
            ):
                print("[BRIDGE] Heartbeat Luanti потерян; мост завершает работу.")
                stopping = True
                break

            line = log.readline()
            if not line:
                time.sleep(0.25)
                continue
            event = parse_event(line)
            if event:
                if event.get("kind") == "world_shutdown":
                    print("[BRIDGE] Luanti закрывается; мост завершает работу.")
                    stopping = True
                    break
                handle_event(agent, event, planner)

    agent.stop()
    GenomeEncoder.save_to_disk(agent)
    print("[BRIDGE] Ani сохранена.")


if __name__ == "__main__":
    bridge_lock = acquire_bridge_lock()
    if bridge_lock is not None:
        try:
            run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG)
        finally:
            release_bridge_lock(bridge_lock)
