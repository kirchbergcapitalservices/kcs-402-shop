#!/usr/bin/env python3
# Offline-Harness für das Kommentar-Gate des Prüfers (Astra R7 P2 + Review Grok/vibe 25.10.2026): kein Netz, kein gh.
# Aufruf: python3 -I checker/test_kommentar_gate.py [pfad/zu/kcs402_check.py]   — Erwartung: "ERGEBNIS: ALLE PASS", rc 0.
# Null-Mutante: gegen den Stand vor dem Patch muss das Harness rot sein (AttributeError marker/comment_once).
import importlib.util, pathlib, subprocess, sys, tempfile, json
target = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).with_name("kcs402_check.py"))
spec = importlib.util.spec_from_file_location("chk", target); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tmp = pathlib.Path(tempfile.mkdtemp()); m.STATE_DIR = tmp
logs = []; m.log = lambda msg: logs.append(msg)

class FakeGH:
    def __init__(self): self.calls=[]; self.comments={}; self.issue_state={}; self.post_mode="ok"
    def __call__(self, path, method=None, fields=None, allow_404=False, timeout=60):
        self.calls.append((method or "GET", path))
        if path == "user":
            return {"login": "kirchbergcapital"}
        if path.endswith("/issues"):
            return []
        num = int(path.split("/issues/")[1].split("/")[0])
        if path.endswith("/comments"):
            if method == "POST":
                if self.post_mode == "ok":
                    self.comments.setdefault(num, []).append({"id": len(self.calls), "body": fields["body"], "user": {"login": "kirchbergcapital"}}); return {"id": 1}
                if self.post_mode == "lost":   # Kommentar steht, Antwort verloren
                    self.comments.setdefault(num, []).append({"id": len(self.calls), "body": fields["body"], "user": {"login": "kirchbergcapital"}})
                    raise subprocess.TimeoutExpired("gh", 60)
                raise RuntimeError("gh POST rc=1: 502")   # fail: nichts gespeichert
            page = int(fields["page"]); items = self.comments.get(num, [])
            return items[(page-1)*100: page*100]
        if method == "PATCH":
            self.issue_state[num] = "closed"; return None
        return {"state": self.issue_state.get(num, "open"), "number": num}
    def posts(self): return [c for c in self.calls if c[0]=="POST"]
    def patches(self): return [c for c in self.calls if c[0]=="PATCH"]

def fresh(num):
    st = m.empty_state(); st["issues"][str(num)] = {"state": "eingeladen", "login": "alice", "tx": "0x"+"ab"*32}
    return st

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS" if cond else "FAIL"), name, extra); ok = ok and cond

# S1 normal: genau ein POST mit Marker, PATCH, Zustand geschlossen; zweiter Aufruf ohne Außenaktion
g = FakeGH(); m.gh = g; st = fresh(1)
m.close_with_notice(st, 1, "Zahlung geprüft.", False)
check("S1 ein POST mit Marker", len(g.posts())==1 and m.marker("abschluss",1) in g.comments[1][0]["body"])
check("S1 PATCH + geschlossen", len(g.patches())==1 and st["issues"]["1"]["abschluss"]=="geschlossen" and st["issues"]["1"]["notified"] is True)
n = len(g.calls); m.close_with_notice(st, 1, "Zahlung geprüft.", False)
check("S1 Wiederanlauf (offen gelistet, real zu): ein GET, kein POST/PATCH", len(g.calls)==n+1 and g.calls[-1][0]=="GET", str(g.calls[n:]))
n=len(g.calls); m.close_with_notice(st, 1, "Zahlung geprüft.", False, listed_open=False)
check("S1b Wiederanlauf (geschlossen gelistet): kein Aufruf", len(g.calls)==n)

