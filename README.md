# aeon-radio-drama


[![☕ Tips](https://img.shields.io/badge/%E2%98%95_Tips-Support_the_work-ff5e5b?style=flat)](https://github.com/AEON-7/AEON-7#-support-the-work)
> Full-pipeline radio drama / audiobook / audio-fiction production. From a JSON script to a finished mixed master in one command. Multi-character TTS (Qwen3) → ACE Step music score → MMAudio/SAO/ACE-Step SFX (auto-routed by duration) → sidechain-ducked loudness-normalized mix → WAV + MP3 master.

Part of the **AEON Media Production** family.

## What this gives you

- **One-command production:** `python radio_drama.py <project> --stage all` — script.json in, mastered audio out.
- **Per-stage idempotency:** `--stage tts` / `music` / `sfx` / `mix`. Re-runs skip already-rendered work. Recover from any failure mid-render without redoing everything.
- **Three SFX engines, auto-routed:** MMAudio Large 44k v2 (≤10 s, primary), Stable Audio Open 1.0 (10–47 s), ACE Step (47+ s ambient beds). The pipeline picks the right engine per event.
- **Three-Lock voice persistence:** voice + seed + per-emotion overrides, so every line from a character has consistent timbre across an entire production. Designed for episodic series.
- **Sidechain-ducked mix bus:** music drops ~12 dB under speech automatically. Final master is EBU R128 −16 LUFS / −1.5 dBTP / LRA 11 (podcast standard).
- **Standalone helpers when the pipeline isn't a fit:** `music_maker.py` for music-only workflows, `sfx_maker.py` for one-shot SFX. Both wrap the same backend code without requiring the canonical project layout.

## Quick start

```bash
git clone https://github.com/AEON-7/aeon-radio-drama.git
cd aeon-radio-drama
cp .env.example .env       # set COMFYUI_URL + COMFYUI_ROOT
./setup.sh                 # check ComfyUI, install deps, fetch missing models

# Write a minimal script.json
mkdir -p output/my_drama
cat > output/my_drama/script.json <<'EOF'
{
  "title": "test",
  "characters": {"NARRATOR": {"voice": "warm male storyteller, 40s"}},
  "music_cues": {"bed": {"tags": "ambient cinematic slow", "duration": 40, "bpm": 70, "key": "A minor"}},
  "timeline": [
    {"t": 0, "type": "music", "cue": "bed", "fade_in": 2, "volume": 0.85},
    {"t": 3, "type": "line",  "character": "NARRATOR", "text": "This is a test of the broadcast."}
  ]
}
EOF

# Produce
python scripts/radio_drama.py my_drama --stage all
# → output/my_drama/my_drama_radio.wav  (24-bit master)
# → output/my_drama/my_drama_radio.mp3  (~190 kbps distribution)
```

## Script schemas

The pipeline accepts **either** of two formats — both produce identical output:

- **Schema A — flat timeline** (you control absolute offsets): `[{t: 5, type: "line", character: "NARRATOR", text: "..."}, ...]`
- **Schema B — acts/scenes/elements** (closer to a screenplay): `acts[].scenes[].elements[]` with `narration` / `dialogue` / `music_cue` / `sfx_cue` / `pause` / `direction` types

Schema B auto-flattens to Schema A internally, with **dialogue durations re-timed from probed TTS output** so music cues and scene boundaries shift if a voice came in 2 s slower than estimated.

Full schemas with annotated examples in `references/script-format.md`.

## Per-character voice casting

Three-Lock system per character:

```json
"voice_casting": {
  "NARRATOR": {
    "voice_instruct": "Calm measured feminine voice, BBC documentary clarity, unhurried",
    "seed": 200,
    "emotion_overrides": {
      "tense":      ", pace tightening slightly, undercurrent of urgency",
      "reflective": ", softer, almost speaking to herself"
    }
  }
}
```

Voice + seed = consistent timbre. `emotion_overrides[label]` appends to the base instruct for any line tagged with that emotion. Per-line `delivery_note` overrides the entire instruct for a single line. See `references/voice-casting.md` for the full design philosophy and an ensemble-design checklist.

## Mixing

Final mix chain (ffmpeg, applied automatically by `--stage mix`):

```
dialogue → speech bus → alimiter (transient protection)
                             │
                             ├── output to mix
                             └── side-chain key

music + SFX → amix → sidechaincompress (driven by speech key, ratio 8:1, threshold 0.05)
                  → amix with speech (weights 1.0 0.8)
                  → loudnorm I=-16:TP=-1.5:LRA=11
```

All sources are forced to canonical 48 kHz / stereo / pcm_s24le before they reach this chain — fixes the historical sample-rate-cross distortion bug that plagued earlier pipelines. See `references/mixing-guide.md`.

## Standalone helpers

If the canonical project layout doesn't fit your workflow, two CLIs let you generate music or SFX one-shot at a time without `radio_drama.py`:

```bash
# Music cue (any preset, any LUFS target)
python scripts/music_maker.py --prompt "ambient cinematic slow" --duration 45 \
    --bpm 70 --key "A minor" --variant xl_base --master default --target-lufs -18 \
    -o output/my_drama/music/cue1.flac

# SFX clip (auto-routed by duration to MMAudio / SAO / ACE)
python scripts/sfx_maker.py --prompt "Heavy wooden door slamming, stone corridor, deep echo" \
    --duration 4 -o output/my_drama/sfx/door_slam.wav
```

Use these when:
- Dialogue exists from a different source and you only need music/SFX
- You want to pre-generate cues and test before committing to the full mix
- You're orchestrating from a non-canonical project layout

See `SKILL.md` § "Standalone music + SFX generation".

## Prerequisites

- ComfyUI reachable at `${COMFYUI_URL}` (default `http://127.0.0.1:8188`)
- Python 3.10+, ffmpeg + ffprobe on PATH
- ~50 GB disk for full model set (ACE Step XL base + MMAudio + Stable Audio Open + Qwen3-TTS)

`setup.sh` checks all of this and prints download commands for any missing models.

## Project structure

```
aeon-radio-drama/
├── README.md
├── AGENTS.md          agent invocation contract
├── SKILL.md           full skill: prompt engineering, recovery patterns, standalone CLIs
├── PRODUCTION.md      operational runbook (SSH, troubleshooting, deliverables layout)
├── ATTRIBUTION.md     upstream credits
├── LICENSE
├── .env.example
├── setup.sh
├── sync.sh
├── requirements.txt
├── scripts/
│   ├── radio_drama.py     orchestrator (the main entry point)
│   ├── music_maker.py     standalone music CLI (also imported by radio_drama)
│   ├── music_mastering.py dynamics-preserving mastering chain
│   └── sfx_maker.py       standalone SFX CLI
├── templates/
│   ├── ace_step_music_apg_api.json
│   └── ace_step_music_simple_api.json
└── references/
    ├── script-format.md     full JSON schema, dual-format examples
    ├── voice-casting.md     Three-Lock system + ensemble design
    ├── mixing-guide.md      ffmpeg filter chains for assembly + mastering
    └── sfx-prompts.md       SFX prompt library by category
```

## License

MIT. See `LICENSE`.

## See also

- [`aeon-music-maker`](https://github.com/AEON-7/aeon-music-maker) — standalone music generation
- [`aeon-movie-maker`](https://github.com/AEON-7/aeon-movie-maker) — fast cinematic video
- [`aeon-music-video`](https://github.com/AEON-7/aeon-music-video) — audio-reactive video generation
- [`comfyui-aeon-spark`](https://github.com/AEON-7/comfyui-aeon-spark) — base ComfyUI Docker stack

---

## ☕ Support the work

If this release has been useful, tips are deeply appreciated — they go directly toward more compute, more models, and more open releases.

<table align="center">
  <tr>
    <td align="center" width="50%">
      <strong>₿ Bitcoin (BTC)</strong><br/>
      <img src="https://raw.githubusercontent.com/AEON-7/AEON-7/main/assets/qr/btc.png" alt="BTC QR" width="200"/><br/>
      <sub><code>bc1q09xmzn00q4z3c5raene0f3pzn9d9pvawfm0py4</code></sub>
    </td>
    <td align="center" width="50%">
      <strong>Ξ Ethereum (ETH)</strong><br/>
      <img src="https://raw.githubusercontent.com/AEON-7/AEON-7/main/assets/qr/eth.png" alt="ETH QR" width="200"/><br/>
      <sub><code>0x1512667F6D61454ad531d2E45C0a5d1fd82D0500</code></sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <strong>◎ Solana (SOL)</strong><br/>
      <img src="https://raw.githubusercontent.com/AEON-7/AEON-7/main/assets/qr/sol.png" alt="SOL QR" width="200"/><br/>
      <sub><code>DgQsjHdAnT5PNLQTNpJdpLS3tYGpVcsHQCkpoiAKsw8t</code></sub>
    </td>
    <td align="center" width="50%">
      <strong>ⓜ Monero (XMR)</strong><br/>
      <img src="https://raw.githubusercontent.com/AEON-7/AEON-7/main/assets/qr/xmr.png" alt="XMR QR" width="200"/><br/>
      <sub><code>836XrSKw4R76vNi3QPJ5Fa9ugcyvE2cWmKSPv3AhpTNNKvqP8v5ba9JRL4Vh7UnFNjDz3E2GXZDVVenu3rkZaNdUFhjAvgd</code></sub>
    </td>
  </tr>
</table>

> **Ethereum L2s (Base, Arbitrum, Optimism, Polygon, etc.) and EVM-compatible tokens** can be sent to the same Ethereum address.
