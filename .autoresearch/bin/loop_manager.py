#!/usr/bin/env python3
"""
autoresearch-agent: Loop Manager

Manage GitHub Actions-backed experiment loops. This script owns loop.json
metadata, stop conditions, scheduled dispatch, and per-iteration accounting.
It does not decide what edit to make; callers are expected to prepare a
candidate change before invoking `tick`, or to provide a prepare command hook.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_WORKFLOW_FILE = ".github/workflows/autoresearch-loop.yml"
SUPPORTED_PROVIDERS = {"codex", "claude", "gemini"}
TRANSIENT_PREPARE_ERROR_PATTERNS = (
    r"responses_websocket: failed to connect to websocket: HTTP error: 500",
    r"unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, url: https://api\.openai\.com/v1/responses",
)


def find_autoresearch_root(project_root):
    """Find .autoresearch/ in project or user home."""
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


def current_branch(project_root):
    code, out, err = run_git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
    if code != 0:
        print(f"Failed to determine current branch: {err}")
        sys.exit(code)
    return out


def checkout_branch(project_root, branch):
    code, _, err = run_git(["checkout", branch], project_root)
    if code != 0:
        print(f"Failed to checkout {branch}: {err}")
        sys.exit(code)


def load_results(experiment_dir):
    """Load results.tsv into a list of dicts."""
    tsv = experiment_dir / "results.tsv"
    if not tsv.exists():
        return []

    rows = []
    for line in tsv.read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        metric = None
        if parts[1] != "N/A":
            try:
                metric = float(parts[1])
            except ValueError:
                metric = None
        rows.append({
            "commit": parts[0],
            "metric": metric,
            "status": parts[2],
            "description": parts[3],
        })
    return rows


def get_loop_path(experiment_dir):
    return experiment_dir / "loop.json"


def load_loop(experiment_dir):
    path = get_loop_path(experiment_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_loop(experiment_dir, loop_data):
    get_loop_path(experiment_dir).write_text(json.dumps(loop_data, indent=2, sort_keys=True) + "\n")


def delete_loop(experiment_dir):
    path = get_loop_path(experiment_dir)
    if path.exists():
        path.unlink()


def get_experiment_dir(root, experiment):
    experiment_dir = root / experiment
    if not experiment_dir.exists():
        print(f"Experiment not found: {experiment_dir}")
        sys.exit(1)
    return experiment_dir


def count_consecutive_status(results, status):
    streak = 0
    for row in reversed(results):
        if row["status"] == status:
            streak += 1
        else:
            break
    return streak


def stop_reason(loop_data, results):
    if loop_data.get("max_iterations") is not None:
        if loop_data.get("completed_iterations", 0) >= loop_data["max_iterations"]:
            return f"max iterations reached ({loop_data['completed_iterations']}/{loop_data['max_iterations']})"

    threshold = loop_data.get("stop_after_no_improve")
    if threshold is not None and loop_data.get("consecutive_no_improve", 0) >= threshold:
        return (
            "no-improvement threshold reached "
            f"({loop_data.get('consecutive_no_improve', 0)}/{threshold})"
        )

    crash_streak = count_consecutive_status(results, "crash")
    if crash_streak >= 5:
        return f"crash threshold reached ({crash_streak}/5)"

    return None


def print_loop(loop_data, results):
    print(f"  Experiment: {loop_data['experiment']}")
    print(f"  Branch: {loop_data['branch_ref']}")
    print(f"  Interval: {loop_data['interval']}")
    print(f"  Started: {loop_data['started']}")
    print(f"  Backend: {loop_data.get('backend', 'github_actions')}")
    print(f"  Workflow: {loop_data.get('workflow_file', DEFAULT_WORKFLOW_FILE)}")

    if loop_data.get("max_iterations") is not None:
        completed = loop_data.get("completed_iterations", 0)
        max_iterations = loop_data["max_iterations"]
        threshold = loop_data.get("stop_after_no_improve", 5)
        no_improve = loop_data.get("consecutive_no_improve", 0)
        print(f"  Progress: {completed}/{max_iterations}")
        print(f"  No improvement streak: {no_improve}/{threshold}")
    else:
        print("  Progress: unbounded")

    if loop_data.get("prepare_cmd"):
        print(f"  Prepare command: {loop_data['prepare_cmd']}")
    if loop_data.get("prepare_provider"):
        line = f"  Prepare provider: {loop_data['prepare_provider']}"
        if loop_data.get("prepare_model"):
            line += f" ({loop_data['prepare_model']})"
        print(line)

    crash_streak = count_consecutive_status(results, "crash")
    print(f"  Crash streak: {crash_streak}/5")


def parse_evaluator_provider(experiment_dir):
    config_path = experiment_dir / "config.cfg"
    if not config_path.exists():
        return None

    evaluate_cmd = None
    for line in config_path.read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == "evaluate_cmd":
            evaluate_cmd = value.strip()
            break
    if not evaluate_cmd:
        return None

    parts = evaluate_cmd.split()
    script_path = None
    for token in parts:
        if token.endswith(".py"):
            script_path = token
            break
    if not script_path:
        return None

    candidate = (experiment_dir.parents[2] / script_path).resolve()
    if not candidate.exists():
        candidate = (experiment_dir / script_path).resolve()
    if not candidate.exists():
        return None

    match = re.search(r'CLI_TOOL\s*=\s*"([^"]+)"', candidate.read_text())
    if not match:
        return None
    provider = match.group(1).strip()
    return provider if provider in SUPPORTED_PROVIDERS else None


def required_providers(experiment_dir, loop_data):
    providers = set()
    provider = loop_data.get("prepare_provider")
    if provider in SUPPORTED_PROVIDERS:
        providers.add(provider)
    evaluator = parse_evaluator_provider(experiment_dir)
    if evaluator in SUPPORTED_PROVIDERS:
        providers.add(evaluator)
    return sorted(providers)


def iter_experiment_dirs(root):
    for domain_dir in sorted(root.iterdir()):
        if not domain_dir.is_dir() or domain_dir.name.startswith("."):
            continue
        for exp_dir in sorted(domain_dir.iterdir()):
            if exp_dir.is_dir() and (exp_dir / "config.cfg").exists():
                yield f"{domain_dir.name}/{exp_dir.name}", exp_dir


def start_loop(args, project_root, root):
    experiment_dir = get_experiment_dir(root, args.experiment)
    loop_data = load_loop(experiment_dir)
    if loop_data and not args.replace:
        print(f"Loop already exists for {args.experiment}")
        print("Use --replace to overwrite the existing loop metadata.")
        sys.exit(1)

    payload = {
        "backend": "github_actions",
        "branch_ref": args.branch_ref or current_branch(project_root),
        "experiment": args.experiment,
        "interval": args.interval,
        "prepare_provider": args.prepare_provider,
        "started": datetime.now(timezone.utc).isoformat(),
        "workflow_file": args.workflow_file,
    }
    if args.prepare_model:
        payload["prepare_model"] = args.prepare_model
    default_prepare = [
        "python3",
        ".autoresearch/bin/prepare_iteration.py",
        "--experiment",
        args.experiment,
        "--path",
        ".",
        "--provider",
        args.prepare_provider,
    ]
    if args.prepare_model:
        default_prepare.extend(["--model", args.prepare_model])
    payload["prepare_cmd"] = args.prepare_cmd or shlex.join(default_prepare)
    if args.max_iterations is not None:
        payload["max_iterations"] = args.max_iterations
        payload["completed_iterations"] = 0
        payload["stop_after_no_improve"] = args.stop_after_no_improve
        payload["consecutive_no_improve"] = 0

    save_loop(experiment_dir, payload)

    print("Loop started")
    print_loop(payload, load_results(experiment_dir))


def providers_for_interval(args, project_root, root):
    providers = set()
    for _, experiment_dir in iter_experiment_dirs(root):
        loop_data = load_loop(experiment_dir)
        if not loop_data:
            continue
        if loop_data.get("backend", "github_actions") != "github_actions":
            continue
        if loop_data.get("interval") != args.interval:
            continue
        providers.update(required_providers(experiment_dir, loop_data))

    print(" ".join(sorted(providers)))


def status_loop(args, project_root, root):
    experiment_dir = get_experiment_dir(root, args.experiment)
    loop_data = load_loop(experiment_dir)
    if loop_data is None:
        print(f"No active loop for {args.experiment}")
        sys.exit(1)

    results = load_results(experiment_dir)
    reason = stop_reason(loop_data, results)
    print("Loop status")
    print_loop(loop_data, results)
    if reason:
        print(f"  Stop condition met: {reason}")


def stop_loop(args, project_root, root):
    experiment_dir = get_experiment_dir(root, args.experiment)
    loop_data = load_loop(experiment_dir)
    if loop_data is None:
        print(f"No active loop for {args.experiment}")
        return

    delete_loop(experiment_dir)
    print(f"Loop stopped for {args.experiment}")
    print(f"  Workflow: {loop_data.get('workflow_file', DEFAULT_WORKFLOW_FILE)}")
    if loop_data.get("max_iterations") is not None:
        print(
            "  Completed iterations: "
            f"{loop_data.get('completed_iterations', 0)}/{loop_data['max_iterations']}"
        )


def maybe_stop_before_tick(experiment_dir, loop_data, results):
    reason = stop_reason(loop_data, results)
    if reason is None:
        return False

    print(f"Loop stopping before tick: {reason}")
    print(f"  Workflow: {loop_data.get('workflow_file', DEFAULT_WORKFLOW_FILE)}")
    delete_loop(experiment_dir)
    return True


def run_shell_hook(cmd, cwd):
    if isinstance(cmd, str):
        result = subprocess.run(cmd, shell=True, cwd=str(cwd))
    else:
        result = subprocess.run(cmd, cwd=str(cwd))
    return result.returncode


def is_transient_prepare_failure(text):
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in TRANSIENT_PREPARE_ERROR_PATTERNS)


def run_prepare_with_retry(cmd, cwd, max_attempts=3, base_delay_seconds=3):
    for attempt in range(1, max_attempts + 1):
        if isinstance(cmd, str):
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )

        output = (result.stdout or "") + (result.stderr or "")
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        if result.returncode == 0:
            return 0
        if attempt >= max_attempts or not is_transient_prepare_failure(output):
            return result.returncode

        delay = base_delay_seconds * (2 ** (attempt - 1))
        print(
            "Prepare command failed due to a transient Codex transport/auth error; "
            f"retrying in {delay}s ({attempt}/{max_attempts})..."
        )
        time.sleep(delay)

    return 1


def run_iteration(project_root, experiment, experiment_dir, loop_data, dry_run=False, description=None, prepare_cmd=None):
    results_before = load_results(experiment_dir)
    if maybe_stop_before_tick(experiment_dir, loop_data, results_before):
        return "stopped"

    checkout_branch(project_root, loop_data["branch_ref"])

    if dry_run:
        print("Dry run: would checkout branch, prepare one change, and run run_experiment.py --dry-run")
        return "dry_run"

    effective_prepare_cmd = prepare_cmd or loop_data.get("prepare_cmd")
    if effective_prepare_cmd:
        print(f"Running prepare command: {effective_prepare_cmd}")
        hook_code = run_prepare_with_retry(effective_prepare_cmd, project_root)
        if hook_code != 0:
            print(f"Prepare command failed with exit {hook_code}")
            return "prepare_failed"

    runner = Path(__file__).with_name("run_experiment.py")
    cmd = [
        sys.executable,
        str(runner),
        "--experiment",
        experiment,
        "--path",
        str(project_root),
    ]
    cmd.append("--dry-run" if dry_run else "--single")
    if description:
        cmd.extend(["--description", description])

    result = subprocess.run(cmd, cwd=str(project_root))
    if result.returncode != 0:
        return "runner_failed"

    if dry_run:
        print("Dry run completed. Loop metadata unchanged.")
        return "dry_run"

    results_after = load_results(experiment_dir)
    if len(results_after) <= len(results_before):
        print("No new result row was recorded; loop metadata unchanged.")
        return "no_result"

    newest = results_after[-1]
    if loop_data.get("max_iterations") is not None:
        loop_data["completed_iterations"] = loop_data.get("completed_iterations", 0) + 1
        if newest["status"] == "keep":
            loop_data["consecutive_no_improve"] = 0
        elif newest["status"] == "discard":
            loop_data["consecutive_no_improve"] = loop_data.get("consecutive_no_improve", 0) + 1

    save_loop(experiment_dir, loop_data)
    reason = stop_reason(loop_data, results_after)
    if reason is not None:
        print(f"Loop stopping after tick: {reason}")
        print(f"  Workflow: {loop_data.get('workflow_file', DEFAULT_WORKFLOW_FILE)}")
        delete_loop(experiment_dir)
        return "stopped"

    print("Loop metadata updated")
    print_loop(loop_data, results_after)
    return newest["status"]


def tick_loop(args, project_root, root):
    experiment_dir = get_experiment_dir(root, args.experiment)
    loop_data = load_loop(experiment_dir)
    if loop_data is None:
        print(f"No active loop for {args.experiment}")
        sys.exit(1)

    outcome = run_iteration(
        project_root,
        args.experiment,
        experiment_dir,
        loop_data,
        dry_run=args.dry_run,
        description=args.description,
        prepare_cmd=args.prepare_cmd,
    )
    if outcome in {"prepare_failed", "runner_failed"}:
        sys.exit(1)


def dispatch_due(args, project_root, root):
    matched = False
    for experiment, experiment_dir in iter_experiment_dirs(root):
        loop_data = load_loop(experiment_dir)
        if not loop_data:
            continue
        if loop_data.get("backend", "github_actions") != "github_actions":
            continue
        if loop_data.get("interval") != args.interval:
            continue

        matched = True
        print(f"\nDispatching {experiment}")
        outcome = run_iteration(
            project_root,
            experiment,
            experiment_dir,
            loop_data,
            dry_run=args.dry_run,
        )
        if outcome in {"prepare_failed", "runner_failed"} and not args.keep_going:
            sys.exit(1)

    if not matched:
        print(f"No active GitHub Actions loops for interval '{args.interval}'")


def main():
    parser = argparse.ArgumentParser(description="autoresearch-agent loop manager")
    parser.add_argument("--path", default=".", help="Project root")

    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Create loop metadata")
    start.add_argument("--experiment", required=True, help="Experiment path: domain/name")
    start.add_argument("--interval", required=True, help="Loop interval: 5m, 30m, 1h, daily, weekly, monthly")
    start.add_argument("--branch-ref", help="Existing branch to run iterations on (defaults to current branch)")
    start.add_argument("--workflow-file", default=DEFAULT_WORKFLOW_FILE, help="GitHub Actions workflow file path")
    start.add_argument(
        "--prepare-provider",
        choices=sorted(SUPPORTED_PROVIDERS),
        default="codex",
        help="LLM CLI provider for the default prepare step",
    )
    start.add_argument("--prepare-model", help="Optional model override for the default prepare step")
    start.add_argument("--prepare-cmd", help="Command to prepare one candidate change inside the Actions runner")
    start.add_argument("--max-iterations", type=int, help="Maximum total number of iterations")
    start.add_argument(
        "--stop-after-no-improve",
        type=int,
        default=5,
        help="Stop after N consecutive discard results when bounded (default: 5)",
    )
    start.add_argument("--replace", action="store_true", help="Overwrite existing loop.json")

    status = subparsers.add_parser("status", help="Show loop metadata")
    status.add_argument("--experiment", required=True, help="Experiment path: domain/name")

    stop = subparsers.add_parser("stop", help="Delete loop metadata")
    stop.add_argument("--experiment", required=True, help="Experiment path: domain/name")

    tick = subparsers.add_parser("tick", help="Run one managed iteration")
    tick.add_argument("--experiment", required=True, help="Experiment path: domain/name")
    tick.add_argument("--dry-run", action="store_true", help="Call run_experiment.py in dry-run mode")
    tick.add_argument("--description", help="Description forwarded to run_experiment.py")
    tick.add_argument("--prepare-cmd", help="Optional shell command to run before evaluation")

    dispatch = subparsers.add_parser("dispatch-due", help="Run all active loops for one interval")
    dispatch.add_argument("--interval", required=True, help="Loop interval: 5m, 30m, 1h, daily, weekly, monthly")
    dispatch.add_argument("--dry-run", action="store_true", help="Call run_experiment.py in dry-run mode")
    dispatch.add_argument("--keep-going", action="store_true", help="Continue dispatching even if one loop fails")

    providers = subparsers.add_parser("providers-for-interval", help="Print required CLI providers for one interval")
    providers.add_argument("--interval", required=True, help="Loop interval: 5m, 30m, 1h, daily, weekly, monthly")

    args = parser.parse_args()
    project_root = Path(args.path).resolve()
    root = find_autoresearch_root(project_root)
    if root is None:
        print("No .autoresearch/ found. Run setup_experiment.py first.")
        sys.exit(1)

    if args.command == "start":
        start_loop(args, project_root, root)
    elif args.command == "status":
        status_loop(args, project_root, root)
    elif args.command == "stop":
        stop_loop(args, project_root, root)
    elif args.command == "tick":
        tick_loop(args, project_root, root)
    elif args.command == "dispatch-due":
        dispatch_due(args, project_root, root)
    elif args.command == "providers-for-interval":
        providers_for_interval(args, project_root, root)


if __name__ == "__main__":
    main()
