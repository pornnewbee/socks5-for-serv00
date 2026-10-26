#!/usr/bin/env python3
import argparse, hashlib, json, os, smtplib, ssl, sys, tempfile, urllib.request
from email.parser import BytesHeaderParser

MAX_BYTES = 25 * 1024 * 1024

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def download_mime(url, token, agent_id, path):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": agent_id,
        "Accept": "message/rfc822",
        "User-Agent": "oracle-runner-mailer/1",
    })
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=60) as resp:
        expected = (resp.headers.get("X-Email-SHA256") or "").strip().lower()
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_BYTES:
            raise RuntimeError("email exceeds size limit")
        with open(path, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeError("email exceeds size limit")
                out.write(chunk)
                h.update(chunk)
    digest = h.hexdigest()
    if expected and digest != expected:
        raise RuntimeError("email sha256 mismatch")
    return total, digest

def read_meta(path):
    with open(path, "rb") as f:
        head = f.read(65536)
    if b"dkim-signature:" not in head.lower():
        raise RuntimeError("missing DKIM-Signature header")
    msg = BytesHeaderParser().parsebytes(head)
    return msg.get("Message-ID")

def send_smtp(args, mime, password):
    ctx = ssl.create_default_context()
    if args.tls == "ssl":
        client = smtplib.SMTP_SSL(args.smtp_host, args.smtp_port,
                                  timeout=args.timeout, context=ctx)
    else:
        client = smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=args.timeout)
    try:
        client.ehlo()
        if args.tls == "starttls":
            client.starttls(context=ctx)
            client.ehlo()
        if args.smtp_user:
            client.login(args.smtp_user, password)
        refused = client.sendmail(args.envelope_from, args.rcpts, mime)
        if refused:
            raise RuntimeError(f"recipients refused: {list(refused)}")
    finally:
        try:
            client.quit()
        except Exception:
            client.close()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--oracle-url")
    p.add_argument("--email-id", required=True)
    p.add_argument("--from", dest="envelope_from", required=True)
    p.add_argument("--to", dest="rcpts", action="append", required=True)
    p.add_argument("--smtp-host", required=True)
    p.add_argument("--smtp-port", type=int, default=587)
    p.add_argument("--tls", choices=("starttls", "ssl", "none"), default="starttls")
    p.add_argument("--smtp-user")
    p.add_argument("--smtp-pass-env", default="SMTP_PASS")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--state-file", default="/opt/oracle-agent/state.json")
    p.add_argument("--config-file", default="/opt/oracle-agent/config.json")
    args = p.parse_args()

    state = load_json(args.state_file)
    token, agent_id = state.get("token"), state.get("agent_id")
    if not token or not agent_id:
        raise RuntimeError("agent state missing token/agent_id")

    oracle_url = args.oracle_url or load_json(args.config_file).get("oracle_url")
    if not oracle_url:
        raise RuntimeError("oracle_url missing")

    password = os.environ.get(args.smtp_pass_env, "")
    if args.smtp_user and not password:
        raise RuntimeError("SMTP password env missing")

    fd, path = tempfile.mkstemp(prefix="signed-", suffix=".eml")
    os.close(fd)
    try:
        url = oracle_url.rstrip("/") + f"/api/v1/email/{args.email_id}/mime"
        size, sha = download_mime(url, token, agent_id, path)
        message_id = read_meta(path)
        with open(path, "rb") as f:
            mime = f.read()
        if not mime.endswith(b"\r\n"):
            raise RuntimeError("MIME must end with CRLF")
        if b"\n" in mime.replace(b"\r\n", b""):
            raise RuntimeError("MIME contains bare LF")
        send_smtp(args, mime, password)
        print(json.dumps({
            "ok": True, "email_id": args.email_id, "bytes": size,
            "sha256": sha, "message_id": message_id, "provider": "smtp",
        }, ensure_ascii=False))
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
