"""Core rating engine.

Design summary (see conversation / README for the full rationale):

1. INITIAL RATINGS (bootstrap for a season)
   Blended from two bias-resistant, performance/talent-based inputs instead
   of conference reputation or media polls:
     - prior season's final SRS rating (how good the roster/program actually
       was last year), z-scored
     - current year's recruiting talent composite (returning + incoming
       talent), z-scored
   Both are mapped onto a 0-100 scale, blended, and regressed partway toward
   a league-average 50 so no team starts in a permanently unbeatable or
   unwinnable hole. Teams with no prior-year CFBD data (e.g. first FBS
   season) fall back to talent-only, regressed harder toward a low baseline.

2. PER-GAME UPDATE (Elo-style, zero-sum)
   expected_home = logistic((R_home + HFA - R_away) / SCALE)
   delta = K * margin_of_victory_multiplier * (actual_home - expected_home)
   R_home += delta ; R_away -= delta
   This alone already gives: beating a higher-rated team earns more than
   beating a lower-rated one, and losing to a lower-rated team costs more
   than losing to a higher-rated one.

3. RETROACTIVE SEASON RECOMPUTE (the "your week-1 opponent turned out to be
   bad, so that result is revalued" behavior)
   A single forward pass through the schedule can't do this -- it only
   knows an opponent's rating *at the time* they played. Instead, after
   each week we re-solve the whole season so far as a fixed point: run
   several passes over every game played to date, where each pass uses
   every team's rating *from the previous pass* (i.e. their most
   current, fully-informed strength) as the opponent strength in the
   expected-score calculation. Iterating this to convergence means a
   team's entire season (future results included) continuously reshapes
   how much credit/blame it hands out for every game it played, which is
   exactly the "nature of that team has been revealed" effect requested.
   This is the same family of idea as Massey/Colley simultaneous-equation
   ratings, kept in Elo/MOV form instead of a pure linear system.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MEAN_RATING = 50.0
RATING_FLOOR = 0.0
RATING_CEIL = 100.0

# --- Tunable constants -------------------------------------------------
LOGISTIC_SCALE = 24.0      # bigger = ratings gap matters less to win prob
HOME_FIELD_ADV = 2.2       # rating points added to home team's effective rating
BASE_K = 4.0               # base rating points exchanged per game
MOV_BASE = 2.2             # FiveThirtyEight-style margin-of-victory damping
MOV_SLOPE = 0.001
REGRESSION_TO_MEAN = 0.30  # fraction of preseason blend pulled back to 50
PRIOR_YEAR_WEIGHT = 0.55
TALENT_WEIGHT = 0.45
NEW_PROGRAM_BASELINE = 40.0
FCS_BASELINE = 25.0        # flat prior for FCS teams (no talent/SRS data available);
                           # the ~700 FCS-vs-FCS games each season sort them out quickly
CONVERGENCE_ITERATIONS = 40
CONVERGENCE_EPSILON = 0.01
RELAXATION = 0.5  # under-relaxation; a full (undamped) Jacobi update oscillates forever instead of settling


def _zscore(values: dict[str, float]) -> dict[str, float]:
    nums = list(values.values())
    if len(nums) < 2:
        return {k: 0.0 for k in values}
    mean = sum(nums) / len(nums)
    var = sum((x - mean) ** 2 for x in nums) / len(nums)
    std = math.sqrt(var) or 1.0
    return {k: (v - mean) / std for k, v in values.items()}


def compute_initial_ratings(
    team_names: list[str],
    prior_year_srs: dict[str, float],
    talent: dict[str, float],
    fcs_team_names: set[str] | None = None,
) -> dict[str, float]:
    """Blend prior-season SRS + recruiting talent into a 0-100 preseason prior.

    FCS teams skip that blend entirely (CFBD has no talent/SRS data for them)
    and start at a flat FCS_BASELINE instead -- their own in-division games
    provide plenty of signal to separate them over the season.
    """
    fcs_team_names = fcs_team_names or set()
    srs_z = _zscore(prior_year_srs) if prior_year_srs else {}
    talent_z = _zscore(talent) if talent else {}

    ratings: dict[str, float] = {}
    for team in team_names:
        if team in fcs_team_names:
            ratings[team] = _clip(FCS_BASELINE)
            continue
        has_prior = team in srs_z
        has_talent = team in talent_z
        if has_prior and has_talent:
            blended_z = PRIOR_YEAR_WEIGHT * srs_z[team] + TALENT_WEIGHT * talent_z[team]
            spread = 14.0
            base = MEAN_RATING + blended_z * spread
        elif has_talent:
            # New-to-FBS or missing history: talent only, regressed hard low.
            spread = 10.0
            base = NEW_PROGRAM_BASELINE + talent_z[team] * spread
        elif has_prior:
            spread = 14.0
            base = MEAN_RATING + srs_z[team] * spread
        else:
            base = NEW_PROGRAM_BASELINE

        regressed = base + (MEAN_RATING - base) * REGRESSION_TO_MEAN
        ratings[team] = _clip(regressed)
    return ratings


def _clip(r: float) -> float:
    return max(RATING_FLOOR, min(RATING_CEIL, r))


def _expected_score(r_a: float, r_b: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(r_a - r_b) / LOGISTIC_SCALE))


def _mov_multiplier(point_diff: int, winner_rating_diff: float) -> float:
    point_diff = max(1, abs(point_diff))
    return math.log(point_diff + 1) * (MOV_BASE / (winner_rating_diff * MOV_SLOPE + MOV_BASE))


@dataclass
class Game:
    home: str
    away: str
    home_points: int
    away_points: int
    neutral_site: bool = False
    week: int = 0

    @property
    def home_won(self) -> bool:
        return self.home_points > self.away_points


def apply_game(ratings: dict[str, float], game: Game) -> dict[str, float]:
    """Return a *new* ratings dict with one game's zero-sum update applied.

    Deliberately unclamped: clip only the values you ultimately display, not
    the running numbers, or a bad team that ever touches the floor freezes
    there permanently (every future loss reclips to the same floor value
    instead of continuing to separate the merely-bad from the historically
    awful -- see solve_season_to_date for the same reasoning).
    """
    r_home = ratings[game.home]
    r_away = ratings[game.away]
    hfa = 0.0 if game.neutral_site else HOME_FIELD_ADV

    expected_home = _expected_score(r_home + hfa, r_away)
    actual_home = 1.0 if game.home_won else 0.0

    winner_rating_diff = (r_home - r_away) if game.home_won else (r_away - r_home)
    mov_mult = _mov_multiplier(game.home_points - game.away_points, winner_rating_diff)

    delta = BASE_K * mov_mult * (actual_home - expected_home)

    new_ratings = dict(ratings)
    new_ratings[game.home] = r_home + delta
    new_ratings[game.away] = r_away - delta
    return new_ratings


def solve_season_to_date(
    initial_ratings: dict[str, float],
    games_played: list[Game],
) -> dict[str, float]:
    """Fixed-point recompute: replay the whole season so far, repeatedly,
    each pass using the *previous pass's* ending ratings as the opponent
    strength for every game's expected-score calculation. This is what
    makes later-revealed information (an opponent collapsing or surging
    after you played them) retroactively reshape today's ratings.

    Ratings are left unclamped during the replay itself -- each game's delta
    is zero-sum (added to one team, subtracted from the other), so clamping
    mid-pass would break that conservation and, worse, permanently freeze any
    team that ever touches the floor: every subsequent loss would just
    reclip to the same floor value instead of continuing to separate a
    one-loss-away-from-bad team from a historically awful one. The result is
    clipped to the display range exactly once, at the very end.

    A naive Jacobi update here (replacing `current` outright with each pass's
    `working` result) oscillates indefinitely instead of settling -- the
    max-move distance bounces around rather than shrinking. Blending in only
    RELAXATION of each new pass (under-relaxation) damps that oscillation and
    reaches a true fixed point in ~10-15 passes.
    """
    current = dict(initial_ratings)
    for _ in range(CONVERGENCE_ITERATIONS):
        working = dict(initial_ratings)  # always replay from the preseason prior
        opponent_strength = current      # ...but judge opponents by their latest form
        for game in games_played:
            r_home = working[game.home]
            r_away = working[game.away]
            opp_home = opponent_strength[game.home]
            opp_away = opponent_strength[game.away]
            hfa = 0.0 if game.neutral_site else HOME_FIELD_ADV

            expected_home = _expected_score(r_home + hfa, opp_away)
            actual_home = 1.0 if game.home_won else 0.0
            winner_rating_diff = (opp_home - opp_away) if game.home_won else (opp_away - opp_home)
            mov_mult = _mov_multiplier(game.home_points - game.away_points, winner_rating_diff)
            delta = BASE_K * mov_mult * (actual_home - expected_home)

            working[game.home] = r_home + delta
            working[game.away] = r_away - delta

        next_current = {
            team: RELAXATION * working[team] + (1 - RELAXATION) * current[team] for team in working
        }
        max_move = max(abs(next_current[t] - current[t]) for t in next_current)
        current = next_current
        if max_move < CONVERGENCE_EPSILON:
            break
    return {team: _clip(rating) for team, rating in current.items()}