# S2 POST ok, Antwort verloren → unklar, kein PATCH; Folgelauf findet Marker → kommentiert → PATCH; kein 2. POST
g = FakeGH(); g.post_mode="lost"; m.gh = g; st = fresh(2)
m.close_with_notice(st, 2, "Zahlung geprüft.", False)
check("S2 unklar nach verlorener Antwort, kein PATCH", st["issues"]["2"]["abschluss"]=="unklar" and not g.patches() and any("MENSCHEN-GATE" in l for l in logs))
g.post_mode="ok"; m.close_with_notice(st, 2, "Zahlung geprüft.", False)
check("S2 Folgelauf: Marker gefunden, kein 2. POST, PATCH", len(g.posts())==1 and len(g.patches())==1 and st["issues"]["2"]["abschluss"]=="geschlossen")

# S3 POST scheitert hart (nichts gespeichert) → unklar; Folgelauf: kein POST; nach Freigabe: ein neuer POST
g = FakeGH(); g.post_mode="fail"; m.gh = g; st = fresh(3)
m.close_with_notice(st, 3, "x", False)
check("S3 unklar nach Fehler", st["issues"]["3"]["abschluss"]=="unklar" and len(g.posts())==1)
g.post_mode="ok"; m.close_with_notice(st, 3, "x", False)
check("S3 Folgelauf ohne Marker: kein automatischer POST", len(g.posts())==1 and st["issues"]["3"]["abschluss"]=="unklar" and not g.patches())
del st["issues"]["3"]["abschluss"]   # = --kommentar-freigeben
m.close_with_notice(st, 3, "x", False)
check("S3 nach Freigabe: genau ein neuer POST, geschlossen", len(g.posts())==2 and st["issues"]["3"]["abschluss"]=="geschlossen")

# S4 Absturz nach Sicherung 'post-gestartet', vor POST → kein POST, unklar
g = FakeGH(); m.gh = g; st = fresh(4); st["issues"]["4"]["abschluss"]="post-gestartet"
m.close_with_notice(st, 4, "x", False)
check("S4 post-gestartet ohne Marker → unklar, kein POST", not g.posts() and st["issues"]["4"]["abschluss"]=="unklar")

# S5 Altformat notified=True → kein POST, aber Schließen
g = FakeGH(); m.gh = g; st = fresh(5); st["issues"]["5"]["notified"]=True
m.close_with_notice(st, 5, "x", False)
check("S5 Altformat: kein POST, PATCH, geschlossen", not g.posts() and len(g.patches())==1 and st["issues"]["5"]["abschluss"]=="geschlossen")

# S6 revoke zweimal → genau ein Widerruf-Kommentar mit Marker, Hash gebunden, kein DELETE/PATCH
g = FakeGH(); m.gh = g; st = fresh(6)
m.revoke(st, 6, "alice", "0x"+"ab"*32, "low_conf", False); m.revoke(st, 6, "alice", "0x"+"ab"*32, "low_conf", False)
check("S6 revoke: ein POST mit widerruf-Marker, kein PATCH", len(g.posts())==1 and m.marker("widerruf",6) in g.comments[6][0]["body"] and not g.patches() and st["issues"]["6"]["widerruf_kommentar"]=="kommentiert")
check("S6 Zustand widerruf-offen + Hash gebunden", st["issues"]["6"]["state"]=="widerruf-offen" and st["used_hashes"]["0x"+"ab"*32]["state"]=="widerruf-offen")

# S7 close_ephemeral: Marker vorhanden → kein POST; ohne → ein POST mit Marker
g = FakeGH(); m.gh = g; g.comments[7]=[{"id":1,"body":"alt "+m.marker("ephemer",7),"user":{"login":"kirchbergcapital"}}]; st = m.empty_state()
m.close_ephemeral(st, 7, "Geschlossen: foo", False)
check("S7a ephemer mit Marker: kein POST, PATCH", not g.posts() and len(g.patches())==1)
g = FakeGH(); m.gh = g; st = m.empty_state(); m.close_ephemeral(st, 8, "Geschlossen: foo", False)
check("S7b ephemer ohne Marker: ein POST mit Marker", len(g.posts())==1 and m.marker("ephemer",8) in g.comments[8][0]["body"] and st["ephemer"]["8"]["kommentar"]=="geschlossen")
n=len(g.calls); m.close_ephemeral(st, 8, "Geschlossen: foo", False)
check("S7c ephemer geschlossen, real zu: ein GET, kein POST/PATCH", len(g.calls)==n+1 and g.calls[-1][0]=="GET")

