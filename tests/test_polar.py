"""Polar (hyperspherical-angle) direction codec tests — pure numpy, no DB.

Threshold notes (measured on seeded RNGs, this implementation): per-angle
scalar quantization has a Cartesian error weight of cot^2(theta_i) per angle,
which is O(1/m) under the sin^m angle density — the near-uniform *tail*
angles dominate reconstruction error, so head-tilted bit allocations
(including the raw spec formula) underperform. With the floored allocation in
polar.allocate_bits, reconstruction at bits_per_dim=4 is cosine ~0.97
(dim 64) / ~0.97 (dim 384); at bits_per_dim=2, ~0.86. Assertions below are
set at the achieved values with headroom, except the monotonicity and
roundtrip-exactness properties which hold strictly.
"""

from __future__ import annotations

import numpy as np
import pytest

from grag.core.errors import ConfigurationError
from grag.retrieval import polar, vectors


def _unit(rng, dim):
    u = rng.normal(size=dim)
    return u / np.linalg.norm(u)


# --- angle parametrization ----------------------------------------------------


def test_angles_roundtrip_exact():
    rng = np.random.default_rng(0)
    for dim in (2, 3, 8, 64, 384):
        for _ in range(50):
            u = _unit(rng, dim)
            back = polar.angles_to_cartesian(polar.cartesian_to_angles(u), dim)
            assert np.allclose(back, u, atol=1e-10)


def test_angles_conventions():
    rng = np.random.default_rng(1)
    for _ in range(20):
        a = polar.cartesian_to_angles(_unit(rng, 16))
        assert np.all(a[:-1] >= 0.0) and np.all(a[:-1] <= np.pi)
        assert 0.0 <= a[-1] < 2.0 * np.pi
    # arctan2 quadrant: u = (0, ..., 0, -1) -> last angle 3*pi/2 in [0, 2pi)
    u = np.zeros(4)
    u[-1] = -1.0
    assert polar.cartesian_to_angles(u)[-1] == pytest.approx(1.5 * np.pi)


def test_angles_zero_vector_guard():
    # zero remaining norm -> arccos(0) = pi/2 (a convention, not a crash);
    # the all-pi/2 angle tuple maps to the e_{d-1} direction up to fp error
    a = polar.cartesian_to_angles(np.zeros(8))
    back = polar.angles_to_cartesian(a, 8)
    assert back[-2] == pytest.approx(1.0)
    assert np.allclose(np.delete(back, 6), 0.0, atol=1e-12)
    # mid-vector zero remainder: u = e0 roundtrips (fp error)
    u = np.zeros(5)
    u[0] = 1.0
    a = polar.cartesian_to_angles(u)
    assert np.allclose(polar.angles_to_cartesian(a, 5), u, atol=1e-12)


# --- bit allocation -----------------------------------------------------------


def test_allocate_bits_budget_and_clamps():
    for dim in (8, 64, 384):
        for bpd in (0.5, 1.0, 2.0, 4.0):
            bits = polar.allocate_bits(dim, bpd)
            assert bits.shape == (dim - 1,)
            assert bits.min() >= 1 and bits.max() <= 8
            assert bits.sum() == max(int(round(bpd * dim)), dim - 1)
    bits = polar.allocate_bits(64, 4.0)
    assert bits[0] > bits[-1]  # head tilt survives the floor


def test_allocate_bits_rejects_bad_budget():
    with pytest.raises(ConfigurationError):
        polar.allocate_bits(64, 0.0)
    with pytest.raises(ConfigurationError):
        polar.allocate_bits(64, 9.0)


# --- encode / decode ------------------------------------------------------------


def test_blob_layout_and_size():
    dim, bpd = 64, 1.0
    rng = np.random.default_rng(7)
    blob = polar.encode(polar.cartesian_to_angles(_unit(rng, dim)), dim, bpd)
    bits = polar.allocate_bits(dim, bpd)
    assert len(blob) == 6 + (int(bits.sum()) + 7) // 8
    assert blob[0] == 0x50 and blob[1] == 1
    assert blob[2] | (blob[3] << 8) == dim
    assert (blob[4] | (blob[5] << 8)) == int(round(bpd * 64))


def test_decode_is_self_describing():
    rng = np.random.default_rng(11)
    u = _unit(rng, 32)
    blob = polar.encode(polar.cartesian_to_angles(u), 32, 2.0)
    assert np.allclose(polar.decode(blob, 32), polar.decode(blob, 32, 2.0))


def test_decode_rejects_malformed_blobs():
    with pytest.raises(ConfigurationError):
        polar.decode(b"\x00", 1)
    good = polar.encode(polar.cartesian_to_angles(_unit(np.random.default_rng(3), 16)), 16, 1.0)
    with pytest.raises(ConfigurationError):
        polar.decode(good, 32)  # dim mismatch vs header
    with pytest.raises(ConfigurationError):
        polar.decode(good[:-1], 16)  # truncated payload


