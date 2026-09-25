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
PLANNER_ACTIONS = {"explore", "seek_food", "build", "rest", "move_to"}
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
        self.state = {
            "position": None,
            "nodes": [],
            "hunger": None,
            "saturation": None,
            "last_event": None,
            "last_event_data": {},
        }

    def consider(self, event: dict) -> None:
        kind = event.get("kind")
        data = event.get("data") or {}
        with self.lock:
            self.state["last_event"] = kind
            self.state["last_event_data"] = data
            if kind == "world_observation":
                self.state["position"] = data.get("position")
                self.state["nodes"] = list(data.get("nodes") or [])[:24]
            elif kind == "needs_changed":
                self.state["hunger"] = data.get("hunger")
                self.state["saturation"] = data.get("saturation")

        if kind not in PLANNER_EVENTS or self.busy:
            return
        now = time.time()
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
            "Ты — планировщик действий Aya в мире Luanti. "
            "Выбери ровно одну следующую цель. Не пиши код и не объясняй решение. "
            "Разрешены только JSON-действия: "
            "{\"action\":\"explore\"}, "
            "{\"action\":\"seek_food\"}, "
            "{\"action\":\"build\"}, "
            "{\"action\":\"rest\"} или "
            "{\"action\":\"move_to\",\"target\":{\"x\":0,\"y\":0,\"z\":0}}. "
            "Цель: сначала поддерживать жизнь и исследовать мир, "
            "затем собирать ресурсы и постепенно строить жильё. "
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

    @staticmethod
    def _fallback(snapshot: dict) -> dict:
        hunger = snapshot.get("hunger")
        nodes = set(snapshot.get("nodes") or [])
        if hunger is not None and float(hunger) < 8:
            return {"action": "seek_food", "reason": "low_hunger_fallback"}
        if snapshot.get("last_event") == "obstacle":
            return {"action": "explore", "reason": "obstacle_fallback"}
        if any("tree" in node for node in nodes):
            return {"action": "build", "reason": "tree_seen_fallback"}
        return {"action": "explore", "reason": "default_fallback"}

    def _write_command(self, command: dict) -> None:
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

    if kind == "chat":
        text = str(data.get("text", "")).strip()
        if text:
            agent.chat(text)
    elif kind == "food_eaten":
        agent.learn_from_experience(
            stimulus="еда в игровом мире",
            action="есть",
            outcome="голод уменьшился",
            reward=0.8,
            details=data,
        )
    elif kind == "food_harvested":
        agent.learn_from_experience(
            stimulus="яблоко растёт на дереве",
            action="смотреть вверх и собирать",
            outcome="яблоко добыто",
            reward=0.9,
            details=data,
        )
    elif kind == "needs_changed":
        hunger = float(data.get("hunger", 0.0))
        hunger_max = max(1.0, float(data.get("hunger_max", 20.0)))
        deficit = max(0.0, min(1.0, 1.0 - hunger / hunger_max))
        if deficit > 0.0:
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
        agent.learn_from_experience(
            stimulus="препятствие на маршруте",
            action="обойти",
            outcome="маршрут пришлось перестроить",
            reward=-0.15,
            details=data,
        )
    elif kind == "planner_command":
        agent.learn_from_experience(
            stimulus="планировщик выбрал действие",
            action=str(data.get("action", "неизвестно")),
            outcome=str(data.get("result", data.get("reason", "команда принята"))),
            reward=0.05 if data.get("success") else -0.05,
            details=data,
        )
    elif kind == "world_observation":
        nodes = data.get("nodes") or []
        node_summary = ", ".join(str(node) for node in nodes[:12])
        agent.learn_from_experience(
            stimulus="новая клетка мира",
            action="наблюдать и запоминать",
            outcome=node_summary or "окружение осмотрено",
            reward=0.05,
            details=data,
        )
        if node_summary:
            agent.receive_information(
                node_summary,
                source="исследование мира",
                nourishment=0.02,
            )


def run(log_path: str) -> None:
    agent = load_agent()
    planner = WorldPlanner(agent)
    stopping = False

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
            line = log.readline()
            if not line:
                time.sleep(0.25)
                continue
            event = parse_event(line)
            if event:
                handle_event(agent, event, planner)

    agent.stop()
    GenomeEncoder.save_to_disk(agent)
    print("[BRIDGE] Ani сохранена.")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG)
