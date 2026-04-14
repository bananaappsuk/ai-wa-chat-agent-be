from typing import Optional
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from app.config import settings


_client_instance: Client | None = None


def _client() -> Client:
    global _client_instance
    if _client_instance is None:
        _client_instance = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    return _client_instance


_validator_instance: RequestValidator | None = None


def _validator() -> RequestValidator:
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    return _validator_instance


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
    return _validator().validate(url, params, signature)
