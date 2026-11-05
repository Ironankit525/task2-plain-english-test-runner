"""The only file that talks to AI providers: Gemini, and any OpenAI-compatible service."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

RESPONSE = TypeVar("RESPONSE", bound=BaseModel)

REQUEST_TIMEOUT_MS = 60_000
# Services can queue requests for minutes. Streaming keeps the connection alive during the wait,
# so one read only has to last between keep-alive messages, and the whole call has its own limit.
OPENAI_READ_TIMEOUT_SECONDS = 90
OPENAI_CALL_LIMIT_SECONDS = 600
ATTEMPTS_PER_MODEL = 3
RETRY_DELAY_SECONDS = 2.0
RATE_LIMIT_DELAY_SECONDS = 25.0
UNAUTHORISED_CODES = {401, 403}
RATE_LIMITED_CODE = 429
SERVER_ERROR_START = 500


class ModelError(Exception):
    """The AI model couldn't give a usable answer, even after retries."""


class Model(Protocol):
    usage: "Usage"

    def generate(self, system: str, prompt: str, schema: type[RESPONSE], image: bytes | None = None) -> RESPONSE:
        ...


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "models": sorted(self.models),
        }


class GeminiModel:
    """Calls Gemini, retrying busy models and moving to the next model when one is unavailable."""

    def __init__(self, models: list[str], client: genai.Client | None = None, sleep=time.sleep):
        self.models = models
        self.usage = Usage()
        self._out_of_quota: set[str] = set()
        self._sleep = sleep
        self._client = client or genai.Client(
            http_options=types.HttpOptions(
                timeout=REQUEST_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            )
        )

    def generate(self, system: str, prompt: str, schema: type[RESPONSE], image: bytes | None = None) -> RESPONSE:
        contents: list = [prompt]
        if image is not None:
            contents.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
        )

        problems = []
        for model in self.models:
            if model in self._out_of_quota:
                continue
            answer = self._try_model(model, contents, config, schema, problems)
            if answer is not None:
                return answer

        raise ModelError("No AI model gave a usable answer. " + " ".join(problems[-3:]))

    def _try_model(self, model, contents, config, schema, problems) -> BaseModel | None:
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            try:
                response = self._client.models.generate_content(model=model, contents=contents, config=config)
                self._record(model, response)
                return self._parse(response, schema)
            except errors.ClientError as problem:
                if problem.code in UNAUTHORISED_CODES:
                    raise ModelError(f"The API key was rejected ({problem.code}).") from problem
                problems.append(f"{model}: {problem.code}.")
                if _is_daily_limit(problem):
                    # Retrying can't help until tomorrow, so don't ask this model again.
                    self._out_of_quota.add(model)
                    return None
                if problem.code != RATE_LIMITED_CODE:
                    return None
                # A per-minute limit only clears with time, so a short retry would just fail again.
                if attempt < ATTEMPTS_PER_MODEL:
                    self._sleep(RATE_LIMIT_DELAY_SECONDS)
                continue
            except errors.ServerError as problem:
                problems.append(f"{model}: {problem.code}.")
            except (httpx.HTTPError, ValidationError, ValueError) as problem:
                problems.append(f"{model}: {type(problem).__name__}.")

            if attempt < ATTEMPTS_PER_MODEL:
                self._sleep(RETRY_DELAY_SECONDS * attempt)
        return None

    def _record(self, model: str, response) -> None:
        self.usage.calls += 1
        self.usage.models.add(model)
        metadata = response.usage_metadata
        if metadata is not None:
            self.usage.input_tokens += metadata.prompt_token_count or 0
            self.usage.output_tokens += (metadata.candidates_token_count or 0) + (metadata.thoughts_token_count or 0)

    @staticmethod
    def _parse(response, schema):
        if isinstance(response.parsed, schema):
            return response.parsed
        if not response.text:
            raise ValueError("The model returned an empty answer.")
        return schema.model_validate_json(response.text)


def _is_daily_limit(problem: errors.ClientError) -> bool:
    return problem.code == RATE_LIMITED_CODE and "per day" in str(problem).lower()