# S8 Trockenlauf: keine gh-Aufrufe
g = FakeGH(); m.gh = g; st = fresh(9); m.close_with_notice(st, 9, "x", True); m.revoke(st, 9, "alice", "0x"+"ab"*32, "r", True)
check("S8 dry-run: null gh-Aufrufe", not g.calls)

# S9 Zustands-Roundtrip save/load
g = FakeGH(); m.gh = g; st = fresh(10); st["issues"]["10"]["abschluss"]="unklar"; st["issues"]["10"]["widerruf_kommentar"]="kommentiert"
m.save_state(st, False); st2 = m.load_state()
check("S9 Roundtrip Kommentarfelder", st2["issues"]["10"].get("abschluss")=="unklar" and st2["issues"]["10"].get("widerruf_kommentar")=="kommentiert")
st["issues"]["10"]["abschluss"]="quatsch"; m.save_state(st, False); st3 = m.load_state()
check("S9 ungültiger Wert wird verworfen", "abschluss" not in st3["issues"]["10"])

# S10 gh-Wert-Regel: Marker-Body ohne Zeilenumbruch, ≤ 500 Zeichen (sonst need() im echten gh)
body = f"Zahlung geprüft (0.100030 USDC). @{'a'*39} hat Lesezugang zu {m.WARE}. {m.marker('abschluss', 99999999)}"
check("S10 Body-Länge/Zeilen", len(body) <= 500 and "\n" not in body, str(len(body)))

# S12 fremder Marker-Text zählt nicht: eigener Kommentar wird genau einmal gepostet, Issue geschlossen
g = FakeGH(); m.gh = g; st = fresh(12); g.comments[12]=[{"id":1,"body":"böse "+m.marker("abschluss",12),"user":{"login":"mallory"}}]
m.close_with_notice(st, 12, "Zahlung geprüft.", False)
check("S12 fremder Marker ignoriert: ein eigener POST", len(g.posts())==1 and st["issues"]["12"]["abschluss"]=="geschlossen")
g = FakeGH(); m.gh = g; st = m.empty_state(); g.comments[13]=[{"id":1,"body":m.marker("ephemer",13),"user":{"login":"mallory"}}]
m.close_ephemeral(st, 13, "Geschlossen: foo", False)
check("S12b fremder ephemer-Marker ignoriert", len(g.posts())==1)

# S13 build_plan: gate-offenes abgelehnt/eingeladen zählt nicht gegen MAX_OPEN; jüngeres paid wird verarbeitet
st = m.empty_state(); st["issues"]["20"]={"state":"abgelehnt","login":"bob","tx":"","abschluss":"unklar"}
st["issues"]["21"]={"state":"eingeladen","login":"bob","tx":"0x"+"cd"*32,"abschluss":"unklar"}
mk=lambda n,kind,tx=None: {"number":n,"created":float(n),"login":"bob","kind":kind,"tx":tx,"closed":False}
issues=[mk(20,"claim"), mk(21,"claim"), mk(22,"claim"), mk(23,"claim"), mk(24,"paid","0x"+"ef"*32)]
plan={iss["number"]:act for iss,act in m.build_plan(issues, st)}
check("S13 abgelehnt/eingeladen → process, nicht gezählt", plan.get(20)=="process" and plan.get(21)=="process", str(plan))
check("S13 jüngeres paid nicht excess (2 echte claims + paid = 3)", plan.get(24)=="process" and plan.get(22)=="process" and plan.get(23)=="process", str(plan))
issues2=[mk(22,"claim"), mk(23,"claim"), mk(25,"claim"), mk(26,"paid","0x"+"ef"*32)]
plan2={iss["number"]:act for iss,act in m.build_plan(issues2, m.empty_state())}
check("S13b Obergrenze greift weiter: 4. offenes Issue ohne Bindung = excess", plan2.get(26)=="excess", str(plan2))

