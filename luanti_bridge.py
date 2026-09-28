"""Локальный мост между журналом Luanti и когнитивным ядром Ani.

Запуск:
    python3 luanti_bridge.py

Мост читает только строки с маркером [anima_event] и передаёт их
AnimaAgent. Сам мир и его Lua-логика остаются автономными.
"""

import json
import os
import signal
import sys
import threading
import time

from anima_agent import AnimaAgent, GenomeEncoder, _query_groq_chat


DEFAULT_LOG = "/home/fargo/.var/app/org.luanti.luanti/.minetest/debug.txt"
EVENT_MARKER = "[anima_event]"
COMMAND_PATH = os.environ.get("AYA_COMMAND_PATH", "/tmp/aya_command.json")
PLANNER_INTERVAL = 12.0
HUMAN_CONTACT_DURATION = 20.0
SOCIAL_COMMAND_INTERVAL = 3.0
PLANNER_ACTIONS = {"explore", "seek_food", "build", "rest", "move_to"}

BRIDGE_LOCK_PATH = os.environ.get("AYA_BRIDGE_LOCK_PATH", "/tmp/aya_bridge.lock")
GAME_HEARTBEAT_PATH = os.environ.get(
    "AYA_GAME_HEARTBEAT_PATH", "/tmp/aya_luanti_heartbeat"
)


def acquire_bridge_lock():
    """Не допускает второй экземпляр моста и повторную озвучку."""
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
        return GenomeEncoder.load_from_disk(os.path.join("vault", latest))
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
    """Выбирает одну безопасную игровую цель и передаёт её Lua-моду."""

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

        # Чат не должен запускать обычный выбор explore/build: контакт с человеком
        # имеет приоритет даже если LLM-планировщик занят или недавно планировал.
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
        temporary = f"{self.command_path}.{os.getpid()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(command, handle, ensure_ascii=False)
            os.replace(temporary, self.command_path)
            self.last_action = command.get("action")
            self.last_action_at = time.time()
            print(f"[PLANNER] action={self.last_action} reason={command.get('reason', '')}")
        except OSError as exc:
            print(f"❌ [PLANNER] Не удалось передать команду Lua: {exc}")


def handle_event(agent: AnimaAgent, event: dict, planner: WorldPlanner | None = None) -> None:
    if planner is not None:
        planner.consider(event)

    kind = event.get("kind")
    data = event.get("data") or {}
    agent.update_world_state(kind, data)

    if kind == "reflection_request":
        reflection = agent.reflect()
        print("[REFLECTION] " + json.dumps(reflection, ensure_ascii=False, separators=(",", ":")))
    elif kind == "chat":
        text = str(data.get("text", "")).strip()
        if text:
            agent.set_goal("human_contact", "человек обратился к Aya", 0.9, "human")
            agent.chat(text)
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
            reward=0.05 if data.get("success") else -0.05,
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
    elif kind == "build_completed":
        agent.set_goal("rest", "дом завершён, можно восстановиться", 0.75, "construction")
        agent.consolidate_memory("завершение строительства")
    elif kind == "world_observation":
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


def run(log_path: str) -> None:
    agent = load_agent()
    planner = WorldPlanner(agent)
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
