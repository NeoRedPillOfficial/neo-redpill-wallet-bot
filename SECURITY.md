# Security

## How keys are handled (read this before you run the bot for real users)

NeoRedPill Wallet is a **custodial** Telegram wallet. The bot creates and holds each user's private keys.

- Every key is encrypted with AES-256-GCM before it is stored. The encryption key is `MASTER_KEY`, which lives in the
  server's `.env` file and is never stored in the database or in backups.
- Backups are encrypted separately (AES-256-GCM, key derived with scrypt from a backup password) and never contain `MASTER_KEY`.
- Anyone who controls the server (or can read both `.env` and the database) can decrypt every wallet. Protect the server,
  limit who has access, and keep `MASTER_KEY` backed up somewhere separate from the database.
- Keys are only ever shown in a private chat. An exported key is sent as a protected message, hidden behind a spoiler,
  with a Copy button, and is deleted after two minutes.
- The AI assistant can read a user's own balances in a private chat. It cannot send funds, cannot read or reveal keys, and
  does not show other people's wallets.

This project has **not been independently audited**. Treat it as a convenience wallet: tell users to keep only small
amounts in it, and make your first transfers of every kind tiny.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Contact the maintainers privately (see the project's
Telegram support contacts in the bot's Help menu) and give them time to fix it before you publish details.
