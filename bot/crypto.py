"""Encryption of private keys at rest (AES-256-GCM).

Each key is encrypted with the master key and bound to the owner's wallet
address as associated data, so a ciphertext copied onto another row will not
decrypt.

Run `python -m bot.crypto` to generate a new MASTER_KEY.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_SIZE = 12


def generate_master_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _aes(master_key_b64: str) -> AESGCM:
    key = base64.b64decode(master_key_b64)
    if len(key) != 32:
        raise ValueError("MASTER_KEY must decode to exactly 32 bytes")
    return AESGCM(key)


def encrypt_key(master_key_b64: str, secret: bytes, address: str) -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    return nonce + _aes(master_key_b64).encrypt(nonce, secret, address.encode())


def decrypt_key(master_key_b64: str, blob: bytes, address: str) -> bytes:
    nonce, ciphertext = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    return _aes(master_key_b64).decrypt(nonce, ciphertext, address.encode())


if __name__ == "__main__":
    print(generate_master_key())
