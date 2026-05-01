# AGENTS.md — aeon-radio-drama

Instructions for AI agents that operate this tool.

## Step 0 — Determine execution mode

Before issuing any commands, figure out **where the CLI runs vs. where ComfyUI runs**. This shapes every shell command you'll generate.

### Local mode (CLI on the same machine as ComfyUI)

Symptoms:
- `COMFYUI_URL=http://127.0.0.1:8188` AND `curl -sf $COMFYUI_URL/system_stats` succeeds
- `COMFYUI_ROOT` points at a real local directory containing a `models/` subdir
- User mentions running "everything on one machine"

Action: **invoke directly.** No SSH.
```bash
python scripts/radio_drama.py <project> --stage all
```

### Remote mode (ComfyUI on a different machine)

Symptoms:
- `COMFYUI_URL` points to a non-loopback IP, OR is loopback only because of an SSH tunnel
- User mentions "GPU box", "DGX Spark", "headless server", "remote workstation"

Two sub-modes — **pick based on where the SFX library lives**:

**Remote-A — CLI runs LOCALLY, hits remote ComfyUI HTTP API:**
- Best for greenfield projects (no pre-existing SFX library on the remote box)
- `COMFYUI_ROOT` points to a LOCAL staging directory
- All project files (script.json, dialogue WAVs, music FLACs, SFX clips, final master) end up local
- ComfyUI does the GPU work remotely; outputs come back via HTTP
- No SSH commands needed for invocation

**Remote-B — CLI runs ON the remote machine via SSH:**
- Best when SFX library + previous renders already live on the GPU box
- Wrap commands: `ssh ${SSH_USER}@<host> 'cd /path/to/repo && python scripts/radio_drama.py <project> --stage all'`
- Pull final master afterwards: `scp ${SSH_USER}@<host>:/.../<project>_radio.mp3 .`

**Default to Remote-A unless the user explicitly asks for SSH-based runs or has a populated SFX library on the remote box.**

## ⛔ STOP — DO NOT BUILD COMFYUI WORKFLOWS BY HAND

Every workflow this pipeline uses (Qwen3-TTS, ACE Step music, MMAudio SFX, Stable Audio Open SFX) is **already built and tested** inside `scripts/radio_drama.py`. The reference JSON inside `SKILL.md` is documentation of what the orchestrator constructs internally — **not** a contract to reimplement.

**Symptoms you're heading the wrong direction:**
- "I'm checking the model loader for ACE Step…"
- "Let me find the proper VAE node…"
- "I need to figure out the negative conditioning structure…"
- HTTP 400 on `/prompt` submission with cryptic node validation errors
- "I don't have a compatible workflow template"

**The correct way:**
```bash
python scripts/radio_drama.py <project_name> --stage all
```
Or per-stage if you need to iterate: `--stage tts | music | sfx | mix` (idempotent — re-runs skip done work).

## Setup contract

1. **Verify ComfyUI** at `${COMFYUI_URL}`:
   ```bash
   curl -sf "$COMFYUI_URL/system_stats" >/dev/null && echo OK
   ```

2. **Run `./setup.sh`** to install Python deps and check model files. The script lists download commands for any missing models (ACE Step, MMAudio, Stable Audio Open, Qwen3-TTS).

3. **Confirm `.env` is filled in:**
   - `COMFYUI_URL` — required
   - `COMFYUI_ROOT` — only if you want outputs to land in a shared ComfyUI install

## Invocation contract

For a complete drama:
```bash
python scripts/radio_drama.py <project> --stage all
```

For only music or only SFX (or recovery from off-path workflows):
```bash
python scripts/music_maker.py --prompt "..." --duration 45 --bpm 70 --key "A minor" \
    --variant xl_base --master default --target-lufs -18 -o cue.flac

python scripts/sfx_maker.py --prompt "..." --duration 4 -o clip.wav
```

## Recovery patterns

### Dialogue/music exists outside the canonical layout

Don't try to retrofit. Two paths:

**Path A — re-stage and let `radio_drama.py` finish:**
```bash
mkdir -p output/<project>/audio/dialogue
mv <wherever>/dialogue/*.flac output/<project>/audio/dialogue/
# write script.json into output/<project>/, then:
python scripts/radio_drama.py <project> --stage music --stage sfx --stage mix
```

**Path B — fill the gap with standalone CLIs, mix manually:**
```bash
python scripts/music_maker.py ...   # one cue at a time
python scripts/sfx_maker.py ...     # one clip at a time
# then mix per references/mixing-guide.md
```

### Music cue or SFX needs regen

The pipeline is idempotent — delete the canonical file and re-run `--stage music` or `--stage sfx`:
```bash
rm output/<project>/audio/music/<cue>.wav
python scripts/radio_drama.py <project> --stage music
```

## Prompt engineering

Voice casting → `references/voice-casting.md` (Three-Lock system, ensemble contrast)
Music cues (under-dialogue tuning) → `SKILL.md` § Music mastering, "Prompting rules for under-dialogue beds"
SFX → `references/sfx-prompts.md` (specific physical descriptions, time-series phrasing for MMAudio)

## Failure modes

| Symptom | Fix |
|---|---|
| `Submit failed: 400` | Models or custom nodes missing → run `./sync.sh` |
| TTS hangs on one line | Strip emoji / unusual unicode from that line |
| Music cue clipped | `volume > 1.0` in the cue → clamp to ≤ 0.9 |
| SFX at wrong pitch | File dropped at 44.1k without canonicalization → `rm audio/sfx/<name>.wav && --stage sfx` |
| Final WAV truncated | Add a trailing `pause` element or raise `tail_silence` |
| MMAudio fails on first run | `pip install hf_xet` in ComfyUI venv, restart ComfyUI |
| All SFX engines fail | Models missing → `./setup.sh` |

## When NOT to use this repo

| User asks for | Use |
|---|---|
| A standalone music track | `aeon-music-maker` |
| Audio-reactive music video | `aeon-music-video` |
| A film with dialogue + score + SFX | `aeon-movie-maker` (uses this repo internally for the audio pass) |
| Just one-off SFX, no drama context | `scripts/sfx_maker.py` directly (this repo, but skip the orchestrator) |