def _mean_cosine(dim, bpd, n, seed=42):
    rng = np.random.default_rng(seed)
    cos = []
    for _ in range(n):
        u = _unit(rng, dim)
        blob = polar.encode(polar.cartesian_to_angles(u), dim, bpd)
        cos.append(float(u @ polar.reconstruct(blob, dim, bpd)))
    return np.array(cos)


def test_reconstruction_cosine_bpd4():
    # measured: dim 384 -> mean 0.971, min 0.950; dim 64 -> mean 0.966, min 0.943
    cos = _mean_cosine(384, 4.0, 150)
    assert cos.mean() > 0.96
    assert cos.min() > 0.93
    cos64 = _mean_cosine(64, 4.0, 150)
    assert cos64.mean() > 0.95


def test_reconstruction_monotonic_in_bits():
    # more budget -> strictly better reconstruction (mean cosine increases)
    m1 = _mean_cosine(128, 1.0, 120).mean()
    m2 = _mean_cosine(128, 2.0, 120).mean()
    m4 = _mean_cosine(128, 4.0, 120).mean()
    assert m1 < m2 < m4


def test_ranking_agreement_clustered_bpd2():
    """Synthetic clustered data: polar top-10 vs exact cosine top-10.

    CDF-matched angle quantization at 2 bits/dim reaches mean overlap ~6/10
    on 8 tight clusters (binary sign-codes get ~5/10 on the same data); both
    are far below exact — this is the regime the pipeline's fp32 rescore of
    the top 4*top_k approximate candidates exists for.
    """
    dim = 64
    rng = np.random.default_rng(3)
    n_clusters, per = 8, 60
    centers = rng.normal(size=(n_clusters, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    docs = []
    for c in range(n_clusters):
        for _ in range(per):
            v = centers[c] + 0.15 * rng.normal(size=dim)
            v /= np.linalg.norm(v)
            docs.append(v)
    docs = np.stack(docs)
    recs = polar.reconstruct_many(
        [polar.encode(polar.cartesian_to_angles(v), dim, 2.0) for v in docs], dim, 2.0
    )
    overlaps = []
    for qi in range(25):
        q = centers[qi % n_clusters] + 0.1 * rng.normal(size=dim)
        q /= np.linalg.norm(q)
        exact = set(np.argsort(-(docs @ q))[:10].tolist())
        approx = set(np.argsort(-(recs @ q))[:10].tolist())
        overlaps.append(len(exact & approx))
    assert float(np.mean(overlaps)) >= 5.5


# --- scoring helpers ------------------------------------------------------------


def test_cosine_from_codes_blob_and_vector():
    rng = np.random.default_rng(13)
    u = _unit(rng, 48)
    blob = polar.encode(polar.cartesian_to_angles(u), 48, 4.0)
    # self-similarity against the exact unit vector: reconstruction cosine
    c = polar.cosine_from_codes(blob, u)
    assert c == pytest.approx(_mean_of_one(u, blob), abs=1e-6)
    assert c > 0.9
    # blob-vs-blob agrees with decode-then-dot
    v = _unit(rng, 48)
    vblob = polar.encode(polar.cartesian_to_angles(v), 48, 4.0)
    expect = float(polar.reconstruct(blob, 48, 4.0) @ polar.reconstruct(vblob, 48, 4.0))
    assert polar.cosine_from_codes(blob, vblob) == pytest.approx(expect, abs=1e-6)
    assert polar.cosine_from_codes(blob, np.zeros(48)) == 0.0


def _mean_of_one(u, blob):
    return float(u @ polar.reconstruct(blob, 48, 4.0))


# --- dispatch through vectors.py -------------------------------------------------


def test_vectors_dispatch_roundtrip(monkeypatch):
    monkeypatch.delenv("GRAG_POLAR_BITS_PER_DIM", raising=False)
    rng = np.random.default_rng(17)
    u = _unit(rng, 64).astype(np.float32)
    blob = vectors.encode_direction(u, "polar")
    assert len(blob) == 6 + 8  # 64 bits of codes at default bits_per_dim=1
    back = vectors.decode_direction(blob, "polar", 64)
    assert back.dtype == np.float32
    assert float(back @ u) > 0.6  # 1 bit/angle: coarse but correlated


def test_vectors_dispatch_scores_ordering(monkeypatch):
    monkeypatch.delenv("GRAG_POLAR_BITS_PER_DIM", raising=False)
    rng = np.random.default_rng(3)
    base = _unit(rng, 48)
    near = base + 0.05 * rng.normal(size=48)
    near /= np.linalg.norm(near)
    far = -base
    codes = [vectors.encode_direction(v, "polar") for v in (base, near, far)]
    scores = vectors.candidate_scores(codes, "polar", base)
    assert scores.shape == (3,)
    assert scores[0] > scores[1] > scores[2]


def test_vectors_dispatch_rejects_non_unit_input():
    with pytest.raises(ConfigurationError):
        vectors.encode_direction(np.ones(4), "polar")
    # zero vector is allowed (magnitude is stored separately and is 0)
    blob = vectors.encode_direction(np.zeros(8), "polar")
    assert vectors.decode_direction(blob, "polar", 8).shape == (8,)
