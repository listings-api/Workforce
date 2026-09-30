#!/usr/bin/env python3
"""Scripted stand-in for the `claude` CLI. Never touches the network.

Behaviour comes from the JSON scenario file at $WF_FAKE_SCENARIO:
  {"claude": [step, ...], "claude_auth_status": {...}}
Each run (-p) pops the next "claude" step; progress is shared across processes
through $WF_FAKE_SCENARIO.state and every run is logged to $WF_FAKE_SCENARIO.calls.jsonl.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid

CLI = "claude"
FIVE_HOUR_RESET = 1790676000
SEVEN_DAY_RESET = 1790838000

ERROR_TEXTS = {
    "model_unavailable": (
        'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
        '"message":"There\'s an issue with the selected model. It may not exist or you may not have access to it."}}'
    ),
    "auth": 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key · Please run /login"}}',
    "rate_limited": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"rate limit exceeded"}}',
}


def scenario_path():
    path = os.environ.get("WF_FAKE_SCENARIO")
    if not path:
        print("fake_claude: WF_FAKE_SCENARIO is not set", file=sys.stderr)
        sys.exit(5)
    return path


def load_scenario():
    with open(scenario_path()) as fh:
        return json.load(fh)


def pop_step():
    path = scenario_path() + ".state"
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        state = json.loads(raw) if raw.strip() else {}
        index = state.get(CLI, 0)
        state[CLI] = index + 1
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))
        fh.flush()
        fcntl.flock(fh, fcntl.LOCK_UN)
    steps = load_scenario().get(CLI, [])
    if index >= len(steps):
        print(f"fake_claude: scenario exhausted at call {index + 1}", file=sys.stderr)
        sys.exit(4)
    return steps[index], index + 1


def record_call(argv, prompt):
    call_id = uuid.uuid4().hex
    entry = {
        "id": call_id,
        "cli": CLI,
        "argv": argv,
        "cwd": os.getcwd(),
        "prompt": prompt,
        "env_keys": sorted(os.environ.keys()),
        "path": os.environ.get("PATH", ""),
    }
    with open(scenario_path() + ".calls.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return call_id


def attach_run_output(call_id, run_output):
    path = scenario_path() + ".calls.jsonl"
    with open(path, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        lines = fh.read().splitlines()
        for i, line in enumerate(lines):
            entry = json.loads(line)
            if entry.get("id") == call_id:
                entry["run"] = run_output
                lines[i] = json.dumps(entry)
        fh.seek(0)
        fh.truncate()
        fh.write("\n".join(lines) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)


def run_step_command(step, call_id, cwd):
    """Run the step's shell command in cwd; returns (returncode, stdout) or None if no command."""
    command = step.get("run")
    if not command:
        return None
    proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
    attach_run_output(
        call_id,
        {"cmd": command, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
    )
    return proc.returncode, proc.stdout


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def build_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--print", dest="prompt", nargs="?", const="")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--output-format")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--permission-mode")
    parser.add_argument("--setting-sources")
    parser.add_argument("--strict-mcp-config", action="store_true")
    parser.add_argument("--mcp-config")
    parser.add_argument("--json-schema")
    parser.add_argument("--resume")
    parser.add_argument("--chrome", action="store_true")
    parser.add_argument("--settings")
    return parser


def rate_limit_event(rate_limits, resets):
    def window(name, reset):
        return {"utilization": rate_limits[name], "resetsAt": resets.get(name, reset)}

    windows = {}
    if "five_hour" in rate_limits:
        windows["five_hour"] = window("five_hour", FIVE_HOUR_RESET)
    if "seven_day" in rate_limits:
        windows["seven_day"] = window("seven_day", SEVEN_DAY_RESET)
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"unifiedWindows": windows, "status": "allowed"},
    }


def handle_run(args, argv):
    prompt = args.prompt or ("" if sys.stdin.isatty() else sys.stdin.read())
    call_id = record_call(argv, prompt)
    step, number = pop_step()

    match = step.get("match")
    if match is not None:
        for needle in [match] if isinstance(match, str) else match:
            if needle not in prompt:
                print(f"fake_claude: prompt does not contain {needle!r}", file=sys.stderr)
                sys.exit(3)

    session_id = step.get("session_id") or args.resume or f"fake-claude-session-{number}"
    model = args.model or "fake-model"
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": model,
            "apiKeySource": step.get("api_key_source", "none"),
        }
    )

    if step.get("sleep"):
        time.sleep(step["sleep"])

    if step.get("rate_limits"):
        emit(rate_limit_event(step["rate_limits"], step.get("resets_at", {})))

    for rel, content in (step.get("write_files") or {}).items():
        target = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write(content)

    run_outcome = run_step_command(step, call_id, os.getcwd())
    run_error = None
    structured = step.get("structured")
    if run_outcome is not None:
        code, stdout = run_outcome
        if code != 0:
            run_error = f"fake run command failed with exit code {code}"
        elif step.get("structured_from_run"):
            try:
                structured = json.loads(stdout)
            except ValueError:
                run_error = "fake run command stdout was not JSON"

    error = step.get("error")
    if error == "crash":
        print("fake_claude: simulated crash", file=sys.stderr)
        sys.exit(1)

    is_error = False
    text = step.get("text", "OK")
    if error in ERROR_TEXTS:
        is_error, text = True, ERROR_TEXTS[error]
    elif step.get("result_error"):
        is_error, text = True, step["result_error"]
    elif run_error:
        is_error, text = True, run_error

    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    result = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "result": text,
        "session_id": session_id,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {model: {"inputTokens": 10, "outputTokens": 5}},
    }
    if structured is not None and not run_error:
        result["structured_output"] = structured
    emit(result)


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("2.1.284 (Claude Code)")
        return
    if argv[:2] == ["auth", "status"]:
        status = {"loggedIn": True, "authMethod": "claude.ai"}
        if os.environ.get("WF_FAKE_SCENARIO"):
            status = load_scenario().get("claude_auth_status", status)
        print(status if isinstance(status, str) else json.dumps(status))
        return
    args, _ = build_parser().parse_known_args(argv)
    handle_run(args, argv)


if __name__ == "__main__":
    main()
