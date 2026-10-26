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
| `checker/kcs402_check.py` (Prüfer, Stdlib, nur lesend außer Einladung) | **nicht freigegeben, aber verifiziert** — Astra R1–R7 (R7 OK-MIT-FIXES, kein P1); danach Kommentar-Gate, Menschen-Gate-Signal und Nachlade-Grenzen in **sieben Grok/vibe-Runden und vier K2-Fremd-Reviews** geprüft, Schlussstand Grok OK · vibe OK · K2 OK, nie ein P1. Mechanik: Zustand je Kommentarart (`post-gestartet → kommentiert → geschlossen`, `unklar` = Menschen-Gate), unsichtbarer Marker nur in eigenen Kommentaren; offene Gates (`unklar`, `widerruf-offen`, Frist-Ablehnung) → Summenzeile, `notify-telegram` (falls vorhanden, Gedächtnis erst nach Versanderfolg, 24 h) und **rc 2**; `gesehen` mit `not_found` > 7 Tage → Ablehnung ohne Hash-Verbrauch als Menschen-Gate; Reorg-Wache 7 Tage; offene `gesehen` zählen gegen die 3er-Obergrenze. **Inhaber-Befehle:** `--kommentar-freigeben <issue>` (nach Prüfung, dass kein Kommentar existiert: neuer Versuch) · `--gate-erledigt <issue> --befund gefunden|nicht-gefunden` (Frist-Ablehnung: erst die Kette nachsehen, bei gefundener Zahlung Zugang von Hand im Ware-Repo vergeben, dann mit Befund als erledigt markieren; Zeitpunkt und Befund bleiben im State). Der Signaltext nennt je Issue die Art des Gates. Offline-Harness `checker/test_kommentar_gate.py` (73 Szenarien, Mutanten rot). Offline-Harness deckt auch die Ketten grant→Kommentar-Gate→Reorg-Wache→Freigabe und excess→Nachversöhnung→Einladung ab. Offen (P3): Wachstum `issues`/`used_hashes` ohne Bereinigung, verwaistes ephemer-`unklar`. Kein weiterer Astra-Lauf (Vorgabe Inhaber). Klon auf K2 ohne Cron, Testnetz-Kreis noch nicht gelaufen |
| `buyer/kcs402_buy.py` (Käufer, eth-account nur Signatur) | **nicht freigegeben** — Sandbox-Approve `eth-account` erteilt 25.10. (Vendor-Freeze `ApeWorX/eth-account@e521f32817fa`, venv `buyer/.venv`, siehe `buyer/requirements.lock`); Wegwerf-Wallet in der K0-Keychain; nie mit Geld gelaufen, Trockenlauf siehe Wiki |

Zuordnungsregel: Käufer eröffnet Issue `claim` → Nummer N → zahlt exakt `basePrice.units + N` Basiseinheiten USDC → setzt Titel `paid:<txhash>`.
Prüfer: Receipt `status 0x1`, kanonischer Block, genau ein USDC-`Transfer` an die Empfangsadresse mit exakt diesem Betrag, ≥ 5 Bestätigungen,
Blockzeit nach `created_at`, Hash einmalig; Einladung nur an den Issue-Autor. Geschlossene `claim`-Issues werden nachversöhnt (Wasserstand),
Widerruf ist ein Menschen-Gate (kein DELETE durch den Prüfer).

Plan, Sicherheitsmodell und alle Astra-Protokolle: knowledge-hub `wiki/projects/krypto-kirchberg-setup.md` · Agent-Team: `wiki/projects/krypto-agent-team.md`.
