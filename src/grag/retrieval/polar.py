"""PolarQuant-style angular direction quantization — training-free.

A unit vector u in R^d is split (see vectors.split_magnitude) into radius r
and direction u; this module codes u as d-1 hyperspherical angles

    u[i]   = cos(a[i]) * prod_{j<i} sin(a[j])      i = 0 .. d-2
    u[d-1] = prod_{j<d-1} sin(a[j])

with a[0..d-3] in [0, pi] and a[d-2] in [0, 2pi). Angles are scalar-quantized
against precomputed per-angle codebooks.

Codebooks (LEVELS): for a uniform random direction, angle i has density
proportional to sin^(d-2-i)(theta) on [0, pi]; the last angle is (nearly)
uniform on [0, 2pi). Reconstruction levels are the quantiles of that analytic
CDF at (k + 1/2) / 2^b and decision boundaries the quantiles at k / 2^b
(centroid/boundary optimal for uniform cells; within a few percent of
Lloyd-Max optima at a fraction of the precompute cost). The CDF is
sin^m(omega) integrated on a fixed grid and inverted by interpolation.
(CDF-midpoint is *not* Lloyd-Max optimal for a uniform-on-sphere source, but
it wins on real clustered corpora: centroid levels pull every code toward the
angle bulk, which blurs inter-cluster gaps.)

Bit allocation: the per-vector budget is bits_per_dim * d bits, shared among
the d-1 angles proportionally to

    w_i = log2(effective_support_i * peak_density_i)
      effective support = pi (a[0..d-3]) or 2*pi (last angle)
      peak density      = max of the normalized sin^(d-2-i) density
                          = exp(lgamma((m+2)/2) - lgamma((m+1)/2)) / sqrt(pi)

clamped to [1, 8] bits per angle. One deliberate deviation from the raw
formula, forced by reconstruction quality: the shares are floored at half the
uniform share (budget / (d-1) / 2). Pure log2(support*peak) weights give the
last angle (uniform density, product exactly 1 -> weight 0) and other
low-exponent tail angles just 1 bit; that is MSE-catastrophic because the
Cartesian error weight of angle i scales with cot^2(theta_i), which is
O(1/m) under a sin^m density — i.e. the *flat* tail angles are the most
damageable per radian of error. With the floor, the spec formula survives as
a bounded tilt (peaked head angles still get up to ~2x an average share).

Rounding remainder after clamping goes to the lowest-index angles first:
their error propagates through the most cumulative sin factors (every
component j > i picks up a cos/sin factor of theta_i), so spare bits do the
most good there; excess bits are removed from the highest-index angles for
the mirror-image reason.

Blob layout (self-describing, 6-byte header + packed codes):

    offset  size  field
    0       1     magic 0x50 ('P')
    1       1     format version (1)
    2       2     dim, uint16 little-endian
    4       2     bits_per_dim * 64, uint16 little-endian (fixed point)
    6       ...   angle codes MSB-first: code for angle 0 occupies the first
                  bits[0] bits, angle 1 the next bits[1] bits, ... padded to
                  a whole byte with zero bits. ceil(sum(bits)/8) bytes.

cosine_from_codes decodes both sides and dots the reconstructions. Decoding
is cheap (one gather per angle + cumulative products), so we do not do
codebook-space scoring; if profiling ever makes candidate scoring hot, the
next step is per-angle lookup tables of query sin/cos products so a code maps
to a score contribution without materializing the vector.
"""

from __future__ import annotations

import math

import numpy as np

from grag.core.errors import ConfigurationError

DEFAULT_BITS_PER_DIM = 1.0

_MAGIC = 0x50  # 'P'
_VERSION = 1
_HEADER = 6  # magic, version, dim u16, bits_per_dim*64 u16
_MAX_ANGLE_BITS = 8
_GRID_POINTS = 4096

# Caches: (dim, bpd_x64) -> _Tables; sin-exponent m -> (grid, cdf).
_TABLES: dict[tuple[int, int], "_Tables"] = {}
_CDFS: dict[int, tuple[np.ndarray, np.ndarray]] = {}


