#!/usr/bin/env python3
"""Radio Drama Production Tool — dialogue + music + SFX mix for audio-only pieces.

Fixes the sample-rate distortion in prior stitch pipelines by enforcing a
canonical 48kHz / stereo / float32 format at *every* mix input BEFORE any
filter, using explicit `aresample` + `aformat` on each branch.

Pipeline:
  1. TTS dialogue per character line (Qwen3-TTS VoiceDesign preferred, Chatterbox fallback)
  2. ACE Step 1.5 XL turbo music beds (per cue, not a single bed)
  3. SFX sources (in priority order):
       a) manual WAVs in input/sfx/  (best control, drop your own library)
       b) ACE Step with SFX-oriented tags  (ambient / textural, works out of the box)
       c) Stable Audio Open (if ComfyUI-StableAudioOpen installed)
       d) MMAudio if ComfyUI-MMAudio node is installed  (16kHz native)
       e) ElevenLabs SoundEffects API if ELEVENLABS_API_KEY is set  (best quality, paid)
  4. Mix — all inputs hard-converted to 48k/stereo/f32, music side-chained to
     dialogue, SFX level-matched, final loudnorm to -16 LUFS (podcast standard).

Supports TWO script schemas:
  A) Flat timeline (simpler, best when you already know offsets)
  B) acts/scenes/elements (literary, matches the radio-drama-producer skill)

The normalizer auto-detects which schema is in use and converts B → A by
walking elements sequentially and accumulating durations.

---- Schema A: Flat timeline ----
{
  "title": "Episode 1",
  "characters": {
    "NARRATOR": {"voice": "male, warm, 30s, reflective", "instruct": "calm, steady"},
    "AYA":      {"voice": "young feminine, fragile", "instruct": "vulnerable"}
  },
  "music_cues": {
    "open":   {"tags": "ambient drone, sacred, slow",       "duration": 30, "bpm": 65, "key": "E minor"},
    "tension":{"tags": "low strings, dissonance, film score","duration": 20, "bpm": 80, "key": "C minor"}
  },
  "timeline": [
    {"t": 0,    "type": "music", "cue": "open",   "fade_in": 2.0, "volume": 0.9},
    {"t": 4,    "type": "line",  "character": "NARRATOR", "text": "The city was asleep, mostly."},
    {"t": 10,   "type": "sfx",   "source": "door_slam.wav", "volume": 0.7},
    {"t": 11,   "type": "line",  "character": "AYA",      "text": "Who's there?"}
  ]
}

---- Schema B: acts/scenes/elements (from radio-drama-producer skill) ----
{
  "title": "The Last Lighthouse",
  "genre": "mystery/thriller",
  "characters": ["NARRATOR", "INSPECTOR", "KEEPER"],  // OR the dict form from A
  "acts": [
    {
      "act_number": 1,
      "scenes": [
        {
          "scene_id": "act1_scene1",
          "elements": [
            {"type": "sfx_cue",   "description": "heavy storm", "duration_s": 6, "volume": 0.7},
            {"type": "music_cue", "mood": "tense", "description": "low drone", "duration_s": 45, "fade_in_s": 3},
            {"type": "narration", "text": "...", "emotion": "neutral"},
            {"type": "dialogue",  "character": "INSPECTOR", "text": "Hello?", "emotion": "cautious"},
            {"type": "pause",     "duration_s": 2},
            {"type": "direction", "note": "(ignored)"}
          ]
        }
      ]
    }
  ],
  "voice_casting": {              // OPTIONAL — pair with Schema B since
    "NARRATOR":  {"voice_instruct": "...", "seed": 200,
                  "emotion_overrides": {"tense": ", pace tightening"}},
    "INSPECTOR": {"voice_instruct": "...", "seed": 310,
                  "emotion_overrides": {"cautious": ", speaking more quietly"}}
  }
}

Usage:
  python radio_drama.py <project_name> --stage all
  python radio_drama.py <project_name> --stage tts      # regen dialogue only
  python radio_drama.py <project_name> --stage music    # regen music cues only
  python radio_drama.py <project_name> --stage sfx      # stage + generate SFX only
  python radio_drama.py <project_name> --stage mix      # re-mix without regen
"""
import argparse, json, os, random, shutil, subprocess, sys, time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMFYUI_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

FFMPEG = shutil.which("ffmpeg") or os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = shutil.which("ffprobe") or os.environ.get("FFPROBE", "ffprobe")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
OUTPUT_ROOT = os.path.join(COMFYUI_ROOT, "output")

# Canonical format — every mixable stream gets hard-converted to this.
SR = 48000         # sample rate
CH = 2             # channels
FMT = "fltp"       # planar float32 (ffmpeg's native mix format)
AFORMAT = f"aformat=sample_rates={SR}:sample_fmts={FMT}:channel_layouts=stereo"

# Loudness targets (EBU R128 / broadcast podcast standard)
LOUDNORM_I = -16   # integrated LUFS
LOUDNORM_TP = -1.5 # true peak dBTP
LOUDNORM_LRA = 11  # loudness range


def run(cmd, check=True, capture=True):
    """Run a subprocess, print a short trace, raise on nonzero."""
    head = " ".join(f'"{c}"' if " " in c else c for c in cmd[:6])
    print(f"$ {head}{' ...' if len(cmd) > 6 else ''}", flush=True)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        print("STDERR:", (r.stderr or "")[-2000:])
        raise RuntimeError(f"ffmpeg failed: {cmd[0]} returned {r.returncode}")
    return r


def probe_duration(path):
    r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path])
    return float(r.stdout.strip())


