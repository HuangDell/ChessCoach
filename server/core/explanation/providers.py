"""Provider adapters for structured explanation generation."""
from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from server import config
from server.core.explanation.models import ExplanationRequest, ProviderResponse

class ExplanationProviderError(RuntimeError):
    """A model transport or availability failure safe to show through the local API."""


@dataclass(frozen=True)
class ProviderInfo:
    provider: str
    model: str


class ExplanationProvider(ABC):
    """Provider-neutral interface consumed by the explanation service."""

    @property
    @abstractmethod
    def info(self) -> ProviderInfo:
        raise NotImplementedError

    @abstractmethod
    def explain_position(self, request: ExplanationRequest) -> ProviderResponse:
        raise NotImplementedError


def _chat_completions_url(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleProvider(ExplanationProvider):
    """Direct backend-only client for local or remote OpenAI-compatible APIs."""

    def __init__(self, *, base_url: str, model: str, api_key: str = "", local: bool = False):
        self._base_url = base_url.strip()
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._local = local

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            provider="local-openai-compatible" if self._local else "openai-compatible",
            model=self._model,
        )

    def explain_position(self, request: ExplanationRequest) -> ProviderResponse:
        if not self._base_url:
            raise ExplanationProviderError("No OpenAI-compatible base URL is configured.")
        if not self._model:
            raise ExplanationProviderError("No explanation model is configured.")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "stream": False,
            "temperature": 0.2,
        }
        try:
            url = _chat_completions_url(self._base_url)
            # Corporate/system proxy variables must not intercept a configured loopback API.
            # Remote compatible APIs keep normal proxy discovery.
            with httpx.Client(trust_env=not _is_loopback_url(url)) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=config.EXPLANATION_TIMEOUT,
                )
        except httpx.TimeoutException as exc:
            raise ExplanationProviderError(
                f"The explanation model timed out after {config.EXPLANATION_TIMEOUT} seconds."
            ) from exc
        except httpx.HTTPError as exc:
            raise ExplanationProviderError(
                f"Could not reach the explanation model at {self._base_url}."
            ) from exc
        if response.status_code != 200:
            detail = (response.text or "").strip().replace("\n", " ")[:240]
            raise ExplanationProviderError(
                f"The explanation model returned HTTP {response.status_code}"
                + (f": {detail}" if detail else ".")
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ExplanationProviderError(
                "The model response was not OpenAI-compatible chat-completions JSON."
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ExplanationProviderError("The explanation model returned an empty response.")
        return ProviderResponse(text=content.strip())


def configured_provider() -> ExplanationProvider:
    """Resolve the current provider without leaking its credentials into business data."""
    selected = config.EXPLANATION_PROVIDER.strip().lower().replace("_", "-")
    base_url = config.EXPLANATION_BASE_URL.strip()
    model = config.EXPLANATION_MODEL.strip()

    if selected == "auto":
        selected = "openai-compatible"
    if selected in {"openai", "openai-compatible"}:
        return OpenAICompatibleProvider(
            base_url=base_url,
            model=model,
            api_key=config.EXPLANATION_API_KEY,
            local=_is_loopback_url(base_url),
        )
    raise ExplanationProviderError(
        "No explanation provider is configured. Set CHESS_EXPLANATION_BASE_URL and "
        "CHESS_EXPLANATION_MODEL for an OpenAI-compatible API."
    )
