"""The Jira benchmark's re-analysis: cluster and stratified bootstrap, Holm, exact intervals (no network)."""

import pytest

np = pytest.importorskip("numpy")  # the eval harness's own dependency, not the core's

from evals.jira.stats_cluster import (
    bootstrap,
    clopper_pearson,
    cluster_bootstrap,
    holm,
    stratified_bootstrap,
)


def test_holm_step_down_and_monotone():
    adj = holm([0.01, 0.04, 0.03, 0.005])
    assert adj == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])  # capped at 1
    assert holm([]) == []


def test_cluster_bootstrap_is_wider_when_clusters_are_homogeneous():
    # 10 clusters of 10 questions, each cluster all right or all wrong: 10 effective draws, not 100
    values = [float(c % 2) for c in range(10) for _ in range(10)]
    clusters = [f"c{c}" for c in range(10) for _ in range(10)]
    m, lo, hi, _ = cluster_bootstrap(values, clusters, b=4000, seed=0)
    _, ilo, ihi, _ = bootstrap(values, b=4000, seed=0)
    assert m == pytest.approx(0.5)
    assert (hi - lo) > 2 * (ihi - ilo)
    assert cluster_bootstrap(values, clusters, b=500, seed=3) == cluster_bootstrap(
        values, clusters, b=500, seed=3
    )


def test_stratified_bootstrap_collapses_when_strata_are_constant():
    values = [1.0] * 30 + [0.0] * 20
    strata = ["a"] * 30 + ["b"] * 20
    m, lo, hi, _ = stratified_bootstrap(values, strata, b=2000, seed=0)
    assert (
        m == lo == hi == pytest.approx(0.6)
    )  # composition is fixed, nothing varies inside a stratum
    _, ilo, ihi, _ = bootstrap(values, b=2000, seed=0)
    assert ihi > ilo


def test_two_sided_p_of_paired_differences():
    assert cluster_bootstrap([0.0] * 12, ["x", "y"] * 6, b=1000)[3] == 1.0  # identical systems
    assert (
        cluster_bootstrap([1.0, 0.5, 1.0, 0.25] * 5, list("abcd") * 5, b=1000)[3] == 0.0
    )  # never <= 0
    assert stratified_bootstrap([1.0, 0.5] * 10, ["s"] * 20, b=1000)[3] == 0.0


def test_clopper_pearson_exact_interval():
    lo, hi = clopper_pearson(72, 72)
    assert hi == 1.0 and lo == pytest.approx(0.025 ** (1 / 72), abs=1e-4)
    lo, hi = clopper_pearson(0, 30)
    assert lo == 0.0 and hi == pytest.approx(1 - 0.025 ** (1 / 30), abs=1e-4)
    lo, hi = clopper_pearson(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-3) and hi == pytest.approx(0.8129, abs=1e-3)
