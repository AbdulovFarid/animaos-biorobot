"""
anima_world.py — 2D мир для AnimaAgent (Pygame)

Запуск:
    python anima_world.py

Управление:
    Клик по полю ввода снизу → печатать текст → Enter — отправить Ае
    ПРОБЕЛ (удержание)        — позвать Аю к курсору мыши
    ESC / закрыть окно         — выход (геном автоматически сохраняется)

Ая отвечает через DialogueEngine (локальная модель в Ollama) — ответ может
занимать 10-60 секунд на CPU. Пока она "думает", над ней горит индикатор
"...". Когда ответ готов — текст показывается в облаке над ней и
озвучивается (если pyttsx3 доступен).
"""

import math
import os
import random
import time

import pygame

from anima_agent import AnimaAgent, GenomeEncoder

# ── Настройки окна ───────────────────────────────────────────────────────────
WIDTH, HEIGHT = 960, 700
WORLD_HEIGHT = 580   # верхняя часть — мир, нижняя — панель ввода
FPS = 60

# ── Палитра ───────────────────────────────────────────────────────────────────
COLORS = {
    "bg":         (18, 18, 24),
    "garden":     (40, 90, 60),
    "hearth":     (110, 60, 40),
    "wasteland":  (70, 40, 45),
    "den":        (35, 45, 80),
    "agent":      (240, 220, 120),
    "agent_glow": (255, 245, 190),
    "agent_talk": (140, 220, 255),
    "text":       (230, 230, 230),
    "bar_bg":     (50, 50, 55),
    "call_ring":  (255, 255, 255),
    "input_bg":   (30, 30, 38),
    "input_border": (80, 80, 95),
    "bubble_bg":  (45, 45, 60),
}

BIOMES = [
    {"name": "Сад",      "rect": pygame.Rect(40, 40, 380, 230),  "color": COLORS["garden"],
     "effect": {"serotonin": +0.01, "cortisol": -0.006}},
    {"name": "Очаг",     "rect": pygame.Rect(540, 40, 380, 230), "color": COLORS["hearth"],
     "effect": {"oxytocin": +0.012, "dopamine": +0.006}},
    {"name": "Пустошь",  "rect": pygame.Rect(40, 330, 380, 230), "color": COLORS["wasteland"],
     "effect": {"cortisol": +0.01, "adrenaline": +0.005}},
    {"name": "Грот сна", "rect": pygame.Rect(540, 330, 380, 230), "color": COLORS["den"],
     "effect": {"adenosine": -0.015}},
]