# S14 close_ephemeral: POST ok, Antwort verloren → unklar, kein zweiter POST; Folgelauf findet Marker → geschlossen
g = FakeGH(); g.post_mode="lost"; m.gh = g; st = m.empty_state(); m.close_ephemeral(st, 14, "Geschlossen: foo", False)
check("S14 ephemer verlorene Antwort → unklar, kein PATCH", st["ephemer"]["14"]["kommentar"]=="unklar" and not g.patches())
g.post_mode="ok"; m.close_ephemeral(st, 14, "Geschlossen: foo", False)
check("S14 Folgelauf: Marker gefunden, kein 2. POST, geschlossen", len(g.posts())==1 and len(g.patches())==1 and st["ephemer"]["14"]["kommentar"]=="geschlossen")
g.post_mode="fail"; g2=FakeGH(); g2.post_mode="fail"; m.gh=g2; st=m.empty_state(); m.close_ephemeral(st, 15, "x", False); m.close_ephemeral(st, 15, "x", False)
check("S14b ephemer harter Fehler: ein POST, dann Gate, kein Dauer-Kommentieren", len(g2.posts())==1 and st["ephemer"]["15"]["kommentar"]=="unklar")

# S15 Roundtrip ephemer + comment_once ohne Vorgangszustand wird abgewiesen
m.gh = FakeGH(); st=m.empty_state(); st["ephemer"]["30"]={"kommentar":"unklar"}; m.save_state(st, False); st2=m.load_state()
check("S15 Roundtrip ephemer", st2["ephemer"].get("30",{}).get("kommentar")=="unklar")
try:
    st=m.empty_state(); m.comment_once(st, 31, "abschluss", "x"); check("S15b abschluss ohne Vorgang abgewiesen", False)
except Exception as e:
    check("S15b abschluss ohne Vorgang abgewiesen", "kommentar-ohne-vorgang" in str(e), str(e)[:60])

# S16 wiedereröffnetes Terminal-Issue: nur PATCH, kein POST; nicht offen gelistet → gar kein Aufruf
g = FakeGH(); m.gh = g; st = fresh(16); st["issues"]["16"]["abschluss"]="geschlossen"
m.close_with_notice(st, 16, "x", False)
check("S16 wiedereröffnet: GET+PATCH, kein POST", not g.posts() and len(g.patches())==1 and len(g.calls)==2)
n=len(g.calls); m.close_with_notice(st, 16, "x", False, listed_open=False)
check("S16b nicht offen gelistet: kein Aufruf", len(g.calls)==n)
g = FakeGH(); m.gh = g; st = m.empty_state(); st["ephemer"]["17"]={"kommentar":"geschlossen"}; m.close_ephemeral(st, 17, "x", False)
check("S16c ephemer wiedereröffnet: nur PATCH", not g.posts() and len(g.patches())==1)

# S17 fremd geschlossen vor Kommentar → Terminalzustand persistiert, kein POST; Folgelauf ohne GET (reorg-Pfad: closed)
g = FakeGH(); m.gh = g; g.issue_state[18]="closed"; st = fresh(18); m.close_with_notice(st, 18, "x", False)
check("S17 fremd geschlossen: kein POST, Terminalzustand", not g.posts() and st["issues"]["18"]["abschluss"]=="geschlossen")
n=len(g.calls); m.close_with_notice(st, 18, "x", False, listed_open=False); check("S17b Folgelauf ohne GET", len(g.calls)==n)
g = FakeGH(); m.gh = g; g.issue_state[19]="closed"; st = m.empty_state(); m.close_ephemeral(st, 19, "x", False)
check("S17c ephemer fremd geschlossen: Terminalzustand", not g.posts() and st["ephemer"]["19"]["kommentar"]=="geschlossen")

