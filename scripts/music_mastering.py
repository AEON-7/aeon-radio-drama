#!/usr/bin/env python3
"""Music mastering chain for ACE Step output (v2 — dynamics-preserving).

The v1 chain used serial Compressor + BusCompressor + saturation, which squashed
LRA/DR/crest on every test track (orchestral LRA 23 → 14, jazz crest 6.85 → 4.8).

v2 takes a minimalist philosophy:

    raw.flac
       |
       |-- HighpassFilter(20Hz)              [remove sub-rumble]
       |-- LowShelf(-1..2dB @ 150-200Hz)     [tame mud, no dynamics loss]
       |-- PeakFilter(+1..2dB @ 2.5-3.5kHz)  [presence, additive EQ]
       |-- HighShelf(+1.5..3dB @ 10-12kHz)   [air, sparkle]
       |-- [optional] Compressor             [OFF by default — opt-in only]
       |-- Distortion(0..4dB, tape-style)    [subtle harmonic warmth]
       |-- Gain match to target LUFS         [uses ebur128, never boosts by >+6dB]
       |-- Limiter(-1.0 dBTP, slow release)  [true-peak ceiling only]
       |
    mastered.flac

Key principles:
  1. NO `loudnorm LRA=X` constraint anywhere.
  2. NO compressor in the default chain — just EQ + subtle sat + peak-safety limit.
  3. Saturation is light (0-4 dB), acting as harmonic color not a dynamics tool.
  4. Gain cap prevents boosting silence/noise floor.
  5. Limiter release is slow (>100 ms) — catches stray peaks, doesn't pump.

You cannot restore dynamics lost during generation (e.g. if ACE spits out an
LRA-1.8 track, it stays at LRA 1.8). The goal of mastering is to add color +
set loudness WITHOUT making the existing dynamics worse.

Presets:
  - default      → safe balanced EQ + light sat, no compression, -14 LUFS
  - edm          → heavier EQ, more sat, still no compression, -12 LUFS
  - trap         → bass-forward EQ, harder target, -12 LUFS
  - chill        → gentle EQ, min sat, -16 LUFS
  - orchestral   → TRANSPARENT — minimal EQ, zero sat, -18 LUFS
  - jazz         → transparent + whisper of warmth, -15 LUFS
"""
import argparse, json, os, shutil, subprocess, sys, time
import numpy as np
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pedalboard as pb
from pedalboard.io import AudioFile
import librosa

FFMPEG = shutil.which("ffmpeg") or os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = shutil.which("ffprobe") or os.environ.get("FFPROBE", "ffprobe")


# ---------------------------------------------------------------------------
# Mastering presets — tuned per genre. All values are conservative defaults;
# the "default" preset is a safe balanced chain for most music.
# ---------------------------------------------------------------------------