def probe_sample_rate(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate", "-of", "csv=p=0", path])
    return int(r.stdout.strip())


def canonicalize(src, dst):
    """Convert any audio source to the canonical 48kHz stereo WAV used
    throughout the mix. This is the single bottleneck where rate mismatches
    get resolved — every downstream filter sees the same format."""
    run([FFMPEG, "-y", "-i", src,
         "-ac", str(CH), "-ar", str(SR),
         "-c:a", "pcm_s24le",        # 24-bit PCM (headroom before loudnorm)
         dst])


def comfy_request(path, data=None, timeout=30):
    url = f"{COMFYUI_URL}{path}"
    if data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def submit_and_wait(wf, client_id, poll_timeout=600, poll_every=3):
    res = comfy_request("/prompt", {"prompt": wf, "client_id": client_id})
    pid = res.get("prompt_id")
    if not pid:
        raise RuntimeError(f"Submit failed: {res}")
    start = time.time()
    while time.time() - start < poll_timeout:
        h = comfy_request(f"/history/{pid}")
        if pid in h:
            return h[pid]
        time.sleep(poll_every)
    raise TimeoutError(f"{pid} timed out after {poll_timeout}s")


# ============================================================================
# Schema normalizer — accept both flat-timeline and acts/scenes/elements
# ============================================================================

# Mapping from common emotion labels → Qwen3-TTS instruct overrides.
# Matches the table in tts-voice-designer/SKILL.md so voices stay consistent
# when a script authored via that skill is produced here.
EMOTION_OVERRIDES = {
    "neutral":       "",
    "tender":        ", speaking with gentle tenderness and quiet intimacy",
    "broken":        ", voice cracking with grief, barely holding together",
    "commanding":    ", projecting with full authority, each word a decree",
    "fearful":       ", voice tight with fear, words tumbling out faster",
    "intimate":      ", barely above a whisper, close and confiding",
    "furious":       ", voice shaking with barely contained rage",
    "joyful":        ", bright and warm with genuine delight",
    "sorrowful":     ", heavy with sadness, each word weighted with loss",
    "contemplative": ", thoughtful and measured, as if thinking aloud",
    "tense":         ", pace tightening slightly, an undercurrent of urgency",
    "cautious":      ", speaking more quietly, carefully measuring each word",
    "defiant":       ", finding unexpected steel, each word planted firmly",
    "reflective":    ", softer and more contemplative",
    "angry":         ", voice tight with controlled fury",
    "anguished":     ", voice raw with anguish, each word a wound",
}

# Rough TTS duration estimate for Schema B pre-normalization (before we know
# actual render durations). 150 wpm ≈ 0.4 s / word, plus a small tail pad.
WORDS_PER_SECOND = 150 / 60.0


def _estimate_tts_duration(text):
    words = max(1, len(text.split()))
    return words / WORDS_PER_SECOND + 0.4


def _normalize_characters(raw):
    """Accept either a list of character IDs or a dict of {id: {...}}.

    When raw is a list, return a dict with empty descriptors; the caller
    fills in voice data from `voice_casting` or defaults."""
    if isinstance(raw, dict):
        return dict(raw)
    chars = {}
    for name in raw or []:
        chars[name] = {}
    return chars


def _merge_voice_casting(characters, voice_casting):
    """Fold voice_casting (from Schema B) into characters dict.

    voice_casting entries use {voice_instruct, seed, emotion_overrides};
    we translate those into our {voice, instruct, emotion_overrides} keys
    so both schemas end up with the same shape downstream.
    """
    for name, cast in (voice_casting or {}).items():
        c = characters.setdefault(name, {})
        if "voice_instruct" in cast and "voice" not in c:
            c["voice"] = cast["voice_instruct"]
        if "seed" in cast:
            c["seed"] = cast["seed"]
        if "emotion_overrides" in cast:
            c["emotion_overrides"] = cast["emotion_overrides"]
    # Defaults for any character lacking a voice description
    for name, c in characters.items():
        if "voice" not in c:
            c["voice"] = "neutral adult voice, natural pacing"
        if "instruct" not in c:
            c["instruct"] = "natural delivery"
    return characters


def _resolve_instruct(char_def, emotion=None, override=None):
    """Compose the final instruct string: base + per-emotion append + per-line override."""
    base = char_def.get("instruct") or ""
    if emotion:
        # Per-character override table wins over global table
        per_char = (char_def.get("emotion_overrides") or {}).get(emotion, "")
        add = per_char or EMOTION_OVERRIDES.get(emotion, "")
        if add:
            base = base + add if base else add.lstrip(", ")
    if override:
        base = override
    return base or "natural delivery"


def normalize_script(script):
    """Return a canonical script dict with a flat timeline, whatever schema
    was provided on disk.

    Rules for Schema B (acts/scenes/elements):
      - Elements play SEQUENTIALLY; cumulative `t` is the sum of durations
        encountered so far.
      - narration / dialogue duration is *estimated* from word count here;
        the mix stage re-probes actual TTS renders and rebuilds the timeline
        with real durations before final assembly.
      - music_cue elements advance time by 0 (music plays underneath other
        elements). They register a named cue automatically and place a
        `music` event at the current t.
      - sfx_cue advances time by its duration_s (SFX is foreground by default
        in this schema; if you want it layered under dialogue, wrap it in a
        direction or use Schema A).
      - pause advances time by duration_s.
      - direction is skipped.

    Rules for Schema A (flat timeline): passes through unchanged.
    """
    if "timeline" in script and "acts" not in script:
        # Schema A — just make sure characters dict is well-formed
        script = dict(script)
        script["characters"] = _normalize_characters(script.get("characters", {}))
        script["characters"] = _merge_voice_casting(script["characters"],
                                                     script.get("voice_casting"))
        return script

    # Schema B — synthesize
    out = dict(script)
    chars = _normalize_characters(script.get("characters", []))
    chars = _merge_voice_casting(chars, script.get("voice_casting"))
    out["characters"] = chars

    music_cues = dict(script.get("music_cues", {}))
    timeline = []
    t = 0.0
    auto_cue_idx = 0

    def register_cue(element):
        """Register a music_cue element as a named cue for stage_music to generate."""
        nonlocal auto_cue_idx
        # Prefer explicit `preset` or `mood` name; else auto-index
        name = element.get("cue") or element.get("preset") or \
               element.get("mood") or f"cue_{auto_cue_idx}"
        auto_cue_idx += 1
        tags = element.get("description") or element.get("tags") or \
               element.get("mood") or "ambient cinematic"
        music_cues.setdefault(name, {
            "tags": tags,
            "duration": float(element.get("duration_s") or element.get("duration") or 30),
            "bpm": int(element.get("bpm", 72)),
            "key": element.get("key") or element.get("keyscale") or "A minor",
        })
        return name

    for act in script.get("acts", []):
        for scene in act.get("scenes", []):
            for el in scene.get("elements", []):
                etype = el.get("type")
                if etype == "direction":
                    continue
                elif etype == "pause":
                    t += float(el.get("duration_s", el.get("duration", 0)))
                elif etype == "music_cue":
                    name = register_cue(el)
                    timeline.append({
                        "t": t,
                        "type": "music",
                        "cue": name,
                        "fade_in": float(el.get("fade_in_s", el.get("fade_in", 1.5))),
                        "fade_out": float(el.get("fade_out_s", el.get("fade_out", 2.0))),
                        "volume": float(el.get("volume", 0.85)),
                    })
                    # Music does NOT advance the narrative clock
                elif etype == "sfx_cue":
                    # Source defaults to a sanitized description
                    desc = el.get("description") or el.get("source") or "sfx"
                    # Slugify for file name
                    src = el.get("source") or "".join(
                        c if c.isalnum() else "_" for c in desc.lower()
                    )[:48].strip("_") + ".wav"
                    dur = float(el.get("duration_s", el.get("duration", 3)))
                    timeline.append({
                        "t": t,
                        "type": "sfx",
                        "source": src,
                        "name": src,
                        "volume": float(el.get("volume", 0.8)),
                        "generate": el.get("generate", True),
                        "tags": desc,
                        "duration": dur,
                    })
                    t += dur
                elif etype in ("narration", "dialogue"):
                    char = el.get("character") or ("NARRATOR" if etype == "narration" else None)
                    if not char:
                        continue
                    # Ensure character exists (narration can reference a non-listed NARRATOR)
                    if char not in chars:
                        chars[char] = {
                            "voice": "calm measured voice, storyteller cadence",
                            "instruct": "natural delivery",
                        }
                    emotion = el.get("emotion")
                    instruct = _resolve_instruct(chars[char], emotion=emotion,
                                                  override=el.get("delivery_note"))
                    text = el.get("text", "").strip()
                    if not text:
                        continue
                    dur = _estimate_tts_duration(text)
                    timeline.append({
                        "t": t,
                        "type": "line",
                        "character": char,
                        "text": text,
                        "instruct": instruct,
                        "emotion": emotion,
                        "_est_duration": dur,
                    })
                    t += dur

    out["music_cues"] = music_cues
    out["timeline"] = timeline
    out["_duration_source"] = "estimated"  # stage_mix may re-normalize with real durations
    return out


def rebuild_timeline_with_real_durations(script, dialogue_map, sfx_map):
    """After TTS renders, recompute Schema-B timelines using actual audio
    durations. Leaves Schema-A scripts untouched.

    Dialogue events are re-timed; music events shift with the line that
    precedes them so the scene stays coherent even if TTS came out longer
    or shorter than estimate.
    """
    if not script.get("acts"):
        return script  # Schema A — author specified times explicitly, don't second-guess
    # Re-run normalize_script but substitute estimated durations with probed ones
    out = dict(script)
    chars = _normalize_characters(script.get("characters", []))
    chars = _merge_voice_casting(chars, script.get("voice_casting"))
    out["characters"] = chars

    music_cues = dict(script.get("music_cues", {}))
    timeline = []
    t = 0.0
    line_idx = 0
    auto_cue_idx = 0

    for act in script.get("acts", []):
        for scene in act.get("scenes", []):
            for el in scene.get("elements", []):
                etype = el.get("type")
                if etype == "direction":
                    continue
                elif etype == "pause":
                    t += float(el.get("duration_s", el.get("duration", 0)))
                elif etype == "music_cue":
                    name = el.get("cue") or el.get("preset") or \
                           el.get("mood") or f"cue_{auto_cue_idx}"
                    auto_cue_idx += 1
                    music_cues.setdefault(name, {
                        "tags": el.get("description") or el.get("mood") or "ambient",
                        "duration": float(el.get("duration_s", 30)),
                        "bpm": int(el.get("bpm", 72)),
                        "key": el.get("key", "A minor"),
                    })
                    timeline.append({
                        "t": t, "type": "music", "cue": name,
                        "fade_in": float(el.get("fade_in_s", 1.5)),
                        "fade_out": float(el.get("fade_out_s", 2.0)),
                        "volume": float(el.get("volume", 0.85)),
                    })
                elif etype == "sfx_cue":
                    desc = el.get("description") or el.get("source") or "sfx"
                    src = el.get("source") or "".join(
                        c if c.isalnum() else "_" for c in desc.lower()
                    )[:48].strip("_") + ".wav"
                    actual = sfx_map.get(src)
                    dur = probe_duration(actual) if actual and os.path.exists(actual) \
                          else float(el.get("duration_s", 3))
                    timeline.append({
                        "t": t, "type": "sfx",
                        "source": src, "name": src,
                        "volume": float(el.get("volume", 0.8)),
                    })
                    t += dur
                elif etype in ("narration", "dialogue"):
                    char = el.get("character") or ("NARRATOR" if etype == "narration" else None)
                    if not char:
                        continue
                    if char not in chars:
                        chars[char] = {"voice": "calm measured voice", "instruct": "natural delivery"}
                    emotion = el.get("emotion")
                    instruct = _resolve_instruct(chars[char], emotion=emotion,
                                                  override=el.get("delivery_note"))
                    text = el.get("text", "").strip()
                    if not text:
                        continue
                    actual = dialogue_map.get(line_idx)
                    dur = probe_duration(actual) if actual and os.path.exists(actual) \
                          else _estimate_tts_duration(text)
                    timeline.append({
                        "t": t, "type": "line",
                        "character": char, "text": text,
                        "instruct": instruct, "emotion": emotion,
                    })
                    t += dur
                    line_idx += 1

    out["music_cues"] = music_cues
    out["timeline"] = timeline
    out["_duration_source"] = "probed"
    return out


# ============================================================================
# TTS — Qwen3-TTS VoiceDesign (preferred) or Chatterbox (fallback)
# ============================================================================

def build_qwen3_tts_workflow(text, voice_desc, instruct, filename_prefix, seed,
                              model_choice="1.7B", device="cuda",
                              precision="bf16", language="English"):
    """Qwen3-TTS VoiceDesign workflow — 24kHz mono output (canonicalized later).

    Real FB_Qwen3TTSVoiceDesign node schema (inspected from /object_info):
      required:
        text, instruct, model_choice {"0.6B","1.7B"},
        device {"auto","cuda","mps","cpu"},
        precision {"bf16","fp32"}, language (enum)
      optional:
        seed, max_new_tokens, top_p, top_k, temperature,
        repetition_penalty, attention, unload_model_after_generate

    The node does NOT accept separate voice/instruct fields — it has ONE
    `instruct` that carries both the vocal identity and delivery. So we
    concatenate: the voice description and delivery override collapse into
    a single string fed to the node.
    """
    # Compose combined instruct: "<voice_description>, <delivery_override>"
    # voice_desc is the character's vocal DNA; instruct is the per-line delivery
    # (emotion override or delivery_note). Either may be empty.
    parts = [p.strip() for p in (voice_desc, instruct) if p and p.strip()]
    combined_instruct = ", ".join(parts) if parts else "natural delivery"

    return {
        "1": {
            "class_type": "FB_Qwen3TTSVoiceDesign",
            "inputs": {
                "text": text,
                "instruct": combined_instruct,
                "model_choice": model_choice,
                "device": device,
                "precision": precision,
                "language": language,
                "seed": int(seed),
                "max_new_tokens": 2048,
                "top_p": 0.8,
                "top_k": 20,
                "temperature": 1.0,
                "repetition_penalty": 1.05,
                "attention": "auto",
                "unload_model_after_generate": False,
            }
        },
        "2": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["1", 0], "filename_prefix": filename_prefix},
        }
    }


