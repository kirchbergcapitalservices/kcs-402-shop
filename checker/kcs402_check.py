#!/usr/bin/env python3
"""kcs-402 Prüfer — läuft auf K2 als Cron, NUR LESEND nach außen, kein Schlüssel.

Ablauf je Lauf:
  1. offene Issues im Shop-Repo lesen (gh api, ausgehend)
  2. Titel muss exakt `paid:0x<64 hex>` sein, sonst ignorieren
  3. Transaktion per JSON-RPC prüfen: Receipt status 1, ein ERC-20-Transfer-Log vom
     USDC-Vertrag an PAY_TO mit Betrag >= Preis, >= MIN_CONF Bestätigungen,
     Hash noch nicht verbraucht (State-Datei)
  4. bei Erfolg: Issue-Autor als Read-only-Collaborator ins Ware-Repo einladen,
     Issue kommentieren + schließen, Hash als verbraucht merken
  5. bei Misserfolg: Issue kommentieren (Grund) + schließen, Hash NICHT merken

Nur Standardbibliothek. Alles Konfigurierbare steht oben; nichts davon ist geheim.
Aufruf: kcs402_check.py [--dry-run] [--network sepolia|mainnet] [--once-issue N]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ORG = "kirchbergcapitalservices"
SHOP_REPO = f"{ORG}/kcs-402-shop"
WARE_REPO = f"{ORG}/kcs-402-ware-test"
PAY_TO = "0xabe4eece3eae3a2fe8c705d80ab3d23491cc2715"  # kirchberg.wallet (KCS)
NETWORKS = {
    "sepolia": {"rpc": "https://sepolia.base.org", "usdc": "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "price_units": 100_000},   # 0.10 USDC
    "mainnet": {"rpc": "https://mainnet.base.org", "usdc": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "price_units": 1_000_000},  # 1.00 USDC
}
MIN_CONF = 5
DAILY_INVITE_CAP = 10
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TITLE_RE = re.compile(r"^paid:(0x[0-9a-f]{64})$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
STATE_DIR = Path(os.environ.get("KCS402_STATE", Path.home() / ".local/state/kcs-402"))


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


def gh(*args, method=None, fields=None):
    cmd = ["gh", "api", *args]
    if method:
        cmd += ["-X", method]
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} rc={out.returncode}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise RuntimeError(f"rpc {method}: {data['error']}")
    return data["result"]


def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / "state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"used_hashes": {}, "invites": []}


def save_state(st):
    (STATE_DIR / "state.json").write_text(json.dumps(st, indent=1))


def invites_today(st):
    day = time.strftime("%Y-%m-%d")
    return sum(1 for i in st["invites"] if i["day"] == day)


def verify_payment(net, txhash):
    """Gibt (ok, grund, betrag_units) zurück. Liest nur."""
    rcpt = rpc(net["rpc"], "eth_getTransactionReceipt", [txhash])
    if rcpt is None:
        return False, "transaction not found (yet)", 0
    if rcpt.get("status") != "0x1":
        return False, "transaction reverted", 0
    latest = int(rpc(net["rpc"], "eth_blockNumber", []), 16)
    conf = latest - int(rcpt["blockNumber"], 16)
    if conf < MIN_CONF:
        return False, f"only {conf} confirmations (need {MIN_CONF})", 0
    pay_to_topic = "0x" + PAY_TO[2:].rjust(64, "0")
    paid = 0
    for lg in rcpt.get("logs", []):
        if lg.get("address", "").lower() != net["usdc"]:
            continue
        topics = lg.get("topics", [])
        if len(topics) == 3 and topics[0] == TRANSFER_TOPIC and topics[2].lower() == pay_to_topic:
            paid += int(lg["data"], 16)
    if paid == 0:
        return False, "no USDC transfer to the shop address in this transaction", 0
    if paid < net["price_units"]:
        return False, f"paid {paid/1e6:.6f} USDC, price is {net['price_units']/1e6:.2f} USDC", paid
    return True, f"paid {paid/1e6:.6f} USDC with {conf} confirmations", paid


def close_issue(number, comment, dry):
    if dry:
        log(f"  [dry-run] würde Issue #{number} kommentieren: {comment[:80]} … und schließen")
        return
    gh(f"repos/{SHOP_REPO}/issues/{number}/comments", method="POST", fields={"body": comment})
    gh(f"repos/{SHOP_REPO}/issues/{number}", method="PATCH", fields={"state": "closed"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nichts schreiben (kein Invite, kein Kommentar, kein State)")
    ap.add_argument("--network", default=os.environ.get("KCS402_NETWORK", "sepolia"), choices=NETWORKS)
    ap.add_argument("--once-issue", type=int, default=None, help="nur dieses Issue prüfen")
    a = ap.parse_args()
    net = NETWORKS[a.network]
    st = load_state()
    issues = gh(f"repos/{SHOP_REPO}/issues?state=open&per_page=50")
    issues = [i for i in issues if "pull_request" not in i]
    log(f"netz={a.network} offene Issues={len(issues)} dry_run={a.dry_run} verbrauchte Hashes={len(st['used_hashes'])}")
    for iss in issues:
        num, title, login = iss["number"], iss["title"].strip(), iss["user"]["login"]
        if a.once_issue and num != a.once_issue:
            continue
        m = TITLE_RE.match(title)
        if not m:
            log(f"#{num} ignoriert (Titel passt nicht): {title[:60]!r}")
            continue
        txhash = m.group(1)
        if not LOGIN_RE.match(login):
            log(f"#{num} ignoriert (Login ungültig)")
            continue
        if txhash in st["used_hashes"]:
            close_issue(num, f"This transaction hash was already redeemed (issue #{st['used_hashes'][txhash]}).", a.dry_run)
            continue
        if invites_today(st) >= DAILY_INVITE_CAP:
            log(f"#{num} vertagt: Tageslimit {DAILY_INVITE_CAP} Einladungen erreicht")
            continue
        try:
            ok, reason, paid = verify_payment(net, txhash)
        except Exception as e:  # RPC-Fehler: nichts schließen, nächster Lauf
            log(f"#{num} RPC-Fehler, später erneut: {e}")
            continue
        if not ok:
            if "not found" in reason or "confirmations" in reason:
                log(f"#{num} wartet: {reason}")
                continue
            log(f"#{num} abgelehnt: {reason}")
            close_issue(num, f"Payment check failed: {reason}. Nothing was granted.", a.dry_run)
            continue
        log(f"#{num} OK für @{login}: {reason}")
        if a.dry_run:
            log(f"  [dry-run] würde @{login} als pull-Collaborator in {WARE_REPO} einladen")
            continue
        gh(f"repos/{WARE_REPO}/collaborators/{login}", method="PUT", fields={"permission": "pull"})
        st["used_hashes"][txhash] = num
        st["invites"].append({"day": time.strftime("%Y-%m-%d"), "login": login, "issue": num, "tx": txhash, "paid_units": paid})
        save_state(st)
        close_issue(num, f"Verified: {reason}. @{login} has been invited to {WARE_REPO} (read-only). Thank you.", False)
    log("fertig")


if __name__ == "__main__":
    sys.exit(main())
