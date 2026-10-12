"""Offseason carry-over model: turn a season's final ratings into next
season's preseason ratings using real roster/staff churn instead of just
reusing last year's number or a cold-start recruiting-only prior.

Blended signals (all z-scored across whichever teams have that signal,
then remapped onto the 0-100 scale and combined by weight):
  - prior: the team's own final rating from the model, carried forward.
    This is already on our 0-100 scale, so it doesn't need rescaling --
    only z-scoring, to combine it with the other signals.
  - talent: CFBD's team talent composite for the new year. This is a
    whole-roster recruiting-pedigree score that already reflects most
    incoming signees and incoming/outgoing transfers (transfers carry
    their own recruiting rating with them).
  - recruiting: the incoming signing class's own composite points
    (recruiting/teams), given explicit (if smaller) weight since the
    user specifically wants "recruiting class additions" to register
    on their own, not just folded into the roster-wide talent number.
  - portal: net transfer value (incoming player ratings minus outgoing
    player ratings), same reasoning -- explicit credit/blame for portal
    activity beyond what the talent composite already captures.

Coaching changes don't fit the z-score blend (there's no continuous
"how good is this hire" signal available) so a new head coach is applied
as a flat rating penalty representing transition risk/uncertainty, not a
judgment that the new coach is worse.
"""
from __future__ import annotations

import datetime as dt

from .cfbd_client import CfbdClient
from .model import FCS_BASELINE, MEAN_RATING, RATING_CEIL, RATING_FLOOR, _zscore

SIGNAL_WEIGHTS = {
    "prior": 0.40,
    "talent": 0.30,
    "recruiting": 0.15,
    "portal": 0.15,
}
PRESEASON_SPREAD = 14.0
OFFSEASON_REGRESSION = 0.20  # a full season of results anchors this more than a cold start
NEW_COACH_PENALTY = -3.0
COACH_CHANGE_CUTOFF = dt.date(2025, 11, 1)  # hires after last season's games count as "new for 2026"

STAR_VALUE = {5: 0.98, 4: 0.89, 3: 0.80, 2: 0.72, 1: 0.65, 0: 0.60}
UNRATED_PORTAL_VALUE = 0.70


def _clip(r: float) -> float:
    return max(RATING_FLOOR, min(RATING_CEIL, r))


def fetch_coaching_changes(client: CfbdClient, year: int, cutoff: dt.date = COACH_CHANGE_CUTOFF) -> set[str]:
    coaches = client.get("/coaches", year=year)
    changed: set[str] = set()
    for coach in coaches:
        hire_date_str = coach.get("hireDate")
        if not hire_date_str:
            continue
        hire_date = dt.date.fromisoformat(hire_date_str[:10])
        if hire_date < cutoff:
            continue
        for season in coach.get("seasons", []):
            if season.get("year") == year:
                changed.add(season["school"])
    return changed


def fetch_portal_net(client: CfbdClient, year: int) -> dict[str, float]:
    entries = client.get("/player/portal", year=year)
    net: dict[str, float] = {}
    for p in entries:
        destination = p.get("destination")
        if not destination or p.get("eligibility") == "Withdrawn":
            continue
        rating = p.get("rating")
        value = rating if rating is not None else STAR_VALUE.get(p.get("stars"), UNRATED_PORTAL_VALUE)
        origin = p.get("origin")
        net[destination] = net.get(destination, 0.0) + value
        if origin:
            net[origin] = net.get(origin, 0.0) - value
    return net


def fetch_recruiting_points(client: CfbdClient, year: int) -> dict[str, float]:
    rows = client.get("/recruiting/teams", year=year)
    return {r["team"]: r["points"] for r in rows if r.get("points") is not None}


def fetch_talent(client: CfbdClient, year: int) -> dict[str, float]:
    return {row["team"]: row["talent"] for row in client.get("/talent", year=year) if row.get("talent") is not None}


def compute_preseason_ratings(
    team_names: list[str],
    final_prior_ratings: dict[str, float],
    talent: dict[str, float],
    recruiting_points: dict[str, float],
    portal_net: dict[str, float],
    coaching_changed: set[str],
) -> dict[str, float]:
    prior_z = _zscore({t: r for t, r in final_prior_ratings.items() if t in team_names})
    talent_z = _zscore({t: v for t, v in talent.items() if t in team_names})
    recruiting_z = _zscore({t: v for t, v in recruiting_points.items() if t in team_names})
    portal_z = _zscore({t: v for t, v in portal_net.items() if t in team_names})

    ratings: dict[str, float] = {}
    for team in team_names:
        signals = {
            "prior": prior_z.get(team),
            "talent": talent_z.get(team),
            "recruiting": recruiting_z.get(team),
            "portal": portal_z.get(team),
        }
        available = {k: v for k, v in signals.items() if v is not None}
        if not available:
            # No data at all for this team (e.g. a brand-new FCS reclassification):
            # fall back to the standard flat FCS baseline.
            ratings[team] = _clip(FCS_BASELINE)
            continue

        weight_total = sum(SIGNAL_WEIGHTS[k] for k in available)
        blended_z = sum(SIGNAL_WEIGHTS[k] * v for k, v in available.items()) / weight_total

        base = MEAN_RATING + blended_z * PRESEASON_SPREAD
        if team in coaching_changed:
            base += NEW_COACH_PENALTY

        regressed = base + (MEAN_RATING - base) * OFFSEASON_REGRESSION
        ratings[team] = _clip(regressed)
    return ratings
