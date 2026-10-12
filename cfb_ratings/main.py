from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cfbd_client import CfbdClient, DATA_DIR
from .model import Game, compute_initial_ratings, solve_season_to_date

HISTORY_DIR = DATA_DIR / "history"


def load_season(
    client: CfbdClient, year: int
) -> tuple[list[str], set[str], dict[str, float], dict[str, float], list[Game]]:
    fbs_set = {t["school"] for t in client.teams(year)}
    fcs_set = {t["school"] for t in client.fcs_teams(year)}
    team_names = sorted(fbs_set | fcs_set)

    prior_srs_raw = client.srs(year - 1)
    prior_srs = {row["team"]: row["rating"] for row in prior_srs_raw if row.get("rating") is not None}

    talent_raw = client.talent(year)
    talent = {row["team"]: row["talent"] for row in talent_raw if row.get("talent") is not None}

    games_raw = client.games(year, season_type="regular")
    known_teams = fbs_set | fcs_set
    games: list[Game] = []
    for g in games_raw:
        if g.get("homePoints") is None or g.get("awayPoints") is None:
            continue  # not yet played
        if g["homeTeam"] not in known_teams or g["awayTeam"] not in known_teams:
            continue  # skip D-II/D-III opponents -- no ratings for them
        games.append(
            Game(
                home=g["homeTeam"],
                away=g["awayTeam"],
                home_points=g["homePoints"],
                away_points=g["awayPoints"],
                neutral_site=bool(g.get("neutralSite")),
                week=g["week"],
            )
        )
    games.sort(key=lambda g: g.week)
    return team_names, fcs_set, prior_srs, talent, games


def load_initial_ratings(
    year: int, team_names: list[str], fcs_set: set[str], prior_srs: dict[str, float], talent: dict[str, float]
) -> dict[str, float]:
    """Prefer a preseason snapshot (prior-season results + offseason coaching/
    portal/recruiting movement -- see preseason.py) when one has been built for
    this year; fall back to the plain recruiting/SRS bootstrap for any team it
    doesn't cover (or entirely, if no snapshot exists yet)."""
    preseason_path = HISTORY_DIR / f"{year}_preseason.json"
    bootstrap = compute_initial_ratings(team_names, prior_srs, talent, fcs_team_names=fcs_set)
    if not preseason_path.exists():
        return bootstrap

    snapshot_ratings = json.loads(preseason_path.read_text())["ratings"]
    return {team: snapshot_ratings.get(team, bootstrap[team]) for team in team_names}


def run(year: int, through_week: int | None, api_key: str | None, no_cache: bool, show_fcs: bool) -> None:
    client = CfbdClient(api_key=api_key, use_cache=not no_cache)
    team_names, fcs_set, prior_srs, talent, all_games = load_season(client, year)

    initial = load_initial_ratings(year, team_names, fcs_set, prior_srs, talent)

    max_week = through_week or max((g.week for g in all_games), default=0)
    games_to_date = [g for g in all_games if g.week <= max_week]

    final = solve_season_to_date(initial, games_to_date)

    records: dict[str, list[int]] = {t: [0, 0] for t in team_names}
    for g in games_to_date:
        if g.home_won:
            records[g.home][0] += 1
            records[g.away][1] += 1
        else:
            records[g.away][0] += 1
            records[g.home][1] += 1

    display_teams = team_names if show_fcs else [t for t in team_names if t not in fcs_set]
    ranked = sorted(((t, final[t]) for t in display_teams), key=lambda kv: kv[1], reverse=True)

    print(f"\nCFB Ratings — {year}, through week {max_week}\n{'-' * 60}")
    print(f"{'Rk':>3}  {'Team':<28} {'Rating':>7}  {'Record':>7}")
    for i, (team, rating) in enumerate(ranked, start=1):
        w, l = records[team]
        tag = " (FCS)" if team in fcs_set else ""
        print(f"{i:>3}  {team + tag:<28} {rating:>7.2f}  {w:>3}-{l:<3}")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = HISTORY_DIR / f"{year}_week{max_week}.json"
    out_path.write_text(json.dumps({"year": year, "week": max_week, "ratings": final, "initial": initial}, indent=2))
    print(f"\nSaved snapshot to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamic CFB rating system")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--through-week", type=int, default=None, help="Recompute using games through this week only")
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--no-cache", action="store_true", help="Bypass local response cache")
    parser.add_argument("--show-fcs", action="store_true", help="Include FCS teams in the printed table")
    args = parser.parse_args()
    run(args.year, args.through_week, args.api_key, args.no_cache, args.show_fcs)


if __name__ == "__main__":
    main()
