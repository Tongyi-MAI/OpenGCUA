#!/usr/bin/env python3
"""Prepare public trajectory indexes and separate, sanitized media bundles."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

SITE = Path(__file__).resolve().parent
MODEL_IDS = ["gpt-6-astra_all", "opus-5_all", "gemini-3.8-flash_all", "glm-5.3-flash-all",
             "qwen3.8-max_all", "qwen3.8-flash_all", "kimi-k3_all", "deepseek-flash-all"]
PROVENANCE = {"source_dir", "source_traj", "source_path", "source_host", "unique_source_png_sha256"}
REDACTIONS = [
    (re.compile(r"((?:password|api[_-]?key|access[_-]?token|secret[_-]?key)\s*(?:is\s+|[:=]\s*)['\"])(?!\[redacted\])([^'\"\n]{4,})(['\"])", re.I), r"\1[redacted]\3"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})\b"), "[redacted-token]"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----.*?-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", re.S), "[redacted-private-key]"),
    (re.compile(r"[\w.+-]+@[\w.-]*alibaba-inc\.com", re.I), "redacted@example.invalid"),
    (re.compile(r"\b[\w.-]*alibaba-inc\.com\b", re.I), "[internal-host]"),
    (re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"), "[private-address]"),
    (re.compile(r"/home/admin(?:/[^\s\"'<>;,)}\]]*)?|/Users/[^\s\"'<>;,)}\]]+"), "/local/source"),
    (re.compile(r"\b(?:sandbox_jump\w*|gui-?live)\b", re.I), "[redacted]"),
]


def sanitize(value):
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items() if k not in PROVENANCE}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        for pattern, replacement in REDACTIONS:
            value = pattern.sub(replacement, value)
    return value


def score_of(record):
    match = re.search(r"(?:^|\n)score:\s*(-?\d+(?:\.\d+)?)", record.get("result") or "")
    return float(match[1]) if match else None


def task_index(records):
    tasks = {}
    for key, record in records.items():
        if key == "_meta":
            continue
        task_id = record["task_id"]
        steps = record.get("traj") or []
        task = tasks.setdefault(task_id, {"id": task_id, "instruction": "", "runs": []})
        goal = next((s["task_goal"] for s in steps if s.get("task_goal")), "")
        if goal and (not task["instruction"] or score_of(record) is not None):
            task["instruction"] = goal
        metrics = record.get("task_metrics") or {}
        task["runs"].append({"key": key, "id": record["run_id"], "steps": len(steps),
                             "score": score_of(record), "seconds": metrics.get("task_total_time_s"),
                             "output_tokens": metrics.get("output_tokens_total")})
    for task in tasks.values():
        task["runs"].sort(key=lambda r: r["id"])
        # Prefer the latest evaluated attempt, never the highest-scoring retry.
        evaluated = [r for r in task["runs"] if r["score"] is not None]
        nonempty = [r for r in task["runs"] if r["steps"]]
        task["default_run"] = (evaluated or nonempty or task["runs"])[-1]["key"]
    return sorted(tasks.values(), key=lambda t: t["id"])


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


def prepare(source, output, indexes):
    source, output, indexes = source.resolve(), output.resolve(), indexes.resolve()
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Keep publication output separate from the original bundles")
    public_src = (SITE / "src").resolve()
    if output.is_relative_to(public_src) or public_src.is_relative_to(output):
        raise ValueError("Media belongs in the separate asset repository, outside site/src")
    html = (SITE / "src/index.html").read_text()
    paper_rows = json.loads("[" + re.search(r"const results = \[(.*?)\n\];", html, re.S)[1]
                            .replace("name:", '"name":').replace("score:", '"score":')
                            .replace("steps:", '"steps":').replace("success:", '"success":')
                            .replace("tokens:", '"tokens":').replace("'", '"') + "]")
    catalog = {"asset_base": "https://tongyi-mai.github.io/OpenGCUA-blog/",
               "benchmark": "OSWorld-v2 v2.1", "leaderboard_tasks": 82,
               "paper": "https://arxiv.org/abs/2610.22200", "models": []}
    checksums = []
    for model_id, paper in zip(MODEL_IDS, paper_rows, strict=True):
        with zipfile.ZipFile(source / model_id / "traj.json.zip") as archive:
            records = sanitize(json.loads(archive.read("traj.json")))
        tasks = task_index(records)
        write_json(indexes / f"{model_id}.json", {"tasks": tasks})
        target = output / model_id
        target.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
        temporary_zip = target / "traj.json.zip.tmp"
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("traj.json", raw)
        temporary_zip.replace(target / "traj.json.zip")
        meta = records["_meta"]
        videos = [p["file"] for p in meta.get("video_parts", [])] or [meta["video_file"]]
        for name in videos:
            if Path(name).name != name:
                raise ValueError("Video filenames must be local basenames")
            shutil.copy2(source / model_id / name, target / name)
        model = {"id": model_id, "name": paper["name"], "task_count": len(tasks),
                 "record_count": sum(len(t["runs"]) for t in tasks),
                 "index": f"trajectory-data/{model_id}.json", "zip": f"{model_id}/traj.json.zip",
                 "zip_bytes": (target / "traj.json.zip").stat().st_size,
                 "zip_sha256": hashlib.sha256((target / "traj.json.zip").read_bytes()).hexdigest(),
                 "score": paper["score"][1], "baseline_score": paper["score"][0],
                 "success": paper["success"][1], "steps": paper["steps"][1], "tokens": paper["tokens"][1]}
        catalog["models"].append(model)
        for name in ["traj.json.zip", *videos]:
            file = target / name
            checksums.append(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {model_id}/{name}\n")
        print(f"Prepared {model_id}: {len(tasks)} tasks, {model['record_count']} attempts", flush=True)
    write_json(indexes / "catalog.json", catalog)
    (output / "SHA256SUMS").write_text("".join(checksums))
    (output / ".nojekyll").write_text("")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--indexes", type=Path, default=SITE / "src/trajectory-data")
    args = parser.parse_args()
    prepare(args.source, args.output, args.indexes)
