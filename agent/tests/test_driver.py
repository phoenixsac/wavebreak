"""Driver: 429 retry with backoff and the approval loop (no network)."""

from agent import driver


def test_retry_delay_uses_provider_hint_and_caps():
    assert driver.retry_delay("Please retry in 20.8s", 0) == 22.8
    assert driver.retry_delay("no hint", 0) == 15
    assert driver.retry_delay("no hint", 1) == 30
    assert driver.retry_delay("no hint", 9) == 120
    assert driver.retry_delay("retry in 500s", 0) == 120


def test_is_rate_limit():
    assert driver.is_rate_limit("Request failed (429): You exceeded your current quota")
    assert not driver.is_rate_limit("Tool call failed")


def _script(monkeypatch, states):
    sent = []

    def fake_run_turn(base, session_id, items, out=None):
        sent.append(items)
        return states.pop(0)

    monkeypatch.setattr(driver, "run_turn", fake_run_turn)
    return sent


def test_rate_limit_is_retried_with_continue(monkeypatch):
    sleeps = []
    monkeypatch.setattr(driver.time, "sleep", sleeps.append)
    sent = _script(
        monkeypatch,
        [
            {"status": "error", "message": "429 quota. retry in 10s"},
            {"status": "done", "required_actions": []},
        ],
    )
    driver.drive("http://x", "s1", "go")
    assert sleeps == [12.0]
    assert sent[1] == [{"type": "user.message", "content": "continue"}]


def test_rate_limit_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(driver.time, "sleep", lambda s: None)
    sent = _script(monkeypatch, [{"status": "error", "message": "429"}] * (driver.MAX_RETRIES + 1))
    driver.drive("http://x", "s1", "go")
    assert len(sent) == driver.MAX_RETRIES + 1


def test_approval_loop_allows_and_denies(monkeypatch):
    pending = {
        "status": "done",
        "required_actions": [
            {"type": "tool.approval_required", "thread_id": "main", "tool_calls": [{"id": "c1"}]}
        ],
        "_calls": {"c1": {"name": "start_wave", "args": '{"wave":1}'}},
    }
    for auto, want in ((True, "allow"), (False, "deny")):
        sent = _script(monkeypatch, [dict(pending), {"status": "done", "required_actions": []}])
        driver.drive("http://x", "s1", "go", auto=auto)
        assert sent[1] == [
            {
                "type": "user.tool_approval",
                "thread_id": "main",
                "tool_call_id": "c1",
                "approval": {"status": want},
            }
        ]


def test_is_transient():
    assert driver.is_transient("Cannot connect to API: ")
    assert driver.is_transient("Request failed (429): quota")
    assert driver.is_transient("503 Service Unavailable")
    assert not driver.is_transient("Tool call failed: PLAN_NOT_FOUND")


def test_usage_summary_sums_model_messages():
    from agent import usage

    events = [
        {
            "event": {
                "type": "model.message",
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50},
            }
        },
        {"event": {"type": "tool.response"}},
        {"event": {"type": "model.message", "usage": {"input_tokens": 200, "output_tokens": 20}}},
        {"event": {"type": "turn.done", "state": {"output": {"usage": {"input_tokens": 999}}}}},
    ]
    out = usage.summarize(
        events, {"metrics": {"total_cost_in_usd": 0.01, "total_duration_ms": 12000, "total_turns": 2}}
    )
    assert out == {
        "llm_calls": 2,
        "input_tokens": 300,
        "output_tokens": 30,
        "cache_read_tokens": 50,
        "total_tokens": 330,
        "cost_usd": 0.01,
        "duration_s": 12,
        "turns": 2,
    }
