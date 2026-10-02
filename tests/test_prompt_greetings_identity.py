"""Greeting behaviour + per-agent business identity in the system prompt."""
from app.services.ai_prompt import build_system_prompt


def _agent(**kw):
    return {"name": "Workshop Agent", "kind": "inbound", **kw}


def test_agent_prompt_has_greeting_rules():
    p = build_system_prompt(agent=_agent(), company="NextGen Techs", ai_settings={})
    assert "GREETINGS AND SMALL TALK" in p
    assert "Never answer a greeting with only a generic line" in p
    assert "Do not bring back an older topic" in p


def test_agent_with_own_business_does_not_claim_tenant_company():
    agent = _agent(business_description="IT Talent Hub — AI training and career development")
    p = build_system_prompt(agent=agent, company="NextGen Techs", ai_settings={})
    assert "NextGen Techs" not in p
    assert "IT Talent Hub" in p


def test_agent_without_own_business_uses_tenant_company():
    p = build_system_prompt(agent=_agent(), company="NextGen Techs", ai_settings={})
    assert "agent for NextGen Techs" in p


def test_neutral_prompt_greets_by_first_name_without_business_identity():
    p = build_system_prompt(
        agent=None,
        company="NextGen Techs",
        ai_settings={"ai_business_description": "Secret Co"},
        lead_profile={"name": "Ravi Kumar"},
        neutral=True,
    )
    assert "GREETINGS AND SMALL TALK" in p
    assert "Customer first name (trusted platform field): Ravi" in p
    assert "Secret Co" not in p and "NextGen Techs" not in p


def test_summary_is_marked_background_only():
    p = build_system_prompt(agent=_agent(), ai_settings={}, conversation_summary="asked about internships")
    assert "background only" in p and "asked about internships" in p
