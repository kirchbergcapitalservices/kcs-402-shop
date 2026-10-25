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
| `checker/kcs402_check.py` (Prüfer, Stdlib, nur lesend außer Einladung) | **nicht freigegeben** — Astra Runde 5 BLOCK, Patch Runde 6 eingebaut, Verifikation ausstehend; läuft nirgends als Cron |
| `buyer/kcs402_buy.py` (Käufer, eth-account nur Signatur) | **nicht freigegeben** — braucht Sandbox-Approve für `eth-account` und das Käufer-Wallet; nie mit Geld gelaufen |

Zuordnungsregel: Käufer eröffnet Issue `claim` → Nummer N → zahlt exakt `basePrice.units + N` Basiseinheiten USDC → setzt Titel `paid:<txhash>`.
Prüfer: Receipt `status 0x1`, kanonischer Block, genau ein USDC-`Transfer` an die Empfangsadresse mit exakt diesem Betrag, ≥ 5 Bestätigungen,
Blockzeit nach `created_at`, Hash einmalig; Einladung nur an den Issue-Autor. Geschlossene `claim`-Issues werden nachversöhnt (Wasserstand),
Widerruf ist ein Menschen-Gate (kein DELETE durch den Prüfer).

Plan, Sicherheitsmodell und alle Astra-Protokolle: knowledge-hub `wiki/projects/krypto-kirchberg-setup.md` · Agent-Team: `wiki/projects/krypto-agent-team.md`.
