"use strict";

/**
 * Schaufenster für kcs-402/0.2.
 * GET /api/repo-access antwortet mit HTTP 402 und den Zahlungsbedingungen.
 * Node auf Vercel, keine Abhängigkeiten, keine Schlüssel, keine Personendaten.
 *
 * Preis, Empfänger, Token und Repos sind fest verdrahtet.
 * Die Anfrage darf keinen davon überschreiben.
 * KCS402_NETWORK wählt nur das Netz: "base-sepolia" (Standard) oder "base".
 */

const SCHEMA = "kcs-402/0.2";
const VERSION = "0.2";

const SHOP_REPO = "kirchbergcapitalservices/kcs-402-shop";
const WARE_REPO = "kirchbergcapitalservices/kcs-402-ware-test";

const PAY_TO = "0xabe4eece3eae3a2fe8c705d80ab3d23491cc2715";
const PAY_TO_NAME = "kirchberg.wallet";
// topics[2] des Transfer-Logs: Empfänger links mit Nullen auf 32 Byte aufgefüllt.
const PAY_TO_TOPIC =
  "0x000000000000000000000000abe4eece3eae3a2fe8c705d80ab3d23491cc2715";

const TOKEN = "USDC";
const DECIMALS = 6;
// Festpreis je Netz (K0-Fix 25.10.2026, Astra P2-5): Testnetz 0,10 USDC, Base 1,00 USDC. 1 Basiseinheit = 1 Mikro-USDC.

const CONFIRMATIONS = 5;
const OPEN_ISSUES_PER_AUTHOR = 3;
const INVITES_PER_DAY = 10;

// keccak256("Transfer(address,address,uint256)")
const TRANSFER_TOPIC =
  "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
// selector transfer(address,uint256)
const TRANSFER_SELECTOR = "0xa9059cbb";

function amountFormula(net) {
  const u = BigInt(net.priceUnits);
  return (
    "amountUnits = basePrice.units + issueNumber " +
    "(ganze Zahl, kein Gleitkomma; 1 Einheit = 1 Mikro-USDC = 10^-6 USDC; " +
    "Beispiel N=42 → " + (u + 42n).toString() + " Einheiten)"
  );
}

const CLAIM_TITLE = "claim";
const PAID_TITLE_PATTERN = "^paid:0x[0-9a-f]{64}$";

const NETWORKS = Object.freeze({
  "base-sepolia": Object.freeze({
    network: "base-sepolia",
    networkName: "Base Sepolia",
    chainId: 84532,
    rpc: "https://sepolia.base.org",
    tokenContract: "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
    priceUnits: "100000",
    priceDecimal: "0.10",
  }),
  base: Object.freeze({
    network: "base",
    networkName: "Base",
    chainId: 8453,
    rpc: "https://mainnet.base.org",
    tokenContract: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    priceUnits: "1000000",
    priceDecimal: "1.00",
  }),
});

const NETWORK_ALIASES = Object.freeze({
  "base-sepolia": "base-sepolia",
  sepolia: "base-sepolia",
  base: "base",
  mainnet: "base",
  "base-mainnet": "base",
});

/**
 * Leerer Wert bleibt auf dem Testnetz. Ein unbekannter Wert ist ein Fehler,
 * damit niemand gegen das falsche Netz zahlt.
 */
function netzWaehlen(raw) {
  const text = String(raw ?? "").trim().toLowerCase();
  if (text === "") {
    return { net: NETWORKS["base-sepolia"], defaulted: true };
  }
  const id = NETWORK_ALIASES[text];
  if (!id) return null;
  return { net: NETWORKS[id], defaulted: false };
}

