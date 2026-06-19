"""
career_bot/ai_modeling.py
==========================
Pure Bayesian primitives for the AI advisor.

No disk I/O.  All functions are stateless and side-effect-free.

Uses scipy.stats.beta for accurate LCB/UCB quantiles when scipy is available;
falls back to a normal approximation (Wilson-score style) otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BetaPosterior",
    "HierarchicalLevel",
    "hierarchical_posterior",
    "score_program",
    "posterior_from_stats_bucket",
    "global_base_rate",
]

# ── optional scipy ────────────────────────────────────────────────────────────
try:
    from scipy.stats import beta as _scipy_beta
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

import math


def _beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile function of Beta(a, b). Uses scipy when available."""
    if _HAS_SCIPY:
        return float(_scipy_beta.ppf(q, a, b))
    # Normal approximation: mean ± z * std
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    std = math.sqrt(max(var, 1e-12))
    # z for quantile via inverse error function approximation
    # scipy.special.erfinv substitute (Abramowitz & Stegun 26.2.17)
    z = _approx_normal_ppf(q)
    return max(0.0, min(1.0, mean + z * std))


def _approx_normal_ppf(p: float) -> float:
    """Rational approximation to the standard-normal quantile (Beasley-Springer-Moro)."""
    p = max(1e-9, min(1 - 1e-9, p))
    if p == 0.5:
        return 0.0
    sign = 1.0 if p > 0.5 else -1.0
    t = p if p > 0.5 else 1 - p
    u = math.log(1.0 / (t * t))
    c = [2.515517, 0.802853, 0.010328]
    d = [1.432788, 0.189269, 0.001308]
    num = c[0] + c[1] * math.sqrt(u) + c[2] * u
    den = 1 + d[0] * math.sqrt(u) + d[1] * u + d[2] * u * math.sqrt(u)
    return sign * (math.sqrt(u) - num / den)


# ── BetaPosterior ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BetaPosterior:
    """Beta(alpha, beta) posterior over a Bernoulli win rate.

    alpha accumulates "successes" (race wins).
    beta  accumulates "failures" (race non-wins).
    """

    alpha: float
    beta_: float  # named beta_ to avoid shadowing the module-level name

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def from_prior(cls, prior_mean: float = 0.5, prior_strength: float = 4.0) -> "BetaPosterior":
        prior_mean = max(1e-6, min(1.0 - 1e-6, float(prior_mean)))
        prior_strength = max(1e-6, float(prior_strength))
        return cls(
            alpha=prior_mean * prior_strength,
            beta_=(1.0 - prior_mean) * prior_strength,
        )

    @classmethod
    def jeffreys(cls) -> "BetaPosterior":
        return cls(alpha=0.5, beta_=0.5)

    # ── updates ───────────────────────────────────────────────────────────

    def update(self, wins: int, losses: int) -> "BetaPosterior":
        return BetaPosterior(self.alpha + float(max(0, wins)), self.beta_ + float(max(0, losses)))

    def update_one(self, win: bool) -> "BetaPosterior":
        return self.update(1, 0) if win else self.update(0, 1)

    # ── summary statistics ────────────────────────────────────────────────

    @property
    def total(self) -> float:
        return self.alpha + self.beta_

    def mean(self) -> float:
        return self.alpha / self.total

    def variance(self) -> float:
        t = self.total
        return (self.alpha * self.beta_) / (t * t * (t + 1.0))

    def mode(self) -> float:
        if self.alpha > 1 and self.beta_ > 1:
            return (self.alpha - 1.0) / (self.total - 2.0)
        return self.mean()

    def lcb(self, quantile: float = 0.25) -> float:
        return _beta_ppf(quantile, self.alpha, self.beta_)

    def ucb(self, quantile: float = 0.75) -> float:
        return _beta_ppf(quantile, self.alpha, self.beta_)

    def credible_interval(self, mass: float = 0.9) -> Tuple[float, float]:
        tail = (1.0 - mass) / 2.0
        return self.lcb(tail), self.ucb(1.0 - tail)

    # ── serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, float]:
        return {"alpha": self.alpha, "beta": self.beta_}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BetaPosterior":
        return cls(alpha=float(d.get("alpha", 0.5)), beta_=float(d.get("beta", 0.5)))