def build_chatterbox_tts_workflow(text, filename_prefix, seed, exaggeration=0.5, cfg_weight=0.5):
    return {
        "1": {
            "class_type": "ChatterboxTTS",
            "inputs": {
                "model_pack_name": "resembleai_default_voice",
                "text": text, "max_new_tokens": 1000,
                "flow_cfg_scale": 0.7, "exaggeration": exaggeration,
                "temperature": 0.8, "cfg_weight": cfg_weight,
                "repetition_penalty": 1.2, "min_p": 0.05, "top_p": 0.95,
                "seed": seed, "use_watermark": False,
            }
        },
        "2": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["1", 0], "filename_prefix": filename_prefix}
        }
    }


def stage_tts(ctx):
    """Generate per-line TTS audio, canonicalize to 48kHz stereo."""
    print("\n[TTS]")
    script = ctx["script"]
    audio_dir = ctx["audio_dir"]
    os.makedirs(os.path.join(audio_dir, "dialogue"), exist_ok=True)
    os.makedirs(os.path.join(audio_dir, "dialogue_raw"), exist_ok=True)

    # Check which TTS backend is available
    info = comfy_request("/object_info")
    use_qwen = "FB_Qwen3TTSVoiceDesign" in info
    use_chatterbox = "ChatterboxTTS" in info
    if not (use_qwen or use_chatterbox):
        raise RuntimeError("Neither Qwen3-TTS nor Chatterbox node found on ComfyUI")
    backend = "qwen3" if use_qwen else "chatterbox"
    print(f"  TTS backend: {backend}")

    line_idx = 0
    dialogue_map = {}
    for evt in script["timeline"]:
        if evt["type"] != "line":
            continue
        char = evt["character"]
        text = evt["text"]
        cdef = script["characters"].get(char, {})
        voice = cdef.get("voice", "neutral, warm")
        instruct = evt.get("instruct", cdef.get("instruct", "natural"))

        raw_prefix = f"{ctx['project_name']}_raw/line_{line_idx:03d}_{char}"
        canon_path = os.path.join(audio_dir, "dialogue", f"line_{line_idx:03d}.wav")
        if os.path.exists(canon_path):
            dialogue_map[line_idx] = canon_path
            print(f"  [{line_idx:03d}] SKIP: {char} — {text[:50]}")
            line_idx += 1
            continue

        print(f"  [{line_idx:03d}] {char}: \"{text[:60]}{'...' if len(text)>60 else ''}\"", flush=True)
        if backend == "qwen3":
            wf = build_qwen3_tts_workflow(text, voice, instruct, raw_prefix, seed=1000 + line_idx)
        else:
            wf = build_chatterbox_tts_workflow(text, raw_prefix, seed=1000 + line_idx)

        result = submit_and_wait(wf, f"rd-tts-{line_idx:03d}", poll_timeout=180)
        status = result.get("status", {}).get("status_str", "?")
        if status != "success":
            print(f"     FAILED: {status}")
            continue

        # Find raw output file, canonicalize it
        raw_src = None
        for v in result.get("outputs", {}).values():
            for a in v.get("audio", []):
                p = os.path.join(OUTPUT_ROOT, a.get("subfolder", ""), a["filename"])
                if os.path.exists(p):
                    raw_src = p
                    break
            if raw_src: break
        if not raw_src:
            print("     no output file found in history")
            continue

        raw_sr = probe_sample_rate(raw_src)
        print(f"     raw: {raw_sr}Hz → canonicalizing to {SR}Hz stereo")
        canonicalize(raw_src, canon_path)
        dialogue_map[line_idx] = canon_path
        line_idx += 1

    ctx["dialogue_map"] = dialogue_map
    with open(os.path.join(audio_dir, "dialogue_map.json"), "w") as f:
        json.dump({str(k): v for k, v in dialogue_map.items()}, f, indent=2)
    print(f"  TTS OK: {len(dialogue_map)} lines")
    return ctx


