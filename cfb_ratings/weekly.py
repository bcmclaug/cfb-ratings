"""Week-by-week rating history for a season: recomputes the full retroactive
solve as of each week (0 = preseason, through the latest completed week) so
you can see how a team's rating actually moved over the season, not just
where it ended up.
"""
from __future__ import annotations

import argparse
import csv

from .cfbd_client import CfbdClient, DATA_DIR
from .main import load_initial_ratings, load_season
from .model import solve_season_to_date

HISTORY_DIR = DATA_DIR / "history"


def weekly_rating_history(
    client: CfbdClient, year: int
) -> tuple[list[int], dict[str, dict[int, float]], set[str]]:
    team_names, fcs_set, prior_srs, talent, all_games = load_season(client, year)
    initial = load_initial_ratings(year, team_names, fcs_set, prior_srs, talent)
    fbs_set = set(team_names) - fcs_set

    max_week = max((g.week for g in all_games), default=0)
    weeks = list(range(0, max_week + 1))

    history: dict[str, dict[int, float]] = {t: {0: initial[t]} for t in team_names}
    for wk in weeks[1:]:
        games_to_date = [g for g in all_games if g.week <= wk]
        final = solve_season_to_date(initial, games_to_date)
        for t in team_names:
            history[t][wk] = final[t]
    return weeks, history, fbs_set


def export_csv(out_path, weeks: list[int], history: dict[str, dict[int, float]], team_pool: set[str]) -> None:
    latest = weeks[-1]
    rows = sorted(team_pool, key=lambda t: history[t][latest], reverse=True)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        header = ["Rank", "School"] + (["Preseason"] if weeks[0] == 0 else []) + [f"Wk{w}" for w in weeks[1:]]
        header.append("Δ Last Wk")
        writer.writerow(header)
        for i, team in enumerate(rows, start=1):
            cells = [i, team] + [f"{history[team][w]:.2f}" for w in weeks]
            delta = history[team][latest] - history[team][weeks[-2]] if len(weeks) > 1 else 0.0
            cells.append(f"{delta:+.2f}")
            writer.writerow(cells)


def run(year: int, api_key: str | None, no_cache: bool, fbs_only: bool, export_csv_flag: bool) -> None:
    client = CfbdClient(api_key=api_key, use_cache=not no_cache)
    weeks, history, fbs_set = weekly_rating_history(client, year)
    team_pool = fbs_set if fbs_only else set(history)
    latest = weeks[-1]

    rows = sorted(team_pool, key=lambda t: history[t][latest], reverse=True)

    print(f"\n{year} Rating History — Preseason through Week {latest}\n{'-' * 90}")
    header = f"{'Rk':>3}  {'Team':<24} {'Pre':>6}" + "".join(f"  Wk{w:<4}" for w in weeks[1:]) + "  Δ Last"
    print(header)
    for i, team in enumerate(rows, start=1):
        cells = f"{i:>3}  {team:<24} {history[team][0]:>6.2f}"
        for w in weeks[1:]:
            cells += f"  {history[team][w]:>6.2f}"
        delta = history[team][latest] - history[team][weeks[-2]] if len(weeks) > 1 else 0.0
        cells += f"  {delta:+.2f}"
        print(cells)

    if export_csv_flag:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        scope = "fbs" if fbs_only else "all"
        out_path = HISTORY_DIR / f"{year}_weekly_history_{scope}.csv"
        export_csv(out_path, weeks, history, team_pool)
        print(f"\nSaved weekly history table to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Week-by-week rating history for a season")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--fbs-only", action="store_true", help="Restrict to FBS teams (default: all teams)")
    parser.add_argument("--export-csv", action="store_true", help="Also write the full table to a CSV file")
    args = parser.parse_args()
    run(args.year, args.api_key, args.no_cache, args.fbs_only, args.export_csv)


if __name__ == "__main__":
    main()
