"""Agent-based campaign context isolation, topics, knowledge, delivery scope."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.models.campaign import CampaignCreate, campaign_ai_defaults
from app.services.ai_campaign import (
    _load_optional_context,
    _validate_variables,
    build_campaign_prompt_sections,
    delivery_scope,
    is_ai_campaign,
    resolve_campaign_knowledge,
    resolve_context_flags,
    resolve_fallback_template,
    select_content_path,
    snapshot_agent,
    validate_campaign_topics,
)


def _open_lead():
    return {
        "phone": "+447700900000",
        "whatsapp_consent_status": "opted_in",
        "blacklisted": False,
        "last_inbound_at": datetime.now(timezone.utc),
        "name": "Priya",
        "company": "Acme",
    }


def _closed_lead():
    return {
        "phone": "+447700900000",
        "whatsapp_consent_status": "opted_in",
        "blacklisted": False,
        "whatsapp_window_expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "name": "Priya",
    }


def test_template_campaign_create_still_works():
    c = CampaignCreate(name="T1", message="Hello", lead_ids=["x"])
    assert c.content_mode == "template"


def test_ai_campaign_requires_subject_and_goal():
    with pytest.raises(ValidationError):
        CampaignCreate(name="A1", content_mode="ai_agent", campaign_goal="Sell course now please")
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="A1",
            content_mode="ai_agent",
            agent_id="6a635d7e9c6d71ce4cb705c0",
        )
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="A1",
            content_mode="ai_agent",
            agent_id="6a635d7e9c6d71ce4cb705c0",
            campaign_goal="Personalised WhatsApp outreach to opted-in leads",
            lead_ids=["x"],
        )
    # Default delivery is all_eligible_recipients — requires an explicit fallback template
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="Summer Camp",
            content_mode="ai_agent",
            agent_id="6a635d7e9c6d71ce4cb705c0",
            campaign_goal="Invite opted-in leads to AI Summer Camp 2026",
            lead_ids=["x"],
        )
    c = CampaignCreate(
        name="Summer Camp",
        content_mode="ai_agent",
        agent_id="6a635d7e9c6d71ce4cb705c0",
        campaign_goal="Invite opted-in leads to AI Summer Camp 2026",
        fallback_template_id="6a635d7e9c6d71ce4cb705c1",
        lead_ids=["x"],
    )
    assert c.content_mode == "ai_agent"
    assert c.campaign_subject == "Summer Camp"
    assert c.ai_context_mode == "campaign_only"
    assert c.delivery_scope == "all_eligible_recipients"
    assert c.on_window_closed_before_send == "use_static_template"

    open_only = CampaignCreate(
        name="Open Only",
        content_mode="ai_agent",
        agent_id="6a635d7e9c6d71ce4cb705c0",
        campaign_goal="Invite opted-in leads to AI Summer Camp 2026",
        delivery_scope="open_window_only",
        lead_ids=["x"],
    )
    assert open_only.delivery_scope == "open_window_only"
    assert open_only.on_window_closed_before_send == "skip"
    assert open_only.fallback_template_id is None


def test_all_eligible_requires_template_on_create():
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="A1",
            content_mode="ai_agent",
            agent_id="6a635d7e9c6d71ce4cb705c0",
            campaign_subject="Azure Data Engineering",
            campaign_goal="Follow up about Azure Data Engineering certification",
            delivery_scope="all_eligible_recipients",
            lead_ids=["x"],
        )


def test_is_ai_campaign_defaults_old_docs():
    assert is_ai_campaign({}) is False
    assert is_ai_campaign({"content_mode": "template"}) is False
    assert is_ai_campaign({"content_mode": "ai_agent"}) is True


def test_campaign_ai_defaults_are_isolated():
    d = campaign_ai_defaults()
    assert d["ai_context_mode"] == "campaign_only"
    assert d["include_conversation_summary"] is False
    assert d["include_recent_messages"] is False
    assert d["knowledge_scope"] == "none"
    assert d["delivery_scope"] == "all_eligible_recipients"
    assert d["on_window_closed_before_send"] == "use_static_template"
    assert d["fallback_template_id"] is None


def test_resolve_context_flags_campaign_only():
    flags = resolve_context_flags({"ai_context_mode": "campaign_only", "include_recent_messages": True})
    assert flags["include_recent_messages"] is False
    assert flags["include_conversation_summary"] is False
    assert flags["include_lead_profile"] is False


def test_open_window_uses_ai_freeform():
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "allow_freeform_inside_window": True,
        "fallback_template_content_sid": "HXabc",
        "personalise_template_variables": True,
    }
    path = select_content_path(campaign=campaign, lead=_open_lead(), phone="+447700900000")
    assert path.path == "ai_freeform"
    assert path.reason_code == "ok"


def test_closed_window_uses_approved_template_fallback():
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "allow_freeform_inside_window": True,
        "fallback_template_id": "t1",
        "fallback_template_content_sid": "HXabc",
        "personalise_template_variables": True,
    }
    path = select_content_path(campaign=campaign, lead=_closed_lead(), phone="+447700900000")
    assert path.path == "ai_template_variables"
    assert path.reason_code == "ok"


def test_select_content_path_open_window_only_no_template():
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "open_window_only",
        "allow_freeform_inside_window": True,
    }
    path = select_content_path(campaign=campaign, lead=_open_lead(), phone="+447700900000")
    assert path.path == "ai_freeform"

    closed = select_content_path(campaign=campaign, lead=_closed_lead(), phone="+447700900000")
    assert closed.path == "ineligible"
    assert closed.reason_code == "skipped_closed_window"
    assert closed.reason_code != "closed_window_missing_template"


def test_open_window_only_skips_closed_recipients():
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "open_window_only",
        "allow_freeform_inside_window": True,
        # Even if a template exists, open_window_only must skip — never silent-fallback
        "fallback_template_content_sid": "HXshould_not_use",
    }
    closed = select_content_path(campaign=campaign, lead=_closed_lead(), phone="+447700900000")
    assert closed.path == "ineligible"
    assert closed.reason_code == "skipped_closed_window"


def test_open_window_skip_not_retryable_and_same_reason_code():
    from app.services.twilio_errors import classify_send_error, is_retryable_category

    cat = classify_send_error("skipped_closed_window")
    assert cat == "window_closed"
    assert is_retryable_category(cat) is False
    cat2 = classify_send_error("Campaign limited to open WhatsApp windows — recipient skipped")
    assert cat2 == "window_closed"
    assert is_retryable_category(cat2) is False


def test_skipped_closed_window_not_counted_as_failed():
    from app.services.campaign_service import (
        compute_rates,
        finalize_status_from_counts,
        recount_ai_generation_fields,
        recount_campaign_fields,
    )

    fields = recount_campaign_fields({"read": 1, "skipped": 2, "failed": 0}, 3)
    assert fields["skipped_count"] == 2
    assert fields["failed_count"] == 0
    rates = compute_rates(
        {
            "total_recipients": 3,
            "sent_count": 1,
            "delivered_count": 0,
            "read_count": 1,
            "failed_count": 0,
            "skipped_count": 2,
            "cancelled_count": 0,
            "replied_count": 0,
        }
    )
    assert rates["failure_rate"] == 0.0
    assert finalize_status_from_counts(
        {
            "total_recipients": 3,
            "sent_count": 1,
            "failed_count": 0,
            "skipped_count": 2,
            "cancelled_count": 0,
        }
    ) in ("partially_completed", "completed")
    ai = recount_ai_generation_fields({"ready": 1, "skipped": 2, "failed": 0})
    assert ai["ai_failed_count"] == 0


def test_no_automatic_template_selection():
    """Fallback comes only from explicit campaign fields — never invent a template."""
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "allow_freeform_inside_window": True,
    }
    tid, sid = resolve_fallback_template(campaign)
    assert tid is None
    assert sid is None
    path = select_content_path(campaign=campaign, lead=_closed_lead(), phone="+447700900000")
    assert path.path == "ineligible"
    assert path.reason_code == "closed_window_missing_template"

    # Defaults leave fallback empty — UI/API must set it explicitly
    defaults = campaign_ai_defaults()
    assert defaults["fallback_template_id"] is None
    assert defaults["fallback_template_content_sid"] is None


def test_fixed_template_body_cannot_be_rewritten():
    """AI may only fill declared variables; undeclared keys are dropped."""
    ok, out, reason = _validate_variables(
        {
            "1": "Priya",
            "2": "Summer Camp",
            "body": "Completely rewritten message body",
            "message": "Hi there rewrite",
        },
        declared=["1", "2"],
        static_fallback={},
    )
    assert ok
    assert out == {"1": "Priya", "2": "Summer Camp"}
    assert "body" not in out
    assert "message" not in out
    assert reason is None


def test_prepare_template_vars_none_when_template_has_no_placeholders():
    """Padma-style failure: empty {} must not block send when template declares no variables."""
    from app.services.ai_campaign import prepare_template_variables_for_send

    db = MagicMock()
    db.templates.find_one.return_value = {
        "_id": "6a676016189a389992e3c532",
        "user_id": "u1",
        "status": "approved",
        "content_sid": "HXe88d7b0fd3aea2dca5f21d7f87e50a7b",
        "variables": [],
    }
    vars_out = prepare_template_variables_for_send(
        db,
        user_id="u1",
        campaign={
            "fallback_template_id": "6a676016189a389992e3c532",
            "content_variables": {},
        },
        lead={"name": "Padma"},
        template_id="6a676016189a389992e3c532",
        generated={},
    )
    assert vars_out is None


def test_prepare_template_vars_fills_declared_from_crm():
    from app.services.ai_campaign import prepare_template_variables_for_send

    db = MagicMock()
    db.templates.find_one.return_value = {
        "_id": "6a676016189a389992e3c532",
        "user_id": "u1",
        "status": "approved",
        "variables": ["1", "2"],
    }
    vars_out = prepare_template_variables_for_send(
        db,
        user_id="u1",
        campaign={
            "fallback_template_id": "6a676016189a389992e3c532",
            "campaign_subject": "AI Summer Camp Essentials 2026",
            "content_variables": {},
        },
        lead={"name": "Padma Sharma", "company": ""},
        template_id="6a676016189a389992e3c532",
        generated={},
    )
    assert vars_out is not None
    assert vars_out["1"] == "Padma"
    assert "Summer Camp" in vars_out["2"] or vars_out["2"]


def test_select_content_path_all_eligible_requires_template_when_closed():
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "allow_freeform_inside_window": True,
        "personalise_template_variables": True,
    }
    path = select_content_path(campaign=campaign, lead=_closed_lead(), phone="+447700900000")
    assert path.path == "ineligible"
    assert path.reason_code == "closed_window_missing_template"

    campaign["fallback_template_content_sid"] = "HXabc"
    path2 = select_content_path(campaign=campaign, lead=_closed_lead(), phone="+447700900000")
    assert path2.path == "ai_template_variables"


def test_kb_mode_closed_window_uses_fallback_template_not_generative_ai():
    """Knowledge-base selected + closed window → approved template, no generative AI."""
    from app.services.ai_campaign import generate_campaign_content

    db = MagicMock()
    db.users.find_one.return_value = {"_id": "u1"}
    db.templates.find_one.return_value = {
        "_id": "t1",
        "status": "approved",
        "content_sid": "HXabc",
        "variables": [],
    }
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "knowledge_scope": "selected",
        "allow_freeform_inside_window": True,
        "personalise_template_variables": True,
        "fallback_template_id": "6a676016189a389992e3c532",
        "fallback_template_content_sid": "HXabc",
        "agent_id": "6a64d4b671d5ebe46637ed16",
        "agent_snapshot": {"name": "A", "prompt": "x", "tone": "neutral"},
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.ai_campaign.get_campaign_agent",
            lambda *a, **k: {"_id": "a1", "name": "A", "knowledge_base": "Camp facts"},
        )
        mp.setattr(
            "app.services.ai_campaign.resolve_campaign_knowledge",
            lambda *a, **k: ("Camp facts", ["agent_knowledge_base"], {}),
        )
        mp.setattr(
            "app.services.ai_campaign.resolve_ai_settings",
            lambda *a, **k: {"enabled": True, "primary_model": "gpt", "max_output_tokens": 400},
        )
        gen = generate_campaign_content(
            db,
            user_id="6a635d7e9c6d71ce4cb705c0",
            campaign=campaign,
            recipient={"phone": "+447700900000", "lead_id": None},
            lead=_closed_lead(),
            preview=True,
        )
    assert gen.ok
    assert gen.content_source in ("template", "ai_template_variables")
    assert gen.template_content_sid == "HXabc"
    assert gen.model is None
    assert gen.input_tokens == 0
    assert gen.output_tokens == 0
    # Preview placeholder when Twilio template body fetch is unavailable in tests.
    assert gen.message
    assert "[WhatsApp template HXabc]" in gen.message
    assert "kb_mode_closed_window_template_no_generative_ai" in (gen.warnings or [])


def test_select_content_path_legacy_with_fallback_infers_all_eligible():
    campaign = {
        "content_mode": "ai_agent",
        "allow_freeform_inside_window": True,
        "fallback_template_id": "t1",
        "fallback_template_content_sid": "HXxxx",
        "personalise_template_variables": True,
    }
    assert delivery_scope(campaign) == "all_eligible_recipients"
    path = select_content_path(campaign=campaign, lead=_open_lead(), phone="+447700900000")
    assert path.path == "ai_freeform"


def test_select_content_path_blocks_opt_out():
    lead = {
        "phone": "+447700900000",
        "whatsapp_consent_status": "opted_out",
        "blacklisted": False,
    }
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "fallback_template_content_sid": "HXabc",
    }
    path = select_content_path(campaign=campaign, lead=lead, phone=lead["phone"])
    assert path.path == "ineligible"
    assert path.reason_code == "consent_blocked"


def test_select_content_path_blocks_blacklisted():
    lead = {
        "phone": "+447700900000",
        "whatsapp_consent_status": "opted_in",
        "blacklisted": True,
        "last_inbound_at": datetime.now(timezone.utc),
    }
    campaign = {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "fallback_template_content_sid": "HXabc",
        "allow_freeform_inside_window": True,
    }
    path = select_content_path(campaign=campaign, lead=lead, phone=lead["phone"])
    assert path.path == "ineligible"
    assert path.reason_code == "consent_blocked"


def test_template_variables_reject_missing():
    ok, out, reason = _validate_variables(
        {"1": "Priya", "extra": "nope"},
        declared=["1", "2"],
        static_fallback={"2": "AI course"},
    )
    assert ok
    assert out == {"1": "Priya", "2": "AI course"}

    ok2, _, reason2 = _validate_variables({"1": "Priya"}, declared=["1", "2"], static_fallback={})
    assert not ok2
    assert reason2 == "missing_required_variable"


def test_snapshot_agent_excludes_prompt_and_full_kb_payload():
    snap = snapshot_agent(
        {
            "_id": "abc",
            "name": "Sales",
            "kind": "sales",
            "tone": "sales",
            "status": "active",
            "prompt": "Always mention the camp name.",
            "knowledge_base": "Fee £1499 Azure Data Engineering Summer Camp",
            "cta_text": "Book",
            "cta_url": "https://ittalenthub.co.uk/contact",
        }
    )
    assert snap["name"] == "Sales"
    assert "prompt" not in snap
    assert "knowledge_base" not in snap
    assert snap["knowledge_base_available"] is True
    assert "OPENAI" not in str(snap)


def test_campaign_only_prompt_excludes_optional_chat():
    snap = {"name": "Agent", "tone": "friendly", "cta_text": "Register", "cta_url": None, "website_url": None}
    flags = resolve_context_flags({"ai_context_mode": "campaign_only"})
    text = build_campaign_prompt_sections(
        campaign={
            "campaign_subject": "AI Summer Camp Essentials 2026",
            "campaign_goal": "Invite leads to AI Summer Camp Essentials 2026 only",
            "campaign_instructions": "Mention only the Summer Camp.",
            "required_topics": ["AI Summer Camp Essentials 2026"],
            "prohibited_topics": ["Azure Data Engineering", "certification fee"],
        },
        snap=snap,
        knowledge_text="",
        profile={"name": "Priya"},
        summary="Lead asked about Azure fees",
        recent=[{"role": "user", "content": "What is Azure Data Engineering fee?"}],
        flags=flags,
        company="IT Talent Hub",
    )
    assert "OPTIONAL CONVERSATION SUMMARY" not in text
    assert "OPTIONAL RECENT CHAT" not in text
    assert "SAFE LEAD PROFILE" not in text
    assert "SELECTED CAMPAIGN KNOWLEDGE:\n(none)" in text
    assert "AI Summer Camp Essentials 2026" in text
    assert "Agent tone may influence" in text or "SELECTED AGENT STYLE" in text


def test_knowledge_none_sends_no_kb():
    db = MagicMock()
    text, sources, snap = resolve_campaign_knowledge(
        db,
        user_id="u1",
        campaign={"knowledge_scope": "none", "campaign_knowledge_text": "should ignore"},
        agent={"knowledge_base": "Azure fee £1499\n\nSummer Camp brochure"},
    )
    assert text == ""
    assert sources == []
    assert snap["scope"] == "none"


def test_knowledge_selected_uses_campaign_text_only():
    db = MagicMock()
    text, sources, _ = resolve_campaign_knowledge(
        db,
        user_id="u1",
        campaign={
            "knowledge_scope": "selected",
            "campaign_knowledge_text": "Summer Camp runs in July. Registration open.",
        },
        agent={"knowledge_base": "Azure fee £1499"},
    )
    assert "Summer Camp" in text
    assert "£1499" not in text
    assert "campaign_knowledge_text" in sources


KB_TEXT = (
    "--- From file: AI_Exam_Revision_Coach_Pasted_Knowledge.pdf ---\n"
    "AI Exam Revision Coach\n Sample Knowledge Base / Pasted Knowledge\n"
    "Smart Study Academy provides online revision support for Maths, English and Science.\n"
    "A standard online study session lasts 60 minutes and starts from GBP 20.\n"
    "Tutors and the AI revision coach must not assist students with cheating."
)
AI_MESSAGE = (
    "Hi Priya, exam season is coming! Smart Study Academy offers online revision support for "
    "Maths, English and Science, with 60-minute sessions from GBP 20. Reply to plan your revision."
)


def _kb_campaign(scope="selected"):
    return {
        "content_mode": "ai_agent",
        "delivery_scope": "all_eligible_recipients",
        "knowledge_scope": scope,
        "allow_freeform_inside_window": True,
        "personalise_template_variables": True,
        "fallback_template_id": "6a676016189a389992e3c532",
        "fallback_template_content_sid": "HXabc",
        "agent_id": "6a64d4b671d5ebe46637ed16",
        "agent_snapshot": {"name": "AI Exam Revision Coach", "prompt": "x", "tone": "neutral"},
        "campaign_subject": "October Exam Revision Support",
        "campaign_goal": "Invite students to get support with exam revision for Maths, English and Science.",
    }


def _run_generation(campaign, knowledge, lead, reply_text=AI_MESSAGE, business="", company="Smart Study Academy"):
    """generate_campaign_content with the model mocked; returns (result, prompts sent to the model)."""
    import json
    from types import SimpleNamespace

    from app.services.ai_campaign import generate_campaign_content

    prompts = []

    def fake_chat(messages, **kw):
        prompts.append(messages)
        return SimpleNamespace(success=True, text=json.dumps({"message": reply_text, "language": "en",
                               "confidence": 0.9, "warnings": [], "call_to_action": "Reply"}),
                               input_tokens=500, output_tokens=60, model="gpt-4o-mini", finish_reason="stop",
                               error_category=None)

    db = MagicMock()
    db.users.find_one.return_value = {"_id": "u1", "company_name": company}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.ai_campaign.get_campaign_agent",
                   lambda *a, **k: {"_id": "a1", "name": "AI Exam Revision Coach", "knowledge_base": knowledge,
                                    "business_description": business})
        mp.setattr("app.services.ai_campaign.resolve_campaign_knowledge",
                   lambda *a, **k: (knowledge, ["agent_knowledge_base"] if knowledge else [], {}))
        mp.setattr("app.services.ai_campaign.resolve_ai_settings",
                   lambda *a, **k: {"enabled": True, "primary_model": "gpt-4o-mini", "max_output_tokens": 400,
                                    "temperature": 0.5})
        mp.setattr("app.services.ai_campaign.chat_completion", fake_chat)
        gen = generate_campaign_content(db, user_id="6a635d7e9c6d71ce4cb705c0", campaign=campaign,
                                        recipient={"phone": "+447700900000", "lead_id": None}, lead=lead,
                                        preview=True)
    return gen, prompts


def test_kb_mode_open_window_ai_writes_from_the_facts_never_the_raw_text():
    gen, prompts = _run_generation(_kb_campaign(), KB_TEXT, _open_lead())
    assert gen.ok and gen.message == AI_MESSAGE
    assert gen.content_source == "ai_freeform" and gen.input_tokens == 500
    assert "agent_knowledge_base" in (gen.knowledge_sources_used or [])
    system = prompts[0][0]["content"]
    assert "SELECTED CAMPAIGN KNOWLEDGE (facts you may use" in system and "GBP 20" in system
    assert "never copy this text" in system
    for leaked in ("--- From file", "Sample Knowledge Base", "must not assist students with cheating"):
        assert leaked not in gen.message


def test_kb_mode_with_empty_knowledge_stops_for_review_without_calling_the_model():
    gen, prompts = _run_generation(_kb_campaign(), "", _open_lead())
    assert not gen.ok and gen.error_category == "knowledge_base_empty" and gen.needs_manual_review
    assert prompts == []


def test_campaign_without_knowledge_still_gets_no_knowledge_in_the_prompt():
    gen, prompts = _run_generation(_kb_campaign(scope="none"), "", _open_lead())
    assert gen.ok and gen.message == AI_MESSAGE
    assert "SELECTED CAMPAIGN KNOWLEDGE:\n(none)" in prompts[0][0]["content"]


def test_campaign_speaks_for_the_agents_own_business_not_the_tenant_account():
    gen, prompts = _run_generation(_kb_campaign(scope="none"), "", _open_lead(),
                                   business="Smart Study Academy", company="NextGen Techs")
    system = prompts[0][0]["content"]
    assert '"company": "Smart Study Academy"' in system and "NextGen Techs" not in system


def test_campaign_without_agent_business_uses_the_tenant_company():
    gen, prompts = _run_generation(_kb_campaign(scope="none"), "", _open_lead(), business="", company="NextGen Techs")
    assert '"company": "NextGen Techs"' in prompts[0][0]["content"]


@pytest.mark.parametrize("body, variables, lead, expected", [
    ("Hi {{1}}, exam season is approaching!", {"1": ""}, {"name": "Timiya Boniface"}, {"1": "Timiya"}),
    ("Dear {{1}}, thanks", {"1": "  "}, {"name": "Rahul"}, {"1": "Rahul"}),
    ("Hello, {{1}}! Welcome", {"1": ""}, {"name": ""}, {"1": "there"}),
    ("Hi {{1}}", {"1": ""}, {"name": "+447700900123"}, {"1": "there"}),
    ("Hey {{2}}, your order {{1}} shipped", {"1": "A12", "2": ""}, {"name": "Ayisha K"}, {"1": "A12", "2": "Ayisha"}),
    ("Hi {{1}}, exam season", {"1": "Timiya"}, {"name": "Rahul"}, {"1": "Timiya"}),  # typed value kept
])
def test_blank_greeting_variable_gets_the_contacts_first_name(body, variables, lead, expected):
    from app.services.ai_campaign import fill_blank_greeting_variables

    assert fill_blank_greeting_variables(variables, lead=lead, template_body=body) == expected


@pytest.mark.parametrize("body, variables", [
    ("Your appointment is on {{1}}.", {"1": ""}),          # not a greeting → never guess
    ("Hi {{1}}, your code is {{2}}", {"1": "Ann", "2": ""}),
    ("", {"1": ""}),                                       # template body unavailable
])
def test_other_blank_variables_stop_with_a_clear_error(body, variables):
    from app.services.ai_campaign import fill_blank_greeting_variables

    with pytest.raises(ValueError, match=r"Template variable \{\{\d\}\} is empty"):
        fill_blank_greeting_variables(variables, lead={"name": "Ann"}, template_body=body)


def test_no_variables_pass_through_untouched():
    from app.services.ai_campaign import fill_blank_greeting_variables

    assert fill_blank_greeting_variables(None, lead={}, template_body="x") is None
    assert fill_blank_greeting_variables({}, lead={}, template_body="x") == {}


def test_topic_matched_uncertain_uses_no_unrelated_kb():
    db = MagicMock()
    text, sources, snap = resolve_campaign_knowledge(
        db,
        user_id="u1",
        campaign={
            "knowledge_scope": "topic_matched",
            "campaign_subject": "Blockchain Workshop 2099",
            "campaign_goal": "Invite to Blockchain Workshop 2099 only",
        },
        agent={"knowledge_base": "Azure Data Engineering fee £1499\n\nSummer Camp brochure"},
    )
    assert "£1499" not in text
    assert "Summer Camp" not in text
    assert snap.get("matched") is False


def test_validate_prohibited_topics():
    camp = {
        "campaign_subject": "AI Summer Camp Essentials 2026",
        "required_topics": ["Summer Camp"],
        "prohibited_topics": ["Azure Data Engineering", "£1499"],
    }
    bad = validate_campaign_topics(
        "Join our AI Summer Camp Essentials 2026 and also Azure Data Engineering for £1499",
        camp,
    )
    assert bad.passed is False
    assert "Azure Data Engineering" in bad.prohibited_topics_found

    good = validate_campaign_topics(
        "Join AI Summer Camp Essentials 2026 — register today for Summer Camp benefits.",
        camp,
    )
    assert good.passed is True


def test_validate_required_topic_missing():
    camp = {
        "campaign_subject": "AI Summer Camp Essentials 2026",
        "required_topics": ["registration"],
        "prohibited_topics": [],
    }
    result = validate_campaign_topics("Hello about AI Summer Camp Essentials 2026 benefits", camp)
    assert result.passed is False
    assert "registration" in result.required_topics_missing


def test_validate_internal_subject_uses_goal_theme():
    """Subject like Cam6 is an internal label — align to goal wording instead."""
    camp = {
        "campaign_subject": "Cam6",
        "campaign_goal": "AI Summer Camp 2026",
        "required_topics": [],
        "prohibited_topics": [],
    }
    msg = (
        "Hi! We're excited to invite you to the AI Summer Camp 2026! "
        "Whether you're a beginner or looking to enhance your skills, this camp is perfect for you. "
        "Book an appointment now!"
    )
    ok = validate_campaign_topics(msg, camp)
    assert ok.passed is True
    assert ok.topic_alignment_passed is True

    bad = validate_campaign_topics(
        "Hi! Just checking in about your Azure Data Engineering certification fees.",
        camp,
    )
    assert bad.passed is False
    assert bad.reason == "subject_not_represented"


def test_load_optional_context_skipped_for_campaign_only():
    db = MagicMock()
    summary, recent, used = _load_optional_context(
        db,
        user_id="u1",
        lead_id="6a63e388c578b80258ddb7b4",
        flags=resolve_context_flags({"ai_context_mode": "campaign_only"}),
    )
    assert summary is None
    assert recent == []
    assert "conversation_summary" not in used
    assert "recent_messages" not in used
    db.messages.find.assert_not_called()
