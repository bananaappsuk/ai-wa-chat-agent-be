"""How retrieved knowledge reaches the system prompt."""
from app.services.ai_prompt import build_system_prompt, format_kb_context
from app.services.kb.retrieve import Hit, KBContext

AGENT = {"name": "Workshop Agent", "kind": "inbound", "knowledge_base": "LEGACY PASTED TEXT: old price £49"}


def _hit(text, title="Workshop page", url="https://ittalenthub.co.uk/workshop", heading="Fees"):
    return Hit(chunk_id="c1", score=0.82, text=text, heading=heading, title=title, url=url, source_id="s", kb_id="k")


def test_hits_are_listed_with_sources_and_grounding_rule():
    ctx = KBContext(kb_ids=["k"], query="fee", searched=True, hits=[_hit("The fee is £99 per participant.")])
    p = build_system_prompt(agent=AGENT, ai_settings={}, kb_context=ctx)
    assert "KNOWLEDGE BASE RESULTS" in p
    assert "[1] Workshop page — Fees" in p and "£99 per participant" in p
    assert "(Source: https://ittalenthub.co.uk/workshop)" in p
    assert "never guess" in p
    assert "LEGACY PASTED TEXT" not in p  # retrieval replaces the pasted knowledge


def test_no_match_tells_model_not_to_guess():
    ctx = KBContext(kb_ids=["k"], query="internships", searched=True, hits=[])
    p = build_system_prompt(agent=AGENT, ai_settings={}, kb_context=ctx)
    assert "nothing in this business's knowledge base matched" in p
    assert "LEGACY PASTED TEXT" not in p


def test_small_talk_adds_nothing():
    ctx = KBContext(kb_ids=["k"], searched=False)
    assert format_kb_context(ctx) == ""
    p = build_system_prompt(agent=AGENT, ai_settings={}, kb_context=ctx)
    assert "KNOWLEDGE BASE RESULTS" not in p and "LEGACY PASTED TEXT" not in p


def test_retrieval_error_falls_back_to_legacy_text():
    ctx = KBContext(kb_ids=["k"], error="Embedding failed (APIConnectionError).")
    p = build_system_prompt(agent=AGENT, ai_settings={}, kb_context=ctx)
    assert "LEGACY PASTED TEXT" in p


def test_agent_without_kbs_keeps_legacy_text():
    p = build_system_prompt(agent=AGENT, ai_settings={}, kb_context=None)
    assert "LEGACY PASTED TEXT" in p
