"""Reading incoming transfers out of a Solana transaction (jsonParsed form). Pure functions."""
from dataclasses import dataclass

NATIVE = "native"


@dataclass(frozen=True)
class Deposit:
    asset: str  # 'native' for SOL, otherwise the token mint address
    amount: int  # smallest unit
    sender: str | None


def _pubkey(key) -> str:
    return key["pubkey"] if isinstance(key, dict) else key


def parse_deposits(tx: dict, wallet: str, tracked_mints: set[str]) -> list[Deposit]:
    """What `wallet` received in this transaction: SOL, and tokens whose mint is in `tracked_mints`.

    A transaction the wallet itself signed is its own outgoing transfer and is skipped
    (those are recorded by the send flow). A failed transaction moves nothing.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    raw_keys = tx["transaction"]["message"]["accountKeys"]
    pubkeys = [_pubkey(k) for k in raw_keys]
    signers = [_pubkey(k) for k in raw_keys if isinstance(k, dict) and k.get("signer")]
    if wallet in signers:
        return []

    found: list[Deposit] = []
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if wallet in pubkeys and len(pre) == len(post) == len(pubkeys):
        deltas = [b - a for a, b in zip(pre, post)]
        gained = deltas[pubkeys.index(wallet)]
        if gained > 0:
            sender = min(signers, key=lambda s: deltas[pubkeys.index(s)], default=None)
            found.append(Deposit(NATIVE, gained, sender))

    def amounts(balances, owner_only: bool):
        out: dict[tuple, int] = {}
        for entry in balances or []:
            if owner_only and entry.get("owner") != wallet:
                continue
            out[(entry["accountIndex"], entry["mint"], entry.get("owner"))] = int(entry["uiTokenAmount"]["amount"])
        return out

    mine_pre = {(i, m): v for (i, m, _), v in amounts(meta.get("preTokenBalances"), True).items()}
    mine_post = amounts(meta.get("postTokenBalances"), True)
    all_pre, all_post = amounts(meta.get("preTokenBalances"), False), amounts(meta.get("postTokenBalances"), False)
    for (index, mint, _owner), after in mine_post.items():
        if mint not in tracked_mints:
            continue
        gained = after - mine_pre.get((index, mint), 0)
        if gained <= 0:
            continue
        losers = [
            (all_post.get(k, 0) - before, k[2])
            for k, before in all_pre.items()
            if k[1] == mint and k[2] != wallet and all_post.get(k, 0) < before
        ]
        sender = min(losers)[1] if losers else None
        found.append(Deposit(mint, gained, sender))
    return found
