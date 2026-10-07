"""LLM config, client and call layer, vendored from an earlier transcription tool.

Changes from the original:
  - env vars renamed TRANSCRIPT2MMAP_* -> YTTOOL_*
  - tenacity retries on transient API errors; auth errors re-raised immediately
  - the Responses->Chat Completions fallback no longer swallows every exception
  - config path search: --config -> $YTTOOL_CONFIG -> ./config.json -> ~/.config/yttool/config.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


@dataclass
class LlmConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None


def log(message: str) -> None:
    print(f"[yttool] {message}", file=sys.stderr, flush=True)


def resolve_config_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    env_patterns = [
        r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$",
        r"^env:([A-Za-z_][A-Za-z0-9_]*)$",
        r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$",
    ]
    for pattern in env_patterns:
        match = re.match(pattern, text)
        if match:
            return os.environ.get(match.group(1))
    return text


def load_config(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse config {path}: {exc}") from exc


def default_config_path() -> Path | None:
    env_path = os.environ.get("YTTOOL_CONFIG")
    if env_path:
        return Path(env_path)
    for candidate in (Path.cwd() / "config.json", Path.home() / ".config" / "yttool" / "config.json"):
        if candidate.exists():
            return candidate
    return None


def provider_options(config: dict[str, Any], provider: str) -> dict[str, Any]:
    provider_configs = config.get("provider")
    if isinstance(provider_configs, dict):
        provider_config = provider_configs.get(provider) or {}
        options = provider_config.get("options") if isinstance(provider_config, dict) else None
        if isinstance(options, dict):
            return options
    return config


def build_llm_config(
    model_arg: str | None = None,
    provider_arg: str | None = None,
    config_path: Path | None = None,
    azure: bool = False,
) -> LlmConfig:
    config = load_config(config_path or default_config_path())
    configured_provider = config.get("provider") if isinstance(config.get("provider"), str) else None
    model_id = model_arg or os.environ.get("YTTOOL_MODEL") or config.get("model")
    if not model_id:
        model_id = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or "openai/gpt-5-mini"

    provider = provider_arg or configured_provider
    model = model_id
    if provider is None and "/" in model_id:
        provider, model = model_id.split("/", 1)
    provider = provider or "custom-openai"

    options = provider_options(config, provider)

    if provider == "azure" or azure:
        api_key = (
            resolve_config_value(options.get("apiKey"))
            or os.environ.get("AZURE_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        endpoint = (
            resolve_config_value(options.get("endpoint"))
            or resolve_config_value(options.get("azureEndpoint"))
            or os.environ.get("AZURE_OPENAI_ENDPOINT")
        )
        resource_name = resolve_config_value(options.get("resourceName"))
        if not endpoint and resource_name:
            endpoint = f"https://{resource_name}.openai.azure.com"
        api_version = (
            resolve_config_value(options.get("apiVersion"))
            or os.environ.get("AZURE_OPENAI_API_VERSION")
            or "2024-10-21"
        )
        if not api_key or not endpoint:
            raise SystemExit(
                "ERROR: Azure OpenAI configuration is missing. Set apiKey and endpoint "
                "in config.json, or set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT."
            )
        return LlmConfig(provider="azure", model=model, api_key=api_key, azure_endpoint=endpoint, azure_api_version=api_version)

    api_key = (
        resolve_config_value(options.get("apiKey"))
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_API_KEY")
    )
    base_url = resolve_config_value(options.get("baseURL")) or os.environ.get("OPENAI_BASE_URL")
    if not api_key:
        raise SystemExit(
            "ERROR: OpenAI-compatible API key is missing. Set apiKey in config.json "
            "or set OPENAI_API_KEY."
        )
    return LlmConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


def create_client(config: LlmConfig):
    try:
        from openai import AzureOpenAI, OpenAI
    except ImportError:
        raise SystemExit("ERROR: openai package not installed. Run: pip install -e .")

    if config.provider == "azure":
        return AzureOpenAI(
            api_key=config.api_key,
            azure_endpoint=config.azure_endpoint,
            api_version=config.azure_api_version,
        )
    kwargs: dict[str, str] = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return OpenAI(**kwargs)


def _is_retryable(exc: BaseException) -> bool:
    import openai

    return isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.InternalServerError,
        ),
    )


def _is_fatal(exc: BaseException) -> bool:
    """Errors where falling back to another API surface cannot help."""
    import openai

    return isinstance(
        exc,
        (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.RateLimitError,
            openai.APIConnectionError,
            openai.APITimeoutError,
        ),
    )


def extract_text_from_response_object(response: Any) -> str:
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(text)
    return "\n".join(parts)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def call_llm(
    client: Any,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
    api_mode: str = "auto",
    max_output_tokens: int = 10000,
) -> str:
    if api_mode in ("auto", "responses"):
        try:
            response = client.responses.create(
                model=config.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=max_output_tokens,
            )
            output_text = getattr(response, "output_text", None)
            if output_text:
                return output_text
            return extract_text_from_response_object(response)
        except Exception as exc:
            if api_mode == "responses" or _is_fatal(exc):
                raise
            log(f"Responses API failed, falling back to Chat Completions: {type(exc).__name__}")

    response = client.chat.completions.create(
        model=config.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_output_tokens,
    )
    return response.choices[0].message.content or ""


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        raise ValueError("LLM response did not contain a JSON object")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                data = json.loads(text[start : index + 1])
                if isinstance(data, dict):
                    return data
                break
    raise ValueError("failed to extract a JSON object from LLM response")


def call_llm_json(
    client: Any,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
    label: str,
    api_mode: str = "auto",
    max_output_tokens: int = 10000,
) -> dict[str, Any]:
    """call_llm + JSON extraction, with one re-ask if the response is not valid JSON."""
    text = call_llm(client, config, system_prompt, user_prompt, api_mode, max_output_tokens)
    try:
        return extract_json_object(text)
    except ValueError:
        log(f"{label}: response was not valid JSON, re-asking once")
        retry_prompt = user_prompt + "\n\nYour previous response was not valid JSON. Return ONLY a valid JSON object, nothing else."
        text = call_llm(client, config, system_prompt, retry_prompt, api_mode, max_output_tokens)
        try:
            return extract_json_object(text)
        except ValueError as exc:
            preview = text[:500].replace("\n", " ")
            raise ValueError(f"invalid JSON from {label}: {exc}. Preview: {preview}") from exc