# ============================================================================
# Music — ACE Step 1.5 XL turbo per cue
# ============================================================================

# ACE Step variant presets. Each tuple is (unet_name, steps, cfg, sampler, scheduler, shift, weight_dtype)
# Chosen by the `variant` arg to build_ace_workflow().
#
#   xl_base      - full XL base (19.95 GB fp32)   — highest quality, slowest, for hero cues
#   xl_sft       - XL SFT bf16                    — near-base quality, faster
#   xl_base_sft  - XL base+SFT merged bf16        — balance of quality and speed
#   xl_turbo     - XL turbo bf16 (9.97 GB)        — fast (10 steps), validated in film pipeline
#   base_turbo   - 1.5 base turbo (4.8 GB)        — smallest/fastest, lowest quality — legacy default
#
# Settings derived from the NerdyRodent AceStep-XL_v35 workflow (validated in our film pipeline)
# and the ACE Step 1.5 turbo canonical recipe (CFG 1, 8 steps, euler/simple).
ACE_VARIANTS = {
    "xl_base":     ("acestep_v1.5_xl_base.safetensors",                  50, 4.5, "euler", "simple", 3, "default"),
    "xl_sft":      ("acestep_v1.5_xl_sft_bf16.safetensors",              45, 4.0, "euler", "simple", 3, "default"),
    "xl_base_sft": ("acestep_v1.5_xl_merge_base_sft_ta_0.5.safetensors", 35, 3.0, "euler", "simple", 3, "default"),
    "xl_turbo":    ("acestep_v1.5_xl_turbo_bf16.safetensors",            10, 1.0, "euler", "simple", 3, "default"),
    "base_turbo":  ("acestep_v1.5_turbo.safetensors",                     8, 1.0, "euler", "simple", 3, "default"),
}

ACE_DEFAULT_VARIANT = "xl_base"   # The full fp32 base with APG chain, now wired via music_tool.
                                   # Delivers the same near-mastering quality users hear from
                                   # music_maker.py, with only ~3s extra per 60s of audio vs turbo.
                                   # Set "ace_variant": "xl_turbo" in the script for fast previews.


def build_ace_workflow(tags, duration, bpm=75, keyscale="E minor",
                       seed=None, filename_prefix="audio/radio_drama/music",
                       variant=None, steps=None, cfg=None):
    """Build an ACE Step audio workflow, dispatching to the APG or simple chain
    based on the variant. Uses music_tool/music_maker.py as the source of truth
    for both workflow builders — radio drama and standalone music share the
    same high-quality templates now.

    APG chain (SamplerCustomAdvanced + CFGGuider + APG + gradient_estimation)
    is used for xl_base / xl_sft. Simple KSampler is used for xl_base_sft /
    xl_turbo / base_turbo (those variants are either merged or distilled and
    don't require APG).

    Args:
        variant: one of music_maker.VARIANTS keys. Default ACE_DEFAULT_VARIANT.
        steps: override the preset step count.
        cfg:   override the preset CFG.

    Returns (workflow_dict, seed_used).
    """
    # music_maker is bundled in this repo's scripts/ dir
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    import music_maker as mm

    variant = variant or ACE_DEFAULT_VARIANT
    if variant not in mm.VARIANTS:
        raise ValueError(f"Unknown ACE variant '{variant}'. Choices: {list(mm.VARIANTS)}")

    s = seed if seed is not None else random.randint(0, 2**31 - 1)
    # Music maker normalizes unicode flat/sharp to ASCII — reuse that
    keyscale = mm.normalize_key(keyscale)

    wf = mm.build_workflow(
        tags=tags, lyrics="[Instrumental]", duration=duration,
        bpm=bpm, keyscale=keyscale, seed=s,
        variant=variant, filename_prefix=filename_prefix,
        steps_ovr=steps, cfg_ovr=cfg,
    )
    return wf, s


def stage_music(ctx):
    """Generate one music track per named cue in script['music_cues']."""
    print("\n[MUSIC]")
    script = ctx["script"]
    music_dir = os.path.join(ctx["audio_dir"], "music")
    os.makedirs(music_dir, exist_ok=True)

    music_map = {}
    for cue_name, cue in script.get("music_cues", {}).items():
        canon_path = os.path.join(music_dir, f"{cue_name}.wav")
        if os.path.exists(canon_path):
            music_map[cue_name] = canon_path
            print(f"  SKIP {cue_name}")
            continue

        tags = cue["tags"]
        dur = float(cue["duration"])
        bpm = int(cue.get("bpm", 75))
        key = cue.get("key", "E minor")
        seed = int(cue.get("seed", random.randint(0, 2**31 - 1)))
        # Per-cue ACE variant + step/cfg overrides (fall back to script-level, then global default)
        variant = cue.get("variant") or script.get("ace_variant") or ACE_DEFAULT_VARIANT
        steps_ovr = cue.get("steps")
        cfg_ovr = cue.get("cfg")

        print(f"  [{cue_name}] gen {dur:.1f}s @ {bpm}bpm {key} [{variant}]: {tags[:50]}...", flush=True)
        wf, s = build_ace_workflow(tags, dur, bpm=bpm, keyscale=key, seed=seed,
                                    filename_prefix=f"{ctx['project_name']}_music/{cue_name}",
                                    variant=variant, steps=steps_ovr, cfg=cfg_ovr)
        t0 = time.time()
        # XL base can take 3-5 min per 60s of audio; give it headroom
        poll_timeout = 1800 if variant.startswith("xl_base") or variant == "xl_sft" else 600
        result = submit_and_wait(wf, f"rd-music-{cue_name}", poll_timeout=poll_timeout)
        status = result.get("status", {}).get("status_str", "?")
        if status != "success":
            print(f"     FAILED: {status}")
            continue
        print(f"     done in {time.time()-t0:.0f}s")

        # Canonicalize
        raw_src = None
        for v in result.get("outputs", {}).values():
            for a in v.get("audio", []):
                p = os.path.join(OUTPUT_ROOT, a.get("subfolder", ""), a["filename"])
                if os.path.exists(p):
                    raw_src = p; break
            if raw_src: break
        if not raw_src:
            print("     no output file found")
            continue

        # Optional post-generation mastering (dynamics-preserving chain). The
        # music bus here sits UNDER dialogue, so the default target is quieter
        # (-18 LUFS) — the final mix's sidechain ducking + loudnorm handles
        # loudness. Per-cue override wins > script-wide > "default" preset.
        #
        # Cue schema additions (all optional):
        #   "master": "default" | "edm" | "jazz" | "orchestral" | ... | "auto" | "off"
        #   "master_target_lufs": float (override preset's LUFS, default -18 here)
        # Script-wide defaults:
        #   "music_master": preset name or "off"
        #   "music_master_lufs": float
        master_preset = cue.get("master")
        if master_preset is None:
            master_preset = script.get("music_master", "default")
        master_lufs = cue.get("master_target_lufs",
                               script.get("music_master_lufs", -18.0))

        src_for_canon = raw_src
        if master_preset and master_preset != "off":
            try:
                # All sibling modules live in this repo's scripts/ dir
                if SCRIPT_DIR not in sys.path:
                    sys.path.insert(0, SCRIPT_DIR)
                import music_mastering as _mm
                if master_preset == "auto":
                    import music_maker as _mmk
                    master_preset = _mmk.auto_detect_preset(tags)
                    print(f"     master auto → preset='{master_preset}'")
                # Write mastered FLAC next to raw (doesn't touch raw)
                mext = os.path.splitext(raw_src)[1] or ".flac"
                mastered_path = raw_src[:-(len(mext))] + "_mastered" + mext
                print(f"     mastering [{master_preset} @ {master_lufs} LUFS]...")
                _mm.master_track(raw_src, mastered_path, preset_name=master_preset,
                                 target_lufs=master_lufs, verbose=False)
                src_for_canon = mastered_path
            except Exception as e:
                print(f"     master FAILED ({e}); using raw")

        raw_sr = probe_sample_rate(src_for_canon)
        print(f"     raw: {raw_sr}Hz → canonicalizing")
        canonicalize(src_for_canon, canon_path)
        music_map[cue_name] = canon_path

    ctx["music_map"] = music_map
    with open(os.path.join(music_dir, "music_map.json"), "w") as f:
        json.dump(music_map, f, indent=2)
    print(f"  MUSIC OK: {len(music_map)} cues")
    return ctx


