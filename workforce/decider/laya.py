"""Laya decider: talks to a local Ollaya server over its TypeSafe-compatible `/v1/systemone` API."""

import logging
import threading
import time
from typing import Any, Callable, Mapping

import httpx

from workforce.config import DeciderConfig
from workforce.decider import questions
from workforce.decider.base import KEYS, Decision, NullDecider, is_confident
from workforce.decider.fallback import FallbackDecider
from workforce.events import EventLog

logger = logging.getLogger(__name__)

LAYA_MODEL = "laya:en"
API_KEY = "local"
MAX_STATE_CHARS = 1500
MODEL_STATE_CHARS = {"laya:en": 1000, "laya": 1000}
TRUNCATION_RETRIES = 2
RETRY_SHRINK = 0.6
HEALTH_TTL_S = 60.0
_TRUNCATION_MARKER = "\n…[{n} characters cut]…\n"
_HEAD_SHARE = 0.6
_QUESTION_ID = "q"


def truncate_state(state: str, limit: int = MAX_STATE_CHARS) -> tuple[str, int]:
    """Cut `state` to at most `limit` characters keeping head and tail; returns (text, characters cut)."""
    if len(state) <= limit:
        return state, 0
    cut = len(state) - limit
    while True:
        marker = _TRUNCATION_MARKER.format(n=cut)
        keep = limit - len(marker)
        if keep <= 0:
            return state[:limit], len(state) - limit
        new_cut = len(state) - keep
        if new_cut == cut:
            break
        cut = new_cut
    head = int(keep * _HEAD_SHARE)
    tail = keep - head
    return state[:head] + marker + (state[len(state) - tail :] if tail else ""), cut


class LayaError(Exception):
    """A Laya request failed or returned something unusable; caught inside LayaDecider."""


class _StateTooLong(LayaError):
    """Laya answered 422 STATE_TRUNCATED: the state does not fit the model's context."""


