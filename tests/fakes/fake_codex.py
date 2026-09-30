#!/usr/bin/env python3
"""Scripted stand-in for the `codex` CLI. Never touches the network.

Behaviour comes from the JSON scenario file at $WF_FAKE_SCENARIO:
  {"codex": [step, ...], "codex_limits": {...}, "codex_login_status": "..."}
Each `exec` run pops the next "codex" step; progress is shared across processes
through $WF_FAKE_SCENARIO.state and every run is logged to $WF_FAKE_SCENARIO.calls.jsonl.
`app-server` answers initialize, account/rateLimits/read from "codex_limits" and model/list from "codex_models".
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid

CLI = "codex"

ERROR_MESSAGES = {
    "model_unavailable": "The 'fake-model' model is not supported when using Codex with a ChatGPT account.",
    "auth": "401 Unauthorized: not logged in, please log in with `codex login`",
    "rate_limited": "You've hit your usage limit. Try again later.",
}


def scenario_path():
    path = os.environ.get("WF_FAKE_SCENARIO")
    if not path:
        print("fake_codex: WF_FAKE_SCENARIO is not set", file=sys.stderr)
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
        print(f"fake_codex: scenario exhausted at call {index + 1}", file=sys.stderr)
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
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-m", "--model")
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("-s", "--sandbox")
    parser.add_argument("-C", "--cd")
    parser.add_argument("-o", "--output-last-message")
    parser.add_argument("--output-schema")
    parser.add_argument("--disable", action="append", default=[])
    parser.add_argument("--enable", action="append", default=[])
    parser.add_argument("positionals", nargs="*")
    return parser


def handle_exec(rest, argv):
    resume = bool(rest) and rest[0] == "resume"
    if resume:
        rest = rest[1:]
    args, _ = build_parser().parse_known_intermixed_args(rest)
    positionals = args.positionals
    thread_id = positionals[0] if resume and positionals else None
    prompt = positionals[-1] if positionals else ""
    call_id = record_call(argv, prompt)
    step, number = pop_step()

    match = step.get("match")
    if match is not None:
        for needle in [match] if isinstance(match, str) else match:
            if needle not in prompt:
                print(f"fake_codex: prompt does not contain {needle!r}", file=sys.stderr)
                sys.exit(3)

    thread_id = step.get("session_id") or thread_id or f"fake-codex-thread-{number}"
    emit({"type": "thread.started", "thread_id": thread_id})
    emit({"type": "turn.started"})

    if step.get("sleep"):
        time.sleep(step["sleep"])

    workdir = args.cd or os.getcwd()
    for rel, content in (step.get("write_files") or {}).items():
        target = os.path.join(workdir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write(content)

    run_outcome = run_step_command(step, call_id, workdir)
    structured = step.get("structured")
    if run_outcome is not None:
        code, stdout = run_outcome
        if code != 0:
            print(f"fake_codex: run command failed with exit code {code}", file=sys.stderr)
            sys.exit(1)
        if step.get("structured_from_run"):
            try:
                structured = json.loads(stdout)
            except ValueError:
                print("fake_codex: run command stdout was not JSON", file=sys.stderr)
                sys.exit(1)

    error = step.get("error")
    if error == "crash":
        print("fake_codex: simulated crash", file=sys.stderr)
        sys.exit(1)
    if error in ERROR_MESSAGES or step.get("turn_error"):
        message = ERROR_MESSAGES.get(error) or step["turn_error"]
        emit({"type": "error", "message": message})
        emit({"type": "turn.failed", "error": {"message": message}})
        sys.exit(1)

    if structured is not None:
        text = step.get("text") or json.dumps(structured)
        output = json.dumps(structured)
    else:
        text = step.get("text", "OK")
        output = text
    if step.get("raw_output") is not None:
        output = step["raw_output"]
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
        }
    )
    if args.output_last_message:
        with open(args.output_last_message, "w") as fh:
            fh.write(output)


def handle_app_server():
    scenario = load_scenario()
    limits = scenario.get("codex_limits")
    models = scenario.get("codex_models")
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            emit({"id": msg["id"], "result": {"userAgent": "fake-codex/0.158.0"}})
        elif method == "account/rateLimits/read" and limits is not None:
            emit({"id": msg["id"], "result": {"rateLimits": limits}})
        elif method == "model/list" and models is not None:
            emit({"id": msg["id"], "result": {"data": models, "nextCursor": None}})
        else:
            emit({"id": msg["id"], "error": {"code": -32601, "message": f"unsupported: {method}"}})


def main():
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print("codex-cli 0.158.0")
        return
    if argv[:2] == ["login", "status"]:
        status = "Logged in using ChatGPT"
        if os.environ.get("WF_FAKE_SCENARIO"):
            status = load_scenario().get("codex_login_status", status)
        print(status, file=sys.stderr)
        return
    if argv[:1] == ["app-server"]:
        handle_app_server()
        return
    if argv[:1] == ["exec"]:
        handle_exec(argv[1:], argv)
        return
    print(f"fake_codex: unsupported invocation {argv}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
