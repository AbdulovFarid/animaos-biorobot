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
## Experience-based development: Biochemistry remains a factor in motivations and reactions to events. The old Python patch that directly reset cortisol levels has been replaced by JSON-based behavioral change suggestions. Simply stating "I am tired" no longer removes fatigue; recovery occurs during rest, originating from the in-game body. Hunger remains a distinct game need. The following mechanisms have been implemented: - Genome: small experimental modifications to existing genes, plus the original genome and version history. Limits: max 0.03 change per trial and 0.10 deviation from the original value; the accepted version persists across sessions. - Memory: action preferences update based on outcomes, and known resource locations are tied to specific worlds. Depleted resources are marked as unavailable. Knowledge corrections preserve the original entry and source; a human assertion does not automatically become a verified fact. Chat example: `correct memory: apples underwater => apples on trees`. - Self-model: confirmed successes/failures, intentions, success predictions, and prediction errors. This is a computational model of self-observation; Subjective consciousness is not measured here. - Body model: results of jumps and routes, observed height of successful ascents, walking speed tests, and pauses between jumps. Body geometry, gravity, and appearance do not change automatically. - Foraging: returning to remembered resources, verifying their availability, choosing between memory and searching for the nearest tree, and accumulating experience from successful and unsuccessful attempts. - Construction: 5×5 and 7×7 houses, walls 3 or 4 blocks high, and four possible entrance directions. Required planks are calculated based on the plan. Before a house is deemed complete, the system checks for structural support, walls, a roof, an empty interior, an entrance, and an unobstructed space in front of the entrance. When a new design is selected, the dimensions of the previously built house are stored separately. - Behavioral program structure: the order of registered modules can be modified. Essential needs and the safety controller retain priority. The model does not overwrite arbitrary Python files; instead, the active behavioral scheme is modified. Each proposal is first verified against a separate copy of the state: checking for a valid layout, handling hunger/rest/danger scenarios, and reproducing recent contexts. This check does not simulate Luanti's physics engine. Then, a single trial version is permitted in the game. Comparison requires a minimum of 6 observed outcomes (2 for construction), followed by 8 new trial outcomes (3 for construction). A rollback occurs in the event of failure, lack of improvement, or insufficient data within 15 minutes (60 minutes for construction). Events must be those actually executed in the Lua version; merely accepting a command or receiving an AI response does not count as a completed action. Success is evaluated based on in-game outcomes; for houses, factors such as suitability, usable area, and block count are considered. The comparison is currently observational rather than proof of causal improvement, as different areas of the world may vary in complexity. A human can roll back the current trial or the last accepted version. Proposals are submitted no more than once every 5 minutes; without cloud AI, local variants are available for empirical testing. In-game: occur no more than once every 5 minutes; local options are available for verification, without relying on the cloud. In-game:
3 134
## Development through experience

Biochemistry remains a factor in motivations and reactions to events. The old Python patch request that directly cleared cortisol has been replaced by JSON-based proposals for behavioral changes. Simply voicing a phrase about fatigue no longer eliminates it; recovery through rest occurs as a post-event process within the game body. Hunger remains a distinct in-game need.

The following mechanisms have been implemented:

- Genome: small trial modifications to existing genes, plus the original genome and version history. 
Maximum change of 0.03 per trial and 0.10 relative to the original value; the accepted version persists across sessions.
- Memory: action preferences update based on results; known resource locations are tied to specific worlds. Depleted resources are marked as inaccessible. 
Knowledge corrections preserve the original entry and source; a human assertion does not automatically become a verified fact. Chat example:
`fix memory: apples underwater => apples on trees`.
- Self-model: confirmed successes/failures, intention, success prediction, and prediction error. 
This is a computational model of self-observation; subjective consciousness is not measured here.
- Body model: results of jumps and routes, observed height of successful climbs,
trials of walking speed and pauses between jumps. Body geometry, gravity, and appearance
do not change automatically.
- Foraging: returning to remembered resources, checking availability, choosing between
memory and searching for the nearest tree, and accumulating experience from successful and failed attempts.
- Construction: 5×5 and 7×7 houses, walls 3 or 4 blocks high, and four possible entrance directions. The required boards are calculated based on the plan. Before a house is deemed complete, the supports,
walls, roof, interior space, entrance, and the open area in front of the entrance are checked.
When selecting a new project, the dimensions of the already built house are saved separately.
- Behavior program structure: the order of registered modules can change. 
Essential needs and the game's safety controller retain priority. 
The model does not overwrite arbitrary Python files; instead, the executable behavior scheme is modified.

Each proposal is first verified against a separate copy of the state: checking for a valid layout,
handling hunger/rest/danger scenarios, and replaying recent contexts. This verification
does not simulate Luanti physics. Then, a single trial run is permitted in the game. Comparison
requires a minimum of 6 observed outcomes (2 for construction), followed by 8 new trial outcomes
(3 for construction). A rollback occurs in the event of pain, lack of improvement, or insufficient data
within 15 minutes (60 minutes for construction). Events must correspond to the version actually
implemented in Lua; merely accepting a command or receiving an AI response does not count as an executed action.

Success is evaluated based on in-game outcomes; for houses, suitability, usable
floor area, and block count are taken into account. The comparison is currently observational
rather than proof of causal improvement, as different areas of the world may vary in complexity.
A human user can roll back the current trial or the last accepted version. Proposals are
generated no more than once every 5 minutes; in the absence of cloud AI, local variants
are available for empirical testing.

In-game:
```text
/anima_evolution status
/anima_evolution rollback
/anima_reflect
/anima_build start
```
The new memory file is located at `vault/Aya_development.json`, alongside the existing
`Aya_learning.json` and `Aya_gen0.json`. Existing records and personality data are not reset.
Default paths are bound to the project rather than the current terminal directory.

After updating Python and Lua, restart the world and the bridge using `./run_aya_luanti.sh`.
Commands, heartbeats, and the current behavior version are exchanged via the `runtime/`
directory within the installed mod folder. This directory is accessible to both the host
and the Flatpak environment; separate `/tmp` directories are no longer used for exchange.
The directory is created by the Python bridge upon the first state publication. Lua does not
call `minetest.mkdir` inside the mod folder, as such operations are prohibited by Luanti's
security mechanisms, even for trusted mods. Reading files and heartbeats relies on the
already authorized `secure.trusted_mods = anima_bridge` setting; disabling security is unnecessary.
Access is requested in the main `init.lua` file and passed locally to `body.lua`,
since Luanti does not grant access when requested from a secondary file.
To perform a test run without automatically launching Python, you can set
`anima_bridge_autostart = false` in the Luanti configuration.
Lua source files from the repository should be copied over the installed mod's
source files, preserving its `models/` and `textures/` directories.

Checks performed without launching the game, audio output, or API requests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Lua ​​checks utilize the system's Lua 5.3/5.2 library. They verify all plan variants
and the adapter's operation loop within a test world; actual collisions and
animations require separate verification within Luanti.

## Status

The project is under active development. APIs and game mechanics are subject to change.
