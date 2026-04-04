#!/usr/bin/env python3
"""LLM judge for prompt/instruction quality.
Uses the user's existing CLI tool for evaluation.
DO NOT MODIFY after experiment starts — this is the fixed evaluator."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# --- CONFIGURE THESE ---
TARGET_FILE = ".autoresearch/engineering/prompt-etf-google/prompt-etf-google.md"
TEST_CASES_FILE = ".autoresearch/engineering/prompt-etf-google/tests/cases.json"
CLI_TOOL = "codex"
CLI_MODEL = "gpt-5.4-mini"
# --- END CONFIG ---

JUDGE_PROMPT_TEMPLATE = """You are evaluating a system prompt's effectiveness.

SYSTEM PROMPT BEING TESTED:
{prompt}

TEST INPUT:
{input}

EXPECTED OUTPUT (reference):
{expected}

ACTUAL OUTPUT:
{actual}

Score the actual output on these criteria (each 1-10):
1. ACCURACY — Does it match the expected output's intent and facts?
2. COMPLETENESS — Does it cover all required elements?
3. CLARITY — Is it well-structured and easy to understand?
4. INSTRUCTION_FOLLOWING — Does it follow the system prompt's guidelines?

Output EXACTLY: quality_score: <average of all 4>
Nothing else."""

try:
    prompt = Path(TARGET_FILE).read_text()
except FileNotFoundError:
    print(f"Target file not found: {TARGET_FILE}", file=sys.stderr)
    sys.exit(1)

try:
    test_cases = json.loads(Path(TEST_CASES_FILE).read_text())
except FileNotFoundError:
    print(f"Test cases file not found: {TEST_CASES_FILE}", file=sys.stderr)
    sys.exit(1)

scores = []


def run_codex(prompt_text, timeout=180):
    """Run Codex non-interactively and return only the last assistant message."""
    with tempfile.NamedTemporaryFile(mode="r+", suffix=".txt") as tmp:
        try:
            result = subprocess.run(
                [
                    CLI_TOOL,
                    "exec",
                    "-m",
                    CLI_MODEL,
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "--color",
                    "never",
                    "-o",
                    tmp.name,
                    "-",
                ],
                input=prompt_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"Codex call timed out after {timeout}s"
        if result.returncode != 0:
            return result.returncode, "", result.stderr
        tmp.seek(0)
        return 0, tmp.read().strip(), result.stderr

for i, case in enumerate(test_cases):
    # Generate output using the prompt
    gen_prompt = f"{prompt}\n\n{case['input']}"
    gen_code, actual, gen_stderr = run_codex(gen_prompt)
    if gen_code != 0:
        print(f"Generation failed for case {i+1}", file=sys.stderr)
        if gen_stderr:
            print(gen_stderr[:300], file=sys.stderr)
        scores.append(0)
        continue

    # Judge the output
    judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
        prompt=prompt[:500],
        input=case["input"],
        expected=case.get("expected", "N/A"),
        actual=actual[:500]
    )

    judge_code, judge_output, judge_stderr = run_codex(judge_prompt)
    if judge_code != 0:
        if judge_stderr:
            print(judge_stderr[:300], file=sys.stderr)
        scores.append(0)
        continue

    # Parse score
    for line in judge_output.splitlines():
        if "quality_score:" in line:
            try:
                score = float(line.split(":")[-1].strip())
                scores.append(score)
            except ValueError:
                scores.append(0)
            break
    else:
        scores.append(0)

    print(f"  Case {i+1}/{len(test_cases)}: {scores[-1]:.1f}", file=sys.stderr)

if not scores:
    print("No test cases evaluated", file=sys.stderr)
    sys.exit(1)

avg = sum(scores) / len(scores)
quality = avg * 10  # 1-10 scores → 10-100 range

print(f"quality_score: {quality:.2f}")
print(f"cases_tested: {len(scores)}")
print(f"avg_per_case: {avg:.2f}")
