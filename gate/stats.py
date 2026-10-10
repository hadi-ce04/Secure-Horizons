"""Statistics for attack success rates: Wilson 95% CI and agreement rate."""

from __future__ import annotations

from math import sqrt

Z_95 = 1.96


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Implements exactly::

        p_hat = successes / n
        center = (p_hat + z**2/(2n)) / (1 + z**2/n)
        halfwidth = z * sqrt(p_hat(1-p_hat)/n + z**2/(4 n**2)) / (1 + z**2/n)
        lower = max(0, center - halfwidth); upper = min(1, center + halfwidth)

    Raises ValueError when ``n <= 0`` (an interval over zero trials is
    undefined) or when ``successes`` is outside ``[0, n]``.
    """
    if n <= 0:
        raise ValueError("wilson_interval requires n > 0")
    if successes < 0 or successes > n:
        raise ValueError(f"successes must be in [0, n]; got successes={successes}, n={n}")
    p_hat = successes / n
    denom = 1 + (z ** 2) / n
    center = (p_hat + (z ** 2) / (2 * n)) / denom
    halfwidth = z * sqrt((p_hat * (1 - p_hat) / n) + ((z ** 2) / (4 * (n ** 2)))) / denom
    lower = max(0.0, center - halfwidth)
    upper = min(1.0, center + halfwidth)
    return float(lower), float(upper)


def attack_success_rate(successes: int, n: int) -> float:
    """Point estimate of ASR. Raises ValueError when n == 0."""
    if n <= 0:
        raise ValueError("attack_success_rate requires n > 0")
    return float(successes / n)


def agreement_rate(outcomes_by_attack: dict) -> float:
    """Fraction of trials agreeing with their attack's majority outcome.

    For each attack, count ``max(#True, #False)``; sum over attacks and divide
    by the total number of trials. Raises ValueError if there are no trials.
    """
    total = 0
    agreeing = 0
    for outcomes in outcomes_by_attack.values():
        outcomes = list(outcomes)
        trues = sum(1 for o in outcomes if o)
        falses = len(outcomes) - trues
        agreeing += max(trues, falses)
        total += len(outcomes)
    if total == 0:
        raise ValueError("agreement_rate requires at least one trial")
    return float(agreeing / total)


def _aggregate(trials: list[dict]) -> dict:
    n = len(trials)
    successes = sum(1 for t in trials if t["success"])
    lower, upper = wilson_interval(successes, n)
    by_attack: dict[str, list[bool]] = {}
    for t in trials:
        by_attack.setdefault(t["attack_id"], []).append(bool(t["success"]))
    return {
        "n": n,
        "successes": successes,
        "asr": attack_success_rate(successes, n),
        "ci_lower": lower,
        "ci_upper": upper,
        "agreement": agreement_rate(by_attack),
    }


def summarize_trials(trials: list[dict]) -> dict:
    """Aggregate trial records (attack_id, category, success) per category, overall, per attack."""
    if not trials:
        raise ValueError("summarize_trials requires at least one trial")
    by_cat: dict[str, list[dict]] = {}
    by_attack: dict[str, list[dict]] = {}
    for t in trials:
        by_cat.setdefault(t["category"], []).append(t)
        by_attack.setdefault(t["attack_id"], []).append(t)
    categories = {cat: _aggregate(by_cat[cat]) for cat in sorted(by_cat)}
    per_attack = {}
    for attack_id in sorted(by_attack):
        group = by_attack[attack_id]
        n = len(group)
        s = sum(1 for t in group if t["success"])
        per_attack[attack_id] = {
            "category": group[0]["category"],
            "n": n,
            "successes": s,
            "asr": attack_success_rate(s, n),
        }
    return {"categories": categories, "overall": _aggregate(trials), "per_attack": per_attack}
