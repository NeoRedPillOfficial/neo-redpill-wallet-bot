# Go live: step by step (Ubuntu server)

You do NOT need to run the tests. Everything below is the real bot.

## 1. Get the three things
- **Bot token**: in Telegram, open @BotFather, send `/newbot`, copy the token.
  Also send `/setcommands`, pick your bot, and paste:
  `start - Open your wallet` and `tip - Tip people in a group`
- **RPC addresses** (put in `.env` below):
  - `NEO_X_RPC`: mainnet `https://mainnet-1.rpc.banelabs.org` (testnet: `https://neoxt4seed1.ngd.network`)
  - `NEO_N3_RPC`: a Neo N3 node WITH the RpcNep17Tracker and ApplicationLogs plugins (your own node, or a
    provider that confirms those plugins). Without them balances show "unavailable".
  - `BSC_RPC`: a BNB Smart Chain provider that allows `eth_getLogs` (a free key from Ankr, QuickNode, etc.).
    Leave empty to switch BSC off.
  - More EVM networks work the same way, each switched on by its own RPC line: `ETH_RPC` (Ethereum), `POLYGON_RPC`,
    `ARBITRUM_RPC`, `OPTIMISM_RPC`, `BASE_RPC`, `AVALANCHE_RPC`. Any other EVM chain: `EXTRA_EVM_CHAINS`
    (see `.env.example`). Leave a line empty to keep that network off.
  - `SOLANA_RPC` switches on Solana: `https://api.mainnet-beta.solana.com` works for trying (rate limited), a free
    Helius/QuickNode key is better. With `NETWORK=testnet` use a devnet URL. Not live-tested: send tiny amounts first.

## 2. Install on the server
```bash
sudo apt update && sudo apt install -y python3 python3-venv unzip
sudo useradd -r -m walletbot
sudo mkdir -p /opt/neo-wallet-bot && sudo chown walletbot /opt/neo-wallet-bot
# upload neo-wallet-bot.zip to the server, then:
sudo -u walletbot unzip neo-wallet-bot.zip -d /tmp/nwb && sudo -u walletbot cp -r /tmp/nwb/neo-wallet-bot/. /opt/neo-wallet-bot/
cd /opt/neo-wallet-bot
sudo -u walletbot python3 -m venv .venv
sudo -u walletbot .venv/bin/pip install -r requirements.txt
sudo -u walletbot .venv/bin/python -m bot.crypto      # prints a new MASTER_KEY: SAVE IT SOMEWHERE SAFE
sudo -u walletbot cp .env.example .env && sudo -u walletbot nano .env
sudo chmod 600 .env
```
In `.env` set `BOT_TOKEN`, `MASTER_KEY`, the RPC addresses, and `NETWORK=mainnet` (use `testnet` only if
you point the RPCs at testnet nodes). For a real database use PostgreSQL in `DATABASE_URL`; SQLite works to start.

**MASTER_KEY**: back it up separately from the server. If it is lost, every user wallet is unrecoverable.

## 3. Run it forever
```bash
sudo cp deploy/walletbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now walletbot
sudo journalctl -u walletbot -f        # watch the log
```
In the log you should see `Network ... ready` lines and `Token NRP: verified`, `Token FRANK: verified`.
If you see `DISABLED` or `unreadable`, send me that log line.

## 4. First use
Open the bot, send /start, deposit a TINY amount, then try: send, tip in a group, bridge. The bot has not been
run against live networks by me, so the first transfers of each kind are the real test. If anything fails,
send me the message the bot showed and the last lines of the log.
