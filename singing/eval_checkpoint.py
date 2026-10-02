"""用指定 checkpoint 对固定 lead_dry 做换声，方便训练中对比听感。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("out_dir")
    parser.add_argument("--sovits-dir", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", default="configs/config.json")
    parser.add_argument("--diffusion-model", default="")
    parser.add_argument("--diffusion-config", default="configs/diffusion.yaml")
    parser.add_argument("--speaker", default="ATRI")
    parser.add_argument("--noise-scale", type=float, default=0.4)
    parser.add_argument("--k-step", type=int, default=100)
    parser.add_argument("--shallow", action="store_true")
    args = parser.parse_args()

    # Reuse svc_convert CLI
    root = Path(__file__).resolve().parent
    cmd = [
        sys.executable,
        str(root / "svc_convert.py"),
        args.input,
        args.out_dir,
        "--sovits-dir", args.sovits_dir,
        "--model", args.model,
        "--config", args.config,
        "--speaker", args.speaker,
        "--noise-scale", str(args.noise_scale),
        "--k-step", str(args.k_step),
        "--seed", "20260207",
    ]
    if args.shallow and args.diffusion_model:
        cmd += ["--diffusion-model", args.diffusion_model, "--diffusion-config", args.diffusion_config]
    else:
        cmd.append("--no-shallow-diffusion")
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