# ============================================================================
# SFX — prefer manual library, then MMAudio (primary), SAO (fallback), ACE (ambient)
# ============================================================================

SFX_LIB_DIR = os.path.join(COMFYUI_ROOT, "input", "sfx")

# MMAudio Large 44k v2 — primary SFX engine. Wins on sharp transients, foley,
# mechanical motion, and biological/tonal sounds in our A/B testing vs SAO & ACE.
# Coherence window is ~10 s per generation.
MMAUDIO_MAX_DURATION = 10.0
MMAUDIO_MODEL_FILES = {
    "model": "mmaudio_large_44k_v2_fp16.safetensors",
    "vae":   "mmaudio_vae_44k_fp16.safetensors",
    "synch": "mmaudio_synchformer_fp16.safetensors",
    "clip":  "apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors",
}

# Stable Audio Open 1.0 — fallback for >10 s clips (max 47 s) and when MMAudio
# unavailable. Was primary before MMAudio; still strong for ambient textures.
STABLE_AUDIO_CKPT_CANDIDATES = [
    "stable-audio-open-1.0.safetensors",
    "stable_audio_open_10.safetensors",
    "stable-audio-open-1.0/model.safetensors",
]


def find_mmaudio_models():
    """Return True if all 4 required MMAudio fp16 files are present on disk."""
    d = os.path.join(COMFYUI_ROOT, "models", "mmaudio")
    return all(os.path.exists(os.path.join(d, f)) for f in MMAUDIO_MODEL_FILES.values())


def build_mmaudio_workflow(prompt, duration, filename_prefix, seed=None,
                            negative_prompt="music, speech, vocals, singing",
                            steps=50, cfg=5.5):
    """MMAudio Large 44k v2 text-to-audio workflow.

    Output: 44.1 kHz stereo (FLAC via SaveAudio). canonicalize() lifts to 48 k
    for the mix bus. Duration clamped to MMAUDIO_MAX_DURATION — MMAudio's
    coherence window is ~10 s; longer durations drift or glitch.

    Default settings (from the A/B test that beat SAO and ACE):
      steps=50, cfg=5.5, negative="music, speech, vocals, singing"
      mask_away_clip=True (pure text-to-audio; no video conditioning)
      force_offload=True  (release VRAM after each gen for multi-call scripts)
    """
    if not find_mmaudio_models():
        missing = [k for k, f in MMAUDIO_MODEL_FILES.items()
                   if not os.path.exists(os.path.join(COMFYUI_ROOT, "models", "mmaudio", f))]
        raise FileNotFoundError(
            f"MMAudio fp16 bundle incomplete, missing: {missing}. "
            f"Download from huggingface.co/Kijai/MMAudio_safetensors")
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    dur = min(MMAUDIO_MAX_DURATION, max(1.0, float(duration)))

    return {
        "1": {
            "class_type": "MMAudioModelLoader",
            "inputs": {
                "mmaudio_model": MMAUDIO_MODEL_FILES["model"],
                "base_precision": "fp16",
            },
        },
        "2": {
            "class_type": "MMAudioFeatureUtilsLoader",
            "inputs": {
                "vae_model":        MMAUDIO_MODEL_FILES["vae"],
                "synchformer_model":MMAUDIO_MODEL_FILES["synch"],
                "clip_model":       MMAUDIO_MODEL_FILES["clip"],
                "mode": "44k",
                "precision": "fp16",
            },
        },
        "3": {
            "class_type": "MMAudioSampler",
            "inputs": {
                "mmaudio_model": ["1", 0],
                "feature_utils": ["2", 0],
                "duration": dur,
                "steps": int(steps),
                "cfg": float(cfg),
                "seed": int(seed),
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "mask_away_clip": True,
                "force_offload": True,
            },
        },
        "4": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["3", 0], "filename_prefix": filename_prefix},
        },
    }


def mmaudio_generate(ctx, source_filename, description, duration, seed=None):
    """Generate a single SFX via MMAudio and place in input/sfx/."""
    if duration > MMAUDIO_MAX_DURATION:
        print(f"     MMAudio max is {MMAUDIO_MAX_DURATION} s; clamping {duration:.1f} → {MMAUDIO_MAX_DURATION}")
        duration = MMAUDIO_MAX_DURATION
    wf = build_mmaudio_workflow(
        prompt=description, duration=duration,
        filename_prefix=f"{ctx['project_name']}_sfx/{os.path.basename(source_filename).rsplit('.', 1)[0]}",
        seed=seed, steps=50, cfg=5.5,
    )
    result = submit_and_wait(wf, f"rd-mma-{os.path.basename(source_filename)}",
                              poll_timeout=900)
    if result.get("status", {}).get("status_str") != "success":
        raise RuntimeError(f"MMAudio failed: {result.get('status')}")
    for v in result.get("outputs", {}).values():
        for a in v.get("audio", []):
            p = os.path.join(OUTPUT_ROOT, a.get("subfolder", ""), a["filename"])
            if os.path.exists(p):
                target = os.path.join(SFX_LIB_DIR, source_filename)
                return _import_to_library(p, target)
    raise RuntimeError("MMAudio produced no output file")


def find_stable_audio_checkpoint():
    """Return the ckpt_name (relative to models/checkpoints/) to feed
    CheckpointLoaderSimple, or None if the model isn't present."""
    ckpt_dir = os.path.join(COMFYUI_ROOT, "models", "checkpoints")
    for rel in STABLE_AUDIO_CKPT_CANDIDATES:
        if os.path.exists(os.path.join(ckpt_dir, rel.replace("/", os.sep))):
            return rel
    # Also accept anything with "stable_audio" or "stable-audio" in the name
    for name in os.listdir(ckpt_dir):
        lo = name.lower()
        if "stable" in lo and "audio" in lo and name.endswith(".safetensors"):
            return name
    return None


# Candidate locations for the separately-loaded T5 text encoder. The official
# stabilityai/stable-audio-open-1.0 `model.safetensors` lacks the text_encoder
# weights (only contains UNet + VAE + seconds embedders), so we load the T5
# explicitly via CLIPLoader(type="stable_audio").
STABLE_AUDIO_T5_CANDIDATES = [
    "stable-audio-open-t5.safetensors",
    "stable_audio_open_t5.safetensors",
    "stable-audio-t5.safetensors",
]


