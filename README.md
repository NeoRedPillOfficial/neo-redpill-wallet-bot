# NeoRedPill Wallet

An open-source **Telegram wallet bot** for **Neo N3**, **Neo X**, EVM chains and **Solana**.
Anyone can open a wallet, send and receive tokens, swap, bridge, vote on Neo and claim GAS, tip friends and join
community events without installing an app or browser extension.

Live bot: [@NeoRedPillWalletBot](https://t.me/NeoRedPillWalletBot)

> **Custody note.** This is a custodial convenience wallet: the bot holds the users' keys (encrypted). Read
> [SECURITY.md](SECURITY.md) before running it for real users. It has not been independently audited.

## What it does

**Wallets**
- One Neo N3 wallet and one EVM wallet (works on every EVM network) plus a Solana wallet per user, created on first use.
- Import an existing wallet, export a key (private chat only, protected message with a Copy button, auto-deleted),
  keep several wallets and switch between them.
- Live balances and USD values on the home page, token logos, price charts, full history with pending transactions.
- Add any token by contract address; unsafe or unlisted tokens are flagged.

**Neo N3**
- NEO and GAS balances and prices, sending to addresses or `@usernames`.
- In-bot **governance**: see the candidates, vote, and claim the GAS you earn.
- Token swaps on **Flamingo** and **FTW Swap**.

**Neo X, EVM and Solana**
- Neo X, BNB Chain, Ethereum, Polygon, Arbitrum, Optimism, Base and Avalanche, and any other EVM chain through one
  config line. Solana is optional.
- **Cross-chain bridge** using two providers, LI.FI and Relay, with the best price and lowest gas shown first, EVM to EVM,
  EVM to Solana and Solana to EVM. If an amount is too small the bot says what the minimum is.
- EVM swaps through Uniswap-style V2, V3 and V4 routes.

**Community**
- Tips in groups, rain, quizzes and airdrop campaigns with tasks and rewards.
- An AI assistant: answers questions about the bot and in general, searches the web (with a supported provider),
  reads the group thread it is asked about and can make memes. It never sends funds and never handles keys.

**Operations**
- Daily statistics for the owners (users, money moved, which features are used).
- Encrypted backups (AES-256-GCM, scrypt) and a recovery guide ([RECOVERY_GUIDE.txt](RECOVERY_GUIDE.txt)).
- Per-token send limits, a dry run on the node before every Neo transaction, and "do not send twice" protection when
  a result cannot be confirmed.

## Quick start

Requires Python 3.12 or newer.

```bash
git clone https://github.com/<your-account>/<this-repo>.git
cd <this-repo>
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then edit .env
python -m bot.crypto                                   # prints a new MASTER_KEY: save it somewhere safe
python -m bot.main
```

In `.env` you must set at least `BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)), `MASTER_KEY` and `NEO_N3_RPC`
(a node with the RpcNep17Tracker and ApplicationLogs plugins). Every other network switches on when you give it an RPC
URL. Start with `NETWORK=testnet`, and make your first transfers of each kind tiny.
Step-by-step server instructions are in [SETUP.md](SETUP.md).

Optional keys (leave empty to switch the feature off): `AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` for the AI assistant,
`RELAY_API_KEY` for the Relay bridge, `COINGECKO_API_KEY` for more reliable prices.

## Tests

```bash
pip install pytest pytest-asyncio
pytest
```

## Project layout

| Path | What is in it |
| --- | --- |
| `bot/main.py` | Telegram handlers and menus (aiogram 3) |
| `bot/service.py`, `bot/db.py` | Wallet logic and the database (SQLAlchemy async; SQLite or PostgreSQL) |
| `bot/crypto.py`, `bot/backup.py` | Key encryption and encrypted backups |
| `bot/neo_rpc.py`, `bot/neo_send.py`, `bot/neovote.py` | Neo N3 reading, sending, voting and GAS claims |
| `bot/flamingo.py`, `bot/ftwswap.py` | Neo N3 swaps |
| `bot/evm_rpc.py`, `bot/evm_send.py`, `bot/dex*.py` | EVM reading, sending and swaps |
| `bot/solana_*.py` | Solana keys, RPC, sending and deposit watching |
| `bot/bridge.py`, `bot/xbridge.py` | Cross-chain bridge (LI.FI and Relay) |
| `bot/prices.py`, `bot/pricechart.py` | Live prices and charts |
| `bot/assistant.py`, `bot/memes.py` | AI assistant and meme maker |
| `bot/campaigns.py`, `bot/tipping.py`, `bot/rainstore.py` | Airdrops, tips and rain |
| `tests/` | Automated tests |

## Contributing

Issues and pull requests are welcome. Please run `pytest` before you open a pull request, and never include keys,
tokens or database files in an issue or a commit.

## License

[MIT](LICENSE)
