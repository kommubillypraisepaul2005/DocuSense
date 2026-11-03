"""Benchmark several local Ollama model variants against the same held-out set."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--models", required=True, help="Comma-separated Ollama model tags to test as the vision model")
    parser.add_argument("--text-model", default="qwen2.5:7b")
    parser.add_argument("--mode", choices=["auto", "text", "vision_zero_shot", "vision", "rules"], default="auto")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/model-comparison"))
    parser.add_argument("--min-f1", type=float, default=None, help="Fail if any candidate model is below this field F1")
    args = parser.parse_args()
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for index, model in enumerate(models, 1):
        output = output_dir / f"{index:02d}-{model.replace(':', '-')}.json"
        env = os.environ.copy()
        env["OLLAMA_TEXT_MODEL"] = args.text_model
        env["OLLAMA_VISION_MODEL"] = model
        command = [sys.executable, str(ROOT / "scripts" / "evaluate.py"), str(args.manifest.resolve()), "--mode", args.mode, "--output", str(output)]
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        report = json.loads(output.read_text(encoding="utf-8"))
        summaries.append({"vision_model": model, **report["metrics"]})
    summary_file = output_dir / "comparison.json"
    summary_file.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print(json.dumps({"comparison": str(summary_file), "models": summaries}, indent=2))
    if args.min_f1 is not None and any(item["field_f1"] < args.min_f1 for item in summaries):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