def find_stable_audio_t5():
    """Locate the Stable Audio T5 encoder in models/text_encoders/ or models/clip/."""
    for subdir in ("text_encoders", "clip"):
        d = os.path.join(COMFYUI_ROOT, "models", subdir)
        if not os.path.isdir(d):
            continue
        for rel in STABLE_AUDIO_T5_CANDIDATES:
            if os.path.exists(os.path.join(d, rel)):
                return rel
        # Accept anything with stable + audio in the name
        for name in os.listdir(d):
            lo = name.lower()
            if "stable" in lo and "audio" in lo and name.endswith(".safetensors"):
                return name
    return None


def build_stable_audio_workflow(prompt, duration, filename_prefix, seed=None,
                                 negative_prompt="", steps=50, cfg=7.0):
    """Native-ComfyUI Stable Audio Open 1.0 workflow.

    Reference: https://comfyanonymous.github.io/ComfyUI_examples/audio/

    The stability/stable-audio-open-1.0 `model.safetensors` ships WITHOUT the
    text encoder, so we load the T5 separately via CLIPLoader(type="stable_audio").
    Output is 44.1 kHz stereo; canonicalize() resamples to our 48 kHz mix target.
    Max duration per generation is 47 s.
    """
    ckpt = find_stable_audio_checkpoint()
    if not ckpt:
        raise FileNotFoundError(
            "Stable Audio Open UNet/VAE checkpoint not found in models/checkpoints/. "
            "Expected one of: " + ", ".join(STABLE_AUDIO_CKPT_CANDIDATES))
    t5 = find_stable_audio_t5()
    if not t5:
        raise FileNotFoundError(
            "Stable Audio Open T5 text encoder not found in models/text_encoders/. "
            "Download text_encoder/model.safetensors from stabilityai/stable-audio-open-1.0 "
            "and place it at models/text_encoders/stable-audio-open-t5.safetensors")
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    dur = min(47.0, max(1.0, float(duration)))

    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt},
        },
        "9": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": t5, "type": "stable_audio"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["9", 0]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["9", 0]},
        },
        "4": {
            "class_type": "EmptyLatentAudio",
            "inputs": {"seconds": dur, "batch_size": 1},
        },
        "5": {
            "class_type": "ConditioningStableAudio",
            "inputs": {
                "positive": ["2", 0],
                "negative": ["3", 0],
                "seconds_start": 0.0,
                "seconds_total": dur,
            },
        },
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "seed": seed,
                "steps": int(steps),
                "cfg": float(cfg),
                "sampler_name": "dpmpp_3m_sde_gpu",
                "scheduler": "exponential",
                "positive": ["5", 0],
                "negative": ["5", 1],
                "latent_image": ["4", 0],
                "denoise": 1.0,
            },
        },
        "7": {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["6", 0], "vae": ["1", 2]},
        },
        "8": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["7", 0], "filename_prefix": filename_prefix},
        },
    }


def _import_to_library(comfyui_output_path, target_library_path):
    """Place a ComfyUI-generated audio file into the SFX library with a
    real, player-compatible container.

    ComfyUI's SaveAudio node writes FLAC by default. If the user's script
    refers to the SFX with a `.wav` suffix (the common case), a straight
    shutil.copy produces a file that has FLAC magic bytes but a .wav
    extension — Windows Media Player, Audacity, and most file-explorer
    previewers refuse to play it.

    This function transcodes to the container implied by the target
    extension so the library file is always playable as its name suggests.
    """
    os.makedirs(os.path.dirname(target_library_path), exist_ok=True)
    ext = os.path.splitext(target_library_path)[1].lower()
    if ext == ".flac":
        # Same container — safe to copy
        shutil.copy2(comfyui_output_path, target_library_path)
        return target_library_path
    if ext == ".wav":
        # Transcode to real WAV (pcm_s16le at the source's native rate to
        # keep file small; canonicalize() will lift to 48k/s24 at mix time)
        run([FFMPEG, "-y", "-i", comfyui_output_path,
             "-c:a", "pcm_s16le", target_library_path])
        return target_library_path
    if ext == ".mp3":
        run([FFMPEG, "-y", "-i", comfyui_output_path,
             "-c:a", "libmp3lame", "-q:a", "2", target_library_path])
        return target_library_path
    # Unknown extension — default to copying but warn
    print(f"     unknown SFX extension '{ext}', copying as-is")
    shutil.copy2(comfyui_output_path, target_library_path)
    return target_library_path


def stable_audio_generate(ctx, source_filename, description, duration, seed=None):
    """Generate a single SFX via Stable Audio Open and place the result in
    input/sfx/<source_filename>. Raises on failure.
    """
    if duration > 47:
        print(f"     Stable Audio max is 47 s; clamping {duration:.1f} → 47 s")
        duration = 47.0
    wf = build_stable_audio_workflow(
        prompt=description,
        duration=duration,
        filename_prefix=f"{ctx['project_name']}_sfx/{os.path.basename(source_filename).rsplit('.', 1)[0]}",
        seed=seed,
        steps=60,
        cfg=7.0,
    )
    result = submit_and_wait(wf, f"rd-sao-{os.path.basename(source_filename)}",
                              poll_timeout=600)
    if result.get("status", {}).get("status_str") != "success":
        raise RuntimeError(f"Stable Audio failed: {result.get('status')}")
    for v in result.get("outputs", {}).values():
        for a in v.get("audio", []):
            p = os.path.join(OUTPUT_ROOT, a.get("subfolder", ""), a["filename"])
            if os.path.exists(p):
                target = os.path.join(SFX_LIB_DIR, source_filename)
                return _import_to_library(p, target)
    raise RuntimeError("Stable Audio produced no output file")


def resolve_sfx(source, sfx_dir_canon):
    """Return path to a canonical 48k stereo SFX WAV.

    Resolution order:
      1. absolute / existing path
      2. input/sfx/<source>
      3. input/sfx/<source>.wav
    Raises if unresolvable.
    """
    candidates = [
        source,
        os.path.join(SFX_LIB_DIR, source),
        os.path.join(SFX_LIB_DIR, source if source.endswith((".wav", ".mp3", ".flac", ".ogg")) else source + ".wav"),
    ]
    for c in candidates:
        if os.path.exists(c):
            canon = os.path.join(sfx_dir_canon, os.path.basename(c).rsplit(".", 1)[0] + ".wav")
            if not os.path.exists(canon):
                canonicalize(c, canon)
            return canon
    raise FileNotFoundError(f"SFX '{source}' not found. Searched: {candidates}")