class OpenAICompatibleModel:
    """Calls any service with an OpenAI-style /chat/completions endpoint.

    Providers differ in how they enforce JSON output, so the schema goes in the prompt and
    the answer is validated here, which works the same everywhere.
    """

    def __init__(self, base_url: str, api_key: str, model: str, usage: Usage | None = None,
                 client: httpx.Client | None = None, sleep=time.sleep):
        self.model = model
        self.usage = usage or Usage()
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(OPENAI_READ_TIMEOUT_SECONDS, connect=20),
        )

    def generate(self, system: str, prompt: str, schema: type[RESPONSE], image: bytes | None = None) -> RESPONSE:
        body = {"model": self.model, "stream": True, "messages": self._messages(system, prompt, schema, image)}

        problems = []
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            try:
                with self._client.stream("POST", "chat/completions", json=body) as response:
                    if response.status_code in UNAUTHORISED_CODES:
                        raise ModelError(f"{self.model} refused the request ({response.status_code}).")
                    if response.status_code == RATE_LIMITED_CODE or response.status_code >= SERVER_ERROR_START:
                        problems.append(f"{self.model}: {response.status_code}.")
                    elif response.status_code >= 400:
                        raise ModelError(f"{self.model} rejected the request ({response.status_code}).")
                    else:
                        return self._parse(*self._read(response), schema)
            except (httpx.HTTPError, ValidationError, ValueError, LookupError, TypeError) as problem:
                problems.append(f"{self.model}: {type(problem).__name__}.")

            if attempt < ATTEMPTS_PER_MODEL:
                self._sleep(RETRY_DELAY_SECONDS * attempt)

        raise ModelError("The model gave no usable answer. " + " ".join(problems[-3:]))

    @staticmethod
    def _messages(system: str, prompt: str, schema: type[BaseModel], image: bytes | None) -> list[dict]:
        instructions = (
            f"{system}\n\nReply with one JSON object and nothing else. "
            f"It must match this JSON schema:\n{json.dumps(schema.model_json_schema())}"
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image is not None:
            encoded = base64.b64encode(image).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        return [{"role": "system", "content": instructions}, {"role": "user", "content": content}]

    @staticmethod
    def _read(response: httpx.Response) -> tuple[str, dict]:
        """Returns the answer text and token usage, from a stream or a plain JSON reply."""
        if "text/event-stream" not in response.headers.get("content-type", ""):
            data = json.loads(response.read())
            return data["choices"][0]["message"]["content"], data.get("usage") or {}

        deadline = time.monotonic() + OPENAI_CALL_LIMIT_SECONDS
        parts: list[str] = []
        usage: dict = {}
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise ValueError("The call took too long.")
            # Lines starting with ":" are keep-alive messages, sent while the request is queued.
            if not line.startswith("data:"):
                continue
            payload = line.removeprefix("data:").strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                parts.append((choice.get("delta") or {}).get("content") or "")
        return "".join(parts), usage

    def _parse(self, text: str, usage: dict, schema: type[RESPONSE]) -> RESPONSE:
        self.usage.calls += 1
        self.usage.models.add(self.model)
        self.usage.input_tokens += usage.get("prompt_tokens") or 0
        self.usage.output_tokens += usage.get("completion_tokens") or 0

        # Some models wrap JSON in a code fence or add reasoning before it.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise ValueError("The answer contained no JSON.")
        return schema.model_validate_json(match.group(0))


class ModelChain:
    """Tries each model in turn, so one provider's outage doesn't end the test."""

    def __init__(self, models: list[Model], usage: Usage):
        self.models = models
        self.usage = usage

    def generate(self, system: str, prompt: str, schema: type[RESPONSE], image: bytes | None = None) -> RESPONSE:
        problems = []
        for model in self.models:
            try:
                return model.generate(system, prompt, schema, image)
            except ModelError as problem:
                problems.append(str(problem))
        raise ModelError(" ".join(problems))


def build_model(gemini_models: list[str], provider: str = "auto", chosen_model: str | None = None) -> Model:
    """Builds the models that have keys: Gemini first, then the OpenAI-compatible service."""
    usage = Usage()
    models: list[Model] = []

    if provider in ("auto", "gemini") and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        gemini = GeminiModel(gemini_models)
        gemini.usage = usage
        models.append(gemini)

    key = os.environ.get("NLTEST_OPENAI_API_KEY")
    url = os.environ.get("NLTEST_OPENAI_BASE_URL")
    name = (chosen_model if provider == "openai" else None) or os.environ.get("NLTEST_OPENAI_MODEL")
    if provider in ("auto", "openai") and key and url and name:
        models.append(OpenAICompatibleModel(url, key, name, usage=usage))

    if not models:
        raise ModelError(f"No AI model is set up for provider {provider}. See the README.")
    return ModelChain(models, usage)
