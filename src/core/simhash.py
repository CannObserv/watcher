"""64-bit SimHash fingerprinting for fuzzy text similarity."""

import hashlib
import re


def simhash(text: str, hashbits: int = 64) -> int:
    """Compute a 64-bit SimHash fingerprint from text.

    Tokenizes on word boundaries after whitespace normalization.
    Returns 0 for empty text.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    tokens = re.findall(r"\w+", normalized.lower())
    if not tokens:
        return 0

    v = [0] * hashbits
    for token in tokens:
        h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
        for i in range(hashbits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1

    fingerprint = 0
    for i in range(hashbits):
        if v[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two integers."""
    return bin(a ^ b).count("1")


def similarity(a: int, b: int, hashbits: int = 64) -> float:
    """Return similarity score between 0.0 (opposite) and 1.0 (identical)."""
    return 1.0 - (hamming_distance(a, b) / hashbits)