# ---------------------------------------------------------------------------
# angle parametrization
# ---------------------------------------------------------------------------


def cartesian_to_angles(u: np.ndarray) -> np.ndarray:
    """Unit vector (d,) -> (d-1,) hyperspherical angles.

    angles[0..d-3] = arccos(u[i] / ||u[i:]||) in [0, pi]; when the remaining
    norm is zero all remaining angles are 0. The last angle is
    arctan2(u[-1], u[-2]) mapped to [0, 2pi).
    """
    u = np.asarray(u, dtype=np.float64).ravel()
    d = u.size
    if d < 2:
        return np.zeros(0, dtype=np.float64)
    rem = np.sqrt(np.cumsum(u[::-1] ** 2)[::-1])  # rem[i] = ||u[i:]||
    # cos(theta_i) = u[i]/rem[i] computed via out/where so no division by zero
    # is ever evaluated (the where=False lanes are never read downstream either)
    cosv = np.zeros(d - 1, dtype=np.float64)
    np.divide(u[: d - 1], rem[: d - 1], out=cosv, where=rem[: d - 1] > 0.0)
    angles = np.arccos(np.clip(cosv, -1.0, 1.0))
    # arctan2(0, 0) == 0, which is exactly the zero-remainder convention
    angles[d - 2] = np.arctan2(u[d - 1], u[d - 2]) % (2.0 * np.pi)
    return angles


def angles_to_cartesian(angles: np.ndarray, dim: int) -> np.ndarray:
    """Exact inverse of cartesian_to_angles (cumulative sin products)."""
    angles = np.asarray(angles, dtype=np.float64).ravel()
    if angles.size != dim - 1:
        raise ValueError(f"expected {dim - 1} angles for dim={dim}, got {angles.size}.")
    if dim == 1:
        return np.ones(1, dtype=np.float64)
    return _angles_to_cartesian_batch(np.atleast_2d(angles))[0]


def _angles_to_cartesian_batch(A: np.ndarray) -> np.ndarray:
    """(n, d-1) angles -> (n, d) unit vectors."""
    n, d1 = A.shape
    d = d1 + 1
    sinp = np.cumprod(np.sin(A), axis=1)
    cos = np.cos(A)
    U = np.empty((n, d), dtype=np.float64)
    U[:, 0] = cos[:, 0]
    U[:, 1 : d - 1] = cos[:, 1:] * sinp[:, :-1]
    U[:, d - 1] = sinp[:, d - 2]
    return U


# ---------------------------------------------------------------------------
# analytic CDF + quantization levels
# ---------------------------------------------------------------------------


def _sinm_cdf(m: int) -> tuple[np.ndarray, np.ndarray]:
    """(grid, cdf) of density prop to sin^m(theta) on [0, pi], trapezoid rule."""
    cached = _CDFS.get(m)
    if cached is not None:
        return cached
    grid = np.linspace(0.0, np.pi, _GRID_POINTS + 1)
    with np.errstate(divide="ignore", under="ignore", invalid="ignore"):
        dens = np.exp(m * np.log(np.sin(grid)))
    dens = np.nan_to_num(dens)  # sin(0)=sin(pi)=0 -> log -inf -> density 0
    cdf = np.concatenate([[0.0], np.cumsum((dens[:-1] + dens[1:]) / 2.0)])
    cdf /= cdf[-1]
    # strictly increasing, else np.interp at a flat stretch is ill-defined
    cdf += 1e-12 * np.linspace(0.0, 1.0, cdf.size)
    cdf /= cdf[-1]
    _CDFS[m] = (grid, cdf)
    return grid, cdf


def _angle_levels(m: int, nbits: int, last: bool) -> tuple[np.ndarray, np.ndarray]:
    """(levels, boundaries) with 2^nbits cells for one angle."""
    nlevels = 1 << nbits
    q = (np.arange(nlevels) + 0.5) / nlevels
    b = np.arange(1, nlevels) / nlevels
    if last:  # (nearly) uniform on [0, 2pi)
        return q * (2.0 * np.pi), b * (2.0 * np.pi)
    grid, cdf = _sinm_cdf(m)
    return np.interp(q, cdf, grid), np.interp(b, cdf, grid)


