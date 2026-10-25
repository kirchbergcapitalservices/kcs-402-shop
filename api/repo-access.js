// Schaufenster: antwortet immer mit HTTP 402 und den Zahlungsbedingungen.
// Keine Abhängigkeiten, keine Geheimnisse, kein Zustand.
const TERMS = {
  sepolia: {
    network: "eip155:84532",
    networkName: "Base Sepolia (Testnetz)",
    usdc: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    price: "0.10",
    rpcHint: "https://sepolia.base.org",
  },
  mainnet: {
    network: "eip155:8453",
    networkName: "Base",
    usdc: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    price: "1.00",
    rpcHint: "https://mainnet.base.org",
  },
};
const ACTIVE = process.env.KCS402_NETWORK === "mainnet" ? "mainnet" : "sepolia";
const PAY_TO = process.env.KCS402_PAY_TO || "0xAbe4eeCe3Eae3A2Fe8C705d80ab3D23491CC2715";
const SHOP_REPO = process.env.KCS402_SHOP_REPO || "kirchbergcapitalservices/kcs-402-shop";

module.exports = (req, res) => {
  const t = TERMS[ACTIVE];
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.status(402).send(
    JSON.stringify(
      {
        status: 402,
        message: "Payment Required",
        product: "Read-only access to the private repo kcs-402-ware-test",
        issuer: "Kirchberg Capital & Services GmbH",
        payment: {
          scheme: "erc20-transfer",
          network: t.network,
          networkName: t.networkName,
          token: "USDC",
          tokenContract: t.usdc,
          decimals: 6,
          amount: t.price,
          payTo: PAY_TO,
          rpcHint: t.rpcHint,
        },
        then: {
          action: "open_issue",
          repo: SHOP_REPO,
          title: "paid:<transaction-hash>",
          note: "The issue author (your GitHub account) will be invited within ~5 minutes after 5 confirmations.",
        },
        version: "kcs-402/0.1",
      },
      null,
      2,
    ),
  );
};
