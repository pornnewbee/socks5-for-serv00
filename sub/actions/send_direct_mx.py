#!/usr/bin/env python3
"""Send a signed MIME message directly to recipient MX servers over SMTP/25.

Design notes:
- Oracle signs the message (DKIM) and stores the raw MIME.
- The runner downloads the MIME, extracts recipients from To/Cc/Bcc,
  resolves each recipient domain's MX records, and delivers on port 25.
- Bcc headers are stripped before delivery so they are not visible to
  recipients; Bcc addresses are still included in RCPT TO.
- The script is stateless: Oracle owns retries, idempotency, and status.
"""

import argparse
import email.utils
import hashlib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from email.parser import BytesHeaderParser

MAX_BYTES_DEFAULT = 25 * 1024 * 1024
DEFAULT_TIMEOUT = 30
DIG_BIN_DEFAULT = "dig"

# Bcc header (possibly with folded continuation lines)
_BCC_HEADER_RE = re.compile(rb"^bcc:", re.IGNORECASE)
_BCC_CONTINUATION_RE = re.compile(rb"^[ \t]")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def download_mime(url, token, agent_id, path, size_limit):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": agent_id,
            "Accept": "message/rfc822",
            "User-Agent": "oracle-runner-mailer-dmx/1",
        },
    )
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=60) as resp:
        expected = (resp.headers.get("X-Email-SHA256") or "").strip().lower()
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > size_limit:
            raise RuntimeError("email exceeds size limit")
        with open(path, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > size_limit:
                    raise RuntimeError("email exceeds size limit")
                out.write(chunk)
                h.update(chunk)
    digest = h.hexdigest()
    if expected and digest != expected:
        raise RuntimeError("email sha256 mismatch")
    return total, digest


def read_meta(mime_bytes):
    head = mime_bytes[:65536]
    if b"dkim-signature:" not in head.lower():
        raise RuntimeError("missing DKIM-Signature header")
    msg = BytesHeaderParser().parsebytes(head)
    return msg.get("Message-ID")


def parse_recipients(mime_bytes):
    """Extract deduplicated To/Cc/Bcc addresses from the MIME headers."""
    msg = BytesHeaderParser().parsebytes(mime_bytes)
    raw = []
    for header in ("To", "Cc", "Bcc"):
        for value in msg.get_all(header) or []:
            raw.append(value)
    seen = set()
    addrs = []
    for name, addr in email.utils.getaddresses(raw):
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        addrs.append(addr)
    return addrs


def strip_bcc_headers(mime_bytes):
    """Remove Bcc headers and their folded continuation lines.

    DKIM signatures are normally computed without Bcc, so removing the
    header does not invalidate the signature.
    """
    lines = mime_bytes.split(b"\r\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _BCC_HEADER_RE.match(line):
            i += 1
            while i < len(lines) and _BCC_CONTINUATION_RE.match(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    return b"\r\n".join(out)


def envelope_from_default(mime_bytes, override):
    if override:
        return override
    msg = BytesHeaderParser().parsebytes(mime_bytes[:65536])
    rp = msg.get("Return-Path") or msg.get("Resent-From")
    if rp:
        addr = email.utils.getaddresses((rp,))[0][1]
        if addr:
            return addr
    frm = msg.get("From")
    if frm:
        addr = email.utils.getaddresses((frm,))[0][1]
        if addr:
            return addr
    return ""


def domain_of(addr):
    if not addr or "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1].lower()


def dig_mx(domain, dig_bin, timeout):
    """Return (mx_list, error). mx_list = [(preference, host), ...] sorted."""
    cmd = [dig_bin, "+short", "MX", domain]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return [], f"dig binary not found: {dig_bin}"
    except subprocess.TimeoutExpired:
        return [], f"dig MX lookup timed out for {domain}"
    if proc.returncode != 0:
        return [], f"dig MX rc={proc.returncode} for {domain}: {proc.stderr.strip()}"

    mxs = []
    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pref = int(parts[0])
        except ValueError:
            pref = 0
        host = parts[1].rstrip(".")
        if host:
            mxs.append((pref, host))

    if mxs:
        mxs.sort(key=lambda x: (x[0], x[1]))
        return mxs, None

    # RFC 5321: if no MX record, use A record of the domain itself.
    for rtype in ("A", "AAAA"):
        cmd = [dig_bin, "+short", rtype, domain]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
        if proc.returncode != 0:
            continue
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            mxs.append((0, line))
        if mxs:
            mxs.sort(key=lambda x: (x[0], x[1]))
            return mxs, None

    return [], f"no MX or A/AAAA record for {domain}"


def send_to_mx(mx_host, addresses, envelope_from, mime_bytes, args):
    """Send to all addresses in one domain via a single MX host.

    Returns {address: result_dict}. Raises on connection/MAIL FROM errors.
    """
    if args.tls_verify:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl._create_unverified_context()

    client = smtplib.SMTP(
        mx_host, args.smtp_port, timeout=args.timeout, local_hostname=args.helo
    )
    try:
        client.ehlo()
        has_starttls = client.has_extn("starttls")
        if args.tls in ("auto", "required") and has_starttls:
            client.starttls(context=ctx)
            client.ehlo()
        elif args.tls == "required" and not has_starttls:
            raise RuntimeError(f"STARTTLS required but not offered by {mx_host}")

        refused = client.sendmail(envelope_from, list(addresses), mime_bytes)
        results = {}
        refused = refused or {}
        for addr in addresses:
            key = addr.lower()
            if key in refused:
                code, message = refused[key]
                results[addr] = {
                    "status": "refused",
                    "code": code,
                    "message": message,
                }
            else:
                results[addr] = {"status": "accepted", "code": 250}
        return results
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-url", help="Oracle server URL (overrides config)")
    parser.add_argument("--email-id", required=True)
    parser.add_argument("--from", dest="envelope_from", default="")
    parser.add_argument("--smtp-port", type=int, default=25)
    parser.add_argument("--tls", choices=("auto", "required", "none"), default="auto")
    parser.add_argument(
        "--tls-verify",
        action="store_true",
        default=False,
        help="verify STARTTLS certificates (default: no, for MX compatibility)",
    )
    parser.add_argument("--helo", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--state-file", default="/opt/oracle-agent/state.json")
    parser.add_argument("--config-file", default="/opt/oracle-agent/config.json")
    parser.add_argument("--dig-bin", default=DIG_BIN_DEFAULT)
    parser.add_argument("--size-limit", type=int, default=MAX_BYTES_DEFAULT)
    args = parser.parse_args()

    state = load_json(args.state_file)
    token = state.get("token")
    agent_id = state.get("agent_id")
    if not token or not agent_id:
        raise RuntimeError("agent state missing token/agent_id")

    oracle_url = args.oracle_url or load_json(args.config_file).get("oracle_url")
    if not oracle_url:
        raise RuntimeError("oracle_url missing")

    fd, path = tempfile.mkstemp(prefix="signed-", suffix=".eml")
    os.close(fd)
    try:
        url = oracle_url.rstrip("/") + f"/api/v1/email/{args.email_id}/mime"
        size, sha = download_mime(url, token, agent_id, path, args.size_limit)
        with open(path, "rb") as f:
            mime = f.read()
        if not mime.endswith(b"\r\n"):
            raise RuntimeError("MIME must end with CRLF")
        if b"\n" in mime.replace(b"\r\n", b""):
            raise RuntimeError("MIME contains bare LF")

        message_id = read_meta(mime)
        envelope_from = envelope_from_default(mime, args.envelope_from)
        if not envelope_from:
            raise RuntimeError("envelope From address missing and not inferable")
        if not args.helo:
            args.helo = domain_of(envelope_from) or envelope_from

        recipients = parse_recipients(mime)
        if not recipients:
            raise RuntimeError("no recipients found in MIME headers")

        delivery_mime = strip_bcc_headers(mime)
        if not delivery_mime.endswith(b"\r\n"):
            delivery_mime += b"\r\n"

        # Group recipients by domain and resolve MX for each domain.
        domain_map = {}
        for addr in recipients:
            dom = domain_of(addr)
            if not dom:
                raise RuntimeError(f"bad recipient address: {addr}")
            domain_map.setdefault(dom, []).append(addr)

        all_results = []
        for dom, addrs in domain_map.items():
            mx_list, mx_err = dig_mx(dom, args.dig_bin, args.timeout)
            if not mx_list:
                for a in addrs:
                    all_results.append(
                        {
                            "address": a,
                            "domain": dom,
                            "status": "failed",
                            "error": mx_err or "no MX",
                        }
                    )
                continue

            # Try MXes in order; stop at the first reachable MX.
            last_err = None
            for pref, mx_host in mx_list:
                try:
                    per_addr = send_to_mx(
                        mx_host, addrs, envelope_from, delivery_mime, args
                    )
                    for a, res in per_addr.items():
                        entry = {
                            "address": a,
                            "domain": dom,
                            "status": res.get("status"),
                            "mx": mx_host,
                            "mx_pref": pref,
                        }
                        if res.get("code") is not None:
                            entry["code"] = res.get("code")
                        if res.get("message"):
                            entry["message"] = res.get("message")
                        all_results.append(entry)
                    break
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    continue
            else:
                for a in addrs:
                    all_results.append(
                        {
                            "address": a,
                            "domain": dom,
                            "status": "failed",
                            "error": last_err or "all MX hosts failed",
                        }
                    )

        ok = sum(1 for r in all_results if r.get("status") == "accepted")
        out = {
            "ok": ok > 0,
            "email_id": args.email_id,
            "message_id": message_id,
            "bytes": size,
            "sha256": sha,
            "envelope_from": envelope_from,
            "helo": args.helo,
            "recipients": all_results,
            "summary": {
                "total": len(all_results),
                "accepted": ok,
                "refused": sum(1 for r in all_results if r.get("status") == "refused"),
                "failed": sum(1 for r in all_results if r.get("status") == "failed"),
            },
        }
        print(json.dumps(out, ensure_ascii=False))
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
