"""Run the labelled probes in tests/laya_probes against a live Ollaya server and report per-key reliability.

    python -m workforce.decider.calibrate [--model laya:en|laya:typed-decisions] [--url URL] [--json]
"""

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

from workforce import config as wf_config
from workforce.decider import questions
from workforce.decider.base import KEYS
from workforce.decider.laya import LAYA_MODEL, LayaDecider
from workforce.errors import ConfigError

PROBE_DIR = Path(__file__).resolve().parents[2] / "tests" / "laya_probes"
TARGET_PRECISION = 0.9
MIN_KEPT = 4
REQUEST_TIMEOUT_S = 30.0
MODELS = ("laya:en", "laya:typed-decisions")


class _NoEvents:
    def emit(self, kind: str, **data: Any) -> dict:
        return {}


@dataclass
class ProbeResult:
    label: Any
    answer: Any
    confidence: float | None
    error: str | None

    @property
    def correct(self) -> bool:
        return self.error is None and self.answer == self.label


@dataclass
class KeyReport:
    key: str
    n: int
    errors: int
    accuracy: float | None
    chance: float
    mean_conf_right: float | None
    mean_conf_wrong: float | None
    threshold: float | None
    kept: int
    precision: float | None

    @property
    def coverage(self) -> float | None:
        answered = self.n - self.errors
        return self.kept / answered if answered else None


