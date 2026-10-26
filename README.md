# kcs-402-shop

Prototyp der Kirchberg Capital & Services GmbH: **Zugang zu einem Repo gegen USDC, vollautomatisch, ohne Fremdcode.**

Ablauf (HTTP 402 „Payment Required“, selbst gebaut):

1. `GET https://<shop>/api/repo-access` antwortet mit **402** und den Zahlungsbedingungen (Preis, Netzwerk, USDC-Vertrag, Empfangsadresse).
2. Der Käufer (Mensch oder Agent) überweist den Betrag in USDC an die Empfangsadresse.
3. Der Käufer eröffnet in **diesem** Repo ein Issue mit dem Titel `paid:<Transaktions-Hash>`.
4. Ein Prüfer (Cron, nur lesend) verifiziert die Zahlung auf der Blockchain und lädt den **Issue-Autor** als Read-only-Collaborator in das Ware-Repo ein. Das Issue wird mit Kommentar geschlossen.

Es gibt keinen Server mit Schlüssel. Die Identität des Käufers ist sein GitHub-Konto (Issue-Autor), die Zahlung ist öffentlich auf der Chain prüfbar.

## Status (25.10.2026)

| Teil | Stand |
|---|---|
| `api/repo-access.js` (Schaufenster, 0 Abhängigkeiten) | **live**, Testnetz Base Sepolia, Schema `kcs-402/0.2`; von Astra in 5 Runden als konsistent bestätigt |
| `checker/kcs402_check.py` (Prüfer, Stdlib, nur lesend außer Einladung) | **nicht freigegeben, aber verifiziert** — Astra Runde 7 (Delta Patch R7 `88e9029`) **OK-MIT-FIXES, kein P1**; der dort offene P2 (Kommentar-Wiederholung) ist gepatcht: Kommentar-Gate mit eigenem Zustand je Art (`post-gestartet → kommentiert → geschlossen`, `unklar` = Menschen-Gate `--kommentar-freigeben`), unsichtbarer Marker nur in eigenen Kommentaren, `close_ephemeral` im selben Automaten, entschiedene Issues zählen nicht gegen die 3er-Obergrenze. Prüfungen des Gates: **Grok + vibe (Mistral) fünf Runden** (R1 BLOCK/BLOCK → R2 OK-MIT-FIXES/OK → R3 OK/OK → nach K2-Befunden R4 OK-MIT-FIXES/OK → R5 OK-MIT-FIXES/OK) und **K2-Fremd-Review zwei Runden** (OK-MIT-FIXES, Vorreview-Befunde erledigt) — nie ein P1. Menschen-Gate signalisiert: offene `unklar`/`widerruf-offen` → Summenzeile, `notify-telegram` (falls vorhanden, Dedupe 24 h) und **rc 2**. Offline-Harness `checker/test_kommentar_gate.py` (51 Szenarien, Null-Mutanten rot). Offen (P2, kein Geldpfad): Signal-Zeitpunkt erst nach Versanderfolg setzen, Signal-Gedächtnis bei leerer Menge löschen, `--kommentar-freigeben` auch für `post-gestartet`; Bestand: unbegrenztes Nachladen von `gesehen`/`eingeladen`. Kein weiterer Astra-Lauf (Vorgabe Inhaber). Klon auf K2 ohne Cron, Testnetz-Kreis noch nicht gelaufen |
| `buyer/kcs402_buy.py` (Käufer, eth-account nur Signatur) | **nicht freigegeben** — Sandbox-Approve `eth-account` erteilt 25.10. (Vendor-Freeze `ApeWorX/eth-account@e521f32817fa`, venv `buyer/.venv`, siehe `buyer/requirements.lock`); Wegwerf-Wallet in der K0-Keychain; nie mit Geld gelaufen, Trockenlauf siehe Wiki |

Zuordnungsregel: Käufer eröffnet Issue `claim` → Nummer N → zahlt exakt `basePrice.units + N` Basiseinheiten USDC → setzt Titel `paid:<txhash>`.
Prüfer: Receipt `status 0x1`, kanonischer Block, genau ein USDC-`Transfer` an die Empfangsadresse mit exakt diesem Betrag, ≥ 5 Bestätigungen,
Blockzeit nach `created_at`, Hash einmalig; Einladung nur an den Issue-Autor. Geschlossene `claim`-Issues werden nachversöhnt (Wasserstand),
Widerruf ist ein Menschen-Gate (kein DELETE durch den Prüfer).

Plan, Sicherheitsmodell und alle Astra-Protokolle: knowledge-hub `wiki/projects/krypto-kirchberg-setup.md` · Agent-Team: `wiki/projects/krypto-agent-team.md`.
