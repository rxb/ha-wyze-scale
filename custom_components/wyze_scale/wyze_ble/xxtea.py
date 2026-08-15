"""Minimal XXTEA (Corrected Block TEA) operating on independent 8-byte blocks.

The Wyze scale applies XXTEA per 8-byte block (two little-endian uint32
words, n=2, 32 mixing rounds) with a 16-byte key — effectively ECB with a
fixed block size. See PROTOCOL.md §3.3.
"""

from __future__ import annotations

import struct

_DELTA = 0x9E3779B9
_MASK = 0xFFFFFFFF


def _mx(total: int, y: int, z: int, p: int, e: int, k: tuple[int, ...]) -> int:
    return (
        (((z >> 5) ^ ((y << 2) & _MASK)) + ((y >> 3) ^ ((z << 4) & _MASK)))
        ^ ((total ^ y) + (k[(p & 3) ^ e] ^ z))
    ) & _MASK


def _key_words(key: bytes) -> tuple[int, ...]:
    if len(key) != 16:
        raise ValueError("XXTEA key must be exactly 16 bytes")
    return struct.unpack("<4I", key)


def encrypt_block(block: bytes, key: bytes) -> bytes:
    """Encrypt a single 8-byte block."""
    if len(block) != 8:
        raise ValueError("XXTEA block must be exactly 8 bytes")
    k = _key_words(key)
    v = list(struct.unpack("<2I", block))
    n = len(v)
    rounds = 6 + 52 // n
    total = 0
    z = v[-1]
    for _ in range(rounds):
        total = (total + _DELTA) & _MASK
        e = (total >> 2) & 3
        for p in range(n):
            y = v[(p + 1) % n]
            v[p] = (v[p] + _mx(total, y, z, p, e, k)) & _MASK
            z = v[p]
    return struct.pack("<2I", *v)


def decrypt_block(block: bytes, key: bytes) -> bytes:
    """Decrypt a single 8-byte block."""
    if len(block) != 8:
        raise ValueError("XXTEA block must be exactly 8 bytes")
    k = _key_words(key)
    v = list(struct.unpack("<2I", block))
    n = len(v)
    rounds = 6 + 52 // n
    total = (rounds * _DELTA) & _MASK
    y = v[0]
    while total:
        e = (total >> 2) & 3
        for p in range(n - 1, -1, -1):
            z = v[(p - 1) % n]
            v[p] = (v[p] - _mx(total, y, z, p, e, k)) & _MASK
            y = v[p]
        total = (total - _DELTA) & _MASK
    return struct.pack("<2I", *v)


def encrypt_payload(payload: bytes, key: bytes) -> bytes:
    """Zero-pad to a multiple of 8 and encrypt each block independently."""
    padded = payload + b"\x00" * (-len(payload) % 8)
    return b"".join(
        encrypt_block(padded[i : i + 8], key) for i in range(0, len(padded), 8)
    )


def decrypt_payload(data: bytes, length: int, key: bytes) -> bytes:
    """Decrypt whole 8-byte blocks and truncate to the declared length."""
    usable = len(data) - len(data) % 8
    plain = b"".join(
        decrypt_block(data[i : i + 8], key) for i in range(0, usable, 8)
    )
    return plain[:length]
