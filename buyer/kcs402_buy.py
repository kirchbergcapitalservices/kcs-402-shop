#!/usr/bin/env python3
"""KCS-402 Käufer. Betrag = Preis + Issue-Nummer, USDC-Basiseinheiten (6 Stellen).

Zustände: angelegt → reserviert → signiert → gesendet → bestätigt | abgebrochen.
Nonce, signierte Roh-Tx und Hash werden vor dem Broadcast gespeichert.
„gesendet“ sendet nie wieder. Dieselbe Issue wird nie zweimal bezahlt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import fcntl
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak  # liegt eth-account bei; nur zur Hashprüfung
from hexbytes import HexBytes  # Abhängigkeit von eth-account (Astra R2 P2-4)

BASE = Path(__file__).resolve().parent
STATE_PATH = BASE / "kcs402-state.json"
LEDGER_PATH = BASE / "ledger.jsonl"
LOCK_PATH = BASE / "kcs402.lock"
SHOP = "kirchbergcapitalservices/kcs-402-shop"
PAYEE = "0xabe4eece3eae3a2fe8c705d80ab3d23491cc2715"
# RPC aus dem 402-JSON wird ignoriert. Nur diese Endpunkte sind erlaubt.
NETS = {
    "base-sepolia": (84532, "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "https://sepolia.base.org"),
    "base": (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "https://mainnet.base.org"),
}
ALIASES = {
    "base-sepolia": "base-sepolia", "basesepolia": "base-sepolia",
    "base": "base", "base-mainnet": "base",
}
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SELECTOR = bytes.fromhex("a9059cbb")  # transfer(address,uint256)
BAL_SEL = bytes.fromhex("70a08231")  # balanceOf(address)
MAX_BAL = 10_000_000  # 10 USDC
MAX_PAY = 2_000_000  # Betrag > 2 USDC ist Abbruch
# K0-Fix 25.10.2026 (Astra P1): Festpreis je Netz ist lokal gepinnt; das 402-JSON darf ihn nur bestätigen.
PRICE_UNITS = {"base-sepolia": 100_000, "base": 1_000_000}
MIN_WEI = 10**14
FEE_CAP = 50 * 10**9
PRIO_CAP = 2 * 10**9
GAS_CAP = 100_000
MAX_NATIVE_COST = 2 * 10**15  # 0,002 ETH L2-Ausführungsbudget je Transaktion (gas × maxFeePerGas), Astra P2-9
# Astra R2 P1-1: Base erhebt zusätzlich L1-Datengebühren, die vorab nicht hart deckelbar sind. Die harte
# Verlustobergrenze ist deshalb der ETH-Bestand des Wegwerf-Wallets selbst: mehr als MAX_NATIVE_BAL darf nie
# darauf liegen (Abbruch vor Signatur und vor Broadcast). Betriebsregel: während eines offenen Auftrags kein ETH nachfüllen.
MAX_NATIVE_BAL = 10**16  # 0,01 ETH
CONF = 5
STATES = {"angelegt", "reserviert", "signiert", "gesendet", "bestätigt", "abgebrochen"}
NEXT = {
    "angelegt": {"reserviert", "abgebrochen"},
    "reserviert": {"signiert", "abgebrochen"},
    "signiert": {"gesendet"},
    "gesendet": {"bestätigt"},
    "bestätigt": set(),
    "abgebrochen": set(),
}

class Fail(Exception):
    pass

class RpcFail(Fail):
    pass

def info(msg: str) -> None:
    print(msg, flush=True)

def hx(b) -> str:
    s = b.lower() if isinstance(b, str) else bytes(b).hex()
    return s if s.startswith("0x") else "0x" + s

def addr(v) -> str:
    h = bytes(v).hex() if isinstance(v, (bytes, bytearray)) else re.sub(r"[^0-9a-f]", "", str(v).lower())
    if len(h) < 40:
        raise Fail("Adresse ungültig")
    return "0x" + h[-40:]

def as_int(v) -> int:
    if isinstance(v, bool):
        raise Fail("Zahl ungültig")
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)):
        return int.from_bytes(bytes(v), "big") if v else 0
    s = str(v).strip()
    return int(s, 16) if s.startswith(("0x", "0X")) else int(s)

def parse_units(v) -> int:
    # Ganzzahl = Basiseinheiten. „0.10“ / „0,10“ = USDC, höchstens 6 Nachkommastellen.
    if isinstance(v, (float, bool)) or v is None:
        raise Fail("Preis fehlt oder ist eine Fließkommazahl")
    if isinstance(v, int):
        return v
    s = str(v).strip().replace(",", ".")
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.fullmatch(r"(\d+)\.(\d{1,6})", s)
    if not m:
        raise Fail("Preisformat ungültig")
    return int(m.group(1)) * 1_000_000 + int(m.group(2).ljust(6, "0"))

def parse_time(s: str) -> int:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise Fail("Issue-Zeit ohne Zeitzone")
    return int(dt.timestamp())

def pad_addr(a: str) -> bytes:
    return bytes.fromhex(addr(a)[2:]).rjust(32, b"\x00")

def encode_transfer(to: str, amount: int) -> bytes:
    if not 0 <= amount < 2**256:
        raise Fail("Betrag ungültig")
    return SELECTOR + pad_addr(to) + amount.to_bytes(32, "big")

def atomic_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise Fail(f"Symlink abgelehnt: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        dfd = os.open(path.parent, os.O_RDONLY)  # Verzeichniseintrag dauerhaft (Astra P2-7)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

def save(order: dict) -> None:
    atomic_write(STATE_PATH, json.dumps(order, indent=2, sort_keys=True) + "\n")

def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    if STATE_PATH.is_symlink():
        raise Fail("Symlink abgelehnt: kcs402-state.json")
    doc = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1 or doc.get("state") not in STATES:
        raise Fail("Zustandsdatei ungültig")
    return doc

def set_state(order: dict, new: str) -> None:
    old = order["state"]
    if new != old and new not in NEXT[old]:
        raise Fail(f"Übergang {old} → {new} verboten")
    order["state"] = new
    save(order)

@contextmanager
def exklusiv():
    BASE.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Fail("Parallelstart blockiert (Sperrdatei)") from None
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

def ledger_hit(issue: int | None = None, tx: str | None = None) -> bool:
    if not LEDGER_PATH.exists():
        return False
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise Fail("Kassenbuch beschädigt — Abbruch, bitte manuell prüfen") from e
        if issue is not None and rec.get("issue") == issue and rec.get("state") == "bestätigt":
            return True
        if tx and str(rec.get("tx", "")).lower() == tx.lower():
            return True
    return False

def append_ledger(order: dict) -> None:
    if ledger_hit(tx=order["txHash"]):
        return
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issue": order["issue"], "tx": order["txHash"], "amountUnits": order["amountUnits"],
        "priceUnits": order["priceUnits"], "network": order["network"],
        "from": order["from"], "state": "bestätigt",
    }
    existed = LEDGER_PATH.exists()
    fd = os.open(LEDGER_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:  # neuer Verzeichniseintrag dauerhaft (Astra P2-7)
        dfd = os.open(LEDGER_PATH.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

def http_json(url: str, payload: dict | None, ok: tuple[int, ...]) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read(65537)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(65537)
    if status not in ok:
        raise (RpcFail if payload is not None else Fail)(f"HTTP {status}")
    if len(raw) > 65536:
        raise Fail("Antwort zu groß")
    doc = json.loads(raw.decode())
    if not isinstance(doc, dict):
        raise Fail("JSON-Objekt erwartet")
    return status, doc

def rpc(order: dict, method: str, params: list):
    _status, doc = http_json(order["rpc"], {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, (200,))
    if doc.get("error"):
        err = doc["error"]
        msg = err.get("message", "Fehler") if isinstance(err, dict) else str(err)
        raise RpcFail(f"{method}: {str(msg)[:180]}")
    if "result" not in doc:
        raise RpcFail(f"{method}: kein Ergebnis")
    return doc["result"]

def gh(args: list[str]):
    proc = subprocess.run(args, capture_output=True, text=True)  # Argumentliste, keine Shell
    if proc.returncode != 0:
        raise Fail(f"gh fehlgeschlagen: {(proc.stderr or '')[:200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise Fail("gh lieferte kein JSON") from e

def keychain_key() -> str:
    # Geheimnis nur auf stdout, nie in argv und nie ins Log.
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", "kirchberg-agent-buyer", "-w"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise Fail("Keychain-Eintrag kirchberg-agent-buyer fehlt")
    key = proc.stdout.strip()
    if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", key):
        raise Fail("Schlüssel hat nicht das erwartete Format")
    return key if key.startswith(("0x", "0X")) else "0x" + key

def wallet_address() -> str:
    key = keychain_key()
    try:
        return Account.from_key(key).address
    finally:
        del key

def balance_of(order: dict, holder: str) -> int:
    data = "0x" + (BAL_SEL + pad_addr(holder)).hex()
    return as_int(rpc(order, "eth_call", [{"to": order["usdc"], "data": data}, "latest"]))

def assert_pins(order: dict) -> None:
    if order.get("network") not in NETS:
        raise Fail("Netz nicht freigegeben")
    cid, usdc, url = NETS[order["network"]]
    if int(order["chainId"]) != cid or order.get("rpc") != url:
        raise Fail("Chain oder RPC weicht von der Festlegung ab")
    if addr(order["usdc"]) != usdc or addr(order["payee"]) != PAYEE:
        raise Fail("USDC oder Empfänger weicht von der Festlegung ab")
    price = int(order["priceUnits"])
    if price != PRICE_UNITS[order["network"]]:
        raise Fail("Preis weicht vom lokal gepinnten Festpreis ab")
    if price <= 0 or price > MAX_PAY:
        raise Fail("Preis außerhalb des Budgets")
    if order.get("issue") is not None:
        n = int(order["issue"])
        if n <= 0 or int(order["amountUnits"]) != price + n or int(order["amountUnits"]) > MAX_PAY:
            raise Fail("Betrag ist nicht exakt Preis plus Issue-Nummer")
    if not re.fullmatch(r"[0-9a-f]{16}", str(order.get("claimToken") or "")):
        raise Fail("Claim-Token ungültig")

def guard_wallet(order: dict) -> None:
    assert_pins(order)
    saldo = balance_of(order, order["from"])
    if saldo > MAX_BAL:
        raise Fail("Wallet-Saldo über 10 USDC — Abbruch")
    if saldo < int(order.get("amountUnits") or order["priceUnits"]):
        raise Fail("USDC-Guthaben reicht nicht")
    wei = as_int(rpc(order, "eth_getBalance", [order["from"], "latest"]))
    if wei < MIN_WEI:
        raise Fail("zu wenig ETH für Gas")
    if wei > MAX_NATIVE_BAL:
        raise Fail("ETH-Bestand des Wegwerf-Wallets über 0,01 ETH — Verlustobergrenze, Abbruch")

def load_terms(url: str) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise Fail("Schaufenster-URL muss https sein")
    status, doc = http_json(url, None, (402,))
    if status != 402:
        raise Fail("Schaufenster antwortet nicht mit 402")
    # K0-Fix 25.10.2026 (Astra P2-2): Schema kcs-402/0.2 strikt, keine Alias-Felder.
    if doc.get("schema") != "kcs-402/0.2":
        raise Fail("Schaufenster-Schema ist nicht kcs-402/0.2")
    net = ALIASES.get(str(doc.get("network") or "").lower())
    if net is None:
        raise Fail("Netz nicht freigegeben")
    cid, usdc, rpc_url = NETS[net]
    if as_int(doc.get("chainId")) != cid:
        raise Fail("Chain-ID passt nicht")
    if addr(doc.get("payTo")) != PAYEE or addr(doc.get("tokenContract")) != usdc:
        raise Fail("Empfänger oder USDC-Vertrag weicht ab")
    if as_int(doc.get("decimals")) != 6:
        raise Fail("Dezimalstellen sind nicht 6")
    base = doc.get("basePrice")
    if not isinstance(base, dict):
        raise Fail("basePrice fehlt")
    units = parse_units(base.get("units"))
    if units != PRICE_UNITS[net]:
        raise Fail("Angebotspreis weicht vom lokal gepinnten Festpreis ab")
    return {
        "version": 1, "state": "angelegt", "network": net, "chainId": cid, "rpc": rpc_url,
        "usdc": usdc, "payee": PAYEE, "priceUnits": units, "claimToken": secrets.token_hex(8),
        "issue": None, "issueCreatedAt": None, "amountUnits": None, "from": None, "nonce": None,
        "rawTx": None, "txHash": None, "gas": None, "maxFeePerGas": None, "maxPriorityFeePerGas": None,
    }

def my_issues() -> list[dict]:
    me = gh(["gh", "api", "user"])
    login = str(me.get("login") if isinstance(me, dict) else "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise Fail("GitHub-Login ungültig")
    pages = gh(["gh", "api", "--paginate", "--slurp", f"/repos/{SHOP}/issues?state=all&creator={login}&per_page=100"])
    if not isinstance(pages, list):
        raise Fail("Issue-Liste ungültig")
    data = []
    for page in pages:  # --slurp: Liste von Seiten, jede Seite eine Liste (Astra P2-10)
        if isinstance(page, dict) and "message" in page:
            raise Fail(f"GitHub: {str(page.get('message'))[:160]}")
        if not isinstance(page, list):
            raise Fail("Issue-Seite ungültig")
        data.extend(page)
    return [i for i in data if isinstance(i, dict) and "pull_request" not in i and "number" in i]

def find_token(token: str) -> dict | None:
    needle = f"kcs-ref:{token}"
    hits = [i for i in my_issues() if needle in str(i.get("body") or "")]
    if len(hits) > 1:
        raise Fail("Claim-Token mehrdeutig — keine Zahlung")
    return hits[0] if hits else None

def ensure_issue(order: dict, relist: bool) -> None:
    assert_pins(order)
    if order.get("issue"):
        return
    hit = find_token(order["claimToken"])
    if hit is None and relist:  # verlorene POST-Antwort nicht durch ein zweites Issue ersetzen
        time.sleep(3)
        hit = find_token(order["claimToken"])
    if hit is None:
        if sum(i.get("state") == "open" for i in my_issues()) >= 3:
            raise Fail("schon 3 offene Issues — kein neues Claim")
        body = f"kcs-ref:{order['claimToken']} netz:{order['network']} preis:{order['priceUnits']}"
        hit = gh(["gh", "api", "--method", "POST", f"/repos/{SHOP}/issues", "-f", "title=claim", "-f", f"body={body}"])
    if not isinstance(hit, dict):
        raise Fail("Issue-Antwort ungültig")
    title = str(hit.get("title") or "")
    if title.startswith("paid:"):
        raise Fail("Issue bereits bezahlt — keine zweite Zahlung")
    if title != "claim" or hit.get("state") != "open":
        raise Fail("Claim-Issue ist nicht offen")
    n = int(hit["number"])
    amount = int(order["priceUnits"]) + n  # exakt N Mikro-USDC obendrauf
    if ledger_hit(issue=n):
        raise Fail("Issue steht schon im Kassenbuch — keine zweite Zahlung")
    order["issue"] = n
    order["issueCreatedAt"] = parse_time(hit["created_at"])
    order["amountUnits"] = amount
    if amount > MAX_PAY:
        gh(["gh", "api", "--method", "PATCH", f"/repos/{SHOP}/issues/{n}", "-f", "state=closed"])
        order["error"] = "Betrag über 2 USDC"
        set_state(order, "abgebrochen")
        raise Fail("Betrag über 2 USDC — nichts gesendet")
    save(order)  # Issue gebunden, Zustand bleibt angelegt, noch keine Signatur

def recheck_issue(order: dict, allow_paid: bool) -> dict:
    """Astra R2 P1-2: Issue erneut lesen. Vor Signatur/erstem Broadcast muss es offen, exakt `claim`,
    vom selben Autor und mit derselben Erstellungszeit sein. Nach dem Broadcast (allow_paid) darf der
    Titel schon unser paid:<hash> tragen; geschlossen = Klärungsfall, nie neue Zahlung."""
    n = int(order["issue"])
    cur = gh(["gh", "api", f"/repos/{SHOP}/issues/{n}"])
    if not isinstance(cur, dict) or int(cur.get("number", -1)) != n:
        raise Fail("Issue nicht lesbar")
    me = gh(["gh", "api", "user"])
    if str((cur.get("user") or {}).get("login")) != str(me.get("login")):
        raise Fail("Issue gehört nicht diesem GitHub-Konto")
    if parse_time(cur["created_at"]) != int(order["issueCreatedAt"]):
        raise Fail("Issue-Erstellungszeit weicht vom Auftrag ab")
    title = str(cur.get("title") or "")
    own_paid = bool(order.get("txHash")) and title == f"paid:{order['txHash']}"
    if not allow_paid:
        # vor Signatur / erstem Broadcast: offen und exakt `claim`, sonst keine Zahlung
        if cur.get("state") != "open":
            raise Fail(f"Issue #{n} ist nicht mehr offen — keine Zahlung")
        if title != "claim":
            raise Fail(f"Issue #{n} trägt unerwarteten Titel {title[:40]!r} — keine Zahlung")
        return cur
    # nach dem Broadcast (Astra R3 P1/P2): geschlossen ist erlaubt — der Prüfer versöhnt kürzlich
    # geschlossene Claims nach; Titel darf `claim` (dann setzen wir ihn) oder unser eigenes paid sein.
    if title == "claim" or own_paid:
        if cur.get("state") != "open":
            info(f"Hinweis: Issue #{n} ist geschlossen — Titel wird dennoch gesetzt, Prüfer versöhnt nach")
        return cur
    raise Fail(f"Issue #{n} trägt fremden Titel {title[:40]!r} — Klärungsfall, keine neue Zahlung")

def gas_plan(order: dict, sender: str, data_hex: str) -> tuple[int, int, int]:
    base = as_int(rpc(order, "eth_getBlockByNumber", ["latest", False])["baseFeePerGas"])
    try:
        prio = as_int(rpc(order, "eth_maxPriorityFeePerGas", []))
    except RpcFail:
        prio = 100_000_000
    prio = min(max(prio, 0), PRIO_CAP)
    if base + prio > FEE_CAP:
        raise Fail("Basisgebühr über dem Deckel")
    est = as_int(rpc(order, "eth_estimateGas", [{
        "from": sender, "to": order["usdc"], "data": data_hex, "value": "0x0",
    }]))
    if not 21_000 <= est <= 80_000:
        raise Fail("Gasschätzung außerhalb des Rahmens")
    gas, max_fee = min(est * 125 // 100 + 2_000, GAS_CAP), min(base * 2 + prio, FEE_CAP)
    if gas * max_fee > MAX_NATIVE_COST:
        raise Fail("Gebührenbudget überschritten (gas × maxFeePerGas)")
    return gas, max_fee, prio

def assert_raw(order: dict) -> None:
    """Roh-Tx gegen Betrag, Empfänger, Nonce und Absender prüfen. Nichts neu bauen."""
    raw = str(order["rawTx"])
    blob = bytes.fromhex(raw.removeprefix("0x"))
    if hx(keccak(blob)) != str(order["txHash"]).lower():
        raise Fail("gespeicherter Hash passt nicht zur Roh-Tx")
    if not blob or blob[0] != 2:
        raise Fail("Transaktionstyp ist nicht EIP-1559")
    fields = TypedTransaction.from_bytes(HexBytes(blob)).as_dict()
    data = fields["data"]
    data = bytes(data) if isinstance(data, (bytes, bytearray)) else bytes.fromhex(str(data).removeprefix("0x"))
    if data != encode_transfer(order["payee"], int(order["amountUnits"])):
        raise Fail("Calldata ist kein transfer an den Empfänger")
    if addr(fields["to"]) != order["usdc"] or as_int(fields["value"]) != 0:
        raise Fail("Ziel oder ETH-Wert weicht ab")
    if as_int(fields["nonce"]) != int(order["nonce"]) or as_int(fields["chainId"]) != int(order["chainId"]):
        raise Fail("Nonce oder Chain-ID weicht ab")
    if Account.recover_transaction(raw).lower() != str(order["from"]).lower():
        raise Fail("Absender der Signatur weicht ab")
    # Gebührenprüfung auch bei Wiederaufnahme (Astra P2-9)
    gas, max_fee, prio = as_int(fields["gas"]), as_int(fields["maxFeePerGas"]), as_int(fields["maxPriorityFeePerGas"])
    if gas > GAS_CAP or max_fee > FEE_CAP or prio > PRIO_CAP or prio > max_fee or gas * max_fee > MAX_NATIVE_COST:
        raise Fail("Gas oder Gebühren der Roh-Tx außerhalb des Budgets")

def sign_order(order: dict) -> None:
    if order.get("rawTx"):
        assert_raw(order)
        if order["state"] != "signiert":
            set_state(order, "signiert")
        return
    key = keychain_key()
    try:
        acct = Account.from_key(key)
        if order.get("from") and acct.address.lower() != str(order["from"]).lower():
            raise Fail("Wallet weicht vom gespeicherten Absender ab")
        order["from"] = acct.address
        guard_wallet(order)
        recheck_issue(order, allow_paid=False)  # Astra R2 P1-2: vor der Signatur
        if order.get("nonce") is None:
            latest = as_int(rpc(order, "eth_getTransactionCount", [acct.address, "latest"]))
            pending = as_int(rpc(order, "eth_getTransactionCount", [acct.address, "pending"]))
            if pending != latest:
                raise Fail("Nonce im Mempool nicht eindeutig — keine Reservierung")
            order["nonce"] = latest
            set_state(order, "reserviert")
        elif as_int(rpc(order, "eth_getTransactionCount", [acct.address, "pending"])) != int(order["nonce"]):
            raise Fail("reservierte Nonce nicht mehr frei — keine neue Signatur")
        data_hex = "0x" + encode_transfer(order["payee"], int(order["amountUnits"])).hex()
        gas, max_fee, prio = gas_plan(order, acct.address, data_hex)
        signed = Account.sign_transaction({
            "type": 2, "chainId": int(order["chainId"]), "nonce": int(order["nonce"]),
            "maxPriorityFeePerGas": prio, "maxFeePerGas": max_fee, "gas": gas,
            "to": bytes.fromhex(order["usdc"][2:]), "value": 0, "data": data_hex,  # 20 Byte, keine Checksum nötig (Astra R2 P2-3)
        }, key)
        raw = getattr(signed, "raw_transaction", None)
        order["rawTx"] = hx(raw if raw is not None else signed.rawTransaction)
        order["txHash"] = hx(signed.hash)
        order["gas"], order["maxFeePerGas"], order["maxPriorityFeePerGas"] = gas, max_fee, prio
        assert_raw(order)
        set_state(order, "signiert")  # persistiert vor jedem Broadcast
    finally:
        del key

def broadcast(order: dict) -> None:
    # Nur die gespeicherte Roh-Tx, und nur solange noch nicht „gesendet“.
    if order["state"] == "gesendet":
        return
    if order["state"] != "signiert":
        raise Fail("Senden nur aus dem Zustand signiert")
    assert_raw(order)
    if rpc(order, "eth_getTransactionByHash", [order["txHash"]]) is not None:
        set_state(order, "gesendet")  # bereits gesendet: kein Guthaben-Check mehr nötig (Astra P2-8)
        return
    guard_wallet(order)
    recheck_issue(order, allow_paid=False)  # Astra R2 P1-2: vor dem ersten Broadcast; geschlossen = kein Senden
    latest = as_int(rpc(order, "eth_getTransactionCount", [order["from"], "latest"]))
    pending = as_int(rpc(order, "eth_getTransactionCount", [order["from"], "pending"]))
    if latest > int(order["nonce"]) or pending != int(order["nonce"]):
        raise Fail("Hash/Nonce stimmen nicht — sende nicht neu")
    try:
        sent = rpc(order, "eth_sendRawTransaction", [order["rawTx"]])
    except RpcFail as e:
        low = str(e).lower()
        seen = rpc(order, "eth_getTransactionByHash", [order["txHash"]]) is not None
        if "already known" in low or (seen and "nonce too low" in low):
            set_state(order, "gesendet")
            return
        if "nonce too low" in low:
            raise Fail("Nonce verbraucht, Hash unbekannt — sende nicht neu") from e
        raise
    if hx(sent) != order["txHash"]:
        raise Fail("Knoten meldet einen anderen Hash")
    set_state(order, "gesendet")

def receipt_ready(order: dict, rcpt: dict) -> bool:
    tx = rpc(order, "eth_getTransactionByHash", [order["txHash"]])
    if not isinstance(tx, dict):
        return False
    inp = str(tx.get("input") or tx.get("data") or "0x")
    expect = encode_transfer(order["payee"], int(order["amountUnits"]))
    if bytes.fromhex(inp.removeprefix("0x")) != expect or addr(tx.get("to")) != order["usdc"]:
        raise Fail("Tx ist kein passender USDC-Transfer")
    blk = rpc(order, "eth_getBlockByNumber", [rcpt["blockNumber"], False])
    if not isinstance(blk, dict) or hx(blk.get("hash")) != hx(rcpt.get("blockHash")):
        return False  # nicht kanonisch, weiter warten
    if as_int(rpc(order, "eth_blockNumber", [])) - as_int(rcpt["blockNumber"]) < CONF:
        return False
    if as_int(rcpt.get("status")) != 1:
        raise Fail("Transaktion fehlgeschlagen — keine neue Zahlung")
    if as_int(blk["timestamp"]) <= int(order["issueCreatedAt"]):
        raise Fail("Blockzeit liegt nicht nach dem Issue — keine neue Zahlung")
    want_from, want_to = "0x" + addr(order["from"])[2:].rjust(64, "0"), "0x" + PAYEE[2:].rjust(64, "0")
    amounts = []
    for log in rcpt.get("logs") or []:
        raw_a = str(log.get("address") or "")
        topics = [str(t).lower() for t in log.get("topics") or []]
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", raw_a) or raw_a.lower() != order["usdc"]:
            continue
        if len(topics) < 3 or topics[0] != TRANSFER or topics[1] != want_from or topics[2] != want_to:
            continue
        amounts.append(as_int(log.get("data")))
    if amounts != [int(order["amountUnits"])]:
        raise Fail("nicht genau ein passender USDC-Transfer")
    return True

def confirm(order: dict) -> None:
    deadline = time.monotonic() + 600
    while True:
        try:
            rcpt = rpc(order, "eth_getTransactionReceipt", [order["txHash"]])
            if isinstance(rcpt, dict) and receipt_ready(order, rcpt):
                break
        except RpcFail as e:
            if time.monotonic() > deadline:
                raise Fail("RPC-Fehler, Auftrag bleibt gesendet — bitte --resume") from e
        if time.monotonic() > deadline:
            raise Fail("weniger als 5 Bestätigungen — bitte --resume")
        time.sleep(3)
    title = f"paid:{order['txHash']}"
    path = f"/repos/{SHOP}/issues/{int(order['issue'])}"
    # Astra R2 P1-2: nach dem Broadcast nie still als Erfolg verbuchen, wenn das Issue zu ist.
    cur = recheck_issue(order, allow_paid=True)  # wirft bei geschlossenem Issue; Zustand bleibt gesendet
    if cur.get("title") != title:
        if str(cur.get("title") or "").startswith("paid:"):
            raise Fail("Issue trägt einen anderen paid-Titel")
        gh(["gh", "api", "--method", "PATCH", path, "-f", f"title={title}"])
        cur = gh(["gh", "api", path])
        if not isinstance(cur, dict) or cur.get("title") != title:
            raise Fail("Titel wurde nicht gesetzt")
    append_ledger(order)
    set_state(order, "bestätigt")
    info(f"bestätigt Issue {order['issue']} {order['txHash']} Betrag {order['amountUnits']}")

def wait_after_issue(order: dict) -> None:
    deadline = time.monotonic() + 300
    while True:
        ts = as_int(rpc(order, "eth_getBlockByNumber", ["latest", False])["timestamp"])
        if ts > int(order["issueCreatedAt"]):
            return
        if time.monotonic() > deadline:
            raise Fail("Blockzeit noch nicht nach Issue-Anlage — bitte --resume")
        time.sleep(2)

def fortsetzen(order: dict, relist: bool) -> None:
    assert_pins(order)
    if order["state"] == "abgebrochen":
        info("Auftrag abgebrochen, keine Zahlung")
        return
    if order["state"] == "bestätigt":
        append_ledger(order)
        info(f"bereits bestätigt: Issue {order.get('issue')} {order.get('txHash')}")
        return
    if order["state"] == "angelegt":
        if not order.get("from"):
            order["from"] = wallet_address()
            save(order)
        ensure_issue(order, relist)
        guard_wallet(order)
        wait_after_issue(order)
        sign_order(order)
    if order["state"] == "reserviert":
        sign_order(order)
    if order["state"] == "signiert":
        broadcast(order)
    if order["state"] == "gesendet":
        confirm(order)
        return
    raise Fail(f"unerwarteter Zustand {order['state']}")

def fresh(url: str) -> None:
    existing = load_state()
    if existing and existing["state"] not in ("bestätigt", "abgebrochen"):
        raise Fail("offener Auftrag — bitte --resume")
    order = load_terms(url)
    order["from"] = wallet_address()
    guard_wallet(order)
    save(order)
    fortsetzen(order, relist=False)

def dry(url: str) -> None:
    existing = load_state()
    if existing and existing["state"] not in ("bestätigt", "abgebrochen"):
        info(f"Hinweis: offener Auftrag Zustand={existing['state']} Issue={existing.get('issue')}")
    order = load_terms(url)
    order["from"] = wallet_address()
    saldo = balance_of(order, order["from"])
    info(f"Trockenlauf Netz={order['network']} Preis={order['priceUnits']} Empfänger={PAYEE} USDC={order['usdc']} Saldo={saldo}")
    info("Zahlung später exakt Preis + Issue-Nummer. Kein Issue, keine Transaktion.")
    if saldo > MAX_BAL:
        raise Fail("Wallet-Saldo über 10 USDC — Abbruch")
    if saldo < order["priceUnits"]:
        raise Fail("USDC-Guthaben unter dem Grundpreis")

def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="KCS-402 Käufer-Agent")
    p.add_argument("--url", default=os.environ.get("KCS402_GATE_URL", ""))
    p.add_argument("--dry-run", action="store_true", help="nur lesen und prüfen")
    p.add_argument("--resume", action="store_true", help="gespeicherten Auftrag fortsetzen")
    args = p.parse_args(argv)
    with exklusiv():
        if args.resume:
            order = load_state()
            if order is None:
                raise Fail("kein Zustand für --resume")
            if args.dry_run:
                info(f"Trockenlauf Zustand={order['state']} Issue={order.get('issue')} Hash={order.get('txHash')}")
                return
            fortsetzen(order, relist=True)
            return
        if not args.url:
            raise Fail("URL fehlt (--url oder KCS402_GATE_URL)")
        dry(args.url) if args.dry_run else fresh(args.url)

if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Fail as e:
        print(f"Fehler: {e}", file=sys.stderr)
        raise SystemExit(1) from None
