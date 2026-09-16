import pytest

from connbench.provider import ModelConfig, OpenRouterProvider, ProviderError, load_models


def make(responses, sleeps):
    it = iter(responses)

    def transport(payload, headers, timeout):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    return OpenRouterProvider(api_key="sk-or-v1-test", transport=transport, sleep=sleeps.append, max_retries=3)


OK_BODY = {
    "model": "anthropic/claude-opus-5-20260401",
    "choices": [{"message": {"content": '{"guess": ["A","B","C","D"]}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0021, "completion_tokens_details": {"reasoning_tokens": 12}},
}
CFG = ModelConfig(slug="a__b", openrouter_id="a/b", reasoning={"effort": "high"})


def test_success_parses_usage_and_resolved_model():
    p = make([(200, OK_BODY)], [])
    r = p.complete([{"role": "user", "content": "hi"}], CFG)
    assert r.model == "anthropic/claude-opus-5-20260401"
    assert (r.prompt_tokens, r.completion_tokens, r.reasoning_tokens) == (100, 20, 12)
    assert r.cost_usd == 0.0021
    assert r.finish_reason == "stop"


def test_payload_shape():
    p = make([], [])
    body = p.build_payload([{"role": "user", "content": "x"}], CFG)
    assert body["model"] == "a/b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["reasoning"] == {"effort": "high"}
    assert body["usage"] == {"include": True}
    cfg2 = ModelConfig(slug="a", openrouter_id="a", json_schema=False)
    assert "response_format" not in p.build_payload([], cfg2)


def test_retries_on_429_and_5xx_then_succeeds():
    sleeps = []
    p = make([(429, {"error": {"message": "slow down"}}), (503, {}), (200, OK_BODY)], sleeps)
    assert p.complete([], CFG).text.startswith("{")
    assert sleeps == [1.0, 2.0]


def test_transport_exception_is_retried():
    sleeps = []
    p = make([OSError("boom"), (200, OK_BODY)], sleeps)
    assert p.complete([], CFG).model
    assert len(sleeps) == 1


def test_gives_up_after_max_retries():
    p = make([(500, {})] * 4, [])
    with pytest.raises(ProviderError, match="gave up"):
        p.complete([], CFG)


def test_auth_failure_is_not_retried():
    sleeps = []
    p = make([(401, {"error": {"message": "bad key"}})], sleeps)
    with pytest.raises(ProviderError, match="401"):
        p.complete([], CFG)
    assert sleeps == []


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()


def test_load_models_applies_defaults(tmp_path):
    y = tmp_path / "models.yaml"
    y.write_text(
        "defaults: {temperature: 0, max_tokens: 1000, max_cost_usd_per_run: 0.25}\n"
        "models:\n"
        "  - slug: x__y\n    openrouter_id: x/y\n    reasoning: {effort: low}\n"
        "  - slug: p__q\n    openrouter_id: p/q\n    max_tokens: 50\n"
    )
    ms = load_models(y)
    assert ms[0].max_tokens == 1000 and ms[0].reasoning == {"effort": "low"} and ms[0].max_cost_usd_per_run == 0.25
    assert ms[1].max_tokens == 50 and ms[1].reasoning is None


def test_load_models_rejects_duplicate_slugs(tmp_path):
    y = tmp_path / "models.yaml"
    y.write_text("models:\n  - {slug: a, openrouter_id: a}\n  - {slug: a, openrouter_id: b}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_models(y)
