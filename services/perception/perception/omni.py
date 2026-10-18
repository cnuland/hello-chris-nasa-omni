"""AC-PER-3 omni client over OpenAI-compatible ``POST {base_url}/v1/chat/completions``.

Request: a system message listing the catalog class names and the observation schema; a user message with one
``image_url`` part per frame (``data:image/jpeg;base64,...``) and, when the window has audio, one ``input_audio``
part (``{"data": <b64 wav>, "format": "wav"}``); ``modalities``/``output_modalities`` = ["text"].
Response: the first JSON object of ``choices[0].message.content`` is validated against the schema. On failure, one
retry with a repair instruction; if that fails too -> OmniResult(status="invalid"). Transport/HTTP errors ->
OmniResult(status="error"). Never raises for model/server problems.

AC-PER-24..28 (output budget): the prompt caps objects/descriptions/people/events and asks for compact JSON;
``max_tokens`` (OMNI_MAX_TOKENS) and ``repetition_penalty`` (OMNI_REPETITION_PENALTY, sent only when != 1.0) are in
the request; a cut-off reply is salvaged from its complete prefix (``perception.salvage``) and NOT retried
(``OmniResult.truncated``); at most ``max_objects`` objects are kept (label-diverse, highest confidence first);
every request logs an ``omni usage`` record (tokens, finish_reason, latency).
"""

from __future__ import annotations

import base64
import json
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import PerceptionConfig
from .media import encode_jpeg, resample_pcm16, to_wav_bytes
from .salvage import salvage_observation
from .schema import extract_first_json_object, load_observation_schema, validate_observation
from .types import OmniResult, Window

logger = logging.getLogger(__name__)

OMNI_AUDIO_RATE = 16000
DEFAULT_MAX_TOKENS = 700
DEFAULT_MAX_OBJECTS = 12
DEFAULT_REPETITION_PENALTY = 1.05
MAX_PEOPLE = 8
MAX_EVENTS = 8
MAX_DESCRIPTION_WORDS = 12


class OmniModel(Protocol):
    def analyze(self, window: Window, *, context: str | None = None) -> OmniResult: ...


def build_system_prompt(
    catalog_names: Sequence[str],
    context: str | None = None,
    *,
    max_objects: int = DEFAULT_MAX_OBJECTS,
    asr_transcript: bool = False,
) -> str:
    """System prompt shared by the chat and realtime clients. ``max_objects`` is the object budget (AC-PER-24);
    ``asr_transcript`` True when an ASR endpoint transcribes the audio (then omni's transcript must be "")."""
    names = ", ".join(sorted({str(n) for n in catalog_names if str(n).strip()})) or "(none provided)"
    schema = json.dumps(load_observation_schema(), separators=(",", ":"))
    if asr_transcript:
        transcript_rule = 'transcript must be "" (the speech is transcribed separately); '
    else:
        transcript_rule = (
            'transcript is the intelligible speech heard, verbatim, or "" when nothing intelligible is said (never '
            'a placeholder such as "No speech detected"); '
        )
    prompt = (
        "You observe the inside of the International Space Station through a camera and a microphone. "
        "For the frames (in time order, frame_index starts at 0) and the audio you are given, report what you see "
        "and hear as ONE JSON object and nothing else.\n"
        f"Known catalog classes (use these exact names as object labels when they fit): {names}.\n"
        "Rules: list each distinct physical object ONCE per window, even when it is visible in several frames; "
        "frame_index = the frame where it is clearest, and bbox is [x1,y1,x2,y2] normalised to 0..1 on that frame; "
        "two objects get two entries only if they are two physically different objects. People get refs P1, P2, "
        "... with a non-biometric appearance (clothing, headset), never a name; held_by is the person ref holding "
        "an object; events are only what you actually saw or heard; "
        + transcript_rule
        + "location_hint is the module/zone where the camera is, only if signage or speech says so (not a "
        "destination mentioned in speech), else null.\n"
        f"Budget: report at most {int(max_objects)} objects, the most salient or inventory-relevant first (tools, "
        "equipment, bags, containers, and anything you cannot identify); skip station structure (walls, panels, "
        "racks, handrails, handles, hatches, windows, lights, vents, covers and cables) and anything outside the "
        "station (Earth, spacecraft); people go in people, never in objects; "
        f"each description {MAX_DESCRIPTION_WORDS} words or fewer; attributes only when they add identifying "
        "information (label text, serial number, a colour that tells two items apart), else omit them; "
        f"at most {MAX_PEOPLE} people and at most {MAX_EVENTS} events; scene_summary is one short sentence.\n"
        "Format: reply with compact JSON on a single line (no indentation, no line breaks, no code fences) and no "
        "text before or after it. Write the keys in this order: scene_summary, location_hint, objects, people, "
        "events, transcript, audio_events; always include location_hint (null when unknown).\n"
        f"The JSON object must validate against this JSON schema: {schema}"
    )
    if context:
        prompt += f"\nContext: {context}"
    return prompt


