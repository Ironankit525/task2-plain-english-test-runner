"""Tests retries and model fallback without calling any real service."""
import json
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel

from nltest.config import DEFAULT_MODELS, ConfigError, load_credentials, models_from
from nltest.llm import (
    ATTEMPTS_PER_MODEL,
    GeminiModel,
    ModelChain,
    ModelError,
    OpenAICompatibleModel,
    RATE_LIMIT_DELAY_SECONDS,
    Usage,
    build_model,
)


class Answer(BaseModel):
    value: str


def server_error(code):
    return errors.ServerError(code, {"error": {"code": code, "message": "busy", "status": "UNAVAILABLE"}})


def client_error(code):
    return errors.ClientError(code, {"error": {"code": code, "message": "no", "status": "X"}})


def good_response():
    return SimpleNamespace(parsed=Answer(value="ok"), text='{"value":"ok"}', usage_metadata=None)


class FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.tried = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.tried.append(model)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def model_with(results, models=("first", "second")):
    client = FakeClient(results)
    return GeminiModel(list(models), client=client, sleep=lambda _: None), client


def test_a_busy_model_is_retried():
    model, client = model_with([server_error(503), good_response()])
    assert model.generate("s", "p", Answer).value == "ok"
    assert client.tried == ["first", "first"]


def test_it_moves_to_the_backup_model_when_one_stays_busy():
    model, client = model_with([server_error(503)] * ATTEMPTS_PER_MODEL + [good_response()])
    assert model.generate("s", "p", Answer).value == "ok"
    assert client.tried[-1] == "second"


def test_a_retired_model_is_skipped_at_once():
    model, client = model_with([client_error(404), good_response()])
    model.generate("s", "p", Answer)
    assert client.tried == ["first", "second"]


def test_a_rejected_key_stops_immediately():
    model, client = model_with([client_error(403)])
    with pytest.raises(ModelError, match="API key"):
        model.generate("s", "p", Answer)
    assert client.tried == ["first"]


def test_it_gives_up_with_a_clear_error_when_everything_is_down():
    model, _ = model_with([server_error(503)] * (ATTEMPTS_PER_MODEL * 2))
    with pytest.raises(ModelError, match="No AI model"):
        model.generate("s", "p", Answer)


def test_invalid_output_is_retried():
    bad = SimpleNamespace(parsed=None, text="not json", usage_metadata=None)
    model, _ = model_with([bad, good_response()])
    assert model.generate("s", "p", Answer).value == "ok"


def test_credentials_are_flattened_to_placeholder_names(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"customer": {"email": "a@b.c", "password": "x"}}')
    assert load_credentials(str(path)) == {"customer_email": "a@b.c", "customer_password": "x"}


def test_a_broken_credentials_file_is_reported(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{nope")
    with pytest.raises(ConfigError):
        load_credentials(str(path))


def test_a_chosen_model_goes_first_and_defaults_stay_as_backups(monkeypatch):
    monkeypatch.delenv("NLTEST_MODEL", raising=False)
    models = models_from("my-model")
    assert models[0] == "my-model"
    assert models[1:] == DEFAULT_MODELS


def compatible_model(handler):
    client = httpx.Client(base_url="https://provider.test/v1/", transport=httpx.MockTransport(handler))
    return OpenAICompatibleModel("https://provider.test/v1", "key", "qwen", client=client, sleep=lambda _: None)


def chat_reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 5}})


def test_compatible_model_reads_plain_json():
    model = compatible_model(lambda request: chat_reply('{"value": "ok"}'))
    assert model.generate("s", "p", Answer).value == "ok"
    assert model.usage.input_tokens == 5


def test_compatible_model_finds_json_inside_a_code_fence():
    model = compatible_model(lambda request: chat_reply('Sure:\n```json\n{"value": "ok"}\n```'))
    assert model.generate("s", "p", Answer).value == "ok"


def test_compatible_model_sends_the_schema_and_the_screenshot():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return chat_reply('{"value": "ok"}')

    compatible_model(handler).generate("Rules", "p", Answer, image=b"jpeg")
    assert "value" in seen["messages"][0]["content"]
    assert seen["messages"][1]["content"][1]["type"] == "image_url"


def test_compatible_model_retries_when_busy():
    replies = [httpx.Response(503), chat_reply('{"value": "ok"}')]
    model = compatible_model(lambda request: replies.pop(0))
    assert model.generate("s", "p", Answer).value == "ok"


def test_compatible_model_retries_invalid_answers_then_gives_up():
    calls = []

    def handler(request):
        calls.append(1)
        return chat_reply("no json here")

    with pytest.raises(ModelError):
        compatible_model(handler).generate("s", "p", Answer)
    assert len(calls) == ATTEMPTS_PER_MODEL


def test_a_refused_key_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403)

    with pytest.raises(ModelError):
        compatible_model(handler).generate("s", "p", Answer)
    assert len(calls) == 1


def test_the_chain_moves_to_the_next_provider():
    class Down:
        def generate(self, *args):
            raise ModelError("down")

    class Up:
        def generate(self, *args):
            return "answer"

    assert ModelChain([Down(), Up()], Usage()).generate("s", "p", Answer) == "answer"


def test_the_chain_reports_when_every_provider_fails():
    class Down:
        def generate(self, *args):
            raise ModelError("down")

    with pytest.raises(ModelError, match="down"):
        ModelChain([Down(), Down()], Usage()).generate("s", "p", Answer)


def test_no_keys_at_all_is_a_clear_error(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "NLTEST_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ModelError, match="No AI model"):
        build_model(DEFAULT_MODELS)


def daily_limit_error():
    return errors.ClientError(429, {"error": {"code": 429, "message": "Requests per day limit exceeded", "status": "X"}})


def test_a_model_out_of_daily_quota_is_skipped_for_the_rest_of_the_run():
    model, client = model_with([daily_limit_error(), good_response(), good_response()])
    model.generate("s", "p", Answer)
    model.generate("s", "p", Answer)
    assert client.tried == ["first", "second", "second"]


def test_a_short_rate_limit_is_retried():
    model, client = model_with([client_error(429), good_response()])
    model.generate("s", "p", Answer)
    assert client.tried == ["first", "first"]


def test_a_per_minute_limit_waits_long_enough_to_clear():
    waits = []
    client = FakeClient([client_error(429), good_response()])
    model = GeminiModel(["first"], client=client, sleep=waits.append)
    model.generate("s", "p", Answer)
    assert waits == [RATE_LIMIT_DELAY_SECONDS]


def test_compatible_model_waits_through_a_queue_and_joins_a_streamed_answer():
    stream = (
        ": bharatcode queued\n\n"
        ": bharatcode queued\n\n"
        'data: {"choices": [{"delta": {"reasoning_content": "thinking {not this}"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "{\\"value\\": "}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "\\"ok\\"}"}}], "usage": {"prompt_tokens": 9}}\n\n'
        "data: [DONE]\n\n"
    )
    reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)
    model = compatible_model(lambda request: reply)
    assert model.generate("s", "p", Answer).value == "ok"
    assert model.usage.input_tokens == 9