def load_probes(key: str, directory: Path = PROBE_DIR) -> list[dict]:
    """Read `<directory>/<key>.jsonl`; every line is `{"args": {...}, "label": ...}`."""
    if key not in KEYS:
        raise ValueError(f"unknown decision key '{key}'")
    path = Path(directory) / f"{key}.jsonl"
    probes = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            probe = json.loads(line)
            probe["args"], probe["label"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: not a probe line ({exc})") from exc
        probes.append(probe)
    if not probes:
        raise ValueError(f"{path} has no probes")
    return probes


def is_bool_key(key: str) -> bool:
    return questions.CHOICE_OPTIONS.get(key) is None


def run_probes(decider: LayaDecider, key: str, probes: Sequence[dict]) -> list[ProbeResult]:
    """Ask Laya every probe of `key`; a fallback decision (server error, timeout) is recorded as an error."""
    results = []
    for probe in probes:
        state, question, options = questions.BUILDERS[key](**probe["args"])
        decision = decider.ask_choice(key, state, question, options) if options else decider.ask_bool(key, state, question)
        error = None if decision.source == "laya" else str((decision.raw or {}).get("error", "no answer"))
        results.append(ProbeResult(probe["label"], decision.answer, decision.confidence, error))
    return results


def _chance(results: Sequence[ProbeResult]) -> float:
    labels = [result.label for result in results]
    return max(labels.count(label) for label in set(labels)) / len(labels)


def _floor3(value: float) -> float:
    return math.floor(value * 1000) / 1000


def analyse(key: str, results: Sequence[ProbeResult], min_kept: int = MIN_KEPT, target: float = TARGET_PRECISION) -> KeyReport:
    """Accuracy, mean confidence right/wrong, and the lowest threshold whose kept answers are >= `target` precise.

    A threshold only counts if at least `min_kept` answers clear it; otherwise the key is unreliable (threshold None).
    """
    answered = [result for result in results if result.error is None]
    right = [result for result in answered if result.correct]
    wrong = [result for result in answered if not result.correct]

    def mean(items: Sequence[ProbeResult]) -> float | None:
        return sum(item.confidence for item in items) / len(items) if items else None

    threshold = kept = precision = None
    for candidate in sorted({_floor3(result.confidence) for result in answered}):
        clear = [result for result in answered if result.confidence >= candidate]
        if len(clear) < min_kept:
            break
        share = sum(result.correct for result in clear) / len(clear)
        if share >= target:
            threshold, kept, precision = candidate, len(clear), share
            break
    return KeyReport(
        key=key,
        n=len(results),
        errors=len(results) - len(answered),
        accuracy=len(right) / len(answered) if answered else None,
        chance=_chance(results),
        mean_conf_right=mean(right),
        mean_conf_wrong=mean(wrong),
        threshold=threshold,
        kept=kept or 0,
        precision=precision,
    )


def calibrate(
    decider: LayaDecider,
    directory: Path = PROBE_DIR,
    keys: Sequence[str] | None = None,
    details: dict[str, list[tuple[dict, ProbeResult]]] | None = None,
) -> list[KeyReport]:
    """Run every probe of `keys` (default all); per-probe results are also stored in `details` when given."""
    reports = []
    for key in keys or sorted(KEYS):
        probes = load_probes(key, directory)
        results = run_probes(decider, key, probes)
        if details is not None:
            details[key] = list(zip(probes, results))
        reports.append(analyse(key, results))
    return reports


def format_details(details: dict[str, list[tuple[dict, ProbeResult]]]) -> str:
    lines = []
    for key, pairs in details.items():
        lines.append(f"{key}:")
        for probe, result in sorted(pairs, key=lambda pair: -(pair[1].confidence or 0)):
            mark = "ERR" if result.error else ("ok " if result.correct else "BAD")
            note = probe.get("note") or ""
            lines.append(f"  {mark} conf={_fmt(result.confidence, 3)} label={result.label!s:12} answer={result.answer!s:12} {note}")
    return "\n".join(lines)


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_report(model: str, reports: Sequence[KeyReport]) -> str:
    header = f"model {model}: {int(TARGET_PRECISION * 100)}% precision target, at least {MIN_KEPT} kept answers"
    rows = [
        ("key", "n", "acc", "chance", "conf right", "conf wrong", "threshold", "kept", "coverage"),
    ]
    for report in reports:
        rows.append(
            (
                report.key,
                f"{report.n}" + (f" ({report.errors} err)" if report.errors else ""),
                _fmt(report.accuracy),
                _fmt(report.chance),
                _fmt(report.mean_conf_right),
                _fmt(report.mean_conf_wrong),
                "unreliable" if report.threshold is None else f"{report.threshold:.3f}",
                str(report.kept) if report.threshold is not None else "-",
                _fmt(report.coverage) if report.threshold is not None else "-",
            )
        )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = [header]
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m workforce.decider.calibrate", description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=LAYA_MODEL, help="model to calibrate (default: %(default)s)")
    parser.add_argument("--url", help="Ollaya base URL; default: [decider] url from ./workforce.toml")
    parser.add_argument("--probes", type=Path, default=PROBE_DIR, help="directory of <key>.jsonl probe files")
    parser.add_argument("--key", action="append", choices=sorted(KEYS), help="only this key (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the reports as JSON")
    parser.add_argument("--details", action="store_true", help="also print every probe with its answer and confidence")
    args = parser.parse_args(argv)

    url = args.url
    if url is None:
        try:
            url = wf_config.load(Path.cwd()).decider.url
        except ConfigError as exc:
            print(f"no --url given and no usable workforce.toml here: {exc}", file=stderr)
            return 2
    decider = LayaDecider(url, 1.0, _NoEvents(), timeout_s=REQUEST_TIMEOUT_S, model=args.model)
    try:
        if not decider.available():
            print(f"{url} is not serving '{args.model}'; start Ollaya and run `ollaya pull {args.model}`", file=stderr)
            return 1
        details: dict[str, list[tuple[dict, ProbeResult]]] = {}
        reports = calibrate(decider, args.probes, args.key, details if args.details else None)
    except (OSError, ValueError) as exc:
        print(f"calibration failed: {exc}", file=stderr)
        return 2
    finally:
        decider.close()

    if args.json:
        print(json.dumps({"model": args.model, "keys": [asdict(report) for report in reports]}, indent=2), file=stdout)
    else:
        print(format_report(args.model, reports), file=stdout)
        if args.details:
            print("\n" + format_details(details), file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
