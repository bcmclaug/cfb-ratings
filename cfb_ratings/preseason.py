"""Build preseason ratings for a new season from the previous season's final
ratings plus real offseason movement (coaching changes, transfer portal,
recruiting class). See offseason.py for the blend itself.
"""
from __future__ import annotations

import argparse
import csv
import json

from .cfbd_client import CfbdClient, DATA_DIR
from .main import load_season
from .model import solve_season_to_date, compute_initial_ratings
from .offseason import (
    compute_preseason_ratings,
    fetch_coaching_changes,
    fetch_portal_net,
    fetch_recruiting_points,
    fetch_talent,
)

HISTORY_DIR = DATA_DIR / "history"


def final_ratings_and_records_for(
    client: CfbdClient, year: int
) -> tuple[dict[str, float], dict[str, tuple[int, int]]]:
    team_names, fcs_set, prior_srs, talent, all_games = load_season(client, year)
    initial = compute_initial_ratings(team_names, prior_srs, talent, fcs_team_names=fcs_set)
    final = solve_season_to_date(initial, all_games)

    records = {t: [0, 0] for t in team_names}
    for g in all_games:
        if g.home_won:
            records[g.home][0] += 1
            records[g.away][1] += 1
        else:
            records[g.away][0] += 1
            records[g.home][1] += 1
    return final, {t: (w, l) for t, (w, l) in records.items()}


def export_fbs_csv(
    out_path, preseason: dict[str, float], fbs_set: set[str], prior_records: dict[str, tuple[int, int]], prior_year: int
) -> None:
    rows = [(team, preseason[team]) for team in fbs_set if team in preseason]
    rows.sort(key=lambda r: r[1], reverse=True)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Rank", "School", "Rating", f"{prior_year} Record"])
        for i, (team, rating) in enumerate(rows, start=1):
            w, l = prior_records.get(team, (0, 0))
            writer.writerow([i, team, f"{rating:.2f}", f"{w}-{l}"])


def run(prior_year: int, new_year: int, api_key: str | None, no_cache: bool, export_csv: bool) -> None:
    client = CfbdClient(api_key=api_key, use_cache=not no_cache)

    print(f"Computing final {prior_year} ratings (carry-over baseline)...")
    final_prior, prior_records = final_ratings_and_records_for(client, prior_year)

    fbs_set = {t["school"] for t in client.teams(new_year)}
    fcs_set = {t["school"] for t in client.fcs_teams(new_year)}
    team_names = sorted(fbs_set | fcs_set)

    talent = fetch_talent(client, new_year)
    recruiting_points = fetch_recruiting_points(client, new_year)
    portal_net = fetch_portal_net(client, new_year)
    coaching_changed = fetch_coaching_changes(client, new_year)

    preseason = compute_preseason_ratings(
        team_names, final_prior, talent, recruiting_points, portal_net, coaching_changed
    )

    ranked = sorted(preseason.items(), key=lambda kv: kv[1], reverse=True)

    print(f"\n{new_year} Preseason Ratings (built from {prior_year} results + offseason movement)")
    print("-" * 78)
    print(f"{'Rk':>3}  {'Team':<28} {'Rating':>7}  {'Prior':>7}  {'Coach?':>7}  {'Div':>4}")
    for i, (team, rating) in enumerate(ranked, start=1):
        prior = final_prior.get(team)
        prior_s = f"{prior:.2f}" if prior is not None else "—"
        coach_tag = "NEW" if team in coaching_changed else ""
        div = "FBS" if team in fbs_set else "FCS"
        print(f"{i:>3}  {team:<28} {rating:>7.2f}  {prior_s:>7}  {coach_tag:>7}  {div:>4}")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = HISTORY_DIR / f"{new_year}_preseason.json"
    out_path.write_text(
        json.dumps(
            {
                "year": new_year,
                "based_on": prior_year,
                "ratings": preseason,
                "prior_final_ratings": final_prior,
                "coaching_changed": sorted(coaching_changed),
            },
            indent=2,
        )
    )
    print(f"\nSaved snapshot to {out_path}")

    if export_csv:
        csv_path = HISTORY_DIR / f"{new_year}_preseason_fbs_table.csv"
        export_fbs_csv(csv_path, preseason, fbs_set, prior_records, prior_year)
        print(f"Saved FBS table (Rank, School, Rating, {prior_year} Record) to {csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build preseason ratings from offseason data")
    parser.add_argument("--prior-year", type=int, required=True, help="Season to carry forward from, e.g. 2025")
    parser.add_argument("--new-year", type=int, required=True, help="Season to build a preseason prior for, e.g. 2026")
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--export-csv", action="store_true", help="Also write a Rank/School/Rating/Record CSV for FBS teams"
    )
    args = parser.parse_args()
    run(args.prior_year, args.new_year, args.api_key, args.no_cache, args.export_csv)


if __name__ == "__main__":
    main()