def generate_sfx(ctx, src, description, duration, seed=None):
    """Generate a single SFX using the best available backend.

    Priority (each tested at call time):
      1. MMAudio Large 44k v2        — primary. Best for transients, foley, mechanical,
                                       biological. 44.1 kHz stereo. ≤10 s coherence window.
      2. Stable Audio Open 1.0       — fallback for >10 s or if MMAudio unavailable.
                                       Max 47 s. Strong on ambient texture.
      3. ACE Step 1.5                — last resort. Use for 47+ s ambient beds only;
                                       music model, weak on discrete SFX events.
      4. ElevenLabs SoundEffects API — TODO: wire if ELEVENLABS_API_KEY is set
                                       (hero-tier paid option, up to 22 s).

    Returns the path of the generated file in input/sfx/, or raises if
    no backend succeeded.
    """
    # 1. MMAudio (primary — if duration is within coherence window)
    if duration <= MMAUDIO_MAX_DURATION and find_mmaudio_models():
        try:
            print(f"  [gen/mma] {src}: MMAudio Large 44k v2 ({duration:.1f}s) — \"{description[:60]}\"")
            return mmaudio_generate(ctx, src, description, duration, seed=seed)
        except Exception as e:
            print(f"     MMAudio failed: {e}. Falling back to Stable Audio Open.")

    # 2. Stable Audio Open (fallback for >10s or if MMAudio unavailable)
    if find_stable_audio_checkpoint():
        try:
            reason = "> MMAudio window" if duration > MMAUDIO_MAX_DURATION else "MMAudio unavailable"
            print(f"  [gen/sao] {src}: Stable Audio Open ({duration:.1f}s, {reason}) — \"{description[:60]}\"")
            return stable_audio_generate(ctx, src, description, duration, seed=seed)
        except Exception as e:
            print(f"     Stable Audio failed: {e}. Falling back to ACE Step.")

    # 3. ACE Step (last resort for very long ambient beds)
    tags = description or f"sound effect, {src.replace('_', ' ').replace('.wav','')}, foley, realistic"
    print(f"  [gen/ace] {src}: ACE Step ({duration:.1f}s, last resort) — \"{tags[:60]}\"")
    wf, _ = build_ace_workflow(tags, duration, bpm=90, keyscale="C minor", seed=seed,
        filename_prefix=f"{ctx['project_name']}_sfx/{os.path.basename(src).rsplit('.',1)[0]}")
    result = submit_and_wait(wf, f"rd-sfx-ace-{src}", poll_timeout=300)
    if result.get("status", {}).get("status_str") == "success":
        for v in result.get("outputs", {}).values():
            for a in v.get("audio", []):
                p = os.path.join(OUTPUT_ROOT, a.get("subfolder", ""), a["filename"])
                if os.path.exists(p):
                    target = os.path.join(SFX_LIB_DIR, src)
                    return _import_to_library(p, target)
    raise RuntimeError(f"All SFX backends failed for {src}")


def stage_sfx(ctx):
    """Canonicalize all SFX referenced in the timeline. For any SFX flagged
    `generate: true`, synthesize via the best available backend."""
    print("\n[SFX]")
    script = ctx["script"]
    sfx_dir_canon = os.path.join(ctx["audio_dir"], "sfx")
    os.makedirs(sfx_dir_canon, exist_ok=True)

    # Announce which backends are available so the logs are self-documenting
    mma = "YES" if find_mmaudio_models() else "no"
    sao = find_stable_audio_checkpoint()
    print(f"  SFX backends:  MMAudio 44k v2 = {mma} (≤{MMAUDIO_MAX_DURATION:.0f}s),  "
          f"Stable Audio Open = {'YES' if sao else 'no'} (≤47s),  "
          f"ACE Step = yes (fallback),  manual library = {SFX_LIB_DIR}")

    sfx_map = {}
    for evt in script["timeline"]:
        if evt["type"] != "sfx":
            continue
        src = evt["source"]
        key = evt.get("name", src)
        if key in sfx_map:
            continue
        lib_path = os.path.join(SFX_LIB_DIR, src)
        if evt.get("generate") and not os.path.exists(lib_path):
            try:
                generate_sfx(
                    ctx, src,
                    description=evt.get("tags") or evt.get("description") or "",
                    duration=float(evt.get("duration", 3.0)),
                    seed=evt.get("seed"),
                )
            except Exception as e:
                print(f"     GEN FAILED: {e}")

        try:
            sfx_map[key] = resolve_sfx(src, sfx_dir_canon)
            print(f"  [{key}] -> {sfx_map[key]}")
        except FileNotFoundError as e:
            print(f"  [{key}] MISSING — {e}")
            print(f"           Drop a WAV at {lib_path} or set generate:true in the script")

    ctx["sfx_map"] = sfx_map
    print(f"  SFX OK: {len(sfx_map)} resolved")
    return ctx


# ============================================================================
# Mix — concatenate timeline with proper ducking + loudnorm
# ============================================================================

