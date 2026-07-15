import base64
import hashlib
import hmac
import json

import main
from handlers import label_applied, task_complete, task_create


class Req:
    def __init__(self, *, path="/", method="POST", headers=None, body=b""):
        self.path = path
        self.method = method
        self.headers = headers or {}
        self._body = body

    def get_data(self):
        return self._body


def _cloud_event(payload: dict):
    class CE:
        data = {"message": {"data": base64.b64encode(json.dumps(payload).encode())}}

    return CE()


def test_process_dispatches_email_classified(monkeypatch):
    seen = []
    monkeypatch.setattr(task_create, "handle", lambda e: seen.append(e))
    main.process(_cloud_event({"event": "email_classified", "message_id": "m1"}))
    assert seen[0]["message_id"] == "m1"


def test_process_dispatches_label_applied(monkeypatch):
    seen = []
    monkeypatch.setattr(label_applied, "handle", lambda e: seen.append(e))
    main.process(_cloud_event({"event": "label_applied", "message_id": "m1"}))
    assert len(seen) == 1


def test_process_ignores_unknown_event(monkeypatch):
    main.process(_cloud_event({"event": "mystery"}))  # must not raise


def test_webhook_handshake_echoes_secret():
    body, status, headers = main.webhook(Req(headers={"X-Hook-Secret": "s3cret"}))
    assert status == 200
    assert headers["X-Hook-Secret"] == "s3cret"


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("ASANA_WEBHOOK_SECRET", "key")
    _, status = main.webhook(Req(headers={"X-Hook-Signature": "bogus"}, body=b'{"events": []}'))
    assert status == 401


def test_webhook_dispatches_completion_events(monkeypatch):
    monkeypatch.setenv("ASANA_WEBHOOK_SECRET", "key")
    payload = json.dumps(
        {
            "events": [
                {
                    "action": "changed",
                    "change": {"field": "completed"},
                    "resource": {"gid": "42"},
                },
                {"action": "added", "resource": {"gid": "99"}},
            ]
        }
    ).encode()
    sig = hmac.new(b"key", payload, hashlib.sha256).hexdigest()
    handled = []
    monkeypatch.setattr(task_complete, "handle", lambda gid: handled.append(gid))

    _, status = main.webhook(Req(headers={"X-Hook-Signature": sig}, body=payload))
    assert status == 200
    assert handled == ["42"]


def test_webhook_escalate_route(monkeypatch):
    from services import escalation

    monkeypatch.setattr(escalation, "run", lambda: {"scanned": 0, "escalated": 0})
    result, status = main.webhook(Req(path="/escalate"))
    assert status == 200
    assert result == {"scanned": 0, "escalated": 0}