# ---------------------------------------------------------------------------
# bit allocation
# ---------------------------------------------------------------------------


def allocate_bits(dim: int, bits_per_dim: float = DEFAULT_BITS_PER_DIM) -> np.ndarray:
    """Per-angle bit widths, summing to ~bits_per_dim * dim (see module docstring)."""
    n = dim - 1
    if n <= 0:
        return np.zeros(0, dtype=np.int64)
    if not (0.0 < float(bits_per_dim) <= 8.0):
        raise ConfigurationError(
            f"bits_per_dim must be in (0, 8], got {bits_per_dim!r}.",
            hint="Set GRAG_POLAR_BITS_PER_DIM between 0 and 8 (default 1.0).",
        )
    budget = max(int(round(bits_per_dim * dim)), n)  # >= 1 bit per angle
    # spec weights: w_i = log2(effective support x peak density)
    w = np.zeros(n, dtype=np.float64)
    log2_pi = math.log2(math.pi)
    for i in range(n - 1):  # last angle stays 0 (uniform: support*peak == 1)
        m = dim - 2 - i
        log_peak = (
            math.lgamma((m + 2) / 2.0) - math.lgamma((m + 1) / 2.0) - 0.5 * math.log(math.pi)
        )
        w[i] = log2_pi + log_peak / math.log(2.0)
    if w.sum() <= 0.0:
        share = np.full(n, 1.0 / n)
    else:
        # floor at half the uniform share: keeps near-uniform tail angles at a
        # working resolution (their cot^2 error weight is the largest), while
        # the formula still tilts spare bits toward peaked head angles.
        share = np.maximum(w / w.sum(), 0.5 / n)
        share /= share.sum()
    raw = budget * share
    bits = np.clip(np.floor(raw), 1, _MAX_ANGLE_BITS).astype(np.int64)
    rem = budget - int(bits.sum())
    i = 0
    while rem > 0 and i < n * _MAX_ANGLE_BITS:
        j = i % n  # spare bits to the lowest indices (most sin factors downstream)
        if bits[j] < _MAX_ANGLE_BITS:
            bits[j] += 1
            rem -= 1
        i += 1
    j = n - 1
    while rem < 0 and j >= 0:
        if bits[j] > 1:  # take back from the highest indices (least propagation)
            bits[j] -= 1
            rem += 1
        j -= 1
    return bits


# ---------------------------------------------------------------------------
# cached codebook tables
# ---------------------------------------------------------------------------


class _Tables:
    """Precomputed codebooks for one (dim, bits_per_dim)."""

    def __init__(self, dim: int, bits_per_dim: float):
        self.dim = dim
        self.bits_per_dim = bits_per_dim
        self.bits = allocate_bits(dim, bits_per_dim)  # (d-1,) int64, 1..8
        self.total_bits = int(self.bits.sum())
        self.nbytes = (self.total_bits + 7) // 8
        # padded level matrix: (d-1, 256); unused entries NaN (never gathered:
        # codes are < 2^bits[i] by construction)
        self.levels = np.full((dim - 1, 1 << _MAX_ANGLE_BITS), np.nan)
        self.boundaries: list[np.ndarray] = []
        for i in range(dim - 1):
            b = int(self.bits[i])
            lv, bd = _angle_levels(dim - 2 - i, b, last=(i == dim - 2))
            self.levels[i, : 1 << b] = lv
            self.boundaries.append(bd)
        self.starts = np.zeros(dim - 1, dtype=np.int64)
        self.starts[1:] = np.cumsum(self.bits)[:-1]
        self._unpack_plan = [
            (self.starts[sel] + (self.bits[sel] - 1 - k), k)
            for k in range(_MAX_ANGLE_BITS)
            if (sel := np.nonzero(self.bits > k)[0]).size
        ]