class LayaDecider:
    """Implements `Decider` against Ollaya. Never raises: any failure yields a `source="fallback"` decision."""

    def __init__(
        self,
        url: str,
        confidence: float,
        events: EventLog,
        timeout_s: float = 2.0,
        model: str = LAYA_MODEL,
        clock: Callable[[], float] = time.monotonic,
        thresholds: Mapping[str, float] | None = None,
        models: Mapping[str, str] | None = None,
    ):
        """`thresholds` (key → confidence) overrides `confidence` per key; when it is given, a key missing from it
        is unreliable and always answers with its fallback. `models` (key → model name) overrides `model` per key."""
        for name, table in (("thresholds", thresholds), ("models", models)):
            unknown = set(table or {}) - KEYS
            if unknown:
                raise ValueError(f"unknown decision keys in {name}: {sorted(unknown)}")
        for key, value in (thresholds or {}).items():
            if not 0 < value <= 1:
                raise ValueError(f"threshold for '{key}' ({value}) must be greater than 0 and at most 1")
        self.url = url.rstrip("/")
        self.confidence = confidence
        self.thresholds = dict(thresholds) if thresholds is not None else None
        self.models = dict(models or {})
        self.events = events
        self.timeout_s = timeout_s
        self.model = model
        self._clock = clock
        self._client = httpx.Client(
            base_url=self.url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        self._health_lock = threading.Lock()
        self._health_at: float | None = None
        self._health_ok = False

    def close(self) -> None:
        self._client.close()

    def model_for(self, key: str) -> str:
        return self.models.get(key, self.model)

    def threshold_for(self, key: str) -> float | None:
        """The confidence a `key` answer needs; None marks the key unreliable (always unsure)."""
        if self.thresholds is None:
            return self.confidence
        return self.thresholds.get(key)

    def available(self) -> bool:
        """True when the server answers and has every model in use; the result is cached for 60 seconds."""
        with self._health_lock:
            now = self._clock()
            if self._health_at is not None and now - self._health_at < HEALTH_TTL_S:
                return self._health_ok
            self._health_ok = self._probe()
            self._health_at = now
            return self._health_ok

    def _probe(self) -> bool:
        try:
            response = self._client.get("/v1/models")
            response.raise_for_status()
            names = {entry.get("name") for entry in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.info("laya health check failed: %s", exc)
            return False
        return {self.model, *self.models.values()} <= names

    def ask_bool(self, key: str, state: str, question: str) -> Decision:
        """Ask a yes/no question, sent as a two-option choice (see docs/laya.md); the answer is a bool."""
        options = list(questions.BOOL_OPTIONS)
        decision = self._ask(key, state, question, options)
        if decision.source == "laya":
            decision.answer = decision.answer == "yes"
        return decision

    def ask_choice(self, key: str, state: str, question: str, options: list[str]) -> Decision:
        """Ask a pick-one question; the answer is one of `options`."""
        return self._ask(key, state, question, list(options))

    def _ask(self, key: str, state: str, question: str, options: list[str]) -> Decision:
        threshold = self.threshold_for(key)
        if threshold is None:
            decision = FallbackDecider().decide(key, question)
            decision.raw = {**(decision.raw or {}), "unreliable": True}
            self._log_decision(decision, model=None, threshold=None)
            return decision
        model = self.model_for(key)
        limit = MODEL_STATE_CHARS.get(model, MAX_STATE_CHARS)
        text, _ = truncate_state(state, limit)
        try:
            answer, text = self._post_shrinking(key, model, text, question, options)
        except LayaError as exc:
            self._emit("error", source="laya", key=key, message=str(exc))
            decision = FallbackDecider().decide(key, question)
            decision.raw = {**(decision.raw or {}), "error": str(exc)}
            self._log_decision(decision, model=model, threshold=threshold)
            return decision
        cut = len(state) - len(text) if text != state else 0
        if cut > 0:
            logger.info("laya state for '%s' truncated by %d characters", key, cut)
            self._emit("note", source="laya", key=key, message=f"state truncated by {cut} characters to fit Laya's context")
        decision = Decision(
            key=key,
            question=question,
            answer=answer["choice"],
            confidence=float(answer["confidence"]),
            source="laya",
            raw={**answer, "model": model, "threshold": threshold},
        )
        self._log_decision(decision, model=model, threshold=threshold)
        return decision

    def _post_shrinking(
        self, key: str, model: str, text: str, question: str, options: list[str]
    ) -> tuple[dict[str, Any], str]:
        """Post the question; if Laya says the state still does not fit, retry with a shorter one."""
        for attempt in range(TRUNCATION_RETRIES + 1):
            body = {
                "model": model,
                "state": text,
                "questions": {
                    _QUESTION_ID: {
                        "type": "choice",
                        "instructions": question,
                        "criteria": questions.option_descriptions(key, options),
                    }
                },
            }
            try:
                return self._post(body, options), text
            except _StateTooLong as exc:
                if attempt == TRUNCATION_RETRIES:
                    raise LayaError(str(exc)) from exc
                text, _ = truncate_state(text, int(len(text) * RETRY_SHRINK))
        raise LayaError("unreachable")

    def _post(self, body: dict[str, Any], options: list[str]) -> dict[str, Any]:
        try:
            response = self._client.post("/v1/systemone", json=body)
        except httpx.TimeoutException as exc:
            raise LayaError(f"laya request timed out after {self.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise LayaError(f"laya request failed: {exc}") from exc
        if response.status_code == 422 and "STATE_TRUNCATED" in response.text:
            raise _StateTooLong(f"laya returned HTTP 422: {response.text[:300]}")
        if response.status_code != 200:
            raise LayaError(f"laya returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            answer = response.json()["answers"][_QUESTION_ID]
            choice = answer["choice"]
            confidence = answer["confidence"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LayaError(f"laya returned an unexpected body: {response.text[:300]}") from exc
        if choice not in options:
            raise LayaError(f"laya answered '{choice}', which is not one of {options}")
        if not isinstance(confidence, (int, float)):
            raise LayaError(f"laya returned a non-numeric confidence: {confidence!r}")
        return answer

    def _log_decision(self, decision: Decision, model: str | None, threshold: float | None) -> None:
        self._emit(
            "decision",
            key=decision.key,
            question=decision.question,
            answer=decision.answer,
            confidence=decision.confidence,
            source=decision.source,
            threshold=threshold,
            model=model,
        )

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.events.emit(kind, **data)
        except Exception:
            logger.exception("could not write %s event", kind)


def confident(decision: Decision, default_threshold: float) -> bool:
    """Whether `decision` clears its own per-key threshold (recorded by LayaDecider), else `default_threshold`."""
    threshold = (decision.raw or {}).get("threshold") if decision.source == "laya" else None
    return is_confident(decision, default_threshold if threshold is None else threshold)


def can_answer(decider: Any, key: str) -> bool:
    """Whether asking `decider` about `key` is worthwhile: it is available and the key is not marked unreliable.

    Call sites use this in place of `decider.available()` so an unreliable key is skipped instead of always
    triggering its "when unsure" behaviour.
    """
    if not decider.available():
        return False
    threshold_for = getattr(decider, "threshold_for", None)
    return threshold_for is None or threshold_for(key) is not None


def build_decider(config: DeciderConfig, events: EventLog):
    """The decider for `[decider]` config: Laya when backend is "laya", a NullDecider when it is "off".

    Reads the optional `thresholds` and `models` tables when the config object has them.
    """
    if config.backend == "laya":
        return LayaDecider(
            config.url,
            config.confidence,
            events,
            thresholds=getattr(config, "thresholds", None),
            models=getattr(config, "models", None),
        )
    return NullDecider()