# S18 main() bereinigt geschlossene ephemer-Einträge, behält unklar; Wasserstand gesetzt
g = FakeGH(); m.gh = g; st = m.empty_state(); st["ephemer"]={"40":{"kommentar":"geschlossen"},"41":{"kommentar":"unklar"}}; m.save_state(st, False)
sys.argv=["kcs402_check.py","--network","sepolia"]; rc=m.main(); st2=m.load_state()
check("S18 ephemer bereinigt (geschlossen weg, unklar bleibt), rc 2 wegen #41-Gate, last_ok", rc==2 and "40" not in st2["ephemer"] and st2["ephemer"]["41"]["kommentar"]=="unklar" and st2["last_ok"]>0, str(st2["ephemer"]))
check("S18b main ohne Issues: keine Schreibaufrufe", not g.posts() and not g.patches())

# S19 own_login scheitert (gh user 401) → Abbruch VOR post-gestartet, kein POST, Zustand None
class GH401(FakeGH):
    def __call__(self, path, method=None, fields=None, allow_404=False, timeout=60):
        if path == "user": raise RuntimeError("gh GET user rc=1: HTTP 401")
        return super().__call__(path, method, fields, allow_404, timeout)
m._OWN_LOGIN = None; g = GH401(); m.gh = g; st = fresh(19)
try:
    m.close_with_notice(st, 19, "x", False); check("S19 own_login-Fehler wirft", False)
except RuntimeError as e:
    check("S19 own_login-Fehler: kein POST, kein post-gestartet", not g.posts() and st["issues"]["19"].get("abschluss") is None, str(e)[:40])
m._OWN_LOGIN = None

# S20 Freigabe über main --kommentar-freigeben (echter CLI-Pfad) löscht nur unklar, startet keinen Lauf
g = FakeGH(); m.gh = g; st = m.empty_state(); st["issues"]["50"]={"state":"eingeladen","login":"bob","tx":"0x"+"aa"*32,"abschluss":"unklar"}; st["ephemer"]["51"]={"kommentar":"unklar"}; m.save_state(st, False)
sys.argv=["kcs402_check.py","--kommentar-freigeben","50"]; rc=m.main(); st2=m.load_state()
check("S20 Freigabe #50: abschluss weg, ephemer #51 bleibt, keine gh-Aufrufe, rc 0", rc==0 and "abschluss" not in st2["issues"]["50"] and st2["ephemer"]["51"]["kommentar"]=="unklar" and not g.calls)

# S21 Probe B (K2): Widerruf-Kommentar setzt notified nicht → späterer Abschluss-Kommentar wird noch gepostet
g = FakeGH(); m.gh = g; st = fresh(21); m.revoke(st, 21, "alice", "0x"+"ab"*32, "r", False)
st["issues"]["21"]["state"]="eingeladen"; m.close_with_notice(st, 21, "Zugang besteht.", False)
check("S21 widerruf + abschluss = zwei verschiedene Kommentare", len(g.posts())==2 and st["issues"]["21"]["abschluss"]=="geschlossen" and st["issues"]["21"].get("notified") is True)

# S22 Menschen-Gate-Signal: main endet mit rc 2 und ruft signal_mensch; ohne Gate rc 0
g = FakeGH(); m.gh = g; st = m.empty_state(); st["issues"]["60"]={"state":"eingeladen","login":"bob","tx":"0x"+"bb"*32,"abschluss":"unklar"}; m.save_state(st, False)
signale=[]; m.signal_mensch = lambda t: (signale.append(t), True)[1]; sys.argv=["kcs402_check.py","--network","sepolia"]; rc=m.main()
check("S22 Gate offen → rc 2 + Signal mit Issue-Nummer", rc==2 and len(signale)==1 and "#60" in signale[0])
signale.clear(); sys.argv=["kcs402_check.py","--network","sepolia","--dry-run"]; rc=m.main()
check("S22b Trockenlauf: rc 2, aber kein Signal", rc==2 and not signale)
st = m.empty_state(); m.save_state(st, False); rc=m.main(); check("S22c ohne Gate rc 0", rc==0 and not signale)

# S23 close_with_notice ohne Vorgangszustand wird abgewiesen (K2 P3)
try:
    g = FakeGH(); m.gh = g; st = m.empty_state(); m.close_with_notice(st, 70, "x", False); check("S23 ohne Vorgang abgewiesen", False)
