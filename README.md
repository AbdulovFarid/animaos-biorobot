# Biorobot / AnimaOS

An experimental project featuring an autonomous AI agent named Aya (Ani) that lives in a game world and evolves through memory, biochemistry, emotions, and environmental interaction.

The project decouples the agent's cognitive core from the game adapter. This allows the agent's personality and memory to be transferred between different worlds and game environments.

## Features

- biochemical model: dopamine, serotonin, oxytocin, cortisol, adrenaline, and adenosine;
- genome, mutations, and generational persistence;
- long-term memory and experiential learning;
- empathy, trust, stress, and speech response;
- fast local female voices: Piper (`en_US-amy-medium` / `ru_RU-irina-medium`), Kokoro as a high-quality fallback, and `espeak` as a final backup;
- Ollama and Groq API integration;
- autonomous world exploration, foraging, apple gathering, and building;
- Luanti/Repixture adapter with game-controlled physics and collision handling.

## Architecture

- `anima_agent.py` — cognitive core, biochemistry, memory, dialogue, and evolution;
- `anima_evolution.py` — persistent experience, reversible behavioral/genetic trials, and decision-module composition;
- `luanti_bridge.py` — bridge between the core and the Luanti log, including a goal planner;
- `luanti_mod/anima_bridge/` — versioned Lua source for the installed Luanti adapter (existing models and textures are retained separately);
- `anima_world.py` — separate 2D world visualization;
- `main_Panda.py` — experimental Panda3D scene;
- `assets/models/` — Aya's models and textures;
- `world/` — game components of the experimental world.

## Installation

Requires Python 3.10+ and, for the core functionality, `numpy`. Voice output is optional; if TTS is missing, the system's `espeak` is used (if installed).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install numpy