PRESETS = {
    "default": {
        "target_lufs":    -14.0,
        "hpf_hz":         20.0,
        "low_shelf_hz":   180.0, "low_shelf_db": -1.0,
        "presence_hz":    3200.0, "presence_db": 1.5, "presence_q": 0.7,
        "high_shelf_hz":  11000.0, "high_shelf_db": 2.0,
        "saturation_db":  2.5,
        "use_compressor": False,
        # only used if use_compressor True:
        "comp_threshold": -20.0, "comp_ratio": 1.8, "comp_attack": 20.0, "comp_release": 200.0,
        "limiter_db":     -1.0, "limiter_release": 150.0,
        "max_boost_db":   6.0,   # cap upward gain match
    },
    "edm": {
        # EDM is almost always already compressed — don't add more. Add color.
        "target_lufs":    -12.0,
        "hpf_hz":         28.0,
        "low_shelf_hz":   200.0, "low_shelf_db": -1.5,
        "presence_hz":    2800.0, "presence_db": 2.0, "presence_q": 0.8,
        "high_shelf_hz":  12000.0, "high_shelf_db": 3.0,
        "saturation_db":  3.5,
        "use_compressor": False,
        "comp_threshold": -16.0, "comp_ratio": 2.0, "comp_attack": 10.0, "comp_release": 100.0,
        "limiter_db":     -0.8, "limiter_release": 80.0,
        "max_boost_db":   6.0,
    },
    "trap": {
        # 808-forward, crisp hats. Still no compression — EDM+ source is already flat.
        "target_lufs":    -12.0,
        "hpf_hz":         25.0,
        "low_shelf_hz":   150.0, "low_shelf_db": -0.8,
        "presence_hz":    4500.0, "presence_db": 2.0, "presence_q": 0.9,
        "high_shelf_hz":  13000.0, "high_shelf_db": 3.0,
        "saturation_db":  3.0,
        "use_compressor": False,
        "comp_threshold": -16.0, "comp_ratio": 2.0, "comp_attack": 10.0, "comp_release": 100.0,
        "limiter_db":     -0.8, "limiter_release": 60.0,
        "max_boost_db":   6.0,
    },
    "chill": {
        # Lofi / ambient. Very transparent. Tiny warmth only.
        "target_lufs":    -16.0,
        "hpf_hz":         25.0,
        "low_shelf_hz":   180.0, "low_shelf_db": -0.5,
        "presence_hz":    3000.0, "presence_db": 0.8, "presence_q": 0.6,
        "high_shelf_hz":  11000.0, "high_shelf_db": 1.5,
        "saturation_db":  1.5,
        "use_compressor": False,
        "comp_threshold": -22.0, "comp_ratio": 1.5, "comp_attack": 30.0, "comp_release": 300.0,
        "limiter_db":     -1.5, "limiter_release": 200.0,
        "max_boost_db":   8.0,
    },
    "orchestral": {
        # TRANSPARENT. EQ only. Zero saturation. Zero compression.
        "target_lufs":    -18.0,
        "hpf_hz":         18.0,
        "low_shelf_hz":   120.0, "low_shelf_db": -0.3,
        "presence_hz":    2500.0, "presence_db": 0.4, "presence_q": 0.5,
        "high_shelf_hz":  10000.0, "high_shelf_db": 0.8,
        "saturation_db":  0.0,    # NO saturation — preserve classical accuracy
        "use_compressor": False,
        "comp_threshold": -24.0, "comp_ratio": 1.2, "comp_attack": 60.0, "comp_release": 500.0,
        "limiter_db":     -2.0, "limiter_release": 300.0,
        "max_boost_db":   4.0,    # stay conservative on classical
    },
    "jazz": {
        # Near-transparent. Whisper of tape warmth.
        "target_lufs":    -15.0,
        "hpf_hz":         20.0,
        "low_shelf_hz":   160.0, "low_shelf_db": -0.5,
        "presence_hz":    3500.0, "presence_db": 0.8, "presence_q": 0.6,
        "high_shelf_hz":  11500.0, "high_shelf_db": 1.2,
        "saturation_db":  1.0,    # whisper
        "use_compressor": False,
        "comp_threshold": -20.0, "comp_ratio": 1.5, "comp_attack": 30.0, "comp_release": 250.0,
        "limiter_db":     -1.5, "limiter_release": 200.0,
        "max_boost_db":   6.0,
    },
}


# ---------------------------------------------------------------------------
# Metrics helpers (match the analysis style already validated against tracks)
# ---------------------------------------------------------------------------

def measure(path):
    """Return a dict of integrated loudness + dynamic-range metrics."""
    # ffmpeg loudnorm analysis pass (for LUFS-I, TP, LRA)
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path,
        "-af", "loudnorm=print_format=json:I=-16:TP=-1.5:LRA=11",
        "-f", "null", "-"], capture_output=True, text=True)
    i_lufs = i_tp = i_lra = None
    try:
        out = r.stderr
        idx = out.rfind("{")
        js = json.loads(out[idx:out.rfind("}") + 1])
        i_lufs = float(js.get("input_i", 0))
        i_tp   = float(js.get("input_tp", 0))
        i_lra  = float(js.get("input_lra", 0))
    except Exception:
        pass

    # Crest + DR from librosa
    y, sr = librosa.load(path, sr=None, mono=True)
    rms = float(np.sqrt(np.mean(y ** 2)))
    peak = float(np.max(np.abs(y)))
    crest = peak / rms if rms > 0 else 0.0
    hop = sr // 10
    rmses = librosa.feature.rms(y=y, hop_length=hop)[0]
    dr_db = float(20 * np.log10(np.percentile(rmses, 95) / max(np.percentile(rmses, 10), 1e-8)))
    return {"lufs_i": i_lufs, "tp": i_tp, "lra": i_lra, "dr_db": dr_db,
            "crest": crest, "rms": rms, "peak": peak}