HOME_POS = (WIDTH // 2, WORLD_HEIGHT // 2)


def biome_for_deficit(blood: dict) -> dict:
    scores = {
        "Сад":      blood["cortisol"] * 1.0 + (1 - blood["serotonin"]) * 0.6,
        "Очаг":     (1 - blood["oxytocin"]) * 1.0 + (1 - blood["dopamine"]) * 0.4,
        "Пустошь":  -1.0,
        "Грот сна": blood["adenosine"] * 1.2,
    }
    best_name = max(scores, key=scores.get)
    return next(b for b in BIOMES if b["name"] == best_name)


class WorldAgent:
    SPEED = 90.0

    def __init__(self, core: AnimaAgent, pos=None):
        self.core = core
        self.pos = list(pos or HOME_POS)
        self.target = list(self.pos)
        self.current_biome_name = "—"
        self.last_decision_time = 0.0
        self.called = False

        # ── Состояние диалога для визуализации ──
        self.is_thinking = False
        self.last_said = ""
        self.last_said_until = 0.0  # time.time(), до которого показывать облако

    def decide_target(self, now: float, call_pos=None):
        if call_pos is not None:
            self.called = True
            self.target = list(call_pos)
            self.current_biome_name = "← Зов"
            return

        self.called = False
        if now - self.last_decision_time < 2.0:
            return
        self.last_decision_time = now

        biome = biome_for_deficit(self.core.blood)
        self.current_biome_name = biome["name"]
        rect = biome["rect"]
        margin = 20
        self.target = [
            random.randint(rect.left + margin, rect.right - margin),
            random.randint(rect.top + margin, rect.bottom - margin),
        ]

    def update_position(self, dt: float):
        dx = self.target[0] - self.pos[0]
        dy = self.target[1] - self.pos[1]
        dist = math.hypot(dx, dy)
        if dist < 2:
            return
        step = min(self.SPEED * dt, dist)
        self.pos[0] += dx / dist * step
        self.pos[1] += dy / dist * step

    def apply_biome_effect(self):
        if self.called:
            return
        for biome in BIOMES:
            if biome["rect"].collidepoint(self.pos):
                with self.core.lock:
                    for key, delta in biome["effect"].items():
                        self.core.blood[key] = max(0.0, min(1.0, self.core.blood[key] + delta))
                break

    # ── Диалог ────────────────────────────────────────────────────────────────
    def send_message(self, text: str):
        """Отправляет сообщение Ае и просит её визуально показать что думает."""
        self.is_thinking = True

        def on_reply(reply: str):
            self.is_thinking = False
            self.last_said = reply
            self.last_said_until = time.time() + max(4.0, len(reply) * 0.08)

        self.core.chat(text, on_reply=on_reply)


class TextInput:
    """Простое однострочное текстовое поле для Pygame."""

    def __init__(self, rect: pygame.Rect, font):
        self.rect = rect
        self.font = font
        self.text = ""
        self.active = False
        self.cursor_visible = True
        self.cursor_timer = 0.0

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
        elif event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_RETURN:
                submitted = self.text.strip()
                self.text = ""
                return submitted
            elif event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key == pygame.K_ESCAPE:
                self.active = False
            else:
                if event.unicode and event.unicode.isprintable():
                    self.text += event.unicode
        return None

    def update(self, dt: float):
        self.cursor_timer += dt
        if self.cursor_timer >= 0.5:
            self.cursor_timer = 0.0
            self.cursor_visible = not self.cursor_visible

    def draw(self, screen):
        border_color = (140, 180, 220) if self.active else COLORS["input_border"]
        pygame.draw.rect(screen, COLORS["input_bg"], self.rect, border_radius=8)
        pygame.draw.rect(screen, border_color, self.rect, width=2, border_radius=8)

        display = self.text
        if self.active and self.cursor_visible:
            display += "│"
        surf = self.font.render(display or "Напиши Ае что-нибудь и нажми Enter...", True,
                                 COLORS["text"] if (self.text or self.active) else (110, 110, 115))
        screen.blit(surf, (self.rect.x + 12, self.rect.y + (self.rect.height - surf.get_height()) // 2))


def draw_biomes(screen, font):
    for biome in BIOMES:
        pygame.draw.rect(screen, biome["color"], biome["rect"], border_radius=14)
        pygame.draw.rect(screen, (255, 255, 255), biome["rect"], width=1, border_radius=14)
        label = font.render(biome["name"], True, COLORS["text"])
        screen.blit(label, (biome["rect"].x + 10, biome["rect"].y + 8))


def wrap_text(text, font, max_width):
    words = text.split(" ")
    lines, current = [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if font.size(trial)[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_agent(screen, wa: WorldAgent, font):
    x, y = int(wa.pos[0]), int(wa.pos[1])
    pulse = math.sin(time.time() * 3)
    glow_radius = 16 + int(4 * pulse)

    color = COLORS["agent_talk"] if (wa.is_thinking or time.time() < wa.last_said_until) else COLORS["agent_glow"]
    pygame.draw.circle(screen, color, (x, y), glow_radius, width=2)
    pygame.draw.circle(screen, COLORS["agent"], (x, y), 10)
    if wa.called:
        pygame.draw.circle(screen, COLORS["call_ring"], (x, y), 22, width=1)

    # ── Индикатор "думает" ──
    if wa.is_thinking:
        dots = "." * (1 + int(time.time() * 2) % 3)
        label = font.render(dots, True, COLORS["agent_talk"])
        screen.blit(label, (x - 6, y - 34))

    # ── Облако с последней репликой ──
    elif time.time() < wa.last_said_until and wa.last_said:
        lines = wrap_text(wa.last_said, font, 260)
        padding = 10
        line_height = font.get_height() + 2
        bubble_w = 280
        bubble_h = padding * 2 + line_height * len(lines)
        bubble_x = max(10, min(x - bubble_w // 2, WIDTH - bubble_w - 10))
        bubble_y = max(10, y - 30 - bubble_h)

        bubble_rect = pygame.Rect(bubble_x, bubble_y, bubble_w, bubble_h)
        pygame.draw.rect(screen, COLORS["bubble_bg"], bubble_rect, border_radius=10)
        pygame.draw.rect(screen, COLORS["agent_talk"], bubble_rect, width=1, border_radius=10)

        for i, line in enumerate(lines):
            surf = font.render(line, True, COLORS["text"])
            screen.blit(surf, (bubble_x + padding, bubble_y + padding + i * line_height))


def draw_biopanel(screen, font, wa: WorldAgent):
    b = wa.core.blood
    line = (
        f"{wa.core.name}  | Gen {wa.core.generation}  | Биом: {wa.current_biome_name}  | "
        f"Сообщений: {wa.core.interaction_count}"
    )
    surf = font.render(line, True, COLORS["text"])
    screen.blit(surf, (16, WORLD_HEIGHT - 26))

    bar_x = WIDTH - 170
    bar_y = 16
    for key, color in [
        ("dopamine", (240, 200, 80)), ("serotonin", (100, 200, 140)),
        ("oxytocin", (230, 120, 180)), ("cortisol", (210, 90, 90)),
        ("adenosine", (120, 140, 220)),
    ]:
        value = b[key]
        pygame.draw.rect(screen, COLORS["bar_bg"], (bar_x, bar_y, 150, 10))
        pygame.draw.rect(screen, color, (bar_x, bar_y, int(150 * value), 10))
        label = font.render(key[:3].upper(), True, COLORS["text"])
        screen.blit(label, (bar_x - 36, bar_y - 2))
        bar_y += 16


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("AnimaWorld — мир Аи")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas", 16)
    small_font = pygame.font.SysFont("consolas", 14)
    bubble_font = pygame.font.SysFont("consolas", 15)

    vault_files = GenomeEncoder.list_vault()
    if vault_files:
        latest = sorted(vault_files)[-1]
        core = GenomeEncoder.load_from_disk(os.path.join("vault", latest))
    else:
        core = AnimaAgent(name="Aya")

    wa = WorldAgent(core, pos=HOME_POS)
    text_input = TextInput(pygame.Rect(16, WORLD_HEIGHT + 16, WIDTH - 32, 40), font)

    running = True
    while running:
        dt = clock.tick(FPS) / 1000.0
        now = time.time()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            else:
                submitted = text_input.handle_event(event)
                if submitted:
                    wa.send_message(submitted)

        keys = pygame.key.get_pressed()
        if keys[pygame.K_SPACE] and not text_input.active:
            wa.decide_target(now, call_pos=pygame.mouse.get_pos())
        else:
            wa.decide_target(now, call_pos=None)

        wa.update_position(dt)
        wa.apply_biome_effect()
        text_input.update(dt)

        screen.fill(COLORS["bg"])
        draw_biomes(screen, font)
        draw_agent(screen, wa, bubble_font)
        draw_biopanel(screen, small_font, wa)
        text_input.draw(screen)

        hint = small_font.render(
            "ПРОБЕЛ — позвать к курсору   |   Enter — отправить сообщение   |   ESC — выход",
            True, (150, 150, 150),
        )
        screen.blit(hint, (16, 10))

        pygame.display.flip()

    core.stop()
    GenomeEncoder.save_to_disk(core)
    pygame.quit()
    print("[SYSTEM] Мир закрыт. Геном сохранён.")


if __name__ == "__main__":
    main()