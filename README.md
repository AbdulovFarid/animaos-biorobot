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
- `luanti_bridge.py` — bridge between the core and the Luanti log, including a goal planner;
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

## Memory and Private Data

Files in `vault/` contain Aya's state and personal memory. They are intentionally excluded from Git and must remain local.

## Status

The project is under active development. APIs and game mechanics are subject to change.