# ---------------------------------------------------------------------------
# Mastering chain
# ---------------------------------------------------------------------------

def build_chain(preset_name="default"):
    """Build a pedalboard `Pedalboard` from a preset name.

    v2 signal flow (dynamics-preserving):
      HPF -> EQ (low shelf cut, presence peak, air shelf)
          -> [optional] Compressor (only if preset["use_compressor"] == True)
          -> Distortion (light saturation, skipped if 0)
          -> [limiter is applied AFTER gain match, see master_track()]
    """
    if preset_name not in PRESETS:
        raise ValueError(f"Unknown preset '{preset_name}'. Choices: {list(PRESETS)}")
    p = PRESETS[preset_name]

    chain = [
        pb.HighpassFilter(cutoff_frequency_hz=p["hpf_hz"]),
        pb.LowShelfFilter(
            cutoff_frequency_hz=p["low_shelf_hz"],
            gain_db=p["low_shelf_db"],
            q=0.7,
        ),
        pb.PeakFilter(
            cutoff_frequency_hz=p["presence_hz"],
            gain_db=p["presence_db"],
            q=p["presence_q"],
        ),
        pb.HighShelfFilter(
            cutoff_frequency_hz=p["high_shelf_hz"],
            gain_db=p["high_shelf_db"],
            q=0.7,
        ),
    ]

    if p.get("use_compressor", False):
        chain.append(pb.Compressor(
            threshold_db=p["comp_threshold"],
            ratio=p["comp_ratio"],
            attack_ms=p["comp_attack"],
            release_ms=p["comp_release"],
        ))

    # Light tape-style saturation for harmonic warmth. Skip if drive is 0.
    sat_db = p.get("saturation_db", 0.0)
    if sat_db > 0.01:
        chain.append(pb.Distortion(drive_db=sat_db))

    board = pb.Pedalboard(chain)
    return board, p