except Exception as e:
    check("S23 ohne Vorgang abgewiesen, kein gh-Aufruf, kein leerer Datensatz (K2 N1)", "abschluss-ohne-vorgang" in str(e) and not g.calls and "70" not in st["issues"])

# S24 post-gestartet zählt als offenes Gate (Grok R4 P2)
st = m.empty_state(); st["issues"]["80"]={"state":"eingeladen","login":"bob","tx":"0x"+"cc"*32,"abschluss":"post-gestartet"}; st["ephemer"]["81"]={"kommentar":"post-gestartet"}
check("S24 post-gestartet in offene_gates", m.offene_gates(st)==[80,81], str(m.offene_gates(st)))

# S25 Dedupe: gleiches Gate → ein Signal in zwei Läufen; geänderte Menge → neues Signal; Zeitpunkt im State
g = FakeGH(); m.gh = g; st = m.empty_state(); st["issues"]["90"]={"state":"eingeladen","login":"bob","tx":"0x"+"dd"*32,"abschluss":"unklar"}; m.save_state(st, False)
signale=[]; m.signal_mensch = lambda t: (signale.append(t), True)[1]; sys.argv=["kcs402_check.py","--network","sepolia"]
rc1=m.main(); rc2=m.main()
check("S25 zwei Läufe, gleiches Gate: rc 2 beide, genau ein Signal", rc1==2 and rc2==2 and len(signale)==1, str(len(signale)))
st=m.load_state(); check("S25b Signal im State persistiert", st["signal"]["gates"]==[90] and st["signal"]["at"]>0)
st["ephemer"]["91"]={"kommentar":"unklar"}; m.save_state(st, False); rc3=m.main()
check("S25c geänderte Menge → neues Signal", rc3==2 and len(signale)==2 and "#91" in signale[-1])
st=m.load_state(); st["signal"]["at"]=0; m.save_state(st, False); m.main()
check("S25d nach Ablauf der Frist erneut", len(signale)==3)

# S26 Signal-Gedächtnis erst nach Versanderfolg (Grok R5 P2)
g = FakeGH(); m.gh = g; st = m.empty_state(); st["issues"]["100"]={"state":"eingeladen","login":"bob","tx":"0x"+"ee"*32,"abschluss":"unklar"}; m.save_state(st, False)
versuche=[]; m.signal_mensch = lambda t: (versuche.append(t), False)[1]; sys.argv=["kcs402_check.py","--network","sepolia"]
rc=m.main(); rc=m.main(); st2=m.load_state()
check("S26 Versand scheitert: kein Gedächtnis, jeder Lauf versucht erneut, rc 2", rc==2 and len(versuche)==2 and "signal" not in st2)
m.signal_mensch = lambda t: (versuche.append(t), True)[1]; rc=m.main(); st2=m.load_state()
check("S26b Versand gelingt: Gedächtnis gesetzt", st2.get("signal",{}).get("gates")==[100])

# S27 leere Menge löscht das Signal-Gedächtnis
st2["issues"]["100"]["abschluss"]="geschlossen"; m.save_state(st2, False); rc=m.main(); st3=m.load_state()
check("S27 kein Gate mehr: rc 0, signal gelöscht", rc==0 and "signal" not in st3)

# S28 Freigabe setzt auch post-gestartet zurück
st = m.empty_state(); st["issues"]["110"]={"state":"abgelehnt","login":"bob","tx":"","abschluss":"post-gestartet"}; st["ephemer"]["111"]={"kommentar":"post-gestartet"}; m.save_state(st, False)
sys.argv=["kcs402_check.py","--kommentar-freigeben","110"]; m.main(); sys.argv=["kcs402_check.py","--kommentar-freigeben","111"]; m.main(); st2=m.load_state()
check("S28 Freigabe löscht post-gestartet (issues + ephemer)", "abschluss" not in st2["issues"]["110"] and "kommentar" not in st2["ephemer"].get("111", {}))

