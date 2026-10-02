"""Persistent, outcome-based development. Model proposals are data, never Python.

The mutable program is an ordered composition of registered decision modules.
Biochemistry supplies motives; fitness uses world outcomes, never hormone levels.
Static validation precedes a reversible trial, not a claim of proven improvement.
"""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time


MODULES = ("resource_memory", "learned_actions", "construction", "exploration")
ACTIONS = ("explore", "seek_food", "build", "rest", "socialize", "move_to")
DEFAULT_POLICY = {
    "modules": list(MODULES),
    "body": {"walk_speed": 1.5, "jump_cooldown": 0.9},
    "gathering": {"prefer_memory": True},
    "construction": {"half_size": 2, "wall_height": 3, "door": "north"},
}
QUALITY_KEYS = ("support", "interior", "doorway", "roof", "walls", "access")
# Only completed, sensor-observed outcomes can evaluate a trial.
OUTCOMES = {
    "food_harvested": ("seek_food", "gathering", 0.6),
    "food_eaten": ("seek_food", "gathering", 0.8),
    "resource_gathered": ("build", "gathering", 0.7),
    "resource_missing": ("seek_food", "gathering", -0.3),
    "harvest_failed": ("seek_food", "gathering", -0.5),
    "build_completed": ("build", "construction", 1.0),
    "build_failed": ("build", "construction", -0.8),
    "navigation_result": ("move_to", "body", 0.4),
    "jump_result": ("move_to", "body", 0.4),
    "rest_completed": ("rest", "rest", 0.4),
    "pain": ("move_to", "body", -1.0),
}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as f:
            name = f.name
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Ожидалось конечное число")
    return float(value)


def position(value):
    if not isinstance(value, dict):
        return None
    try:
        return {k: round(number(value[k]), 2) for k in ("x", "y", "z")}
    except (KeyError, ValueError):
        return None


def distance(a, b):
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in ("x", "y", "z")))


def validate_policy(policy, origin):
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY) | {"genes"}:
        raise ValueError("Неверные разделы программы поведения")
    if not isinstance(policy["modules"], list) or sorted(policy["modules"]) != sorted(MODULES):
        raise ValueError("Программа должна содержать каждый зарегистрированный модуль один раз")
    for section in ("body", "gathering", "construction"):
        if not isinstance(policy[section], dict) or set(policy[section]) != set(DEFAULT_POLICY[section]):
            raise ValueError("Неверная схема " + section)
    for key, low, high in (("walk_speed", 1.1, 1.7), ("jump_cooldown", 0.8, 1.5)):
        if not low <= number(policy["body"][key]) <= high:
            raise ValueError("Параметр движения вне границ: " + key)
    if type(policy["gathering"]["prefer_memory"]) is not bool:
        raise ValueError("prefer_memory должен быть bool")
    c = policy["construction"]
    if type(c["half_size"]) is not int or c["half_size"] not in (2, 3):
        raise ValueError("Разрешены дома 5x5 и 7x7")
    if type(c["wall_height"]) is not int or c["wall_height"] not in (3, 4):
        raise ValueError("Высота стен должна быть 3 или 4")
    if c["door"] not in ("north", "south", "east", "west"):
        raise ValueError("Неверное направление входа")
    genes = policy["genes"]
    if not isinstance(genes, dict) or set(genes) != set(origin):
        raise ValueError("Состав генома должен сохраняться")
    for k, value in genes.items():
        if not 0.0 <= number(value) <= 1.0 or abs(value - origin[k]) > 0.100001:
            raise ValueError("Мутация слишком далека от исходного генома: " + k)


