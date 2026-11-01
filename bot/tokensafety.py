"""Keeping scam tokens out of the wallet.

Three layers, all on the side of safety:
  1. A token that "arrives" from its own contract address is the classic spam airdrop: no "Yes, add it" button is offered.
  2. Honeypot check (honeypot.is, a free public service) before a token can be added on an EVM network: a token that
     cannot be sold, or that takes a huge tax on every trade, is refused. The check itself runs by simulating a buy and a sell.
  3. If the check service does not answer, the token is NOT refused (that would block real tokens whenever the service
     is down) but the person sees a warning that it could not be checked.
The answer is read defensively: a missing field is never treated as a verdict.
"""
import logging

import aiohttp

from . import tls

log = logging.getLogger("walletbot.safety")

URL = "https://api.honeypot.is/v2/IsHoneypot"
BLOCK_TAX = 50.0  # percent: a token taking this much on a trade is a trap
WARN_TAX = 10.0


def sent_by_itself(sender: str | None, token: str | None) -> bool:
    return bool(sender and token and str(sender).lower() == str(token).lower())


def _percent(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def judge(data) -> tuple[str | None, list[str]]:
    """(reason to refuse or None, warnings) from honeypot.is's answer."""
    if not isinstance(data, dict):
        return None, ["I could not check this token for scams."]
    result = data.get("honeypotResult") or {}
    sim = data.get("simulationResult") or {}
    if result.get("isHoneypot") is True:
        why = str(result.get("honeypotReason") or "").strip()[:80]
        return "it is a honeypot: you could buy it but not sell it" + (f" ({why})" if why else ""), []
    warnings: list[str] = []
    buy, sell = _percent(sim.get("buyTax")), _percent(sim.get("sellTax"))
    for name, tax in (("buy", buy), ("sell", sell)):
        if tax is not None and tax >= BLOCK_TAX:
            return f"it takes {tax:.0f}% of every {name}", []
    for name, tax in (("buy", buy), ("sell", sell)):
        if tax is not None and tax >= WARN_TAX:
            warnings.append(f"It takes a {tax:.0f}% tax when you {name} it.")
    if "isHoneypot" not in result:
        warnings.append("I could not check this token for scams.")
    return None, warnings


async def check(chain_id: int, contract: str, timeout: float = 12.0) -> tuple[str | None, list[str]]:
    """(reason to refuse or None, warnings). Never raises."""
    try:
        t = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=t, connector=aiohttp.TCPConnector(ssl=tls.context())) as s:
            async with s.get(URL, params={"address": contract, "chainID": str(chain_id)}) as resp:
                if resp.status != 200:
                    return None, ["I could not check this token for scams."]
                return judge(await resp.json(content_type=None))
    except Exception as exc:
        log.warning("Honeypot check unavailable: %s", type(exc).__name__)
        return None, ["I could not check this token for scams."]
