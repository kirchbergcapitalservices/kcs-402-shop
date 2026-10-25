# kcs-402-shop

Prototyp der Kirchberg Capital & Services GmbH: **Zugang zu einem Repo gegen USDC, vollautomatisch, ohne Fremdcode.**

Ablauf (HTTP 402 „Payment Required“, selbst gebaut):

1. `GET https://<shop>/api/repo-access` antwortet mit **402** und den Zahlungsbedingungen (Preis, Netzwerk, USDC-Vertrag, Empfangsadresse).
2. Der Käufer (Mensch oder Agent) überweist den Betrag in USDC an die Empfangsadresse.
3. Der Käufer eröffnet in **diesem** Repo ein Issue mit dem Titel `paid:<Transaktions-Hash>`.
4. Ein Prüfer (Cron, nur lesend) verifiziert die Zahlung auf der Blockchain und lädt den **Issue-Autor** als Read-only-Collaborator in das Ware-Repo ein. Das Issue wird mit Kommentar geschlossen.

Es gibt keinen Server mit Schlüssel. Die Identität des Käufers ist sein GitHub-Konto (Issue-Autor), die Zahlung ist öffentlich auf der Chain prüfbar.

Stand: Testnetz (Base Sepolia). Plan und Sicherheitsmodell: knowledge-hub `wiki/projects/krypto-kirchberg-setup.md`.
