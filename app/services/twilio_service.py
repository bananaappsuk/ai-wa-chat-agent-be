from typing import Optional
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from app.config import settings


def _client() -> Client:
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def to_whatsapp(num: str) -> str:
    n = num.strip()
    if not n.startswith("whatsapp:"):
        if not n.startswith("+"):
            n = "+" + n.lstrip("0")
        n = "whatsapp:" + n
    return n


def from_whatsapp(num: str) -> str:
    return num.replace("whatsapp:", "").strip()


def send_whatsapp(to: str, body: str, media_url: Optional[str] = None) -> dict:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials not configured")
    kwargs = {
        "from_": settings.TWILIO_WHATSAPP_FROM,
        "to": to_whatsapp(to),
        "body": body[:1600],
    }
    if media_url:
        kwargs["media_url"] = [media_url]
    msg = _client().messages.create(**kwargs)
    return {"sid": msg.sid, "status": msg.status}


def validate_signature(url: str, params: dict, signature: str) -> bool:
    if not settings.TWILIO_VALIDATE_SIGNATURE:
        return True
    if not settings.TWILIO_AUTH_TOKEN or not signature:
        return False
    validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)
