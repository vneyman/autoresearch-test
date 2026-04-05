#!/usr/bin/env python3
"""
autoresearch-agent: Prepare Iteration

Make exactly one candidate change to the experiment target using a configured
LLM CLI provider, then commit only that target file. This is intended to run
inside GitHub Actions or other non-interactive loop environments before
`run_experiment.py --single`.
"""

import argparse
import subprocess
import sys
from pathlib import Path

SUPPORTED_PROVIDERS = {"codex", "claude", "gemini"}


def find_autoresearch_root(project_root):
    project_root_root = project_root / ".autoresearch"
    if project_root_root.exists():
        return project_root_root
    user_root = Path.home() / ".autoresearch"
    if user_root.exists():
        return user_root
    return None


def run_git(args, cwd):
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def load_config(experiment_dir):
    config = {}
    cfg_file = experiment_dir / "config.cfg"
    if not cfg_file.exists():
        raise FileNotFoundError(f"Missing config: {cfg_file}")
    for line in cfg_file.read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        config[key.strip()] = value.strip()
    return config


def load_recent_results(experiment_dir, limit=10):
    tsv = experiment_dir / "results.tsv"
    if not tsv.exists():
        return []
    rows = []
    for line in tsv.read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rows.append({
            "commit": parts[0],
            "metric": parts[1],
            "status": parts[2],
            "description": parts[3],
        })
    return rows[-limit:]


def build_prompt(target_path, target_text, program_text, results):
    recent = "\n".join(
        f"- {row['commit']} | {row['status']} | {row['metric']} | {row['description']}"
        for row in results
    ) or "- no prior results"
    return f"""You are running one autoresearch iteration inside a git repository.

Goal:
- Improve the metric for the active experiment by editing exactly one file.

Hard rules:
- Edit ONLY this file: {target_path}
- Make exactly one focused candidate change.
- Do NOT edit evaluator files, config files, tests, workflow files, or any other file.
- Keep the change small and reviewable.
- If the target already has constraints, preserve them.

Experiment guidance from program.md:
{program_text}

Recent experiment history:
{recent}

Current target file contents:
```text
{target_text}
```

Return the complete revised contents for {target_path} with exactly one new
candidate change aimed at improving the experiment metric.

Hard output rule:
- Output ONLY the full file contents for {target_path}
- Do NOT add explanations
- Do NOT wrap the answer in markdown fences
"""


def build_cli_command(provider, prompt, model):
    if provider == "codex":
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "--color",
            "never",
            "-",
        ]
        if model:
            cmd[2:2] = ["-m", model]
        return cmd, prompt

    if provider == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        return cmd, None

    if provider == "gemini":
        cmd = ["gemini", "-p", prompt, "--output-format", "text"]
        if model:
            cmd.extend(["-m", model])
        return cmd, None

    raise ValueError(f"Unsupported provider: {provider}")


def run_provider(project_root, provider, prompt, model, timeout):
    cmd, stdin_payload = build_cli_command(provider, prompt, model)
    return subprocess.run(
        cmd,
        cwd=str(project_root),
        input=stdin_payload,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def normalize_generated_text(text):
    content = text.strip()
    if not content:
        return content

    if content.startswith("```") and content.endswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).rstrip() + "\n"

    if not content.endswith("\n"):
        content += "\n"
    return content


def changed_files(project_root):
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or "git status failed")

    files = []
    for entry in result.stdout.split("\0"):
        if len(entry) < 4:
            continue
        files.append(entry[3:])
    return files


def commit_target(project_root, target, experiment):
    code, _, err = run_git(["add", "--", target], project_root)
    if code != 0:
        raise RuntimeError(err or "git add failed")
    code, diff_stat, err = run_git(["diff", "--cached", "--stat"], project_root)
    if code != 0:
        raise RuntimeError(err or "git diff --cached failed")
    summary = diff_stat.splitlines()[0].strip() if diff_stat else f"update {Path(target).name}"
    message = f"experiment: {summary[:60]}"
    code, _, err = run_git(["commit", "-m", message], project_root)
    if code != 0:
        raise RuntimeError(err or "git commit failed")
    return message


def main():
    parser = argparse.ArgumentParser(description="Prepare one candidate iteration change")
    parser.add_argument("--experiment", required=True, help="Experiment path: domain/name")
    parser.add_argument("--path", default=".", help="Project root")
    parser.add_argument(
        "--provider",
        default="codex",
        choices=sorted(SUPPORTED_PROVIDERS),
        help="LLM CLI provider to use for candidate generation",
    )
    parser.add_argument("--model", help="Optional model override for the selected provider")
    parser.add_argument("--timeout", type=int, default=600, help="CLI timeout in seconds")
    args = parser.parse_args()

    project_root = Path(args.path).resolve()
    root = find_autoresearch_root(project_root)
    if root is None:
        print("No .autoresearch/ found. Run setup first.", file=sys.stderr)
        sys.exit(1)

    experiment_dir = root / args.experiment
    if not experiment_dir.exists():
        print(f"Experiment not found: {experiment_dir}", file=sys.stderr)
        sys.exit(1)

    config = load_config(experiment_dir)
    target = config.get("target")
    if not target:
        print("config.cfg is missing target", file=sys.stderr)
        sys.exit(1)

    target_path = project_root / target
    if not target_path.exists():
        print(f"Target file not found: {target_path}", file=sys.stderr)
        sys.exit(1)

    program_path = experiment_dir / "program.md"
    program_text = program_path.read_text() if program_path.exists() else ""
    target_text = target_path.read_text()
    prompt = build_prompt(target, target_text, program_text, load_recent_results(experiment_dir))

    try:
        result = run_provider(project_root, args.provider, prompt, args.model, args.timeout)
    except subprocess.TimeoutExpired:
        print(f"{args.provider} edit timed out after {args.timeout}s", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        print(stderr or stdout or f"{args.provider} edit failed", file=sys.stderr)
        sys.exit(result.returncode)

    generated_text = normalize_generated_text(result.stdout or "")
    if not generated_text.strip():
        print(f"{args.provider} returned empty file contents", file=sys.stderr)
        sys.exit(1)

    target_path.write_text(generated_text)

    files = changed_files(project_root)
    unexpected = [path for path in files if path != target]
    if unexpected:
        print(
            "Prepare step changed files outside the target: "
            + ", ".join(unexpected),
            file=sys.stderr,
        )
        sys.exit(1)
    if target not in files:
        print("Prepare step produced no target change", file=sys.stderr)
        sys.exit(1)

    message = commit_target(project_root, target, args.experiment)
    print(message)


if __name__ == "__main__":
    main()
