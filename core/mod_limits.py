"""How many bytes a MOD sample may hold, and how long a sustain fits in them."""

# A MOD sample header holds its length in 16-bit words, so one sample is at most this long
# (128 KiB less one word).  Paula's length register is a word count too.
MAX_MOD_SAMPLE_BYTES = 65535 * 2


def sample_limit_bytes(kb: int) -> int:
    """Bytes one sample may hold for a `max_sample_kb` setting: 128 is the format's own limit
    (131070 bytes), 64 is the original ProTracker editor's (65534 bytes, its four-hex-digit
    length field).  The value is the KiB boundary less one word, as both limits are."""
    if isinstance(kb, bool) or not isinstance(kb, int) or not 1 <= kb <= 128:
        raise ValueError(f"max_sample_kb must be an integer from 1 to 128 (64 or 128 in practice), got {kb!r}")
    return kb * 1024 - 2


def max_sustain_secs(rate: int, release_secs: float, max_bytes: int = MAX_MOD_SAMPLE_BYTES,
                     margin_bytes: int = 64) -> float:
    """Longest sustain (seconds) a sample synthesised at `rate` Hz can hold and still fit in
    `max_bytes` once `release_secs` of release tail follows it.  The margin covers the
    resampler's rounding.  Never negative: a release longer than the limit leaves 0.
    """
    return max(0.0, (max_bytes - margin_bytes) / rate - release_secs)