# Fast CPU-based Piper voice + high-quality Kokoro fallback
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install piper-tts "kokoro>=0.9.4" soundfile
python -m piper.download_voices en_US-amy-medium ru_RU-irina-medium --download-dir models/piper
```

2D visualization requires `pygame`, while the Panda3D scene requires `panda3d` and `simplepbr`.

## Connecting Groq

The key is not stored in the project. Set it in your environment before launching:

```bash
export GROQ_API_KEY=your_key
export ANIMA_LLM_PROVIDER=groq
export GROQ_MODEL=openai/gpt-oss-20b
```

Local Ollama remains the fallback provider. Never commit the API key to Git.

## Launching the Luanti Bridge

```bash
cd /path/to/Biorobot
source ~/.config/biorobot/groq.env  # if using Groq
.venv/bin/python luanti_bridge.py
```

In the Repixture world, the `/anima_spawn` command creates Aya's body.

Key commands: `/anima_auto on`, `/anima_explore on`, `/anima_build start`, `/anima_hunger`, `/anima_memory`.

## Microphone Dialogue

The host-side launcher supports microphone dialogue through Groq Whisper. Start the game and bridge together:

```bash
./run_aya_luanti.sh
```

Inside Luanti, use:

```
/anima_listen
```

Aya listens for eight seconds and passes the transcription through the same dialogue, biochemistry, and memory pipeline as typed chat. Groq Whisper is tried first; if its audio endpoint is unavailable, the locally installed Vosk Russian model is used as a fallback. The Vosk package and model must be installed separately; model files are not included in Git. Set `AYA_MIC_RECORD_SECONDS` or `AYA_MIC_DEVICE` in the environment when needed. The recording is sent to Groq only when the Groq path is available.

## Memory and Private Data

Files in `vault/` contain Aya's state and personal memory. They are intentionally excluded from Git and must remain local.

## Развитие через опыт

Биохимия остаётся частью мотивов и реакции на события. Старый запрос Python-патча,
напрямую сбрасывавшего кортизол, заменён предложениями изменений поведения в JSON.
Озвучивание фразы об усталости само по себе больше не убирает усталость: восстановление
при отдыхе происходит после события из игрового тела. Голод остаётся отдельной игровой потребностью.

Реализованы следующие механизмы:

- Геном: пробные небольшие изменения существующих генов, исходный геном и история версий.
  Максимум 0.03 за пробу и 0.10 от исходного значения; принятая версия сохраняется между запусками.
- Память: предпочтения действий обновляются по результатам, известные места ресурсов
  привязаны к конкретному миру. Исчезнувший ресурс помечается недоступным.
  Исправления знаний сохраняют прежнюю запись и источник; человеческое утверждение не
  становится автоматически проверенным фактом. Пример в чате:
  `исправь память: яблоки под водой => яблоки на деревьях`.
- Модель себя: подтверждённые успехи/неудачи, намерение, прогноз успеха и ошибка прогноза.
  Это вычислительная модель самонаблюдения; субъективное сознание здесь не измеряется.
- Модель тела: результаты прыжков и маршрутов, наблюдаемая высота успешного подъёма,
  пробы скорости ходьбы и паузы между прыжками. Геометрия тела, гравитация и внешность
  не меняются автоматически.
- Добыча: возврат к запомненным ресурсам, проверка их наличия, выбор между памятью
  и поиском ближайшего дерева, накопление опыта удачных и неудачных попыток.
- Строительство: дома 5×5 и 7×7, стены высотой 3 или 4 блока, четыре направления входа.
  Требуемые доски вычисляются по плану. Перед признанием дома готовым проверяются опора,
  стены, крыша, пустое внутреннее пространство, вход и свободная клетка перед входом.
  При выборе нового проекта размеры уже построенного дома сохраняются отдельно.
- Структура программы поведения: порядок зарегистрированных модулей может изменяться.
  Обязательные потребности и игровой контроллер безопасности сохраняют приоритет.
  Произвольные Python-файлы модель не переписывает: изменяется исполняемая схема поведения.

Каждое предложение сначала проверяется на отдельной копии состояния: допустимая схема,
сценарии голода/отдыха/опасности и воспроизведение недавних контекстов. Эта проверка
не симулирует физику Luanti. Затем разрешается одна пробная версия в игре. Для сравнения
нужны минимум 6 наблюдаемых исходов (2 для строительства), затем 8 новых исходов пробы
(3 для строительства). При боли, отсутствии улучшения или нехватке данных за 15 минут
(60 минут для строительства) происходит откат. Нужны события именно применённой в Lua
версии: принятие команды или ответ ИИ не считаются выполненным действием.

Успешность оценивается по игровым исходам; для домов учитываются пригодность, полезная
площадь и число блоков. Сравнение пока наблюдательное, а не доказательство причинного
улучшения: разные участки мира могут иметь разную сложность. Человек может откатить
текущую пробу или последнюю принятую версию. Предложения поступают не чаще раза в 5 минут;
без облачного ИИ доступны локальные варианты для проверки опытом.

В игре:

```text
/anima_evolution status
/anima_evolution rollback
/anima_reflect
/anima_build start
```

Новая память находится в `vault/Aya_development.json` рядом с существующими
`Aya_learning.json` и `Aya_gen0.json`. Существующие записи и личность не сбрасываются.
Пути по умолчанию привязаны к проекту, а не к текущему каталогу терминала.

После обновления Python и Lua перезапустите мир и мост через `./run_aya_luanti.sh`.
Команды, heartbeat и текущая версия поведения обмениваются через `runtime/`
в каталоге установленного мода. Этот каталог доступен и хосту, и Flatpak;
их отдельные каталоги `/tmp` для обмена больше не используются.
Каталог создаёт Python-мост при первой публикации состояния. Lua не вызывает
`minetest.mkdir` в папке мода: такая операция запрещена защитой Luanti даже для
доверенного мода. Для чтения файлов и heartbeat используется уже разрешённый
`secure.trusted_mods = anima_bridge`; отключать защиту не требуется.
Доступ запрашивается в основном коде `init.lua` и передаётся локально в `body.lua`:
Luanti не выдаёт его при запросе из вспомогательного файла.
Для тестового запуска без автозапуска Python можно установить
`anima_bridge_autostart = false` в конфигурации Luanti.
Исходники Lua в репозитории следует копировать поверх исходников установленного
мода, сохраняя его каталоги `models/` и `textures/`.

Проверки без запуска игры, озвучивания и запросов к API:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Lua-проверки используют системную библиотеку Lua 5.3/5.2. Они проверяют все варианты
плана и цикл работы адаптера на тестовом мире; реальные столкновения и анимация
требуют отдельной проверки в Luanti.

## Status

The project is under active development. APIs and game mechanics are subject to change.
