#!/usr/bin/env python3
"""kcs-402 Prüfer. Nur Standardbibliothek, kein Schlüssel, nur ausgehend (gh, JSON-RPC).

Front-Running-Bindung: genau ein USDC-Transfer, Betrag exakt Preis + Issue-Nummer
(1 Mikro-USDC = 1 Basiseinheit), kanonische Blockzeit strikt nach created_at.
Zustände je Issue: offen → geprüft → eingeladen | abgelehnt | widerruf-offen.
Hash und Issue werden vor dem PUT reserviert (State atomar via temp + os.replace).
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ORG = "kirchbergcapitalservices"
SHOP = f"{ORG}/kcs-402-shop"
WARE = f"{ORG}/kcs-402-ware-test"
PAY_TO = "0xabe4eece3eae3a2fe8c705d80ab3d23491cc2715"
PADDED_TO = "0x" + PAY_TO[2:].lower().zfill(64)  # topics[2], 32 Byte links aufgefüllt
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NETS = {
    "sepolia": ("https://sepolia.base.org", "0x036cbd53842c5426634e7929541ec2318f3dcf7e", 100_000),
    "mainnet": ("https://mainnet.base.org", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 1_000_000),
}
MIN_CONF, CAP, MAX_OPEN, MAX_PAGES = 5, 10, 3, 200
RECONCILE_H = 48  # Astra R3 P1: kürzlich geschlossene Claims nachversöhnen (Schließen/Broadcast-Race)
NET_ALIASES = {"sepolia": "sepolia", "base-sepolia": "sepolia", "mainnet": "mainnet", "base": "mainnet", "base-mainnet": "mainnet"}
STATES = {"offen", "gesehen", "geprüft", "eingeladen", "abgelehnt", "widerruf-offen"}
# Astra R7 P2: Kommentar/Abschluss hat einen eigenen Zustand je Art. "post-gestartet" wird VOR dem POST gesichert;
# ein Wiederanlauf in diesem Zustand postet nie automatisch erneut, sondern sucht den Marker in den vorhandenen
# Kommentaren — fehlt er, ist der Ausgang unklar und ein Mensch entscheidet (--kommentar-freigeben).
KOMMENTAR_FELD = {"abschluss": "abschluss", "widerruf": "widerruf_kommentar", "ephemer": "kommentar"}
# "ephemer" (Fremdtitel/Überzahl ohne Vorgang) liegt in st["ephemer"][<nr>] = {"kommentar": <zustand>}, nicht in issues
# (dort verlangt load_state einen Vorgangszustand). Review Grok/vibe 25.10.: close_ephemeral durch denselben Automaten.
KOMMENTAR_ZUSTAENDE = {"post-gestartet", "kommentiert", "geschlossen", "unklar"}
PERMS = {"admin", "maintain", "write", "triage", "read"}
HEX64 = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEXQ = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
ADDR = re.compile(r"^0x[0-9a-f]{40}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
PAID_RE = re.compile(r"^paid:(0x[0-9a-f]{64})$")  # exakt, nur Kleinbuchstaben
CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$")
ISSUE_KEY = re.compile(r"^[1-9][0-9]{0,8}$")
GH_PATH = re.compile(r"^repos/[A-Za-z0-9_./-]{1,180}$")
STATE_DIR = Path.home() / ".local" / "state" / "kcs-402"
MSG = {
    "foreign": "Geschlossen: Titel muss exakt 'claim' oder 'paid:' plus 0x und 64 Hexzeichen sein.",
    "excess": "Geschlossen: höchstens 3 offene Issues pro Autor.",
    "bad_login": "Geschlossen: Issue-Autor ist kein gültiger GitHub-Login.",
}


class RpcError(Exception):
    """Knoten nicht verwertbar. Issue bleibt offen."""


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def usdc(n):
    n = abs(int(n))
    return f"{n // 1_000_000}.{n % 1_000_000:06d}"


def need(ok, msg="ungültig"):
    if not ok:
        raise RuntimeError(msg)


def hex_qty(v):
    if not isinstance(v, str) or not HEXQ.fullmatch(v):
        raise RpcError("hex")
    return int(v, 16)


def login_or_empty(v):
    if v in ("", None):
        return ""
    need(isinstance(v, str) and LOGIN_RE.fullmatch(v), "login")
    return v


def gh(path, method=None, fields=None, allow_404=False, timeout=60):
    need(path == "user" or GH_PATH.fullmatch(path), "gh-pfad")  # "user" nur für own_login()
    cmd = ["gh", "api", path]
    # K0-Fix 25.10.2026: ohne explizites -X macht `gh api` aus -f-Feldern einen POST.
    # Ein Lesen mit Filtern würde so versuchen, ein Issue anzulegen (HTTP 422 im Trockenlauf).
    if method is None and fields:
        method = "GET"
    if method is not None:
        need(method in {"GET", "POST", "PATCH", "PUT"}, "gh-methode")  # kein DELETE (K0-Fix 25.10.)
        cmd.extend(["-X", method])
    for key, val in (fields or {}).items():
        need(re.fullmatch(r"[A-Za-z0-9_]{1,32}", key), "gh-feld")
        need(isinstance(val, str) and not re.search(r"[\r\n]", val) and len(val) <= 500, "gh-wert")
        cmd.extend(["-f", f"{key}={val}"])  # Argumentliste, kein Shell
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().replace("\n", " ")[:240]
        if allow_404 and "404" in err:
            return None
        raise RuntimeError(f"gh {method or 'GET'} {path} rc={proc.returncode}: {err}")
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if method in {"PUT", "PATCH"}:
            return None
        raise RuntimeError("gh-json")


def rpc(url, method, params):
    need(method in {"eth_getTransactionReceipt", "eth_blockNumber", "eth_getBlockByNumber"}, "rpc")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": "kcs-402-checker"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(4_000_000)
    except Exception as exc:
        raise RpcError(exc.__class__.__name__) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RpcError("json") from exc
    if not isinstance(data, dict) or data.get("id") != 1 or "result" not in data:
        raise RpcError("antwort")
    if data.get("error"):
        raise RpcError(str(data["error"])[:160])
    return data["result"]


def empty_state():
    return {"used_hashes": {}, "issues": {}, "invites": [], "ephemer": {}, "_dry_holds": 0, "last_ok": 0}


def load_state():
    path = STATE_DIR / "state.json"
    st = empty_state()
    if not path.exists():
        return st
    need(path.is_file() and path.stat().st_size <= 5_000_000, "state.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("state.json unlesbar") from exc
    need(isinstance(data, dict), "state")
    raw_h, raw_i, raw_v = data.get("used_hashes", {}), data.get("issues", {}), data.get("invites", [])
    if isinstance(data.get("last_ok"), (int, float)) and data["last_ok"] >= 0:
        st["last_ok"] = int(data["last_ok"])  # Wasserstand des letzten erfolgreichen Laufs (Astra R4 P1)
    need(isinstance(raw_h, dict) and isinstance(raw_i, dict) and isinstance(raw_v, list), "state")
    for key, val in raw_h.items():
        need(isinstance(key, str) and HEX64.fullmatch(key), "hash")
        hl = key.lower()
        need(hl not in st["used_hashes"], "hash-doppel")
        if isinstance(val, int):  # Altformat: Hash → Issue-Nummer, bereits eingelöst
            val = {"issue": val, "state": "eingeladen", "login": ""}
        need(isinstance(val, dict) and isinstance(val.get("issue"), int) and val.get("state") in STATES, "hash")
        st["used_hashes"][hl] = {"issue": val["issue"], "state": val["state"], "login": login_or_empty(val.get("login"))}
    for key, val in raw_i.items():
        need(isinstance(key, str) and ISSUE_KEY.fullmatch(key) and isinstance(val, dict), "issue")
        need(val.get("state") in STATES, "issue-zustand")
        tx = val.get("tx") or ""
        need(tx == "" or (isinstance(tx, str) and HEX64.fullmatch(tx)), "issue-tx")
        item = {"state": val["state"], "login": login_or_empty(val.get("login")), "tx": tx.lower() if tx else ""}
        for flag in ("notified", "invite_started", "we_added", "pre_existing"):
            if isinstance(val.get(flag), bool):
                item[flag] = val[flag]
        for feld in KOMMENTAR_FELD.values():  # Astra R7 P2
            if val.get(feld) in KOMMENTAR_ZUSTAENDE:
                item[feld] = val[feld]
        if isinstance(val.get("paid_units"), int) and val["paid_units"] >= 0:
            item["paid_units"] = val["paid_units"]
        st["issues"][key] = item
    for inv in raw_v:
        need(isinstance(inv, dict) and isinstance(inv.get("issue"), int), "invite")
        need(isinstance(inv.get("day"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", inv["day"]), "tag")
        st["invites"].append({"day": inv["day"], "issue": inv["issue"]})
    raw_e = data.get("ephemer", {})
    need(isinstance(raw_e, dict), "ephemer")
    for key, val in raw_e.items():
        need(isinstance(key, str) and ISSUE_KEY.fullmatch(key) and isinstance(val, dict), "ephemer")
        if val.get("kommentar") in KOMMENTAR_ZUSTAENDE:
            st["ephemer"][key] = {"kommentar": val["kommentar"]}
    return st


def save_state(st, dry):
    if dry:
        return
    clean = {"used_hashes": st["used_hashes"], "issues": st["issues"], "invites": st["invites"], "ephemer": st.get("ephemer", {}),
             "last_ok": st.get("last_ok", 0)}
    payload = json.dumps(clean, indent=1, sort_keys=True) + "\n"
    tmp = STATE_DIR / ".state.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, STATE_DIR / "state.json")  # atomar, selbes Verzeichnis
    dirfd = os.open(STATE_DIR, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def ensure_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def acquire_lock():
    fd = os.open(STATE_DIR / "run.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def at_cap(st):
    day = today()
    invited = sum(1 for i in st["invites"] if i.get("day") == day)
    flight = sum(1 for r in st["issues"].values() if r.get("state") == "geprüft")
    return invited + flight + st.get("_dry_holds", 0) >= CAP


def list_issues(state, since=None):
    """Alle Issues eines Zustands, älteste zuerst. Volle Seite ohne neue Nummern = Fehler (Astra R3 P2)."""
    found, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        fields = {"state": state, "per_page": "100", "sort": "created", "direction": "asc", "page": str(page)}
        if since:
            fields["since"] = since
        batch = gh(f"repos/{SHOP}/issues", fields=fields, timeout=90)
        need(isinstance(batch, list), "issues-liste")
        if not batch:
            break
        fresh = 0
        for item in batch:
            num = item.get("number") if isinstance(item, dict) else None
            if num in seen:
                continue
            seen.add(num)
            found.append(item)
            fresh += 1
        if len(batch) < 100:
            break
        if fresh == 0:
            raise RuntimeError("Pagination liefert volle Seite ohne neue Issues — Lauf abgebrochen")
    else:
        # Astra R2 P2-5: unvollständige Liste nie als regulären Lauf behandeln
        raise RuntimeError(f"Pagination-Grenze {MAX_PAGES} Seiten erreicht — Lauf abgebrochen, Liste unvollständig")
    return found


def list_open_issues():
    return list_issues("open")


def list_recently_closed(st):
    """Astra R4 P1: kein festes Fenster — Wasserstand mit Überlappung. Nach einem Prüfer-Ausfall reicht das
    Fenster bis 24 h vor den letzten erfolgreichen Lauf zurück; ohne Wasserstand (Erstlauf) volle Historie."""
    last_ok = int(st.get("last_ok") or 0)
    if last_ok <= 0:
        return list_issues("closed")
    start = min(time.time() - RECONCILE_H * 3600, last_ok - 24 * 3600)
    return list_issues("closed", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)))


def pending_from_state(st, loaded_numbers):
    """Astra R4 P1: reservierte/offene Vorgänge werden unabhängig vom Suchfenster direkt nachgeladen."""
    out = []
    for key, rec in st["issues"].items():
        # Astra R5: Warteschlange (gesehen), Reservierung (geprüft), Menschen-Gate (widerruf-offen)
        # und Reorg-Wache für bereits Eingeladene — alle unabhängig vom Suchfenster
        if rec.get("state") not in ("gesehen", "geprüft", "widerruf-offen", "eingeladen"):
            continue
        num = int(key)
        if num in loaded_numbers:
            continue
        try:
            raw = gh(f"repos/{SHOP}/issues/{num}", allow_404=True)
        except Exception as exc:  # Astra R5 P2: ein Ladefehler blockiert nicht die übrigen Vorgänge
            log(f"#{num} vorgemerkt, Laden fehlgeschlagen (später erneut): {exc}")
            continue
        if isinstance(raw, dict):
            out.append(raw)
        else:
            log(f"#{num} vorgemerkt, aber nicht ladbar (404) — manuell klären")
    return out


def classify(title):
    if title == "claim":
        return "claim", None
    m = PAID_RE.fullmatch(title)
    if m:
        return "paid", m.group(1).lower()
    return "foreign", None


def normalize(raw):
    if not isinstance(raw, dict) or "pull_request" in raw:
        return None  # PRs niemals schließen
    num = raw.get("number")
    if not isinstance(num, int) or not 1 <= num <= 99_999_999:
        log("Eintrag ohne gültige Issue-Nummer übersprungen")
        return None
    title = raw.get("title")
    title = title if isinstance(title, str) else ""  # exakter Titel, kein Trimmen
    if len(title) > 300:
        title = ""
    created_raw = raw.get("created_at")
    if not isinstance(created_raw, str) or not CREATED_RE.fullmatch(created_raw):
        log(f"#{num} created_at ungültig, bleibt offen")
        return None
    created = datetime.fromisoformat(created_raw[:-1] + "+00:00").timestamp()
    user = raw.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        login = None
    kind, tx = classify(title)
    closed = raw.get("state") == "closed"
    return {"number": num, "created": created, "login": login, "kind": kind, "tx": tx, "closed": closed}


def stored_login(st, num, tx):
    for rec in (st["issues"].get(str(num)) or {}, st["used_hashes"].get(tx) or {} if tx else {}):
        login = rec.get("login") if isinstance(rec, dict) else ""
        if isinstance(login, str) and LOGIN_RE.fullmatch(login):
            return login
    return None


def build_plan(issues, st):
    kept, plan = {}, []
    hot = {"geprüft", "eingeladen", "widerruf-offen"}
    for iss in issues:
        num = iss["number"]
        rec = st["issues"].get(str(num)) or {}
        committed = rec.get("state") in hot
        # Astra R5 P2: persistierte Zustände gehen vor dem aktuellen Titel — gespeicherter Hash zählt
        if rec.get("state") in ("gesehen", "geprüft", "eingeladen", "widerruf-offen") and rec.get("tx"):
            iss["kind"], iss["tx"] = "paid", rec["tx"]
            iss["login"] = iss["login"] or stored_login(st, num, rec["tx"])
            if iss["login"]:
                plan.append((iss, "process"))
            continue
        if iss.get("closed"):
            # Nachversöhnung (Astra R3 P1): nur paid-Titel mit gültigem Login; nichts wird geschlossen oder gezählt
            if iss["kind"] == "paid" and (iss["login"] or stored_login(st, num, iss.get("tx"))):
                iss["login"] = iss["login"] or stored_login(st, num, iss.get("tx"))
                plan.append((iss, "process"))
            continue
        # Review Grok 25.10. (P1): ein vom Kommentar-Gate offen gehaltenes, schon entschiedenes Issue (abgelehnt,
        # eingeladen, geprüft, widerruf-offen) darf kein jüngeres bezahltes Issue desselben Autors als "excess" verdrängen —
        # es wird verarbeitet, zählt aber nicht gegen MAX_OPEN.
        if committed or rec.get("state") == "abgelehnt":
            iss["login"] = iss["login"] or stored_login(st, num, rec.get("tx") or iss.get("tx"))
            plan.append((iss, "process"))
            continue
        login = iss["login"] or stored_login(st, num, iss.get("tx"))
        iss["login"] = login
        if not login and not committed:
            plan.append((iss, "bad_login"))
            continue
        bucket = login or f"#{num}"
        if not committed and kept.get(bucket, 0) >= MAX_OPEN:
            plan.append((iss, "excess"))
            continue
        if iss["kind"] == "foreign" and not committed:
            plan.append((iss, "foreign"))
            continue
        kept[bucket] = kept.get(bucket, 0) + 1
        plan.append((iss, "process"))
    return plan


def verify_payment(net, txhash, issue_no, created_ts):
    """(status, code, detail, betrag). status: ok | pending | reject. Betrag nur int."""
    rpc_url, token_contract, price = net
    expected = price + issue_no
    rcpt = rpc(rpc_url, "eth_getTransactionReceipt", [txhash])
    if rcpt is None:
        return "pending", "not_found", "Transaktion noch nicht gefunden", 0
    if not isinstance(rcpt, dict):
        raise RpcError("receipt")
    got = rcpt.get("transactionHash")
    if not isinstance(got, str) or got.lower() != txhash:
        raise RpcError("txhash")
    bn_raw, bh = rcpt.get("blockNumber"), rcpt.get("blockHash")
    bn = hex_qty(bn_raw)
    if not isinstance(bh, str) or not HEX64.fullmatch(bh):
        raise RpcError("blockhash")
    block = rpc(rpc_url, "eth_getBlockByNumber", [bn_raw, False])
    if not isinstance(block, dict) or not isinstance(block.get("hash"), str):
        raise RpcError("block")
    # Kanonisch: Receipt-Blockhash muss der Hash dieser Blockhöhe sein.
    if hex_qty(block.get("number")) != bn or block["hash"].lower() != bh.lower():
        return "pending", "not_canonical", "Block nicht kanonisch", 0
    logs = rcpt.get("logs")
    if not isinstance(logs, list) or len(logs) > 500:
        raise RpcError("logs")
    if any(isinstance(lg, dict) and lg.get("removed") is True for lg in logs):
        return "pending", "log_removed", "Log als removed markiert", 0
    conf = hex_qty(rpc(rpc_url, "eth_blockNumber", [])) - bn
    if conf < MIN_CONF:
        return "pending", "low_conf", f"nur {conf} Bestätigungen, brauche {MIN_CONF}", 0
    status_raw = rcpt.get("status")
    if not isinstance(status_raw, str) or not HEXQ.fullmatch(status_raw):
        raise RpcError("status")
    if int(status_raw, 16) != 1:
        return "reject", "reverted", "status ist nicht 0x1", 0
    bts = hex_qty(block.get("timestamp"))
    if bts > time.time() + 3600:
        raise RpcError("blockzeit")
    if bts <= created_ts:
        return "reject", "time", "Blockzeit liegt nicht nach der Issue-Erstellung", 0
    matched = []
    for lg in logs:
        if not isinstance(lg, dict):
            raise RpcError("log")
        addr, topics = lg.get("address"), lg.get("topics")
        if not isinstance(addr, str) or addr.lower() != token_contract:
            continue
        if not isinstance(topics, list) or len(topics) != 3:
            continue
        if not all(isinstance(t, str) and HEX64.fullmatch(t) for t in topics):
            continue
        if topics[0].lower() != TRANSFER or topics[2].lower() != PADDED_TO:
            continue
        data = lg.get("data")
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", data):
            raise RpcError("transfer-data")
        matched.append(int(data, 16))
    if len(matched) != 1:
        return "reject", "count", f"genau 1 USDC-Transfer an die Shop-Adresse erwartet, gesehen {len(matched)}", 0
    if matched[0] != expected:
        return "reject", "amount", f"Betrag {matched[0]} != Preis+Issue {expected} ({usdc(matched[0])} != {usdc(expected)} USDC)", matched[0]
    return "ok", "ok", f"{usdc(matched[0])} USDC, {conf} Bestätigungen, Issue #{issue_no}", matched[0]


def next_action(status, code, started):
    if status == "ok":
        return "invite"
    # PUT könnte schon passiert sein: fehlende oder endgültig falsche Zahlung entziehen.
    if started and (status == "reject" or code == "not_found"):
        return "revoke"
    if status == "reject":
        return "deny"
    return "wait"


def marker(kind, num):
    """Deterministischer, unsichtbarer Marker je Issue und Kommentarart (HTML-Kommentar, von GitHub nicht gerendert)."""
    need(kind in ("abschluss", "widerruf", "ephemer") and isinstance(num, int), "marker")
    return f"<!-- kcs-402:{kind}:#{num} -->"


_OWN_LOGIN = None


def own_login():
    """Login des Prüfer-Kontos (einmal je Lauf). Review Grok/vibe 25.10.: Marker zählen nur in eigenen Kommentaren."""
    global _OWN_LOGIN
    if _OWN_LOGIN is None:
        me = gh("user")
        login = me.get("login") if isinstance(me, dict) else None
        need(isinstance(login, str) and LOGIN_RE.fullmatch(login), "own-login")
        _OWN_LOGIN = login
    return _OWN_LOGIN


def list_comments(num):
    """Alle Kommentare eines Issues als (login, body), vollständig paginiert; Grenze erreicht = Fehler, nie Teilmenge."""
    bodies, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        batch = gh(f"repos/{SHOP}/issues/{num}/comments", fields={"per_page": "100", "page": str(page)}, timeout=90)
        need(isinstance(batch, list), "kommentar-liste")
        if not batch:
            break
        fresh = 0
        for item in batch:
            cid = item.get("id") if isinstance(item, dict) else None
            if cid in seen:
                continue
            seen.add(cid)
            user = item.get("user") if isinstance(item, dict) else None
            author = user.get("login") if isinstance(user, dict) else None
            bodies.append((author if isinstance(author, str) else "", str(item.get("body") or "")))
            fresh += 1
        if len(batch) < 100:
            break
        if fresh == 0:
            raise RuntimeError("Kommentar-Pagination liefert volle Seite ohne neue Einträge — abgebrochen")
    else:
        raise RuntimeError(f"Kommentar-Pagination-Grenze {MAX_PAGES} Seiten erreicht — abgebrochen, Liste unvollständig")
    return bodies


def has_marker(num, kind):
    """Nur ein Kommentar des eigenen Kontos mit dem Marker zählt — ein fremder Marker-Text bleibt wirkungslos."""
    mark, me = marker(kind, num), own_login()
    return any(author == me and mark in body for author, body in list_comments(num))


def comment_once(st, num, kind, body):
    """Astra R7 P2: genau ein Kommentar je Issue und Art, auch bei verlorener POST-Antwort oder Absturz.
    True = Kommentar steht (jetzt oder schon vorher); False = Menschen-Gate, kein weiterer POST."""
    feld = KOMMENTAR_FELD[kind]
    if kind == "ephemer":
        rec = st.setdefault("ephemer", {}).setdefault(str(num), {})
    else:
        rec = st["issues"].setdefault(str(num), {})
        need(rec.get("state") in STATES, "kommentar-ohne-vorgang")  # sonst wäre state.json beim nächsten Lauf unlesbar
    cur = rec.get(feld)
    if cur in ("kommentiert", "geschlossen"):
        return True
    if kind == "abschluss" and cur is None and rec.get("notified"):  # Altformat vor R7
        rec[feld] = "kommentiert"
        save_state(st, False)
        return True
    if cur in ("post-gestartet", "unklar"):
        # Ausgang eines früheren POST unbekannt: nur den Marker suchen, nie automatisch erneut posten.
        if has_marker(num, kind):
            rec[feld], rec["notified"] = "kommentiert", True
            save_state(st, False)
            return True
        if cur != "unklar":
            rec[feld] = "unklar"
            save_state(st, False)
        log(f"#{num} MENSCHEN-GATE {kind}-Kommentar unklar: früherer POST ohne bestätigtes Ergebnis, kein Marker gefunden"
            f" — manuell prüfen, dann --kommentar-freigeben {num}")
        return False
    if has_marker(num, kind):  # Absturz zwischen POST und Zustandssicherung in einem früheren Lauf
        rec[feld], rec["notified"] = "kommentiert", True
        save_state(st, False)
        return True
    rec[feld] = "post-gestartet"
    save_state(st, False)  # VOR dem POST sichern: ein Absturz hinterlässt einen sichtbaren, nicht wiederholenden Zustand
    try:
        gh(f"repos/{SHOP}/issues/{num}/comments", "POST", {"body": f"{body} {marker(kind, num)}"})
    except Exception as exc:  # noqa: BLE001 — Ausgang unbekannt (Timeout, 5xx, Netz): kein zweiter Versuch
        rec[feld] = "unklar"
        save_state(st, False)
        log(f"#{num} MENSCHEN-GATE {kind}-Kommentar unklar: POST ohne bestätigtes Ergebnis ({str(exc)[:160]})"
            f" — manuell prüfen, dann --kommentar-freigeben {num}")
        return False
    rec[feld], rec["notified"] = "kommentiert", True
    save_state(st, False)
    return True


def still_open(num):
    """Astra R4 P2: vor jedem Kommentar/Schließen den aktuellen Zustand lesen; geschlossen = keine Außenaktion."""
    cur = gh(f"repos/{SHOP}/issues/{num}", allow_404=True)
    return isinstance(cur, dict) and cur.get("state") == "open"


def close_ephemeral(st, num, comment, dry):
    """Fremdtitel/Überzahl: Kommentar + Schließen über denselben Automaten wie close_with_notice (Review Grok/vibe 25.10.)."""
    if dry:
        log(f"  [dry-run] schlösse #{num}: {comment}")
        return
    rec = st.setdefault("ephemer", {}).setdefault(str(num), {})
    if rec.get("kommentar") == "geschlossen":  # Review Grok R2 P2: wiedereröffnet → nur schließen, kein neuer Kommentar
        if still_open(num):
            gh(f"repos/{SHOP}/issues/{num}", "PATCH", {"state": "closed"})
        return
    if not still_open(num):
        log(f"#{num} inzwischen geschlossen — kein Kommentar, kein Schließen")
        rec["kommentar"] = "geschlossen"
        save_state(st, False)
        return
    if not comment_once(st, num, "ephemer", comment):  # Menschen-Gate → auch kein Schließen
        return
    if still_open(num):  # Astra R5 P2: vor jedem Schreibaufruf getrennt prüfen
        gh(f"repos/{SHOP}/issues/{num}", "PATCH", {"state": "closed"})
    rec["kommentar"] = "geschlossen"
    save_state(st, False)


def close_with_notice(st, num, comment, dry, listed_open=True):
    """listed_open: GitHub führt das Issue in diesem Lauf als offen (Review Grok R2 P2: wiedereröffnete Terminal-Issues nur schließen)."""
    if dry:
        log(f"  [dry-run] schlösse #{num}: {comment[:160]}")
        return
    rec = st["issues"].setdefault(str(num), {})
    if rec.get("abschluss") == "geschlossen":  # Review Grok P3: kein GET für längst Abgeschlossenes
        if listed_open and still_open(num):  # wiedereröffnet → nur schließen, kein neuer Kommentar
            gh(f"repos/{SHOP}/issues/{num}", "PATCH", {"state": "closed"})
        return
    if not still_open(num):
        log(f"#{num} inzwischen geschlossen — Zustand gesichert, keine Außenaktion")
        rec["abschluss"] = "geschlossen"  # Review Grok R2 P2: Terminalzustand sichern (z. B. Absturz zwischen PATCH und save), sonst GET je Lauf
        save_state(st, False)
        return
    if not comment_once(st, num, "abschluss", comment):  # Astra R7 P2: Menschen-Gate → auch kein Schließen
        return
    if still_open(num):  # Astra R5 P2
        gh(f"repos/{SHOP}/issues/{num}", "PATCH", {"state": "closed"})
    rec["abschluss"] = "geschlossen"
    save_state(st, False)


def has_access(login):
    data = gh(f"repos/{WARE}/collaborators/{login}/permission", allow_404=True)
    return isinstance(data, dict) and data.get("permission") in PERMS


def bind_hash(st, tx, num, login, state):
    st["used_hashes"][tx] = {"issue": num, "state": state, "login": login}


def fail_issue(st, num, login, tx, comment, dry, consume):
    """Endgültig ablehnen. Hash nur verbrauchen, wenn er diesem Issue schon gehörte."""
    if dry:
        log(f"  [dry-run] lehne #{num} ab: {comment}")
        return
    rec = st["issues"].setdefault(str(num), {})
    rec.update({"state": "abgelehnt", "login": login or rec.get("login") or ""})
    if consume and tx:
        bind_hash(st, tx, num, rec["login"], "abgelehnt")
    save_state(st, False)
    close_with_notice(st, num, comment, False)


def revoke(st, num, login, tx, reason, dry):
    rec = st["issues"].setdefault(str(num), {})
    rec.update({"state": "widerruf-offen", "login": login or rec.get("login") or "", "tx": tx or rec.get("tx") or ""})
    tx = rec["tx"]
    if tx:
        bind_hash(st, tx, num, rec["login"], "widerruf-offen")
    if dry:
        log(f"  [dry-run] würde #{num} widerrufen: {reason}")
        return
    save_state(st, False)
    # K0-Fix 25.10.2026 (Astra P2-10): kein DELETE durch den Prüfer. Schreibumfang nach außen ist
    # Einladung, Kommentar, Schließen. Ein Widerruf bleibt "widerruf-offen" und wird von einem Menschen
    # entschieden; das Issue bleibt offen, der Hash bleibt gebunden.
    log(f"#{num} P1 WIDERRUF-OFFEN: {reason} — manuell prüfen (we_added={rec.get('we_added')}, login={rec['login']})")
    if rec.get("widerruf_kommentar") not in ("kommentiert", "geschlossen") and still_open(num):
        comment_once(st, num, "widerruf", f"Zahlung nach Einladung nicht mehr gültig ({str(reason)[:200]}). Wird manuell geprüft.")  # Astra R7 P2


def grant(st, net_name, iss, tx, paid, dry):
    num, login = iss["number"], iss["login"]
    rec = st["issues"].setdefault(str(num), {})
    if dry:
        log(f"  [dry-run] würde @{login} read-only in {WARE} einladen ({usdc(paid)} USDC)")
        return
    if not rec.get("invite_started"):
        rec["invite_started"] = True
        rec["we_added"] = False
        save_state(st, False)
    # Herkunft nur EINMAL vor dem ersten PUT festhalten; ein Wiederanlauf überschreibt sie nicht
    # (K0-Fix 25.10.2026, Astra P2-6: sonst ginge "wir haben eingeladen" nach angenommener Einladung verloren).
    if "pre_existing" not in rec:
        rec["pre_existing"] = has_access(login)
        rec["we_added"] = not rec["pre_existing"]
        save_state(st, False)
    already = bool(rec.get("pre_existing"))
    if not already:
        gh(f"repos/{WARE}/collaborators/{login}", "PUT", {"permission": "pull"})
    rec["state"] = "eingeladen"
    rec["paid_units"] = int(paid)
    rec["tx"] = tx
    bind_hash(st, tx, num, login, "eingeladen")
    if not any(i.get("issue") == num for i in st["invites"]):
        st["invites"].append({"day": today(), "issue": num, "login": login, "tx": tx, "paid_units": int(paid), "network": net_name})
    save_state(st, False)
    close_with_notice(
        st, num,
        f"Zahlung geprüft ({usdc(paid)} USDC). @{login} hat Lesezugang zu {WARE}.",
        False,
    )


def reorg_watch(st, net, iss, rec, dry):
    """Astra R5: bereits eingeladene Vorgänge in jedem Lauf gegen die Kette prüfen; Reorg → widerruf-offen (Menschen-Gate)."""
    num, tx = iss["number"], rec["tx"]
    try:
        status, code, detail, _paid = verify_payment(net, tx, num, iss["created"])
    except RpcError as exc:
        log(f"#{num} Reorg-Wache: RPC-Fehler, später erneut: {exc}")
        return
    if status == "ok":
        # Astra R6 P2: Abschluss (Kommentar/Schließen) idempotent nachholen, falls er nach dem PUT scheiterte
        close_with_notice(st, num, f"Zahlung geprüft. @{iss['login']} hat Lesezugang zu {WARE}.", dry, listed_open=not iss.get("closed"))
        return
    # Astra R6 P1: jede nach der Einladung festgestellte Abweichung (reject, not_found, not_canonical,
    # log_removed, low_conf) ist ein Menschen-Gate — nur echte RpcError (oben) werden erneut versucht.
    log(f"#{num} P1 Reorg-Wache: Zahlungsgrundlage nach Einladung nicht mehr gültig ({code}: {detail})")
    revoke(st, num, iss["login"], tx, f"{code}: {detail}", dry)


def handle_paid(st, net, net_name, iss, dry):
    num, login, created = iss["number"], iss["login"], iss["created"]
    rec = st["issues"].get(str(num)) or {}
    tx = (rec.get("tx") or iss.get("tx") or "").lower() if rec.get("state") == "geprüft" else iss.get("tx")
    if not isinstance(tx, str) or not HEX64.fullmatch(tx):
        log(f"#{num} Hash ungültig, bleibt offen")
        return
    tx = tx.lower()
    owner = st["used_hashes"].get(tx)
    if owner and owner["issue"] != num:
        fail_issue(st, num, login, tx, f"Transaktion bereits für Issue #{owner['issue']} verbraucht.", dry, False)
        return
    if owner and owner["issue"] == num and rec.get("state", "offen") == "offen" and owner["state"] != "offen":
        rec = st["issues"].setdefault(str(num), {})
        rec.update({"state": owner["state"], "tx": tx, "login": login})
        if not dry:
            save_state(st, False)
    if not login or not LOGIN_RE.fullmatch(login):
        log(f"#{num} Login fehlt, bleibt offen")
        return
    if rec.get("state", "offen") == "offen" and not dry:
        # Astra R5 P1: Warteschlange — entdeckt, noch nicht abgeschlossen; KEINE Hash-Reservierung,
        # damit der Wasserstand diesen Vorgang nie überspringt.
        st["issues"][str(num)] = {"state": "gesehen", "login": login, "tx": tx}
        save_state(st, False)
        rec = st["issues"][str(num)]
    try:
        status, code, detail, paid = verify_payment(net, tx, num, created)
    except RpcError as exc:
        log(f"#{num} RPC-Fehler, bleibt offen: {exc}")
        return
    state = (st["issues"].get(str(num)) or {}).get("state", "offen")
    if state in ("offen", "gesehen"):
        if status == "pending":
            log(f"#{num} wartet: {detail}")
            return
        if status != "ok":
            # Fremden Hash nicht verbrauchen: Betrag passt nicht zu DIESEM Issue.
            fail_issue(st, num, login, tx, f"Zahlung abgelehnt: {detail}. Kein Zugang.", dry, False)
            return
        if at_cap(st):
            log(f"#{num} vertagt: Tageslimit {CAP} Einladungen")
            return
        if dry:
            st["_dry_holds"] = st.get("_dry_holds", 0) + 1
            log(f"  [dry-run] würde @{login} einladen: {detail}")
            return
        st["issues"][str(num)] = {
            "state": "geprüft", "login": login, "tx": tx, "paid_units": paid, "invite_started": False,
        }
        bind_hash(st, tx, num, login, "geprüft")
        save_state(st, False)  # Reservierung vor jedem PUT
        try:
            status, code, detail, paid = verify_payment(net, tx, num, created)
        except RpcError as exc:
            log(f"#{num} RPC-Fehler nach Reservierung, bleibt offen: {exc}")
            return
        rec = st["issues"][str(num)]
    action = next_action(status, code, bool(rec.get("invite_started")))
    if action == "wait":
        log(f"#{num} reserviert, wartet: {detail}")
        return
    if action == "deny":
        fail_issue(st, num, login, tx, f"Zahlung abgelehnt: {detail}. Kein Zugang.", dry, True)
        return
    if action == "revoke":
        revoke(st, num, login, tx, detail, dry)
        return
    grant(st, net_name, iss, tx, paid, dry)


def dispatch(st, net, net_name, iss, action, dry):
    num, price = iss["number"], net[2]
    if action in MSG:
        close_ephemeral(st, num, MSG[action], dry)
        return
    rec = st["issues"].get(str(num)) or {}
    state, login = rec.get("state", "offen"), iss["login"]
    if state == "widerruf-offen":  # Astra R5 P2: ausschließlich Menschen-Gate, nie erneut einladen
        revoke(st, num, login, rec.get("tx") or "", "Widerruf wartet auf Entscheidung des Inhabers.", dry)
        return
    if state == "eingeladen" and rec.get("tx"):
        reorg_watch(st, net, iss, rec, dry)  # Astra R5: Reorg-Wache für Eingeladene
        return
    if iss.get("closed"):
        if state == "abgelehnt":
            return
        handle_paid(st, net, net_name, iss, dry)
        return
    if state == "eingeladen":
        close_with_notice(st, num, f"Zugang besteht: @{login} ist read-only auf {WARE}.", dry)
        return
    if state == "abgelehnt":
        close_with_notice(st, num, "Zahlung bereits abgelehnt. Kein Zugang.", dry)
        return
    if state == "widerruf-offen":
        revoke(st, num, login, rec.get("tx") or "", "Widerruf wird abgeschlossen.", dry)
        return
    if state != "geprüft" and iss["kind"] == "claim":
        log(f"#{num} Anspruch offen, exakt {usdc(price + num)} USDC, danach Titel paid:<txhash>")
        return
    if state != "geprüft" and iss["kind"] != "paid":
        close_ephemeral(st, num, MSG["foreign"], dry)
        return
    handle_paid(st, net, net_name, iss, dry)


def main():
    ap = argparse.ArgumentParser(description="kcs-402 Prüfer")
    ap.add_argument("--dry-run", action="store_true", help="nichts schreiben")
    ap.add_argument("--network", default=os.environ.get("KCS402_NETWORK", "sepolia"), choices=sorted(NET_ALIASES))
    ap.add_argument("--once-issue", type=int, default=None)
    ap.add_argument("--kommentar-freigeben", type=int, default=None, metavar="ISSUE",
                    help="Menschen-Gate (Astra R7 P2): nach manueller Prüfung, dass kein Kommentar existiert, einen neuen Versuch erlauben; sonst kein Lauf")
    args = ap.parse_args()
    if args.once_issue is not None and not 1 <= args.once_issue <= 99_999_999:
        ap.error("--once-issue außerhalb 1..99999999")
    if args.kommentar_freigeben is not None and not 1 <= args.kommentar_freigeben <= 99_999_999:
        ap.error("--kommentar-freigeben außerhalb 1..99999999")
    args.network = NET_ALIASES[args.network.lower()]
    net = NETS[args.network]
    need(net[0].startswith("https://") and ADDR.fullmatch(net[1]) and isinstance(net[2], int) and net[2] > 0, "netz")
    need(ADDR.fullmatch(PAY_TO) and HEX64.fullmatch(PADDED_TO) and HEX64.fullmatch(TRANSFER), "konstanten")
    ensure_dir()
    lock = acquire_lock()
    if lock is None:
        log("Sperre belegt, anderer Lauf aktiv")
        return 0
    try:
        st = load_state()
        if args.kommentar_freigeben is not None:  # Astra R7 P2: Inhaber-Entscheid, kein regulärer Lauf
            key = str(args.kommentar_freigeben)
            rec, eph = st["issues"].get(key) or {}, st.get("ephemer", {}).get(key) or {}
            frei = [f for f in ("abschluss", "widerruf_kommentar") if rec.get(f) == "unklar"] + (["kommentar"] if eph.get("kommentar") == "unklar" else [])
            for f in frei:
                (eph if f == "kommentar" else rec).pop(f, None)
            if frei and not args.dry_run:
                save_state(st, False)
            log(f"#{args.kommentar_freigeben} Kommentar-Freigabe: {', '.join(frei) or 'nichts unklar'} (dry={args.dry_run})")
            return 0
        issues = []
        loaded = list_open_issues() + list_recently_closed(st)
        loaded += pending_from_state(st, {r.get("number") for r in loaded if isinstance(r, dict)})
        for raw in loaded:
            try:
                item = normalize(raw)
            except Exception as exc:  # Astra R3 P2: ein kaputter Eintrag bricht nicht den Lauf
                log(f"Eintrag nicht normalisierbar, übersprungen: {exc}")
                continue
            if item:
                issues.append(item)
        issues.sort(key=lambda i: (i["created"], i["number"]))
        invited = sum(1 for i in st["invites"] if i.get("day") == today())
        log(f"netz={args.network} offen={len(issues)} dry={args.dry_run} hashes={len(st['used_hashes'])} einladungen_heute={invited}")
        if args.once_issue and not any(i["number"] == args.once_issue for i in issues):
            log(f"Issue #{args.once_issue} ist nicht offen")
        for iss, action in build_plan(issues, st):
            if args.once_issue and iss["number"] != args.once_issue:
                continue
            try:
                dispatch(st, net, args.network, iss, action, args.dry_run)
            except Exception as exc:
                log(f"#{iss['number']} Fehler, übersprungen (bleibt offen, falls nicht schon persistiert): {exc}")
        if not args.dry_run and args.once_issue is None:  # Astra R5 P1: Einzel-Lauf verschiebt den Wasserstand nicht
            # Review Grok R2 P2: abgeschlossene ephemer-Einträge nicht mehr offener Issues entfernen (state.json ≤ 5 MB)
            open_nums = {str(i["number"]) for i in issues if not i.get("closed")}
            for key in [k for k, v in st.get("ephemer", {}).items() if v.get("kommentar") == "geschlossen" and k not in open_nums]:
                del st["ephemer"][key]
            st["last_ok"] = int(time.time())
            save_state(st, False)
        log("fertig")
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
