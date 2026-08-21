"""Provider adapters for structured explanation generation."""
from __future__ import annotations

import json
import ipaddress
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from server import config
from server.core.explanation.models import ExplanationRequest, ProviderResponse

_REPO_ROOT = Path(__file__).resolve().parents[3]


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
            # Corporate/system proxy variables must not intercept Ollama/LM Studio on loopback.
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


class ClaudeCLIProvider(ExplanationProvider):
    """Optional provider using the user's existing headless Claude CLI login."""

    def __init__(self, *, model: str = ""):
        self._model = model.strip()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(provider="claude-cli", model=self._model or "claude-cli-default")

    def explain_position(self, request: ExplanationRequest) -> ProviderResponse:
        executable = shutil.which("claude")
        if not executable:
            raise ExplanationProviderError(
                "No explanation model is available. Configure a local/OpenAI-compatible model "
                "or install and sign in to the Claude CLI."
            )
        prompt = f"{request.system_prompt}\n\n{request.user_prompt}"
        command = [executable, "-p", prompt, "--output-format", "json"]
        if self._model:
            command.extend(["--model", self._model])
        env = {**os.environ, "CHESS_WEB_AUTOSTART": "0"}
        env.pop("ANTHROPIC_API_KEY", None)
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=config.EXPLANATION_TIMEOUT,
                cwd=str(_REPO_ROOT),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExplanationProviderError(
                f"Claude CLI timed out after {config.EXPLANATION_TIMEOUT} seconds."
            ) from exc
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "Claude CLI failed.").strip()[:400]
            raise ExplanationProviderError(detail)
        try:
            envelope = json.loads(process.stdout)
            content = envelope.get("result")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise ExplanationProviderError("Claude CLI returned an invalid response envelope.") from exc
        if not isinstance(content, str) or not content.strip() or content.strip() == "/login":
            raise ExplanationProviderError(
                "Claude CLI is not signed in or returned an empty explanation."
            )
        if envelope.get("is_error"):
            raise ExplanationProviderError(content.strip())
        return ProviderResponse(text=content.strip())


def configured_provider() -> ExplanationProvider:
    """Resolve the current provider without leaking its credentials into business data."""
    selected = config.EXPLANATION_PROVIDER.strip().lower().replace("_", "-")
    dedicated_base = config.EXPLANATION_BASE_URL.strip()
    local_base = config.LOCAL_LLM_BASE_URL.strip()
    base_url = dedicated_base or local_base
    model = config.EXPLANATION_MODEL.strip() or config.LOCAL_LLM_MODEL.strip()

    if selected == "auto":
        selected = "openai-compatible" if base_url else "claude-cli"
    if selected in {"openai", "openai-compatible", "local-openai-compatible"}:
        return OpenAICompatibleProvider(
            base_url=base_url,
            model=model,
            api_key=config.EXPLANATION_API_KEY,
            local=(selected == "local-openai-compatible" or not dedicated_base),
        )
    if selected in {"claude", "claude-cli"}:
        return ClaudeCLIProvider(model=config.EXPLANATION_MODEL)
    raise ExplanationProviderError(
        "CHESS_EXPLANATION_PROVIDER must be auto, openai-compatible, or claude-cli."
    )