# ── HierarchicalLevel ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HierarchicalLevel:
    """One level of context in a hierarchical posterior computation."""
    name: str
    key: Any
    stats: Optional[Mapping[str, Any]]


# ── module-level functions ────────────────────────────────────────────────────

def posterior_from_stats_bucket(
    bucket: Mapping[str, Any],
    prior: Optional[BetaPosterior] = None,
) -> BetaPosterior:
    """Build a posterior from a race_programs stats bucket.

    bucket keys: starts, wins (or win_rate), avg_reward
    """
    if prior is None:
        prior = BetaPosterior.from_prior()
    starts = int(bucket.get("starts") or 0)
    if starts <= 0:
        return prior
    wins_raw = bucket.get("wins")
    if wins_raw is not None:
        wins = int(wins_raw)
    else:
        win_rate = float(bucket.get("win_rate") or 0.0)
        wins = round(starts * win_rate)
    losses = max(0, starts - wins)
    return prior.update(wins, losses)


def global_base_rate(
    race_programs: Mapping[str, Any],
    min_total_starts: int = 10,
    fallback: float = 0.5,
) -> float:
    """Compute a global win rate across all race programs."""
    total_starts = 0
    total_wins = 0
    for bucket in race_programs.values():
        if not isinstance(bucket, dict):
            continue
        s = int(bucket.get("starts") or 0)
        w_raw = bucket.get("wins")
        if w_raw is not None:
            w = int(w_raw)
        else:
            w = round(s * float(bucket.get("win_rate") or 0.0))
        total_starts += s
        total_wins += w
    if total_starts < min_total_starts:
        return fallback
    return total_wins / total_starts


def hierarchical_posterior(
    levels: Sequence[HierarchicalLevel],
    prior_mean: float = 0.5,
    prior_strength: float = 4.0,
    parent_discount: float = 0.5,
) -> Tuple[BetaPosterior, List[str]]:
    """Bayesian hierarchical pooling across levels of specificity.

    Walks levels from least-specific to most-specific. At each level that has
    data, converts the current posterior into a discounted prior for the next
    level, then updates with that level's observations.

    Returns (final_posterior, list_of_contributing_level_names).
    """
    posterior = BetaPosterior.from_prior(prior_mean, prior_strength)
    contributed: List[str] = []

    for level in levels:
        bucket = level.stats
        if not bucket or not isinstance(bucket, dict):
            continue
        starts = int(bucket.get("starts") or 0)
        if starts <= 0:
            continue

        # Discount parent posterior into a new prior for this level
        mean = posterior.mean()
        a_discounted = posterior.alpha * parent_discount + prior_mean * prior_strength * (1.0 - parent_discount)
        b_discounted = posterior.beta_ * parent_discount + (1.0 - prior_mean) * prior_strength * (1.0 - parent_discount)
        discounted_prior = BetaPosterior(alpha=a_discounted, beta_=b_discounted)

        wins_raw = bucket.get("wins")
        if wins_raw is not None:
            wins = int(wins_raw)
        else:
            win_rate = float(bucket.get("win_rate") or 0.0)
            wins = round(starts * win_rate)
        losses = max(0, starts - wins)
        posterior = discounted_prior.update(wins, losses)
        contributed.append(level.name)

    return posterior, contributed


def score_program(
    posterior: BetaPosterior,
    avg_reward: float,
    risk_quantile: float = 0.25,
    exploration_bonus: float = 0.0,
) -> Dict[str, float]:
    """Compute a scalar adjustment for a race program.

    adjustment = avg_reward * lcb + exploration_bonus * (ucb - lcb)

    The lcb (lower credible bound) penalises uncertain programs — a program
    with few starts will have a wide posterior and a low lcb, reducing its
    adjustment toward zero.
    """
    lcb = posterior.lcb(risk_quantile)
    ucb = posterior.ucb(1.0 - risk_quantile)
    mean = posterior.mean()
    variance = posterior.variance()
    adjustment = avg_reward * lcb + exploration_bonus * (ucb - lcb)
    return {
        "adjustment": round(adjustment, 4),
        "lcb": round(lcb, 4),
        "ucb": round(ucb, 4),
        "mean": round(mean, 4),
        "variance": round(variance, 6),
        "alpha": round(posterior.alpha, 4),
        "beta": round(posterior.beta_, 4),
    }