def stage_mix(ctx):
    """Build the full master by:
      1. Pre-converting every stream (dialogue / music / SFX) to canonical format
         (this was already done in stage_tts / stage_music / stage_sfx).
      2. Placing each event on the timeline with `adelay` so absolute t= is respected.
      3. Mixing dialogue + SFX into a 'speech' bus.
      4. Ducking music against the speech bus via sidechaincompress.
      5. Final mixdown + loudnorm to -16 LUFS (broadcast standard).
    """
    print("\n[MIX]")
    script = ctx["script"]
    audio_dir = ctx["audio_dir"]
    dialogue_map = ctx.get("dialogue_map") or {}
    music_map = ctx.get("music_map") or {}
    sfx_map = ctx.get("sfx_map") or {}

    # Rebuild maps from disk if running mix in isolation
    dm_path = os.path.join(audio_dir, "dialogue_map.json")
    mm_path = os.path.join(audio_dir, "music", "music_map.json")
    if not dialogue_map and os.path.exists(dm_path):
        dialogue_map = {int(k): v for k, v in json.load(open(dm_path)).items()}
    if not music_map and os.path.exists(mm_path):
        music_map = json.load(open(mm_path))
    if not sfx_map:
        # derive from timeline by re-calling resolve_sfx
        sfx_dir_canon = os.path.join(audio_dir, "sfx")
        for evt in script["timeline"]:
            if evt["type"] != "sfx":
                continue
            try:
                sfx_map[evt.get("name", evt["source"])] = resolve_sfx(evt["source"], sfx_dir_canon)
            except FileNotFoundError:
                pass

    # For Schema B scripts, re-time the timeline with actual rendered durations
    # so the mix stays aligned even when TTS came out shorter/longer than estimate.
    raw = ctx.get("raw_script")
    if raw and raw.get("acts"):
        script = rebuild_timeline_with_real_durations(raw, dialogue_map, sfx_map)
        ctx["script"] = script
        with open(os.path.join(audio_dir, "_normalized_script.json"), "w",
                  encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)
        print(f"  [re-timed Schema B timeline from actual durations]")

    # Determine total length from timeline
    last_t = 0.0
    for evt in script["timeline"]:
        t = float(evt.get("t", 0))
        if evt["type"] == "line":
            idx = _line_index(script, evt)
            path = dialogue_map.get(idx)
            d = probe_duration(path) if path else 0
        elif evt["type"] == "sfx":
            path = sfx_map.get(evt.get("name", evt["source"]))
            d = probe_duration(path) if path else 0
        elif evt["type"] == "music":
            d = float(script["music_cues"][evt["cue"]].get("duration", 0))
        else:
            d = 0
        last_t = max(last_t, t + d)
    total = last_t + float(script.get("tail_silence", 2.0))
    print(f"  Timeline total: {total:.2f}s")

    # Build per-event ffmpeg inputs + filters.
    # Every filter chain ends with `aformat=...` to guarantee canonical format.
    inputs = []
    speech_labels = []   # dialogue + sfx go into the speech bus
    music_labels = []

    for ei, evt in enumerate(script["timeline"]):
        t = float(evt.get("t", 0))
        delay_ms = int(round(t * 1000))
        if evt["type"] == "line":
            idx = _line_index(script, evt)
            path = dialogue_map.get(idx)
            if not path: continue
            vol = float(evt.get("volume", 1.0))
            label = f"d{ei}"
            inputs.append(path)
            # aresample=async=1 absorbs any fractional drift; aformat pins the
            # format so downstream filters get an unambiguous input.
            f = (f"[{len(inputs)-1}:a]aresample=async=1,{AFORMAT},"
                 f"adelay={delay_ms}|{delay_ms},volume={vol:.3f}[{label}]")
            speech_labels.append((label, f))

        elif evt["type"] == "sfx":
            path = sfx_map.get(evt.get("name", evt["source"]))
            if not path: continue
            vol = float(evt.get("volume", 0.8))
            label = f"s{ei}"
            inputs.append(path)
            f = (f"[{len(inputs)-1}:a]aresample=async=1,{AFORMAT},"
                 f"adelay={delay_ms}|{delay_ms},volume={vol:.3f}[{label}]")
            speech_labels.append((label, f))

        elif evt["type"] == "music":
            path = music_map.get(evt["cue"])
            if not path: continue
            vol = float(evt.get("volume", 0.9))
            fade_in = float(evt.get("fade_in", 1.5))
            fade_out = float(evt.get("fade_out", 2.0))
            # Music plays until the next music event for the same channel, or to total
            next_t = total
            for nxt in script["timeline"][ei+1:]:
                if nxt["type"] == "music":
                    next_t = float(nxt["t"])
                    break
            music_dur = next_t - t
            if music_dur <= 0:
                continue
            label = f"m{ei}"
            inputs.append(path)
            mdur = probe_duration(path)
            # If music cue is shorter than needed, we loop it by adding `-stream_loop -1`
            # This is handled in the -i line below (see loop_inputs).
            fade_out_st = max(0, music_dur - fade_out)
            f = (f"[{len(inputs)-1}:a]aresample=async=1,{AFORMAT},"
                 f"atrim=0:{music_dur:.3f},asetpts=PTS-STARTPTS,"
                 f"afade=t=in:st=0:d={fade_in:.3f},"
                 f"afade=t=out:st={fade_out_st:.3f}:d={fade_out:.3f},"
                 f"adelay={delay_ms}|{delay_ms},volume={vol:.3f}[{label}]")
            music_labels.append((label, f))

    # Which inputs need looping (music that is shorter than its active window)?
    loop_idx = set()
    idx_counter = 0
    for evt in script["timeline"]:
        t = float(evt.get("t", 0))
        if evt["type"] == "music":
            path = music_map.get(evt["cue"])
            if not path:
                continue
            next_t = total
            for nxt in script["timeline"][script["timeline"].index(evt)+1:]:
                if nxt["type"] == "music":
                    next_t = float(nxt["t"]); break
            need = next_t - t
            if need > probe_duration(path):
                loop_idx.add(idx_counter)
            idx_counter += 1
        elif evt["type"] in ("line", "sfx"):
            # Only count if the event actually produced an input above
            target_map = dialogue_map if evt["type"] == "line" else sfx_map
            key = _line_index(script, evt) if evt["type"] == "line" else evt.get("name", evt["source"])
            if key in target_map:
                idx_counter += 1

    # Build ffmpeg command
    cmd = [FFMPEG, "-y"]
    for i, p in enumerate(inputs):
        if i in loop_idx:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", p]

    # Speech bus — amix all dialogue + SFX branches
    fc = []
    fc.extend(f for _, f in speech_labels)
    fc.extend(f for _, f in music_labels)
    if speech_labels:
        speech_in = "".join(f"[{lbl}]" for lbl, _ in speech_labels)
        fc.append(f"{speech_in}amix=inputs={len(speech_labels)}:duration=longest:"
                  f"normalize=0:weights={' '.join(['1'] * len(speech_labels))}[speech_raw]")
        fc.append(f"[speech_raw]{AFORMAT},alimiter=level_in=1:level_out=0.95:limit=0.98[speech]")
    else:
        fc.append(f"anullsrc=r={SR}:cl=stereo,atrim=0:{total:.3f}[speech]")
        # anullsrc placeholder; requires -f lavfi which we didn't add — substitute empty stream
        # Safer: skip speech bus entirely if no speech
        pass

    # Music bus — amix music branches then sidechain-duck against speech
    if music_labels:
        music_in = "".join(f"[{lbl}]" for lbl, _ in music_labels)
        fc.append(f"{music_in}amix=inputs={len(music_labels)}:duration=longest:"
                  f"normalize=0[music_raw]")
        fc.append(f"[music_raw]{AFORMAT}[music_pre]")
        # sidechaincompress frame-syncs against its sidechain (key) input and stops
        # once that input ends — if speech is shorter than the music bed (the
        # common case), the whole chain truncates early regardless of the final
        # `-t` output flag. Pad speech to the full timeline length first so the
        # duck stays alive (returning to unity gain) for the entire music bed.
        fc.append(f"[speech]apad=whole_dur={total:.3f}[speech_padded]")
        fc.append("[speech_padded]asplit=2[speech_out][speech_key]")
        fc.append("[music_pre][speech_key]sidechaincompress="
                  "threshold=0.04:ratio=8:attack=20:release=350:makeup=1[music_ducked]")
        # Final mix with weights to prevent clipping, then loudnorm
        fc.append("[speech_out][music_ducked]amix=inputs=2:duration=longest:"
                  "normalize=0:weights=1.0 0.8[mix_raw]")
    else:
        fc.append("[speech]anull[mix_raw]")

    # Loudnorm single-pass to -16 LUFS (broadcast podcast target)
    fc.append(f"[mix_raw]loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:"
              f"print_format=summary[out]")

    filter_complex = ";".join(fc)
    final = os.path.join(ctx["project_dir"], f"{ctx['project_name']}_radio.wav")
    cmd += ["-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:a", "pcm_s24le", "-ar", str(SR), "-ac", str(CH),
            "-t", f"{total:.3f}",
            final]

    # Also write MP3 for delivery
    run(cmd, check=True, capture=True)
    dur = probe_duration(final)
    size_mb = os.path.getsize(final) / 1024 / 1024
    print(f"  MASTER WAV: {final}")
    print(f"  Duration: {dur:.2f}s, {size_mb:.1f} MB, {SR}Hz/{CH}ch/pcm_s24le")

    mp3 = final.replace(".wav", ".mp3")
    run([FFMPEG, "-y", "-i", final,
         "-c:a", "libmp3lame", "-q:a", "2",   # VBR ~190kbps
         "-ar", str(SR), "-ac", str(CH),
         mp3], check=True)
    print(f"  MASTER MP3: {mp3} ({os.path.getsize(mp3)/1024/1024:.1f} MB)")

    ctx["final_wav"] = final
    ctx["final_mp3"] = mp3
    return ctx


def _line_index(script, evt):
    """Map a line event to its index in dialogue_map (0-based by script order)."""
    k = 0
    for e in script["timeline"]:
        if e is evt:
            return k
        if e["type"] == "line":
            k += 1
    return k


# ============================================================================
# Main
# ============================================================================

STAGES = {
    "tts": stage_tts,
    "music": stage_music,
    "sfx": stage_sfx,
    "mix": stage_mix,
}

ORDER = ["tts", "music", "sfx", "mix"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("project")
    p.add_argument("--stage", default="all")
    p.add_argument("--script", help="Path to script JSON (default: output/radio/<project>/script.json)")
    args = p.parse_args()

    project_dir = os.path.join(OUTPUT_ROOT, "radio", args.project)
    os.makedirs(project_dir, exist_ok=True)
    script_path = args.script or os.path.join(project_dir, "script.json")
    if not os.path.exists(script_path):
        raise SystemExit(
            f"Script not found: {script_path}\n"
            f"Create it with a JSON object (see radio_drama.py top-of-file docstring for format).")

    raw_script = json.load(open(script_path, encoding="utf-8"))
    # Auto-detect schema. Flat timeline passes through; acts/scenes/elements
    # gets flattened with estimated TTS durations (refined at mix time).
    script = normalize_script(raw_script)
    ctx = {
        "project_name": args.project,
        "project_dir": project_dir,
        "audio_dir": os.path.join(project_dir, "audio"),
        "script": script,
        "raw_script": raw_script,  # keep for rebuild_timeline_with_real_durations
    }
    os.makedirs(ctx["audio_dir"], exist_ok=True)

    # Persist the normalized script for debugging and for re-runs of --stage mix
    with open(os.path.join(ctx["audio_dir"], "_normalized_script.json"), "w",
              encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)

    stages = ORDER if args.stage == "all" else [args.stage]
    for s in stages:
        if s not in STAGES:
            raise SystemExit(f"Unknown stage: {s}. Choices: {list(STAGES)}")
        ctx = STAGES[s](ctx)


if __name__ == "__main__":
    main()
