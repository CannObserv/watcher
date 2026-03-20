"""Tests for SimHash fingerprinting and Hamming distance."""

from src.core.simhash import hamming_distance, simhash, similarity


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


class TestSimilarity:
    def test_identical_is_one(self):
        assert similarity(0b1010, 0b1010) == 1.0

    def test_all_bits_different_64(self):
        # All 64 bits differ
        a = 0x0000000000000000
        b = 0xFFFFFFFFFFFFFFFF
        assert similarity(a, b) == 0.0

    def test_similar_texts_high_score(self):
        a = simhash("the quick brown fox jumps over the lazy dog")
        b = simhash("the quick brown fox leaps over the lazy dog")
        score = similarity(a, b)
        assert 0.5 < score < 1.0

    def test_returns_float(self):
        assert isinstance(similarity(0, 1), float)