# S29 gesehen + not_found: innerhalb der Frist wartet, nach der Frist abgelehnt ohne Hash-Verbrauch
import time as _t
g = FakeGH(); m.gh = g; st = m.empty_state(); net=("https://x", "0x"+"01"*20, 100000)
m.verify_payment = lambda net, tx, num, created: ("pending", "not_found", "Transaktion noch nicht gefunden", 0)
iss = {"number": 120, "created": 1.0, "login": "bob", "kind": "paid", "tx": "0x"+"ff"*32, "closed": False}
m.handle_paid(st, net, "sepolia", iss, False)
check("S29 erster Kontakt: gesehen mit seit, wartet", st["issues"]["120"]["state"]=="gesehen" and st["issues"]["120"]["seit"]>0 and not g.posts())
st["issues"]["120"]["seit"] = int(_t.time()) - m.GESEHEN_MAX_S - 1; m.handle_paid(st, net, "sepolia", iss, False)
check("S29b nach Frist: abgelehnt, Kommentar, Hash nicht verbraucht", st["issues"]["120"]["state"]=="abgelehnt" and len(g.posts())==1 and "0x"+"ff"*32 not in st["used_hashes"])
g = FakeGH(); m.gh = g; st = m.empty_state(); st["issues"]["121"]={"state":"gesehen","login":"bob","tx":"0x"+"ab"*32,"seit": int(_t.time()) - m.GESEHEN_MAX_S - 1}
m.verify_payment = lambda net, tx, num, created: ("pending", "low_conf", "nur 2 Bestätigungen", 0)
m.handle_paid(st, net, "sepolia", {"number":121,"created":1.0,"login":"bob","kind":"paid","tx":"0x"+"ab"*32,"closed":False}, False)
check("S29c low_conf nach Frist bleibt gesehen (Tx existiert)", st["issues"]["121"]["state"]=="gesehen" and not g.posts())

# S30 pending_from_state: alte eingeladen nicht mehr nachgeladen, frische schon; Altbestand bekommt seit
g = FakeGH(); m.gh = g; st = m.empty_state()
st["issues"]["130"]={"state":"eingeladen","login":"bob","tx":"0x"+"01"*32,"seit": int(_t.time()) - m.REORG_WACHE_S - 1}
st["issues"]["131"]={"state":"eingeladen","login":"bob","tx":"0x"+"02"*32,"seit": int(_t.time())}
st["issues"]["132"]={"state":"eingeladen","login":"bob","tx":"0x"+"03"*32}
out = m.pending_from_state(st, set()); nums = sorted(r["number"] for r in out)
check("S30 Reorg-Wache: 130 beendet, 131 + 132 (Altbestand, seit gesetzt) geladen", nums==[131,132] and st["issues"]["132"].get("seit",0)>0, str(nums))

# S31 gesehen zählt gegen die Autoren-Obergrenze: 3 gesehen + neues paid = excess; geschlossenes gesehen zählt nicht
st = m.empty_state()
for n in (140,141,142): st["issues"][str(n)]={"state":"gesehen","login":"bob","tx":"0x"+("%02x"%n)*32}
mk=lambda n,kind,tx=None,closed=False: {"number":n,"created":float(n),"login":"bob","kind":kind,"tx":tx,"closed":closed}
plan={i["number"]:a for i,a in m.build_plan([mk(140,"paid","0x"+"8c"*32), mk(141,"paid","0x"+"8d"*32), mk(142,"paid","0x"+"8e"*32), mk(143,"paid","0x"+"8f"*32)], st)}
check("S31 drei gesehen + 4. paid → excess", plan.get(143)=="excess" and all(plan.get(n)=="process" for n in (140,141,142)), str(plan))

# S11 Marker-Suche erkennt Pagination-Grenze als Fehler (nicht als 'kein Marker')
g = FakeGH(); m.gh = g; g.comments[11] = [{"id": i, "body": "x"} for i in range(100*m.MAX_PAGES + 1)]
try:
    m.has_marker(11, "abschluss"); check("S11 Pagination-Grenze → Fehler", False)
except RuntimeError as e:
    check("S11 Pagination-Grenze → Fehler", "Grenze" in str(e))
print("ERGEBNIS:", "ALLE PASS" if ok else "FEHLER"); sys.exit(0 if ok else 1)