class AdaptiveDevelopment:
    VERSION = 1
    MIN_BASELINE = 6
    TRIAL_SAMPLES = 8
    TRIAL_SECONDS = 900

    def __init__(self, path, genes, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.RLock()
        initial = deepcopy(DEFAULT_POLICY)
        initial["genes"] = dict(genes)
        self.data = {
            "version": self.VERSION, "origin_genome": dict(genes),
            "policy": initial, "revision": 0, "trial": None,
            "history": [], "episodes": [], "skills": {}, "places": [],
            "body_model": {"successful_jumps": 0, "failed_jumps": 0, "max_observed_rise": 0.0},
            "self_model": {"predictions": 0, "squared_error": 0.0},
            "world": None, "last_proposal_at": 0.0, "next_mutation": 0,
        }
        if self.path.exists():
            # Never silently replace unreadable personal memory with a blank life.
            with self.path.open(encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("version") != self.VERSION:
                raise ValueError("Неизвестная версия памяти развития: " + str(self.path))
            validate_policy(saved["policy"], saved["origin_genome"])
            self.data.update(saved)
        self.pending = None
        self.last_save = 0.0
        self.dirty = False

    def save(self, force=True):
        with self.lock:
            if force or (self.dirty and self.clock() - self.last_save >= 5):
                atomic_json(self.path, self.data)
                self.last_save = self.clock()
                self.dirty = False

    def runtime(self):
        with self.lock:
            return {"revision": self.data["revision"], **deepcopy(self.data["policy"])}

    def _stat(self, action, reward):
        item = self.data["skills"].setdefault(action, {"attempts": 0, "successes": 0, "value": 0.0})
        item["attempts"] += 1
        item["successes"] += int(reward > 0)
        # Recent outcomes can overturn an old successful habit.
        item["value"] += min(0.25, 1 / item["attempts"]) * (reward - item["value"])

    def _remember_place(self, pos, resource, world, available, verified):
        if not pos or not world:
            return
        places = self.data["places"]
        item = next((p for p in places if p["world"] == world and p["resource"] == resource
                     and distance(p["position"], pos) < 1), None)
        if item is None:
            item = {"position": pos, "resource": resource, "world": world,
                    "confirmations": 0, "contradictions": 0, "source": "game_sensor"}
            places.append(item)
        item["confirmations" if available else "contradictions"] += 1
        item.update(available=available, verified=verified, last_seen=self.clock())
        item["confidence"] = (item["confirmations"] + 1) / (
            item["confirmations"] + item["contradictions"] + 2)
        self.data["places"] = places[-160:]

    def observe(self, kind, data, context):
        """Only game events update competence; accepting a command is not success."""
        with self.lock:
            world = data.get("world_id") or self.data["world"]
            if world and world != self.data["world"]:
                if self.data["trial"]:
                    self._rollback("world_changed")
                self.data["world"] = world
                self.pending = None
            if kind == "world_observation":
                for resource in (data.get("resources") or [])[:32]:
                    if isinstance(resource, dict):
                        self._remember_place(position(resource.get("position")), str(resource.get("name", "")),
                                             world, True, True)
            if kind in {"food_harvested", "resource_gathered", "resource_missing"}:
                self._remember_place(position(data.get("position")), str(data.get("item", "")),
                                     world, False, True)
            if kind == "jump_result":
                body = self.data["body_model"]
                success = data.get("success") is True
                body["successful_jumps" if success else "failed_jumps"] += 1
                if success:
                    rise = number(data.get("rise", 0.0))
                    body["max_observed_rise"] = max(body["max_observed_rise"], max(0.0, min(2.0, rise)))
            result = OUTCOMES.get(kind)
            if result:
                action, domain, reward = result
                if kind == "resource_missing" and "tree" in str(data.get("item", "")):
                    action = "build"
                if kind in {"jump_result", "navigation_result"} and data.get("success") is not True:
                    reward = -0.6
                if kind == "build_completed":
                    quality = data.get("quality") or {}
                    if not all(quality.get(k) is True for k in QUALITY_KEYS):
                        reward = -0.8
                    else:
                        spec = data.get("blueprint") or {}
                        half = spec.get("half_size", 2)
                        height = spec.get("wall_height", 3)
                        area = (2 * half - 1) ** 2
                        blocks = (8 * half - 1) * height + (2 * half + 1) ** 2
                        reward = 0.6 + 0.3 * min(1, area / 25) - 0.1 * min(1, blocks / 200)
                self._stat(action, reward)
                record = {"time": self.clock(), "kind": kind, "action": action, "domain": domain,
                          "reward": reward, "world": world, "revision": data.get("development_revision"),
                          "context": deepcopy(context), "details": deepcopy(data)}
                self.data["episodes"].append(record)
                self.data["episodes"] = self.data["episodes"][-400:]
                if self.pending and self.pending["action"] == action:
                    model = self.data["self_model"]
                    model["predictions"] += 1
                    model["squared_error"] += (self.pending["prediction"] - int(reward > 0)) ** 2
                    self.pending = None
                trial = self.data["trial"]
                if trial and data.get("development_revision") == self.data["revision"]:
                    if kind == "pain":
                        self._rollback("pain_during_trial")
                    elif self._matches(record, trial["domain"]):
                        trial["results"].append(reward)
                        required = 3 if trial["domain"] == "construction" else self.TRIAL_SAMPLES
                        if len(trial["results"]) >= required:
                            self._finish_trial()
                self.dirty = True
                self.save(force=True)
            else:
                self.dirty = True
                self.save(force=False)
            return self.summary()

    @staticmethod
    def _matches(record, domain):
        if domain in {"modules", "genes"}:
            # Decisions are scored on terminal actions, not on each step of a route.
            return record["kind"] in {"food_eaten", "build_completed", "build_failed", "rest_completed"}
        return record["domain"] == domain

    def _history(self, status, reason):
        trial = self.data["trial"]
        self.data["history"].append({
            "time": self.clock(), "revision": self.data["revision"], "status": status,
            "reason": reason, "proposal": deepcopy(trial),
        })
        self.data["history"] = self.data["history"][-40:]

    def _rollback(self, reason):
        trial = self.data["trial"]
        if not trial:
            return False
        previous = deepcopy(trial["previous"])
        self._history("rolled_back", reason)
        self.data["policy"] = previous
        self.data["revision"] += 1  # monotonic, including rollback
        self.data["trial"] = None
        print("[EVOLUTION] Откат: " + reason)
        return True

    def rollback(self, reason="human_request"):
        with self.lock:
            if not self.data["trial"] and self.data["history"]:
                last = self.data["history"][-1]
                if last["status"] == "retained" and last["revision"] == self.data["revision"]:
                    self.data["trial"] = deepcopy(last["proposal"])
            changed = self._rollback(reason)
            if changed:
                self.save()
            return changed

    def _finish_trial(self):
        trial = self.data["trial"]
        mean = sum(trial["results"]) / len(trial["results"])
        if mean >= trial["baseline"] + 0.02:
            self._history("retained", "world_outcomes_improved")
            self.data["trial"] = None
            print("[EVOLUTION] Версия сохранена по результатам пробы; это пока ограниченное свидетельство.")
        else:
            self._rollback("no_measured_improvement")

    def propose(self, proposal, source="local"):
        """One section per trial makes credit assignment and rollback reviewable."""
        with self.lock:
            if self.data["trial"]:
                return False, "trial_already_running"
            if not isinstance(proposal, dict) or set(proposal) != {"name", "reason", "changes"}:
                return False, "invalid_proposal_schema"
            changes = proposal["changes"]
            if not isinstance(changes, dict) or len(changes) != 1:
                return False, "change_one_section_at_a_time"
            domain = next(iter(changes))
            if domain not in set(DEFAULT_POLICY) | {"genes"}:
                return False, "unknown_section"
            candidate = deepcopy(self.data["policy"])
            try:
                if domain == "modules":
                    candidate[domain] = changes[domain]
                else:
                    if not isinstance(changes[domain], dict) or not changes[domain]:
                        raise ValueError("empty changes")
                    candidate[domain].update(changes[domain])
                validate_policy(candidate, self.data["origin_genome"])
                if domain == "genes" and any(abs(candidate[domain][k] - self.data["policy"][domain][k]) > 0.030001
                                              for k in candidate[domain]):
                    raise ValueError("Максимальная мутация за пробу: 0.03")
            except (ValueError, TypeError, KeyError) as exc:
                return False, str(exc)
            if candidate == self.data["policy"]:
                return False, "no_change"
            shadow = self.shadow_check(candidate)
            if not shadow["passed"]:
                return False, "offline_scenarios_failed"
            baseline = [r["reward"] for r in self.data["episodes"]
                        if r["world"] == self.data["world"] and self._matches(r, domain)][-24:]
            if len(baseline) < (2 if domain == "construction" else self.MIN_BASELINE):
                return False, "not_enough_world_experience"
            self.data["trial"] = {
                "name": str(proposal["name"])[:80], "reason": str(proposal["reason"])[:300],
                "source": source, "domain": domain, "previous": deepcopy(self.data["policy"]),
                "candidate": deepcopy(candidate),
                "started": self.clock(), "world": self.data["world"],
                "baseline": sum(baseline) / len(baseline), "results": [],
                "offline_check": shadow,
            }
            self.data["policy"] = deepcopy(candidate)
            self.data["revision"] += 1
            self.data["last_proposal_at"] = self.clock()
            self.save()
            print(f"[EVOLUTION] Проба версии {self.data['revision']}: {domain} ({source})")
            return True, "trial_started"

    def shadow_check(self, candidate):
        """Run an isolated decision copy; this does not predict Luanti physics."""
        clone = object.__new__(AdaptiveDevelopment)
        clone.lock = threading.RLock()
        clone.clock = self.clock
        clone.data = deepcopy(self.data)
        clone.data["policy"] = deepcopy(candidate)
        clone.pending = None
        scenarios = [
            ({"hunger": 2, "current_goal": "build"}, "seek_food"),
            ({"hunger": 20, "biochemistry": {"adenosine": 0.95}}, "rest"),
            ({"hunger": 20, "storm": True, "protected": False}, None),
        ]
        passed = True
        for context, expected in scenarios:
            choice = clone.recommend(context)
            passed = passed and (choice.get("action") if choice else None) == expected
        supported = 0
        records = self.data["episodes"][-24:]
        for record in records:
            context = deepcopy(record["context"])
            choice = clone.recommend(context)
            if choice:
                passed = passed and choice["action"] in ACTIONS
                supported += int(choice["action"] == record["action"])
        return {"passed": bool(passed), "need_scenarios": len(scenarios),
                "replayed": len(records), "matching_observed_actions": supported,
                "physics_tested": False}

    def maintain(self):
        with self.lock:
            trial = self.data["trial"]
            limit = 3600 if trial and trial["domain"] == "construction" else self.TRIAL_SECONDS
            if trial and (self.clock() - trial["started"] > limit
                          or trial["world"] != self.data["world"]):
                self._rollback("insufficient_evidence_or_world_changed")
                self.save()

    def ready(self):
        with self.lock:
            return (self.data["trial"] is None and len(self.data["episodes"]) >= self.MIN_BASELINE
                    and self.clock() - self.data["last_proposal_at"] >= 300)

    def local_proposal(self):
        with self.lock:
            p = self.data["policy"]
            slots = [
                {"body": {"walk_speed": 1.3 if p["body"]["walk_speed"] >= 1.5 else 1.5}},
                {"gathering": {"prefer_memory": not p["gathering"]["prefer_memory"]}},
                {"construction": {"half_size": 3 if p["construction"]["half_size"] == 2 else 2}},
                {"modules": list(reversed(p["modules"]))},
                {"genes": {"initiative_prob": round(max(0.0, min(1.0, p["genes"]["initiative_prob"]
                    + (0.02 if p["genes"]["initiative_prob"] <= self.data["origin_genome"]["initiative_prob"] else -0.02))), 6)}},
            ]
            index = self.data["next_mutation"] % len(slots)
            self.data["next_mutation"] += 1
            self.data["last_proposal_at"] = self.clock()
            self.save()
            return {"name": "experience_trial", "reason": "Сравнить вариант с прежними результатами",
                    "changes": slots[index]}

    def note_decision(self, command):
        with self.lock:
            action = command.get("action")
            stat = self.data["skills"].get(action, {})
            self.pending = {"action": action, "reason": command.get("reason"),
                            "prediction": (stat.get("successes", 0) + 1) / (stat.get("attempts", 0) + 2)}

    def recommend(self, context):
        """Hard needs precede the evolvable module order. Return a game intention."""
        with self.lock:
            hunger = context.get("hunger")
            if hunger is not None and float(hunger) < 8:
                return {"action": "seek_food", "reason": "energy_need"}
            if (context.get("night") or context.get("storm")) and not context.get("protected"):
                return None  # Luanti's safety controller owns shelter movement.
            bio = context.get("biochemistry") or {}
            if bio.get("adenosine", 0) > 0.85:
                return {"action": "rest", "reason": "rest_need"}
            current = position(context.get("position"))
            nodes = context.get("nodes") or []
            genes = self.data["policy"]["genes"]
            motives = context.get("motives") or {}
            for module in self.data["policy"]["modules"]:
                if module == "resource_memory" and current and self.data["policy"]["gathering"]["prefer_memory"]:
                    need_food = hunger is not None and hunger < 16
                    need_wood = context.get("current_goal") == "build"
                    if not need_food and not need_wood:
                        continue
                    candidates = [p for p in self.data["places"] if p["world"] == self.data["world"]
                                  and p["available"] and p["confidence"] >= 0.5
                                  and self.clock() - p["last_seen"] < 3600
                                  and ("apple" in p["resource"] if need_food else "tree" in p["resource"])
                                  and 3 < distance(current, p["position"]) <= 16]
                    if candidates:
                        target = min(candidates, key=lambda p: distance(current, p["position"]))
                        return {"action": "move_to", "target": target["position"], "reason": "remembered_resource"}
                elif module == "learned_actions":
                    candidates = []
                    for action in ("seek_food", "build", "explore", "rest"):
                        stat = self.data["skills"].get(action, {})
                        if stat.get("attempts", 0) < 3:
                            continue
                        key = {"seek_food": "energy", "build": "construction", "explore": "curiosity", "rest": "rest"}[action]
                        score = stat.get("value", 0) * 0.3 + motives.get(key, 0)
                        if action == "explore":
                            score += 0.1 * genes["initiative_prob"]
                        if action == "rest":
                            score += 0.05 * genes["adenosine_rate"]
                        if score > 0.65:
                            candidates.append((score, action))
                    if candidates:
                        return {"action": max(candidates)[1], "reason": "learned_outcome_and_motive"}
                elif module == "construction" and context.get("current_goal") == "build":
                    return {"action": "build", "reason": "continue_construction"}
                elif module == "exploration" and not any("apple" in n for n in nodes):
                    if motives.get("curiosity", 0) + genes["initiative_prob"] * 0.1 > 0.45:
                        return {"action": "explore", "reason": "curiosity_and_genome"}
            return None

    def summary(self):
        with self.lock:
            model = self.data["self_model"]
            trial = self.data["trial"]
            return {
                "revision": self.data["revision"],
                "trial": {"name": trial["name"], "domain": trial["domain"], "results": len(trial["results"])} if trial else None,
                "competence": deepcopy(self.data["skills"]),
                "body": deepcopy(self.data["body_model"]),
                "known_resources": len([p for p in self.data["places"] if p["world"] == self.data["world"] and p["available"]]),
                "resource_memories": deepcopy([p for p in self.data["places"] if p["world"] == self.data["world"]][-6:]),
                "intention": deepcopy(self.pending),
                "prediction_error": round(model["squared_error"] / model["predictions"], 4) if model["predictions"] else None,
                "unknowns": ["непроверенные навыки", "неосмотренные места", "субъективный опыт не измеряется"],
                "last_changes": [{k: h[k] for k in ("revision", "status", "reason")} for h in self.data["history"][-3:]],
            }
