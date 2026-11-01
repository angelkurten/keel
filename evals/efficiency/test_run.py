from evals.efficiency import run


def test_eval_anthropic_direct_0_sends_claude_through_the_gateway(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert run.anthropic_key() == "sk-test"
    monkeypatch.setenv("EVAL_ANTHROPIC_DIRECT", "0")
    assert run.anthropic_key() == ""