def _tables(dim: int, bits_per_dim: float) -> _Tables:
    key = (int(dim), int(round(float(bits_per_dim) * 64.0)))
    t = _TABLES.get(key)
    if t is None:
        t = _Tables(int(dim), key[1] / 64.0)
        _TABLES[key] = t
    return t


# ---------------------------------------------------------------------------
# code packing
# ---------------------------------------------------------------------------


def _pack_codes(codes: np.ndarray, t: _Tables) -> bytes:
    """MSB-first bitstream: angle 0's code in its bits[0] bits, then angle 1..."""
    bitarr = np.zeros(t.total_bits, dtype=np.uint8)
    codes32 = codes.astype(np.int64)
    for pos, k in t._unpack_plan:
        sel = t.bits > k
        bitarr[pos] = (codes32[sel] >> k) & 1
    return np.packbits(bitarr).tobytes()


def _unpack_codes(payload: bytes, t: _Tables) -> np.ndarray:
    raw = np.frombuffer(payload, dtype=np.uint8)
    if raw.size != t.nbytes:
        raise _bad_blob(f"payload has {raw.size} bytes, expected {t.nbytes}")
    bitarr = np.unpackbits(raw, count=t.total_bits) if t.total_bits else raw[:0]
    codes = np.zeros(t.dim - 1, dtype=np.int64)
    for pos, k in t._unpack_plan:
        sel = t.bits > k
        codes[sel] |= bitarr[pos].astype(np.int64) << k
    return codes


def _bad_blob(detail: str) -> ConfigurationError:
    return ConfigurationError(
        f"Malformed polar direction code: {detail}.",
        hint="The blob was not written by the polar codec at this dim/bits_per_dim — "
        "check GRAG_VECTOR_CODEC / GRAG_POLAR_BITS_PER_DIM consistency between writer and reader.",
    )


# ---------------------------------------------------------------------------
# encode / decode
# ---------------------------------------------------------------------------


def encode(
    angles: np.ndarray, dim: int, bits_per_dim: float = DEFAULT_BITS_PER_DIM
) -> bytes:
    """(d-1,) angles -> self-describing blob (header + packed codes)."""
    angles = np.asarray(angles, dtype=np.float64).ravel()
    if angles.size != dim - 1:
        raise ValueError(f"expected {dim - 1} angles for dim={dim}, got {angles.size}.")
    t = _tables(dim, bits_per_dim)
    codes = np.empty(dim - 1, dtype=np.int64)
    for i in range(dim - 1):
        codes[i] = np.searchsorted(t.boundaries[i], angles[i], side="right")
    header = bytes(
        (
            _MAGIC,
            _VERSION,
            dim & 0xFF,
            (dim >> 8) & 0xFF,
            int(round(bits_per_dim * 64.0)) & 0xFF,
            (int(round(bits_per_dim * 64.0)) >> 8) & 0xFF,
        )
    )
    return header + _pack_codes(codes, t)


def _parse_header(blob: bytes) -> tuple[int, float]:
    if len(blob) < _HEADER:
        raise _bad_blob(f"{len(blob)} bytes, shorter than the {_HEADER}-byte header")
    if blob[0] != _MAGIC or blob[1] != _VERSION:
        raise _bad_blob(f"bad magic/version 0x{blob[0]:02x}/0x{blob[1]:02x}")
    dim = blob[2] | (blob[3] << 8)
    bpd = (blob[4] | (blob[5] << 8)) / 64.0
    return dim, bpd


def decode(blob: bytes, dim: int, bits_per_dim: float | None = None) -> np.ndarray:
    """blob -> (d-1,) reconstructed angles. bits_per_dim defaults to the blob
    header value (blobs are self-describing); an explicit argument wins."""
    blob = bytes(blob)
    hdim, hbpd = _parse_header(blob)
    if hdim != dim:
        raise _bad_blob(f"header dim={hdim} but dim={dim} was requested")
    t = _tables(dim, bits_per_dim if bits_per_dim is not None else hbpd)
    codes = _unpack_codes(blob[_HEADER:], t)
    rows = t.levels[np.arange(dim - 1), codes] if dim > 1 else np.zeros(0)
    return rows