function schritte(net) {
  return [
    {
      step: 1,
      name: "claim",
      repo: SHOP_REPO,
      title: CLAIM_TITLE,
      instruction:
        "Lege zuerst ein Issue im Shop-Repo an, bevor USDC das Wallet verlässt. " +
        "Titel exakt claim, ohne Prefix, Suffix oder Leerzeichen. " +
        "Lies number als ganze Zahl N und created_at als UTC. " +
        "Dieselbe Kaufabsicht erzeugt nur dieses eine Issue. " +
        "Höchstens 3 offene Issues je Autor; jedes weitere wird sofort geschlossen. " +
        "Titel, die weder claim noch paid:<hash> sind, werden kommentiert und geschlossen.",
    },
    {
      step: 2,
      name: "betrag",
      formula: "amountUnits = " + net.priceUnits + " + N",
      instruction:
        "Rechne amountUnits als ganze Zahl. " +
        "N Mikro-USDC kommen auf den Festpreis, weil 6 Dezimalstellen eine Mikro-USDC zu einer Basiseinheit machen. " +
        "Kein Aufschlag, kein Rabatt, keine Rundung über Gleitkomma. " +
        "Beispiel: N=42 ergibt " + (BigInt(net.priceUnits) + 42n).toString() + " Einheiten.",
    },
    {
      step: 3,
      name: "transfer",
      chainId: net.chainId,
      rpc: net.rpc,
      tokenContract: net.tokenContract,
      method: "transfer(address,uint256)",
      selector: TRANSFER_SELECTOR,
      to: PAY_TO,
      amount: "amountUnits",
      instruction:
        "Sende genau eine ERC-20-transfer-Transaktion auf diesem chainId an den USDC-Vertrag. " +
        "Argument 1 ist payTo, Argument 2 ist amountUnits als uint256. " +
        "Vor dem Broadcast persistent festhalten: Zustand reserviert, Nonce, signierte Rohtransaktion und Transaktionshash. " +
        "Nach einem Absturz denselben Hash und dieselbe Nonce abgleichen und diese Issue nicht ein zweites Mal bezahlen. " +
        "Eine bereits gesendete Rohtransaktion nicht durch eine neue Zahlung ersetzen.",
    },
    {
      step: 4,
      name: "warten",
      confirmations: CONFIRMATIONS,
      instruction:
        "Den Titel claim stehen lassen, bis die Quittung status 0x1 hat, " +
        "der Blockhash der Quittung zum Blockhash von eth_getBlockByNumber passt " +
        "und latest abzüglich blockNumber mindestens 5 ist. " +
        "Der Block-Zeitstempel in Sekunden muss strikt nach created_at des Issues liegen. " +
        "Fehlt die Quittung noch oder antwortet die RPC nicht, nicht erneut zahlen.",
    },
    {
      step: 5,
      name: "paid",
      repo: SHOP_REPO,
      titlePattern: PAID_TITLE_PATTERN,
      instruction:
        "Setze danach den Titel desselben Issues auf paid:<txhash>. " +
        "Der Hash ist 0x plus 64 Hex-Zeichen in Kleinbuchstaben. " +
        "Kein zweites Issue für denselben Hash öffnen und den Body nicht als Betragsquelle verwenden.",
    },
    {
      step: 6,
      name: "einladung",
      wareRepo: WARE_REPO,
      permission: "pull",
      instruction:
        "Der Prüfer lädt nur den Autor dieses Issues nur-lesend ins Ware-Repo ein. " +
        "Dafür müssen Betrag, Zeitstempel, genau ein Transfer-Log und ein noch freier Hash zusammenpassen. " +
        "Tageslimit sind 10 Einladungen. Ein reines claim-Issue bleibt offen und wird nicht eingeladen.",
    },
  ];
}

function angebot(net, defaulted) {
  return {
    schema: SCHEMA,
    version: VERSION,
    error: "payment_required",
    network: net.network,
    networkName: net.networkName,
    chainId: net.chainId,
    rpc: net.rpc,
    networkDefaulted: defaulted,
    token: TOKEN,
    tokenContract: net.tokenContract,
    decimals: DECIMALS,
    basePrice: {
      units: net.priceUnits,
      decimal: net.priceDecimal,
    },
    amountFormula: amountFormula(net),
    payTo: PAY_TO,
    payToName: PAY_TO_NAME,
    payToTopic: PAY_TO_TOPIC,
    shopRepo: SHOP_REPO,
    wareRepo: WARE_REPO,
    confirmations: CONFIRMATIONS,
    transferTopic: TRANSFER_TOPIC,
    binding:
      "Sollbetrag ist der Festpreis plus die Issue-Nummer in Mikro-USDC. " +
      "Der Block liegt zeitlich nach created_at, und jeder Transaktionshash gilt nur einmal. " +
      "Eingeladen wird der Issue-Autor.",
    acceptance: {
      receiptStatus: "0x1",
      canonicalBlock:
        "blockHash der Quittung gleich dem Hash aus eth_getBlockByNumber der blockNumber",
      transferLogs: 1,
      transferTopic0: TRANSFER_TOPIC,
      tokenAddress: net.tokenContract,
      recipientTopic2: PAY_TO_TOPIC,
      amountUnits: "basePrice.units + issueNumber",
      confirmations: "latestBlock - blockNumber >= 5",
      timestamp: "block.timestamp > issue.created_at (beide UTC-Sekunden, strikt größer)",
      hashUnused: true,
      title: "paid:<txhash kleingeschrieben>",
      retryWhile: [
        "RPC-Fehler oder Timeout",
        "Quittung noch nicht vorhanden",
        "weniger als 5 Bestätigungen",
        "Titel ist noch exakt claim",
      ],
      rejectWhen: [
        "Betrag ungleich basePrice.units + N",
        "Block-Zeitstempel nicht strikt nach created_at",
        "status ungleich 0x1",
        "nicht genau ein Transfer-Log",
        "Log-Adresse ungleich tokenContract oder topics[2] ungleich payTo",
        "Hash bereits verbraucht",
      ],
    },
    limits: {
      openIssuesPerAuthor: OPEN_ISSUES_PER_AUTHOR,
      invitesPerDay: INVITES_PER_DAY,
      foreignTitle: "weder claim noch paid:<hash> → Kommentar und schließen",
    },
    steps: schritte(net),
  };
}

function sende(res, status, body) {
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("X-Payment-Schema", SCHEMA);
  res.status(status).json(body);
}

module.exports = function repoAccess(req, res) {
  if (!req || req.method !== "GET") {
    res.setHeader("Allow", "GET");
    sende(res, 405, {
      schema: SCHEMA,
      version: VERSION,
      error: "method_not_allowed",
    });
    return;
  }

  const gewählt = netzWaehlen(process.env.KCS402_NETWORK);
  if (!gewählt) {
    sende(res, 500, {
      schema: SCHEMA,
      version: VERSION,
      error: "unknown_network",
      hint: "KCS402_NETWORK ist base-sepolia oder base",
    });
    return;
  }

  sende(res, 402, angebot(gewählt.net, gewählt.defaulted));
};
