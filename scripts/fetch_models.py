#!/usr/bin/env python3
"""fetch_models.py — download the required ComfyUI model files for aeon-radio-drama.

Reads COMFYUI_ROOT / HF_TOKEN from the environment (sourced from .env by the
caller). Skips any file already present at its destination. The Stable Audio
Open checkpoint is gated and requires HF_TOKEN with the license accepted at
https://huggingface.co/stabilityai/stable-audio-open-1.0 — that entry is
skipped (not failed) when no token is set, so the rest of the run can proceed.
"""

import os
import shutil
import sys
import tempfile

from huggingface_hub import hf_hub_download
from huggingface_hub.utils import HfHubHTTPError

# (destination relative to COMFYUI_ROOT/models, repo_id, filename in repo, gated?)
FILES = [
    ("diffusion_models/acestep_v1.5_xl_base.safetensors",
     "Comfy-Org/ace_step_1.5_ComfyUI_files",
     "split_files/diffusion_models/acestep_v1.5_xl_base_bf16.safetensors", False),
    ("text_encoders/qwen_0.6b_ace15.safetensors",
     "Comfy-Org/ace_step_1.5_ComfyUI_files",
     "split_files/text_encoders/qwen_0.6b_ace15.safetensors", False),
    ("text_encoders/qwen_4b_ace15.safetensors",
     "Comfy-Org/ace_step_1.5_ComfyUI_files",
     "split_files/text_encoders/qwen_4b_ace15.safetensors", False),
    ("vae/ace_1.5_vae.safetensors",
     "Comfy-Org/ace_step_1.5_ComfyUI_files",
     "split_files/vae/ace_1.5_vae.safetensors", False),
    ("mmaudio/mmaudio_large_44k_v2_fp16.safetensors",
     "Kijai/MMAudio_safetensors",
     "mmaudio_large_44k_v2_fp16.safetensors", False),
    ("mmaudio/mmaudio_vae_44k_fp16.safetensors",
     "Kijai/MMAudio_safetensors",
     "mmaudio_vae_44k_fp16.safetensors", False),
    ("mmaudio/mmaudio_synchformer_fp16.safetensors",
     "Kijai/MMAudio_safetensors",
     "mmaudio_synchformer_fp16.safetensors", False),
    ("mmaudio/apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors",
     "Kijai/MMAudio_safetensors",
     "apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors", False),
    ("text_encoders/stable-audio-open-t5.safetensors",
     "google-t5/t5-base",
     "model.safetensors", False),
    ("checkpoints/stable-audio-open-1.0.safetensors",
     "stabilityai/stable-audio-open-1.0",
     "model.safetensors", True),
]


def main():
    comfyui_root = os.environ.get("COMFYUI_ROOT")
    if not comfyui_root:
        print("COMFYUI_ROOT not set", file=sys.stderr)
        sys.exit(1)
    models_dir = os.path.join(comfyui_root, "models")
    token = os.environ.get("HF_TOKEN") or None

    ok, skipped, failed = [], [], []
    for rel_dest, repo_id, filename, gated in FILES:
        dest = os.path.join(models_dir, rel_dest)
        if os.path.exists(dest):
            print(f"[=] already present: {rel_dest}")
            skipped.append(rel_dest)
            continue
        if gated and not token:
            print(f"[ ] SKIP (gated, no HF_TOKEN): {rel_dest}  <- {repo_id}")
            print("      accept the license at https://huggingface.co/"
                  f"{repo_id} then set HF_TOKEN in .env and re-run")
            skipped.append(rel_dest)
            continue

        print(f"[.] fetching {rel_dest}  <- {repo_id}:{filename}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = hf_hub_download(
                    repo_id=repo_id,
                    filename=filename,
                    local_dir=tmp,
                    token=token,
                )
            except HfHubHTTPError as e:
                print(f"[!] FAILED: {rel_dest}: {e}")
                failed.append(rel_dest)
                continue
            shutil.move(path, dest)
        print(f"[x] saved -> {dest}")

        ok.append(rel_dest)

    print(f"\n{len(ok)} downloaded, {len(skipped)} skipped, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()