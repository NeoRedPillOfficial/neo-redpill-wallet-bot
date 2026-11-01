"""Solana keys, addresses and transactions, with no network access.

Only depends on `cryptography`. Covers:
  * base58 (Solana addresses and signatures are base58 text)
  * ed25519 wallets (a 32-byte seed; the address is the base58 public key)
  * program derived addresses (needed for associated token accounts)
  * building and signing a legacy transaction

A wallet's secret is stored as the 32-byte seed. Exporting gives the 64-byte
secret (seed + public key) in base58, which is the format Phantom and Solflare
import.
"""
import hashlib
import json
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ----------------------------------------------------------------------- base58
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _ALPHABET[rem] + out
    zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * zeros + out


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError("not base58")
        number = number * 58 + _INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * zeros + body


# --------------------------------------------------------------------- programs
SYSTEM_PROGRAM = bytes(32)  # 11111111111111111111111111111111
TOKEN_PROGRAM = b58decode("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = b58decode("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = b58decode("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
METADATA_PROGRAM = b58decode("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

LAMPORTS_PER_SIGNATURE = 5000
TOKEN_ACCOUNT_SIZE = 165


# ----------------------------------------------------------------------- addresses
def is_valid_address(text: str) -> bool:
    """A Solana address is the base58 text of 32 bytes."""
    text = (text or "").strip()
    if not 32 <= len(text) <= 44:
        return False
    try:
        return len(b58decode(text)) == 32
    except ValueError:
        return False


# ------------------------------------------------------------------------- wallets
@dataclass(frozen=True)
class SolKeyPair:
    address: str
    seed: bytes  # the 32-byte secret

    @property
    def public_key(self) -> bytes:
        return b58decode(self.address)


def _public_from_seed(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def from_seed(seed: bytes) -> SolKeyPair:
    if len(seed) != 32:
        raise ValueError("A Solana secret is 32 bytes.")
    return SolKeyPair(b58encode(_public_from_seed(seed)), bytes(seed))


def generate() -> SolKeyPair:
    return from_seed(os.urandom(32))


def export_text(kp: SolKeyPair) -> str:
    """Base58 of the 64-byte secret (seed + public key): what Phantom/Solflare import."""
    return b58encode(kp.seed + kp.public_key)


def parse_import(text: str) -> SolKeyPair:
    """Accepts a base58 secret (64 bytes, or a bare 32-byte seed), 64 hex characters,
    or the JSON array of 64 numbers that the Solana command line tool writes."""
    text = (text or "").strip()
    raw: bytes | None = None
    if text.startswith("["):
        try:
            values = json.loads(text)
            raw = bytes(values)
        except (ValueError, TypeError):
            raise ValueError("That JSON key is not valid.")
    elif len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        raw = bytes.fromhex(text)
    else:
        try:
            raw = b58decode(text)
        except ValueError:
            raise ValueError("A Solana private key is base58 text (what Phantom shows), 64 hex characters, or a JSON list.")
    if len(raw) == 64:
        kp = from_seed(raw[:32])
        if kp.public_key != raw[32:]:
            raise ValueError("That key is not consistent (its second half is not the matching public key).")
        return kp
    if len(raw) == 32:
        return from_seed(raw)
    raise ValueError("A Solana private key is 32 or 64 bytes long.")


# ------------------------------------------------- program derived addresses (PDAs)
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def is_on_curve(point: bytes) -> bool:
    """Whether 32 bytes decode to a point on the ed25519 curve (as Solana checks it)."""
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    if u == 0:
        return True
    ratio = (u * pow(v, _P - 2, _P)) % _P
    return pow(ratio, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: bytes) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program_id + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return digest, bump
    raise ValueError("no program address found")


def associated_token_address(owner: bytes, mint: bytes, token_program: bytes = TOKEN_PROGRAM) -> bytes:
    return find_program_address([owner, token_program, mint], ASSOCIATED_TOKEN_PROGRAM)[0]


def metadata_address(mint: bytes) -> bytes:
    return find_program_address([b"metadata", METADATA_PROGRAM, mint], METADATA_PROGRAM)[0]


def parse_metadata(data: bytes) -> tuple[str, str]:
    """(name, symbol) from a Metaplex metadata account."""
    offset = 1 + 32 + 32  # key, update authority, mint
    fields = []
    for _ in range(2):
        (length,) = struct.unpack_from("<I", data, offset)
        if length > 200:
            raise ValueError("unreadable metadata")
        raw = data[offset + 4 : offset + 4 + length]
        if len(raw) != length:
            raise ValueError("unreadable metadata")
        fields.append(raw.rstrip(b"\x00").decode("utf-8", errors="strict").strip())
        offset += 4 + length
    return fields[0], fields[1]


# -------------------------------------------------------------------- instructions
@dataclass(frozen=True)
class Instruction:
    program_id: bytes
    accounts: tuple  # of (pubkey: bytes, is_signer: bool, is_writable: bool)
    data: bytes


def system_transfer(sender: bytes, recipient: bytes, lamports: int) -> Instruction:
    return Instruction(
        SYSTEM_PROGRAM,
        ((sender, True, True), (recipient, False, True)),
        struct.pack("<IQ", 2, lamports),
    )


def create_associated_token_account_idempotent(
    payer: bytes, owner: bytes, mint: bytes, token_program: bytes = TOKEN_PROGRAM
) -> Instruction:
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        (
            (payer, True, True),
            (associated_token_address(owner, mint, token_program), False, True),
            (owner, False, False),
            (mint, False, False),
            (SYSTEM_PROGRAM, False, False),
            (token_program, False, False),
        ),
        bytes([1]),
    )


def token_transfer_checked(
    source: bytes, mint: bytes, destination: bytes, authority: bytes, amount: int, decimals: int,
    token_program: bytes = TOKEN_PROGRAM,
) -> Instruction:
    return Instruction(
        token_program,
        ((source, False, True), (mint, False, False), (destination, False, True), (authority, True, False)),
        struct.pack("<BQB", 12, amount, decimals),
    )


# ---------------------------------------------------------------------- transactions
def compact_u16(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def compile_message(payer: bytes, instructions: list[Instruction], recent_blockhash: bytes) -> bytes:
    """Serialize a legacy-format transaction message. The payer is the first account and signs."""
    if len(recent_blockhash) != 32:
        raise ValueError("bad blockhash")
    flags: dict[bytes, list[bool]] = {payer: [True, True]}  # key -> [is_signer, is_writable]
    order = [payer]

    def touch(key: bytes, signer: bool, writable: bool) -> None:
        if key not in flags:
            flags[key] = [False, False]
            order.append(key)
        flags[key][0] |= signer
        flags[key][1] |= writable

    for ins in instructions:
        for key, signer, writable in ins.accounts:
            touch(key, signer, writable)
        touch(ins.program_id, False, False)

    def group(want_signer: bool, want_writable: bool) -> list[bytes]:
        return [k for k in order if flags[k][0] == want_signer and flags[k][1] == want_writable]

    signed_writable, signed_readonly = group(True, True), group(True, False)
    unsigned_writable, unsigned_readonly = group(False, True), group(False, False)
    keys = signed_writable + signed_readonly + unsigned_writable + unsigned_readonly
    index = {k: i for i, k in enumerate(keys)}

    out = bytearray([len(signed_writable) + len(signed_readonly), len(signed_readonly), len(unsigned_readonly)])
    out += compact_u16(len(keys))
    for key in keys:
        out += key
    out += recent_blockhash
    out += compact_u16(len(instructions))
    for ins in instructions:
        out.append(index[ins.program_id])
        out += compact_u16(len(ins.accounts))
        out += bytes(index[key] for key, _, _ in ins.accounts)
        out += compact_u16(len(ins.data))
        out += ins.data
    return bytes(out)


def sign_transaction(seed: bytes, message: bytes) -> bytes:
    """The wire format: signature count, signatures, message. One signer (the payer)."""
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(message)
    return compact_u16(1) + signature + message


def first_signature(transaction: bytes) -> str:
    """The base58 transaction id (the first signature) of a serialized transaction."""
    return b58encode(transaction[1:65])


# ------------------------------------------------------------------ version-0 messages (address lookup tables)
ADDRESS_LOOKUP_TABLE_PROGRAM_B58 = "AddressLookupTab1e1111111111111111111111111"


def parse_lookup_table(data: bytes) -> list[bytes]:
    """The addresses stored in an address lookup table account (a 56-byte header, then 32-byte addresses)."""
    if len(data) < 56 or (len(data) - 56) % 32:
        raise ValueError("unreadable lookup table")
    return [data[i:i + 32] for i in range(56, len(data), 32)]


def compile_v0_message(payer: bytes, instructions: list[Instruction], recent_blockhash: bytes,
                       tables: list[tuple[bytes, list[bytes]]]) -> bytes:
    """Serialize a version-0 message. `tables` is [(table address, its addresses)]. The payer is first and signs."""
    if len(recent_blockhash) != 32:
        raise ValueError("bad blockhash")
    flags: dict[bytes, list[bool]] = {payer: [True, True]}
    order = [payer]
    programs: set[bytes] = set()

    def touch(key: bytes, signer: bool, writable: bool) -> None:
        if key not in flags:
            flags[key] = [False, False]
            order.append(key)
        flags[key][0] |= signer
        flags[key][1] |= writable

    for ins in instructions:
        for key, signer, writable in ins.accounts:
            touch(key, signer, writable)
        touch(ins.program_id, False, False)
        programs.add(ins.program_id)

    lookups: list[tuple[bytes, list[int], list[int]]] = []  # (table, writable indexes, readonly indexes)
    looked: dict[bytes, tuple[int, bool]] = {}  # key -> (table order, writable)
    writable_keys: list[bytes] = []
    readonly_keys: list[bytes] = []
    for t_order, (table_key, addresses) in enumerate(tables):
        position = {a: i for i, a in enumerate(addresses)}
        wr_idx: list[int] = []
        ro_idx: list[int] = []
        wr_keys: list[bytes] = []
        ro_keys: list[bytes] = []
        for key in order:
            if key in looked or key not in position or flags[key][0] or key in programs:
                continue
            looked[key] = (t_order, flags[key][1])
            if flags[key][1]:
                wr_idx.append(position[key])
                wr_keys.append(key)
            else:
                ro_idx.append(position[key])
                ro_keys.append(key)
        if wr_idx or ro_idx:
            lookups.append((table_key, wr_idx, ro_idx))
            writable_keys += wr_keys
            readonly_keys += ro_keys

    def group(want_signer: bool, want_writable: bool) -> list[bytes]:
        return [k for k in order if k not in looked and flags[k][0] == want_signer and flags[k][1] == want_writable]

    signed_writable, signed_readonly = group(True, True), group(True, False)
    unsigned_writable, unsigned_readonly = group(False, True), group(False, False)
    static = signed_writable + signed_readonly + unsigned_writable + unsigned_readonly
    full = static + writable_keys + readonly_keys
    index = {k: i for i, k in enumerate(full)}
    if len(full) > 256:
        raise ValueError("too many accounts")

    out = bytearray([0x80, len(signed_writable) + len(signed_readonly), len(signed_readonly), len(unsigned_readonly)])
    out += compact_u16(len(static))
    for key in static:
        out += key
    out += recent_blockhash
    out += compact_u16(len(instructions))
    for ins in instructions:
        out.append(index[ins.program_id])
        out += compact_u16(len(ins.accounts))
        out += bytes(index[key] for key, _, _ in ins.accounts)
        out += compact_u16(len(ins.data))
        out += ins.data
    out += compact_u16(len(lookups))
    for table_key, wr_idx, ro_idx in lookups:
        out += table_key
        out += compact_u16(len(wr_idx)) + bytes(wr_idx)
        out += compact_u16(len(ro_idx)) + bytes(ro_idx)
    return bytes(out)
