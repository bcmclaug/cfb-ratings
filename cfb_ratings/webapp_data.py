"""Regenerate the interactive ratings dashboard (ratings_dashboard.html) from
live CFBD data: week-by-week ratings/ranks/records for every FBS+FCS team,
each team's full schedule with opponent ratings and per-game rating deltas,
and that week's AP Poll (or CFP Committee Rankings, once those start) for
comparison.

Usage, after new games go final:
    python -m cfb_ratings.webapp_data --year 2026
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .cfbd_client import CfbdClient, DATA_DIR
from .main import load_initial_ratings, load_season
from .model import solve_season_to_date

HISTORY_DIR = DATA_DIR / "history"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "webapp_template.html"
DASHBOARD_PATH = PROJECT_ROOT / "ratings_dashboard.html"
EXPLORER_TEMPLATE_PATH = PROJECT_ROOT / "explorer_template.html"
EXPLORER_PATH = PROJECT_ROOT / "fbs_explorer.html"
SOS_TEMPLATE_PATH = PROJECT_ROOT / "sos_template.html"
SOS_PATH = PROJECT_ROOT / "sos_report.html"
DATA_PLACEHOLDER = "__DATA_JSON__"

# Published artifact URLs, for the cross-links between the pages. Update a
# *_URL constant here once that page has been published for the first time
# (it doesn't have a URL yet on its first build).
DASHBOARD_URL = "https://claude.ai/code/artifact/f7eb2f0e-644e-4b32-8337-a8391edace01"
EXPLORER_URL = "https://claude.ai/code/artifact/52cac5bd-d014-489e-aa53-d7ca0e475fa1"
SOS_URL = "https://claude.ai/code/artifact/61405ca5-9703-4407-931a-f1dade1b4edc"


def build_data(client: CfbdClient, year: int) -> dict:
    team_names, fcs_set, prior_srs, talent, all_games = load_season(client, year)
    initial = load_initial_ratings(year, team_names, fcs_set, prior_srs, talent)
    fbs_set = set(team_names) - fcs_set

    all_team_rows = client.teams(year) + client.fcs_teams(year)
    conference_by_team = {t["school"]: t.get("conference") for t in all_team_rows}
    color_by_team = {t["school"]: t.get("color") for t in all_team_rows}
    alt_color_by_team = {t["school"]: t.get("alternateColor") for t in all_team_rows}
    fbs_conferences = sorted({conference_by_team[t] for t in fbs_set if conference_by_team.get(t)})

    max_week = max((g.week for g in all_games), default=0)
    weeks = list(range(0, max_week + 1))
    week_labels = ["Preseason"] + [f"Week {w}" for w in weeks[1:]]

    ratings_by_week = {0: dict(initial)}
    for wk in weeks[1:]:
        games_to_date = [g for g in all_games if g.week <= wk]
        ratings_by_week[wk] = solve_season_to_date(initial, games_to_date)

    records_by_week = {0: {t: [0, 0] for t in team_names}}
    running = {t: [0, 0] for t in team_names}
    for wk in weeks[1:]:
        for g in all_games:
            if g.week == wk:
                if g.home_won:
                    running[g.home][0] += 1
                    running[g.away][1] += 1
                else:
                    running[g.away][0] += 1
                    running[g.home][1] += 1
        records_by_week[wk] = {t: list(v) for t, v in running.items()}

    ranks_by_week = {}
    for wk in weeks:
        ordered = sorted(fbs_set, key=lambda t: ratings_by_week[wk][t], reverse=True)
        ranks_by_week[wk] = {t: i + 1 for i, t in enumerate(ordered)}

    fbs_teams = sorted(fbs_set, key=lambda t: ratings_by_week[max_week][t], reverse=True)
    top25 = fbs_teams[:25]

    # Poll comparison: prefer the CFP Committee Rankings the moment they exist
    # (they start publishing in November), fall back to the AP Top 25 until then.
    #
    # CFBD labels a poll by the week it's released ENTERING -- "week N" comes out
    # after week N-1's games finish and reflects results through week N-1, not
    # week N. Our week index `w` is "standings after week w's games", so the poll
    # that's actually contemporaneous with our week `w` is CFBD's week `w + 1`
    # (and our week 0 preseason lines up with CFBD's week 1, which is itself the
    # preseason poll released before any games). Pairing them straight (CFBD week
    # N with our week N) silently compares our post-week-N standings against a
    # poll that's one week stale.
    poll_blobs = client.get("/rankings", year=year)
    poll_rank_by_week: dict[int, dict[str, int]] = {w: {} for w in weeks}
    poll_name_by_week: dict[int, str | None] = {w: None for w in weeks}
    for blob in poll_blobs:
        our_week = blob["week"] - 1
        if our_week not in poll_rank_by_week:
            continue
        polls = blob["polls"]
        chosen = next((p for p in polls if "playoff" in p["poll"].lower()), None) or next(
            (p for p in polls if p["poll"] == "AP Top 25"), None
        )
        if not chosen:
            continue
        poll_name_by_week[our_week] = chosen["poll"]
        for row in chosen["ranks"]:
            poll_rank_by_week[our_week][row["school"]] = row["rank"]

    schedules: dict[str, list[dict]] = {t: [] for t in team_names}
    for g in sorted(all_games, key=lambda g: g.week):
        before = ratings_by_week[g.week - 1]
        after = ratings_by_week[g.week]
        for team, opp, pts, opp_pts, is_home in [
            (g.home, g.away, g.home_points, g.away_points, True),
            (g.away, g.home, g.away_points, g.home_points, False),
        ]:
            delta = after[team] - before[team]
            schedules[team].append(
                {
                    "week": g.week,
                    "opponent": opp,
                    "opponentDiv": "FBS" if opp in fbs_set else "FCS",
                    "opponentRating": round(after[opp], 2),
                    "homeAway": "neutral" if g.neutral_site else ("home" if is_home else "away"),
                    "teamScore": pts,
                    "oppScore": opp_pts,
                    "result": "W" if pts > opp_pts else "L",
                    "ratingBefore": round(before[team], 2),
                    "ratingAfter": round(after[team], 2),
                    "delta": round(delta, 2),
                }
            )

    return {
        "weeks": weeks,
        "weekLabels": week_labels,
        "currentWeek": max_week,
        "top25": top25,
        "conferences": fbs_conferences,
        "explorerUrl": EXPLORER_URL,
        "sosUrl": SOS_URL,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "pollNameByWeek": [poll_name_by_week[w] for w in weeks],
        "teams": {
            t: {
                "division": "FBS" if t in fbs_set else "FCS",
                "conference": conference_by_team.get(t),
                "color": color_by_team.get(t),
                "altColor": alt_color_by_team.get(t),
                "ratings": [round(ratings_by_week[w][t], 2) for w in weeks],
                "ranks": [ranks_by_week[w].get(t) for w in weeks],
                "records": [f"{records_by_week[w][t][0]}-{records_by_week[w][t][1]}" for w in weeks],
                "pollRanks": [poll_rank_by_week[w].get(t) for w in weeks],
                "schedule": schedules[t],
            }
            for t in team_names
        },
    }


def build_dashboard_html(data: dict) -> str:
    template = TEMPLATE_PATH.read_text()
    if DATA_PLACEHOLDER not in template:
        raise RuntimeError(f"{TEMPLATE_PATH} is missing the {DATA_PLACEHOLDER} placeholder")
    return template.replace(DATA_PLACEHOLDER, json.dumps(data))


def build_explorer_data(data: dict) -> dict:
    """Slice the full dashboard data down to the current week's FBS teams --
    the explorer is a conference/rating browser for 'right now', not another
    week-by-week view."""
    wk = len(data["weeks"]) - 1
    rows = [
        {
            "team": t,
            "conference": d["conference"],
            "rating": d["ratings"][wk],
            "rank": d["ranks"][wk],
            "record": d["records"][wk],
        }
        for t, d in data["teams"].items()
        if d["division"] == "FBS"
    ]
    rows.sort(key=lambda r: r["rank"])
    ratings = [r["rating"] for r in rows]
    return {
        "weekLabel": data["weekLabels"][wk],
        "dashboardUrl": DASHBOARD_URL,
        "sosUrl": SOS_URL,
        "conferences": data["conferences"],
        "ratingMin": min(ratings),
        "ratingMax": max(ratings),
        "teams": rows,
    }


def build_explorer_html(explorer_data: dict) -> str:
    template = EXPLORER_TEMPLATE_PATH.read_text()
    if DATA_PLACEHOLDER not in template:
        raise RuntimeError(f"{EXPLORER_TEMPLATE_PATH} is missing the {DATA_PLACEHOLDER} placeholder")
    return template.replace(DATA_PLACEHOLDER, json.dumps(explorer_data))


def build_sos_data(data: dict) -> dict:
    """Strength of schedule per FBS team: the average rating of every opponent
    they've actually played so far (using each opponent's rating as of the week
    that game was played -- the same opponentRating already shown in the
    dashboard's per-team schedule view), not their full future schedule. A
    team with 0 games played (shouldn't happen this deep into a season, but
    cheap to guard) is skipped rather than reported with a misleading 0.0.
    """
    wk = len(data["weeks"]) - 1
    rows = []
    for t, d in data["teams"].items():
        if d["division"] != "FBS" or not d["schedule"]:
            continue
        opponents = [
            {"week": g["week"], "opponent": g["opponent"], "rating": g["opponentRating"], "result": g["result"]}
            for g in d["schedule"]
        ]
        ratings = [o["rating"] for o in opponents]
        sos = sum(ratings) / len(ratings)
        hardest = max(opponents, key=lambda o: o["rating"])
        easiest = min(opponents, key=lambda o: o["rating"])
        rows.append(
            {
                "team": t,
                "conference": d["conference"],
                "record": d["records"][wk],
                "sos": round(sos, 2),
                "gamesPlayed": len(opponents),
                "hardest": hardest,
                "easiest": easiest,
                "opponents": opponents,
            }
        )
    rows.sort(key=lambda r: r["sos"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["sosRank"] = i
    return {
        "weekLabel": data["weekLabels"][wk],
        "dashboardUrl": DASHBOARD_URL,
        "explorerUrl": EXPLORER_URL,
        "conferences": data["conferences"],
        "teams": rows,
    }


def build_sos_html(sos_data: dict) -> str:
    template = SOS_TEMPLATE_PATH.read_text()
    if DATA_PLACEHOLDER not in template:
        raise RuntimeError(f"{SOS_TEMPLATE_PATH} is missing the {DATA_PLACEHOLDER} placeholder")
    return template.replace(DATA_PLACEHOLDER, json.dumps(sos_data))


def run(year: int, api_key: str | None, use_cache: bool) -> None:
    # Defaults to bypassing the response cache: the whole point of a
    # regeneration run is to pick up games/polls that weren't final last time,
    # and the cache has no time-based expiry, so a stale hit would silently
    # look like a successful refresh with last week's data.
    client = CfbdClient(api_key=api_key, use_cache=use_cache)

    print(f"Fetching {year} season data from CFBD (ratings, polls, schedules)...")
    data = build_data(client, year)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    data_path = HISTORY_DIR / f"{year}_webapp_data.json"
    data_path.write_text(json.dumps(data))
    print(f"Saved data snapshot to {data_path}")

    html = build_dashboard_html(data)
    DASHBOARD_PATH.write_text(html)
    print(f"Rebuilt dashboard at {DASHBOARD_PATH} (through {data['weekLabels'][-1]})")

    explorer_data = build_explorer_data(data)
    explorer_html = build_explorer_html(explorer_data)
    EXPLORER_PATH.write_text(explorer_html)
    print(f"Rebuilt FBS explorer at {EXPLORER_PATH}")

    sos_data = build_sos_data(data)
    sos_html = build_sos_html(sos_data)
    SOS_PATH.write_text(sos_html)
    print(f"Rebuilt strength-of-schedule report at {SOS_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate the interactive ratings dashboard from live CFBD data")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Reuse cached API responses instead of refetching (fine for re-styling the page; "
        "skip this after new games/polls have gone final, or you'll rebuild with stale data)",
    )
    args = parser.parse_args()
    run(args.year, args.api_key, args.use_cache)


if __name__ == "__main__":
    main()