def _measure_lufs(path):
    """Fast LUFS-I measurement via ebur128 filter (single pass)."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path,
        "-af", "ebur128=framelog=quiet:metadata=1",
        "-f", "null", "-"], capture_output=True, text=True)
    lufs = None
    for line in r.stderr.splitlines():
        s = line.strip()
        if s.startswith("I:") and "LUFS" in s:
            try:
                lufs = float(s.split("I:")[1].split("LUFS")[0].strip())
            except Exception:
                pass
    return lufs


def master_track(in_path, out_path, preset_name="default", target_lufs=None,
                 verbose=True):
    """Apply the mastering chain to a file, then gain-match to target LUFS.

    v2 flow:
      1. Measure BEFORE metrics.
      2. Apply pedalboard chain (EQ + optional comp + light sat).
      3. Measure post-chain LUFS via ebur128.
      4. Compute gain = target_lufs - measured_lufs; cap upward boost at
         preset["max_boost_db"] so we never amplify silence/noise floor.
      5. Apply gain + Limiter (via pedalboard) in one pass — limiter catches
         stray peaks but has slow release so it doesn't pump.
      6. Measure AFTER metrics.

    Returns a dict with before/after metrics + path to output.
    """
    before = measure(in_path)
    if verbose:
        print(f"[BEFORE] {os.path.basename(in_path)}")
        print(f"  LUFS {before['lufs_i']:>6.1f}  TP {before['tp']:>5.2f}  "
              f"LRA {before['lra']:>5.1f}  DR {before['dr_db']:>5.1f}  "
              f"crest {before['crest']:>5.2f}")

    board, preset = build_chain(preset_name)
    tgt_lufs = target_lufs if target_lufs is not None else preset["target_lufs"]

    with AudioFile(in_path) as f:
        audio = f.read(f.frames)
        sr = f.samplerate
        channels = f.num_channels

    # 1) Apply EQ+sat chain
    processed = board(audio, sample_rate=float(sr))

    # 2) Write chain output to temp, measure LUFS
    tmp_path = str(out_path) + ".tmp.wav"
    with AudioFile(tmp_path, "w", sr, channels) as f:
        f.write(processed)

    measured_lufs = _measure_lufs(tmp_path)
    if measured_lufs is None:
        measured_lufs = -23.0

    # 3) Compute gain match. Cap upward boost per preset (default 6 dB) so we
    # never chase loudness by dragging up low-level material. Cap downward
    # at -20 dB as a sanity floor.
    raw_gain = tgt_lufs - measured_lufs
    max_boost = preset.get("max_boost_db", 6.0)
    gain_db = max(-20.0, min(max_boost, raw_gain))
    if verbose:
        capped = ""
        if abs(gain_db - raw_gain) > 0.01:
            capped = f"  [capped from {raw_gain:+.1f}]"
        print(f"  post-chain LUFS: {measured_lufs:.1f} → "
              f"gain {gain_db:+.1f} dB → target {tgt_lufs:.1f}{capped}")

    # 4) Apply gain + peak ceiling in one pedalboard pass. We use Clipping not
    # Limiter because pedalboard's Limiter adds upward makeup gain (acts like a
    # maximizer) which defeats our LUFS targeting. Clipping is a brick-wall
    # hard clip at threshold with no makeup — in practice it engages rarely
    # on ACE output and only catches stray transients.
    with AudioFile(tmp_path) as f:
        chain_audio = f.read(f.frames)
    final_board = pb.Pedalboard([
        pb.Gain(gain_db=float(gain_db)),
        pb.Clipping(threshold_db=preset["limiter_db"]),
    ])
    final_audio = final_board(chain_audio, sample_rate=float(sr))

    # 5) Write final file in requested format
    ext = Path(out_path).suffix.lower()
    final_tmp = str(out_path) + ".final.wav"
    with AudioFile(final_tmp, "w", sr, channels) as f:
        f.write(final_audio)

    if ext == ".flac":
        codec_args = ["-c:a", "flac", "-compression_level", "8"]
    elif ext == ".mp3":
        codec_args = ["-c:a", "libmp3lame", "-q:a", "2"]
    elif ext == ".wav":
        codec_args = ["-c:a", "pcm_s24le"]
    else:
        codec_args = ["-c:a", "pcm_s24le"]

    subprocess.run([FFMPEG, "-y", "-hide_banner", "-i", final_tmp,
        *codec_args, "-ar", str(sr), str(out_path)],
        check=True, capture_output=True)

    # Clean up temps
    for p_tmp in (tmp_path, final_tmp):
        try:
            os.remove(p_tmp)
        except Exception:
            pass

    after = measure(out_path)
    if verbose:
        delta_lra = (after["lra"] or 0) - (before["lra"] or 0)
        delta_dr = after["dr_db"] - before["dr_db"]
        delta_crest = after["crest"] - before["crest"]
        print(f"[AFTER]  {os.path.basename(out_path)}  (preset='{preset_name}')")
        print(f"  LUFS {after['lufs_i']:>6.1f}  TP {after['tp']:>5.2f}  "
              f"LRA {after['lra']:>5.1f}  DR {after['dr_db']:>5.1f}  "
              f"crest {after['crest']:>5.2f}")
        print(f"  Δ LRA {delta_lra:+.1f}   Δ DR {delta_dr:+.1f}   Δ crest {delta_crest:+.2f}")
    return {"before": before, "after": after, "preset": preset_name,
            "output_path": str(out_path)}


def main():
    p = argparse.ArgumentParser(description="Master an ACE Step track with a real mastering chain.")
    p.add_argument("input", help="Input audio file (FLAC/WAV/MP3)")
    p.add_argument("--output", "-o", default=None,
        help="Output path (default: <input>_mastered.<ext>)")
    p.add_argument("--preset", default="default", choices=list(PRESETS),
        help="Genre preset: default, edm, trap, chill, orchestral, jazz")
    p.add_argument("--target-lufs", type=float, default=None,
        help="Override the preset's target LUFS (EDM -11, pop -14, podcast -16, orchestral -18)")
    args = p.parse_args()

    if args.output is None:
        base = Path(args.input)
        args.output = str(base.with_name(base.stem + "_mastered" + base.suffix))

    master_track(args.input, args.output,
                 preset_name=args.preset,
                 target_lufs=args.target_lufs,
                 verbose=True)


if __name__ == "__main__":
    main()