def decode_many(blobs: list[bytes], dim: int, bits_per_dim: float | None = None) -> np.ndarray:
    """Vectorized decode: list of blobs -> (n, d-1) angle matrix."""
    if not blobs:
        return np.zeros((0, max(dim - 1, 0)), dtype=np.float64)
    first = bytes(blobs[0])
    hdim, hbpd = _parse_header(first)
    if hdim != dim:
        raise _bad_blob(f"header dim={hdim} but dim={dim} was requested")
    t = _tables(dim, bits_per_dim if bits_per_dim is not None else hbpd)
    payload = np.empty((len(blobs), t.nbytes), dtype=np.uint8)
    for i, b in enumerate(blobs):
        b = bytes(b)
        if len(b) != _HEADER + t.nbytes:
            raise _bad_blob(f"blob {i} has {len(b)} bytes, expected {_HEADER + t.nbytes}")
        payload[i] = np.frombuffer(b, dtype=np.uint8, count=t.nbytes, offset=_HEADER)
    if t.total_bits:
        bitarr = np.unpackbits(payload, axis=1, count=t.total_bits)  # (n, total_bits)
        codes = np.zeros((len(blobs), dim - 1), dtype=np.int64)
        for pos, k in t._unpack_plan:
            sel = t.bits > k
            codes[:, sel] |= bitarr[:, pos].astype(np.int64) << k
        return t.levels[np.arange(dim - 1)[None, :], codes]
    return np.zeros((len(blobs), 0), dtype=np.float64)


# ---------------------------------------------------------------------------
# scoring conveniences
# ---------------------------------------------------------------------------


def reconstruct(blob: bytes, dim: int, bits_per_dim: float | None = None) -> np.ndarray:
    """blob -> reconstructed unit direction (float32)."""
    return angles_to_cartesian(decode(blob, dim, bits_per_dim), dim).astype(np.float32)


def reconstruct_many(
    blobs: list[bytes], dim: int, bits_per_dim: float | None = None
) -> np.ndarray:
    """Vectorized reconstruct: list of blobs -> (n, dim) unit vectors (float32)."""
    A = decode_many(blobs, dim, bits_per_dim)
    if A.shape[1] == 0:
        return np.ones((A.shape[0], dim), dtype=np.float32)
    return _angles_to_cartesian_batch(A).astype(np.float32)


def cosine_from_codes(
    blob_q: bytes,
    blob_or_unit_v,
    dim: int | None = None,
    bits_per_dim: float | None = None,
) -> float:
    """Cosine between a polar-coded query and a second coded vector or a raw
    unit vector. Decode-then-dot: decode is a table gather + cumprod, so this
    is O(dim) per candidate; codebook-space scoring is a possible future
    optimization but would couple scoring to the blob layout."""
    if isinstance(blob_or_unit_v, (bytes, bytearray, list)):
        hdim, hbpd = _parse_header(bytes(blob_q))
        d = dim if dim is not None else hdim
        b = bits_per_dim if bits_per_dim is not None else hbpd
        u1 = reconstruct(bytes(blob_q), d, b).astype(np.float64)
        u2 = reconstruct(bytes(blob_or_unit_v), d, b).astype(np.float64)
    else:
        hdim, hbpd = _parse_header(bytes(blob_q))
        d = dim if dim is not None else hdim
        b = bits_per_dim if bits_per_dim is not None else hbpd
        u1 = reconstruct(bytes(blob_q), d, b).astype(np.float64)
        u2 = np.asarray(blob_or_unit_v, dtype=np.float64).ravel()
        if u2.size != d:
            raise ValueError(f"unit vector has {u2.size} dims, expected {d}.")
    n = float(np.linalg.norm(u2))
    if n == 0.0:
        return 0.0
    return float(u1 @ (u2 / n))
