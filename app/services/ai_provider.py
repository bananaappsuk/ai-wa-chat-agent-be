"""Central AI provider — timeouts, retries, fallback, error categories."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from app.config import settings
from app.services.ai_context import estimate_tokens
from app.services.ai_quota import check_quota, record_usage_sync

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None


@dataclass
class AIResult:
    text: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    success: bool = False
    error_category: Optional[str] = None
    used_fallback: bool = False
    finish_reason: Optional[str] = None
    raw: Any = field(default=None, repr=False)


def _client_get() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.OPENAI_API_KEY or "missing",
            timeout=float(settings.OPENAI_REQUEST_TIMEOUT_SECONDS or 30),
            max_retries=0,  # we handle retries
        )
    return _client


def classify_provider_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, APITimeoutError) or "timeout" in msg:
        return "timeout"
    if isinstance(exc, RateLimitError) or "rate" in msg and "limit" in msg:
        return "rate_limited"
    if "auth" in msg or "api key" in msg or "401" in msg or "403" in msg:
        return "authentication"
    if "content" in msg and ("filter" in msg or "policy" in msg or "blocked" in msg):
        return "content_blocked"
    if "quota" in msg or "billing" in msg:
        return "quota_exceeded"
    if isinstance(exc, APIConnectionError) or "unavailable" in msg or "503" in msg or "502" in msg:
        return "provider_unavailable"
    if isinstance(exc, APIStatusError):
        code = getattr(exc, "status_code", None) or 0
        if code in (400, 404, 422):
            return "invalid_request"
        if code in (401, 403):
            return "authentication"
        if code == 429:
            return "rate_limited"
        if code >= 500:
            return "provider_unavailable"
    if "invalid" in msg or "bad request" in msg:
        return "invalid_request"
    return "unknown"


def _should_retry(category: str) -> bool:
    return category in ("timeout", "rate_limited", "provider_unavailable")


def _should_fallback(category: str) -> bool:
    return category in ("timeout", "rate_limited", "provider_unavailable", "unknown")


def _truncate_older_context(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """C12: halve older conversation context (keep system prompt(s) + most-recent half)."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) <= 2:
        return messages
    keep = max(2, len(rest) // 2)
    return system_msgs + rest[-keep:]


def chat_completion(
    *,
    messages: list[dict[str, str]],
    model: str,
    temperature: float = 0.5,
    max_tokens: int = 400,
    fallback_model: Optional[str] = None,
    tenant_id: Optional[str] = None,
    operation: str = "reply",
    conversation_id: Optional[str] = None,
    record_usage: bool = True,
    response_format: Optional[dict] = None,
) -> AIResult:
    if not (settings.OPENAI_API_KEY or "").strip():
        return AIResult(success=False, error_category="authentication", model=model)

    if tenant_id:
        ok, reason = check_quota(tenant_id)
        if not ok:
            return AIResult(success=False, error_category=reason or "quota_exceeded", model=model)

    models = [model]
    fb = (fallback_model or "").strip()
    if fb and fb != model:
        models.append(fb)

    last_category = "unknown"
    est_in = sum(estimate_tokens(m.get("content") or "") for m in messages)
    max_retries = max(0, int(settings.OPENAI_MAX_RETRIES))
    base = float(settings.OPENAI_RETRY_BASE_SECONDS or 1)

    def _record_usage(
        *,
        model_used: str,
        ok: bool,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        category: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if not (record_usage and tenant_id):
            return
        try:
            from pymongo import MongoClient

            db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
            record_usage_sync(
                db,
                tenant_id=tenant_id,
                operation=operation,
                model=model_used,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                latency_ms=latency_ms,
                success=ok,
                error_category=category,
                conversation_id=conversation_id,
                metadata=metadata,
            )
        except Exception:
            logger.warning("usage record failed", exc_info=True)

    def _observe(*, ok: bool, tokens: int, latency_ms: int) -> None:
        try:
            from app.observability.metrics import observe_ai_call

            observe_ai_call(operation=operation, ok=ok, tokens=tokens, latency_ms=latency_ms)
        except Exception:
            pass

    def _extract(resp: Any) -> tuple[str, Optional[str], int, int]:
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        text = ((choice.message.content if choice else None) or "").strip()
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "prompt_tokens", None) or 0)
        out_tok = int(getattr(usage, "completion_tokens", None) or 0)
        return text, finish_reason, in_tok, out_tok

    for mi, use_model in enumerate(models):
        for attempt in range(max_retries + 1):
            started = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {
                    "model": use_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format:
                    kwargs["response_format"] = response_format
                resp = _client_get().chat.completions.create(**kwargs)
                latency = int((time.perf_counter() - started) * 1000)
                text, finish_reason, in_tok, out_tok = _extract(resp)
                in_tok = in_tok or est_in
                out_tok = out_tok or estimate_tokens(text)

                # C12: model was cut off mid-response — retry once with more
                # room (higher max_tokens + trimmed older context) before
                # giving up and surfacing a truncated_response failure.
                if finish_reason == "length":
                    _record_usage(
                        model_used=use_model,
                        ok=True,
                        tokens_in=in_tok,
                        tokens_out=out_tok,
                        latency_ms=latency,
                        metadata={"used_fallback": mi > 0, "finish_reason": finish_reason, "truncated_retry": True},
                    )
                    hard_cap = max(1, int(settings.OPENAI_MAX_OUTPUT_TOKENS or max_tokens)) * 2
                    retry_tokens = max(max_tokens, min(max_tokens * 2, hard_cap))
                    retry_messages = _truncate_older_context(messages)
                    try:
                        started2 = time.perf_counter()
                        kwargs2 = dict(kwargs, max_tokens=retry_tokens, messages=retry_messages)
                        resp2 = _client_get().chat.completions.create(**kwargs2)
                        latency2 = int((time.perf_counter() - started2) * 1000)
                        text2, finish_reason2, in_tok2, out_tok2 = _extract(resp2)
                        in_tok2 = in_tok2 or sum(
                            estimate_tokens(m.get("content") or "") for m in retry_messages
                        )
                        out_tok2 = out_tok2 or estimate_tokens(text2)
                    except Exception:
                        logger.warning(
                            "AI truncated-response retry failed op=%s model=%s",
                            operation,
                            use_model,
                            exc_info=True,
                        )
                        _observe(ok=False, tokens=0, latency_ms=latency)
                        return AIResult(
                            success=False,
                            error_category="truncated_response",
                            text=text,
                            model=use_model,
                            finish_reason=finish_reason,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            latency_ms=latency,
                        )

                    if finish_reason2 == "length" or not text2:
                        _record_usage(
                            model_used=use_model,
                            ok=False,
                            tokens_in=in_tok2,
                            tokens_out=out_tok2,
                            latency_ms=latency2,
                            category="truncated_response",
                            metadata={"finish_reason": finish_reason2},
                        )
                        _observe(ok=False, tokens=0, latency_ms=latency2)
                        return AIResult(
                            success=False,
                            error_category="truncated_response",
                            text=text2 or text,
                            model=use_model,
                            finish_reason=finish_reason2,
                            input_tokens=in_tok2,
                            output_tokens=out_tok2,
                            latency_ms=latency2,
                        )

                    _record_usage(
                        model_used=use_model,
                        ok=True,
                        tokens_in=in_tok2,
                        tokens_out=out_tok2,
                        latency_ms=latency2,
                        metadata={"used_fallback": mi > 0, "finish_reason": finish_reason2, "retried_for_length": True},
                    )
                    _observe(ok=True, tokens=in_tok2 + out_tok2, latency_ms=latency2)
                    return AIResult(
                        text=text2,
                        model=use_model,
                        input_tokens=in_tok2,
                        output_tokens=out_tok2,
                        latency_ms=latency2,
                        success=True,
                        used_fallback=mi > 0,
                        finish_reason=finish_reason2,
                        raw=resp2,
                    )

                # C13: provider returned a "successful" response with no text.
                if not text:
                    _record_usage(
                        model_used=use_model,
                        ok=False,
                        tokens_in=in_tok,
                        tokens_out=out_tok,
                        latency_ms=latency,
                        category="empty_response",
                        metadata={"finish_reason": finish_reason},
                    )
                    _observe(ok=False, tokens=0, latency_ms=latency)
                    return AIResult(
                        success=False,
                        error_category="empty_response",
                        model=use_model,
                        finish_reason=finish_reason,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        latency_ms=latency,
                    )

                result = AIResult(
                    text=text,
                    model=use_model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency,
                    success=True,
                    used_fallback=mi > 0,
                    finish_reason=finish_reason,
                    raw=resp,
                )
                _record_usage(
                    model_used=use_model,
                    ok=True,
                    tokens_in=in_tok,
                    tokens_out=out_tok,
                    latency_ms=latency,
                    metadata={"used_fallback": mi > 0, "finish_reason": finish_reason},
                )
                _observe(ok=True, tokens=in_tok + out_tok, latency_ms=latency)
                return result
            except Exception as exc:
                last_category = classify_provider_error(exc)
                latency = int((time.perf_counter() - started) * 1000)
                logger.warning(
                    "AI call failed op=%s model=%s cat=%s attempt=%s",
                    operation,
                    use_model,
                    last_category,
                    attempt,
                )
                if record_usage and tenant_id and attempt == max_retries and mi == len(models) - 1:
                    try:
                        from pymongo import MongoClient

                        db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
                        record_usage_sync(
                            db,
                            tenant_id=tenant_id,
                            operation=operation,
                            model=use_model,
                            input_tokens=est_in,
                            output_tokens=0,
                            latency_ms=latency,
                            success=False,
                            error_category=last_category,
                            conversation_id=conversation_id,
                        )
                    except Exception:
                        pass
                if not _should_retry(last_category) or attempt >= max_retries:
                    break
                time.sleep(base * (2**attempt))
        if not _should_fallback(last_category):
            break

    try:
        from app.observability.metrics import observe_ai_call

        observe_ai_call(operation=operation, ok=False, tokens=0, latency_ms=0)
    except Exception:
        pass
    return AIResult(success=False, error_category=last_category, model=models[-1])