def _confidence(o: Any) -> float:
    value = o.get("confidence") if isinstance(o, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        return 0.0
    return float(value)


def _label_key(o: Any) -> str:
    label = o.get("label") if isinstance(o, dict) else None
    return " ".join(str(label or "").lower().split())


CAP_INSTANCE_IOU = 0.5  # same label + boxes at IoU >= this = one instance (AC-PER-19's default)


def _box(o: dict[str, Any]) -> tuple[float, float, float, float] | None:
    box = o.get("bbox")
    if not isinstance(box, list) or len(box) != 4:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in box):
        return None
    return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))


def _same_instance(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same normalised label and overlapping boxes (IoU >= CAP_INSTANCE_IOU), or a boxless listing of the label."""
    if _label_key(a) != _label_key(b):
        return False
    ba, bb = _box(a), _box(b)
    if ba is None or bb is None:
        return True
    ix = max(0.0, min(ba[2], bb[2]) - max(ba[0], bb[0]))
    iy = max(0.0, min(ba[3], bb[3]) - max(ba[1], bb[1]))
    inter = ix * iy
    union = (ba[2] - ba[0]) * (ba[3] - ba[1]) + (bb[2] - bb[0]) * (bb[3] - bb[1]) - inter
    return union > 0 and inter / union >= CAP_INSTANCE_IOU


def cap_objects(
    objects: list[Any], max_objects: int, *, keep_labels: set[str] | frozenset[str] = frozenset()
) -> tuple[list[Any], int]:
    """AC-PER-27 client-side cap: keep at most ``max_objects`` objects, in reply order. Priority:

    1. the best listing of each distinct instance named by an event (``keep_labels``, normalised) or held by a
       person, so a reported movement / hand-over never loses its object (the reconciler drops events for
       unobserved objects);
    2. the highest-confidence listing of each other distinct instance (same label and overlapping boxes, IoU >= 0.5,
       or a boxless listing of the label = one instance, as AC-PER-19 consolidates them); so per-frame repeats are
       dropped first, and the only listing of anything, e.g. an unknown object for NASA review or a second laptop,
       survives unless the window has more distinct instances than the cap;
    3. the remaining listings (repeats) by confidence.
    Non-dict entries rank last. Returns (kept, number dropped)."""
    if max_objects <= 0 or len(objects) <= max_objects:
        return list(objects), 0
    wanted = {" ".join(str(label).lower().split()) for label in keep_labels}
    order = sorted(range(len(objects)), key=lambda i: (not isinstance(objects[i], dict), -_confidence(objects[i]), i))

    def must_keep(o: dict[str, Any]) -> bool:
        return _label_key(o) in wanted or bool(o.get("held_by"))

    chosen: list[int] = []
    for first_pass in (True, False):  # best listing per instance: event/held ones, then every other one
        for i in order:
            o = objects[i]
            if not isinstance(o, dict) or i in chosen or (first_pass and not must_keep(o)):
                continue
            if not any(_same_instance(o, objects[j]) for j in chosen):
                chosen.append(i)
    taken = set(chosen)
    chosen += [i for i in order if i not in taken]  # then the repeats by confidence
    keep = sorted(chosen[:max_objects])
    return [objects[i] for i in keep], len(objects) - len(keep)


@dataclass
class Reply:
    """Interpretation of one raw model reply (AC-PER-26/27)."""

    observation: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    dropped_objects: int = 0


def _capped(obj: dict[str, Any], max_objects: int) -> tuple[dict[str, Any], int]:
    objects = obj.get("objects")
    if not isinstance(objects, list):
        return obj, 0
    events = obj.get("events") if isinstance(obj.get("events"), list) else []
    labels = {str(e["object_label"]) for e in events if isinstance(e, dict) and e.get("object_label")}
    kept, dropped = cap_objects(objects, max_objects, keep_labels=labels)
    return ({**obj, "objects": kept} if dropped else obj), dropped


#: AC-PER-33: boxes thinner than this (normalised) on either side are dropped as degenerate.
MIN_BOX_SIDE = 0.02


def _sane_box(box: Any) -> Any:
    """Four numbers: swap inverted coordinates, clamp to [0, 1], None when degenerate (AC-PER-33). Anything else is
    returned unchanged so schema validation still rejects it (AC-PER-3: a malformed box makes the reply invalid)."""
    if not (isinstance(box, (list, tuple)) and len(box) == 4) or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in box
    ):
        return box
    x1, y1, x2, y2 = (min(max(float(v), 0.0), 1.0) for v in box)
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 - x1 < MIN_BOX_SIDE or y2 - y1 < MIN_BOX_SIDE:
        return None
    return [x1, y1, x2, y2]


def _sanitize_boxes(obj: dict[str, Any]) -> dict[str, Any]:
    objects = obj.get("objects")
    if isinstance(objects, list):
        for o in objects:
            if isinstance(o, dict) and o.get("bbox") is not None:
                o["bbox"] = _sane_box(o["bbox"])
    return obj


def interpret_reply(
    raw: str, *, finish_reason: str | None = None, max_objects: int = DEFAULT_MAX_OBJECTS
) -> Reply:
    """Parse a raw reply: the first complete JSON object, capped (AC-PER-27) and validated; when that fails and the
    reply's top-level object is cut off (``finish_reason`` "length", or the JSON stops parsing part-way), the
    salvaged prefix (AC-PER-26), capped and validated, with ``truncated`` True."""
    obj = extract_first_json_object(raw or "")
    errors = ["no JSON object found in the reply"]
    if obj is not None:
        capped, dropped = _capped(_sanitize_boxes(obj), max_objects)
        errors = validate_observation(capped)
        if not errors:
            return Reply(capped, [], False, dropped)
    salvaged = salvage_observation(raw or "")
    if salvaged is not None:
        capped, dropped = _capped(_sanitize_boxes(salvaged), max_objects)
        if not validate_observation(capped):
            return Reply(capped, [], True, dropped)
    if finish_reason == "length":
        errors = ["reply cut off at max_tokens (finish_reason length)", *errors]
    return Reply(None, errors)


def frame_data_uri(image: Any) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(encode_jpeg(image)).decode("ascii")


class OmniClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        catalog_names: Sequence[str] = (),
        http_client: httpx.Client | None = None,
        timeout_s: float = 60.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_objects: int = DEFAULT_MAX_OBJECTS,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
        asr_transcript: bool = False,
    ):
        self.base_url = base_url
        self.model = model
        self.catalog_names = list(catalog_names)
        self.http_client = http_client
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.max_objects = max_objects
        self.repetition_penalty = repetition_penalty
        self.asr_transcript = asr_transcript

    def build_messages(self, window: Window, *, context: str | None = None) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{len(window.frames)} frames from {window.t_start.isoformat()} to {window.t_end.isoformat()}"
                    + (" with the audio of that time." if window.audio is not None else " (no audio).")
                ),
            }
        ]
        for frame in window.frames:
            parts.append({"type": "image_url", "image_url": {"url": frame_data_uri(frame.image)}})
        if window.audio is not None and len(window.audio.samples):
            samples = window.audio.samples
            if window.audio.sample_rate != OMNI_AUDIO_RATE:
                samples = resample_pcm16(samples, window.audio.sample_rate, OMNI_AUDIO_RATE)
            wav = base64.b64encode(to_wav_bytes(samples, OMNI_AUDIO_RATE)).decode("ascii")
            parts.append({"type": "input_audio", "input_audio": {"data": wav, "format": "wav"}})
        return [
            {
                "role": "system",
                "content": build_system_prompt(
                    self.catalog_names, context, max_objects=self.max_objects, asr_transcript=self.asr_transcript
                ),
            },
            {"role": "user", "content": parts},
        ]

    def _complete(self, messages: list[dict[str, Any]]) -> tuple[str, str, str | None, dict[str, Any]]:
        """(content, model, finish_reason, usage) of one chat completion."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "modalities": ["text"],
            "output_modalities": ["text"],
        }
        if self.repetition_penalty != 1.0:
            body["repetition_penalty"] = self.repetition_penalty
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        if self.http_client is not None:
            resp = self.http_client.post(url, json=body, timeout=self.timeout_s)
        else:
            with httpx.Client() as client:
                resp = client.post(url, json=body, timeout=self.timeout_s)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {resp.text[:200]}", request=resp.request, response=resp
            )
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        finish = choice.get("finish_reason")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return str(content), str(data.get("model") or self.model), (str(finish) if finish else None), usage

    def analyze(self, window: Window, *, context: str | None = None) -> OmniResult:
        started = time.monotonic()
        messages = self.build_messages(window, context=context)
        raw, model = "", self.model
        errors: list[str] = []
        finish: str | None = None
        tokens: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None}

        def result(status: str, attempt: int, **kw: Any) -> OmniResult:
            return OmniResult(
                status=status,
                raw=raw,
                model=model,
                attempts=attempt,
                latency_ms=int((time.monotonic() - started) * 1000),
                finish_reason=finish,
                **tokens,
                **kw,
            )

        for attempt in (1, 2):
            sent = time.monotonic()
            try:
                raw, model, finish, usage = self._complete(messages)
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                logger.warning(
                    "omni request failed",
                    extra={"window": window.index, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"},
                )
                return result("error", attempt, error=f"{type(exc).__name__}: {exc}")
            for key, total in tokens.items():
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens[key] = (total or 0) + value
            reply = interpret_reply(raw, finish_reason=finish, max_objects=self.max_objects)
            logger.info(
                "omni usage",
                extra={
                    "window": window.index,
                    "attempt": attempt,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "finish_reason": finish,
                    "latency_ms": int((time.monotonic() - sent) * 1000),
                    "truncated": reply.truncated,
                    "dropped_objects": reply.dropped_objects,
                },
            )
            if reply.observation is not None:
                if reply.truncated:
                    logger.warning(
                        "omni reply cut off; salvaged its complete prefix (not retried)",
                        extra={"window": window.index, "attempt": attempt, "finish_reason": finish,
                               "objects": len(reply.observation.get("objects") or [])},
                    )
                return result(
                    "ok",
                    attempt,
                    observation=reply.observation,
                    truncated=reply.truncated,
                    dropped_objects=reply.dropped_objects,
                )
            errors = reply.errors
            logger.info("omni reply invalid", extra={"window": window.index, "attempt": attempt, "errors": errors[:5]})
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply was not a valid JSON object for the observation schema ("
                        + "; ".join(errors[:5])
                        + "). Reply again with ONLY the corrected JSON object, no prose "
                        "and no code fences."
                    ),
                },
            ]
        return result("invalid", 2, error="; ".join(errors[:5]))


def create_omni_client(
    config: PerceptionConfig, *, catalog_names: Sequence[str] = (), http_client: httpx.Client | None = None
) -> OmniModel:
    """OmniClient by default; RealtimeOmniClient when ``config.omni_realtime`` (flag OMNI_REALTIME). The output
    budget comes from the config (AC-PER-25); with an ASR endpoint (``ASR_URL``, the transcript of record) the
    prompt asks omni for an empty transcript."""
    if not config.omni_url:
        raise ValueError("OMNI_URL is not configured")
    asr_transcript = bool(config.asr_url)
    if config.omni_realtime:
        from .realtime import RealtimeOmniClient

        return RealtimeOmniClient(
            config.omni_url,
            model=config.omni_model,
            catalog_names=catalog_names,
            max_objects=config.omni_max_objects,
            asr_transcript=asr_transcript,
        )
    return OmniClient(
        config.omni_url,
        model=config.omni_model,
        catalog_names=catalog_names,
        http_client=http_client,
        max_tokens=config.omni_max_tokens,
        max_objects=config.omni_max_objects,
        repetition_penalty=config.omni_repetition_penalty,
        asr_transcript=asr_transcript,
    )
