"""Tests for SimHash fingerprinting and Hamming distance."""

from src.core.simhash import hamming_distance, simhash


class TestSimHash:
    def test_identical_text_same_hash(self):
        text = "the quick brown fox jumps over the lazy dog"
        assert simhash(text) == simhash(text)

    def test_similar_text_close_hashes(self):
        a = "the quick brown fox jumps over the lazy dog"
        b = "the quick brown fox leaps over the lazy dog"
        dist = hamming_distance(simhash(a), simhash(b))
        assert dist <= 10

    def test_different_text_far_hashes(self):
        a = "the quick brown fox jumps over the lazy dog"
        b = "completely unrelated content about quantum physics and mathematics"
        dist = hamming_distance(simhash(a), simhash(b))
        assert dist > 10

    def test_empty_text_returns_zero(self):
        assert simhash("") == 0

    def test_returns_64_bit_integer(self):
        result = simhash("hello world")
        assert isinstance(result, int)
        assert 0 <= result < (1 << 64)

    def test_whitespace_normalized(self):
        a = "hello   world\n\tfoo"
        b = "hello world foo"
        assert simhash(a) == simhash(b)


class TestHammingDistance:
    def test_identical_is_zero(self):
        assert hamming_distance(0b1010, 0b1010) == 0

    def test_all_bits_different(self):
        assert hamming_distance(0b0000, 0b1111) == 4

    def test_one_bit_different(self):
        assert hamming_distance(0b1000, 0b1001) == 1

    def test_commutative(self):
        a, b = 0xDEADBEEF, 0xCAFEBABE
        assert hamming_distance(a, b) == hamming_distance(b, a)
