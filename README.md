# StoatChat attachment URL PoC

Minimal local PoC for StoatChat attachment URLs behaving as unauthenticated bearer URLs.

## Problem

StoatChat enforces authentication when a file is uploaded and when messages are read through the normal API. The file download endpoint does not enforce the same authentication or channel access checks.

That means an attachment URL is enough to read the file. The request does not need a session token, a logged-in browser, server membership, channel membership, or message history permission.

This is not only a URL-guessing concern. Attachment URLs are returned by normal authenticated API responses, displayed by the web client, copied by users, stored in browser history, logs, proxies, previews, and any client-side compromise path. Once such a URL is captured, the file can be downloaded outside StoatChat's authorization model.

## Impact

If one account with access to a private channel is compromised, an attacker can collect attachment URLs from message history and keep downloading those files even after losing access to the account.

Operational mitigations such as removing a user, rotating a password, deleting a session, or changing channel membership do not revoke already copied attachment URLs. This turns private attachments into durable bearer links.

The PoC also shows a revocation inconsistency: deleting one message revokes its attachment URL, while bulk message deletion leaves the attachment URL anonymously fetchable.

## Run

```powershell
docker compose up -d
python .\scripts\repro.py
```

Tested with `ghcr.io/stoatchat/api:latest` and `ghcr.io/stoatchat/file-server:latest`, both labeled `v0.12.1` / `61fd13629f9fbf750139c8928f83750502428179`.

The script creates a local user, uploads a private text attachment, sends it to a channel, and then downloads the same attachment URL without any session token.

Expected result:

- anonymous attachment download returns `200`
- single message delete revokes the attachment URL and returns `404`
- bulk message delete leaves the attachment URL fetchable anonymously and returns `200`

No external StoatChat instance is contacted. Everything runs on localhost through Docker. The credentials in `Revolt.toml` are throwaway local defaults for this compose stack only.

## GitHub Actions

The workflow in `.github/workflows/repro.yml` runs the same local Docker Compose stack on a GitHub-hosted runner, executes the PoC, prints the result in the job log, and uploads `out/result.json` as an artifact.
