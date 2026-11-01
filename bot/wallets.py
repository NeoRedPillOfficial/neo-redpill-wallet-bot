"""Wallet generation, import and export for EVM (incl. Neo X) and Neo N3.

Neo X is EVM-compatible, so one EVM key and address work on Neo X and on
every other EVM chain. Neo N3 uses secp256r1 keys and its own address format.

Private keys are handled as raw 32-byte values; the caller encrypts them
(see bot.crypto) before they touch the database.
"""
import hashlib
import os
from dataclasses import dataclass

import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from eth_account import Account

from . import solana_keys

NEO_N3_ADDRESS_VERSION = 0x35
WIF_PREFIX = 0x80
# Verification script of a single-signature account:
#   PUSHDATA1 0x21 <33-byte pubkey> SYSCALL System.Crypto.CheckSig
_SYSCALL_CHECKSIG = bytes.fromhex("4156e7b327")

# secp256r1 group order; a valid private key is 1 <= k < N
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)


@dataclass(frozen=True)
class KeyPair:
    key_type: str  # 'evm' | 'neo_n3' | 'solana'
    address: str
    private_key: bytes  # raw 32 bytes


def _hash160(data: bytes) -> bytes:
    sha = hashlib.sha256(data).digest()
    try:
        return hashlib.new("ripemd160", sha).digest()
    except ValueError:  # OpenSSL 3 builds may drop ripemd160
        from Crypto.Hash import RIPEMD160

        return RIPEMD160.new(sha).digest()


# ----------------------------------------------------------------- EVM / Neo X
def evm_from_private_key(private_key: bytes) -> KeyPair:
    account = Account.from_key(private_key)
    return KeyPair("evm", account.address, bytes(private_key))


def evm_generate() -> KeyPair:
    return evm_from_private_key(Account.create().key)


def evm_export(kp: KeyPair) -> str:
    return "0x" + kp.private_key.hex()


# --------------------------------------------------------------------- Neo N3
def _neo_public_key(private_key: bytes) -> bytes:
    key = ec.derive_private_key(int.from_bytes(private_key, "big"), ec.SECP256R1())
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    )


def neo_n3_address(private_key: bytes) -> str:
    pub = _neo_public_key(private_key)
    script = bytes([0x0C, 0x21]) + pub + _SYSCALL_CHECKSIG
    script_hash = _hash160(script)
    return base58.b58encode_check(bytes([NEO_N3_ADDRESS_VERSION]) + script_hash).decode()


def neo_n3_from_private_key(private_key: bytes) -> KeyPair:
    k = int.from_bytes(private_key, "big")
    if len(private_key) != 32 or not (1 <= k < _P256_ORDER):
        raise ValueError("Invalid Neo N3 private key")
    return KeyPair("neo_n3", neo_n3_address(private_key), bytes(private_key))


def neo_n3_generate() -> KeyPair:
    while True:
        candidate = os.urandom(32)
        k = int.from_bytes(candidate, "big")
        if 1 <= k < _P256_ORDER:
            return neo_n3_from_private_key(candidate)


def neo_n3_export_wif(kp: KeyPair) -> str:
    return base58.b58encode_check(
        bytes([WIF_PREFIX]) + kp.private_key + b"\x01"
    ).decode()


def neo_n3_private_key_from_wif(wif: str) -> bytes:
    raw = base58.b58decode_check(wif)
    if len(raw) != 34 or raw[0] != WIF_PREFIX or raw[-1] != 0x01:
        raise ValueError("Invalid WIF")
    return raw[1:33]


# ---------------------------------------------------------------------- Solana
def solana_generate() -> KeyPair:
    sk = solana_keys.generate()
    return KeyPair("solana", sk.address, sk.seed)


def solana_from_private_key(seed: bytes) -> KeyPair:
    sk = solana_keys.from_seed(seed)
    return KeyPair("solana", sk.address, sk.seed)


# ------------------------------------------------------------------- Importing
def parse_import(key_type: str, text: str) -> KeyPair:
    """Turn what the user pasted into a KeyPair, or raise ValueError."""
    text = text.strip()
    if key_type == "evm":
        hex_str = text[2:] if text.lower().startswith("0x") else text
        if len(hex_str) != 64:
            raise ValueError("An EVM private key is 64 hex characters.")
        try:
            raw = bytes.fromhex(hex_str)
        except ValueError:
            raise ValueError("That is not valid hex.")
        return evm_from_private_key(raw)
    if key_type == "neo_n3":
        if len(text) == 64:
            try:
                return neo_n3_from_private_key(bytes.fromhex(text))
            except ValueError:
                raise ValueError("That is not a valid Neo N3 private key.")
        try:
            return neo_n3_from_private_key(neo_n3_private_key_from_wif(text))
        except Exception:
            raise ValueError("That is not a valid Neo N3 WIF or hex key.")
    if key_type == "solana":
        sk = solana_keys.parse_import(text)
        return KeyPair("solana", sk.address, sk.seed)
    raise ValueError(f"Unknown key type {key_type}")


def export_text(kp: KeyPair) -> str:
    if kp.key_type == "solana":
        return solana_keys.export_text(solana_keys.from_seed(kp.private_key))
    return evm_export(kp) if kp.key_type == "evm" else neo_n3_export_wif(kp)
