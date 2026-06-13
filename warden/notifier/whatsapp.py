"""WhatsApp Cloud API channel (Meta Graph API).

Free-form text messages are delivered when a 24h customer-service window is
open (i.e. the owner has messaged the number recently). Outside the window
Meta requires a pre-approved template; we fall back to the `incident_alert`
utility template with the message as its single body parameter.
"""
from __future__ import annotations

import httpx

from warden.config import Config

GRAPH_URL = "https://graph.facebook.com/v21.0"
TEMPLATE_NAME = "incident_alert"


class WhatsAppChannel:
    def __init__(self, config: Config):
        if not (config.wa_token and config.wa_phone_number_id and config.wa_to):
            raise ValueError("WhatsApp channel requires WA_TOKEN, WA_PHONE_NUMBER_ID, WA_TO")
        self.config = config

    def _post(self, payload: dict) -> httpx.Response:
        return httpx.post(
            f"{GRAPH_URL}/{self.config.wa_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self.config.wa_token}"},
            json={"messaging_product": "whatsapp", "to": self.config.wa_to, **payload},
            timeout=30,
        )

    def send_approval(self, action_id: int, text: str) -> str | None:
        # WhatsApp approvals stay text-based: the owner replies YES/NO <id>.
        self.send(text)
        return None

    def send(self, text: str) -> None:
        resp = self._post({"type": "text", "text": {"body": text[:4000]}})
        if resp.status_code >= 400:
            # outside the 24h window free-form text is rejected -> use template
            template_resp = self._post({
                "type": "template",
                "template": {
                    "name": TEMPLATE_NAME,
                    "language": {"code": "en"},
                    "components": [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": text[:1000]}],
                    }],
                },
            })
            template_resp.raise_for_status()
