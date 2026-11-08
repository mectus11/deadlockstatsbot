"""Aggregate overall stats from a list of matches."""
from collections import defaultdict
from dataclasses import dataclass, field

from models import Match

MAX_MATCHES = 200


@dataclass
class HeroLine:
    hero_id: int
    games: int
    wins: int

    @property
    def winrate(self) -> float | None:
        return self.wins / self.games * 100 if self.games else None


@dataclass
class Overall:
    matches: int = 0
    wins: int = 0
    losses: int = 0
    avg_kills: float | None = None
    avg_deaths: float | None = None
    avg_assists: float | None = None
    kda: float | None = None
    souls_per_min: float | None = None
    top_heroes: list[HeroLine] = field(default_factory=list)
    streak: int = 0            # consecutive wins or losses, most recent match first
    streak_won: bool | None = None

    @property
    def winrate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided * 100 if decided else None


def _avg(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def compute_overall(matches: list[Match], top_n: int = 5) -> Overall:
    ms = matches[:MAX_MATCHES]  # history is newest-first
    o = Overall(matches=len(ms))
    o.wins = sum(1 for m in ms if m.won is True)
    o.losses = sum(1 for m in ms if m.won is False)

    k = [m.kills for m in ms if m.kills is not None]
    d = [m.deaths for m in ms if m.deaths is not None]
    a = [m.assists for m in ms if m.assists is not None]
    o.avg_kills, o.avg_deaths, o.avg_assists = _avg(k), _avg(d), _avg(a)
    if k and d and a and sum(d) > 0:
        o.kda = (sum(k) + sum(a)) / sum(d)
    elif k and a and d:
        o.kda = float(sum(k) + sum(a))  # no deaths at all

    spm = [m.net_worth / (m.duration_s / 60) for m in ms
           if m.net_worth and m.duration_s and m.duration_s > 0]
    o.souls_per_min = _avg(spm)

    games: dict[int, int] = defaultdict(int)
    wins: dict[int, int] = defaultdict(int)
    for m in ms:
        if m.hero_id is None:
            continue
        games[m.hero_id] += 1
        if m.won:
            wins[m.hero_id] += 1
    lines = [HeroLine(h, g, wins[h]) for h, g in games.items()]
    lines.sort(key=lambda h: (-h.games, -h.wins))
    o.top_heroes = lines[:top_n]

    for m in ms:  # newest first: count consecutive wins/losses from the most recent match
        if m.won is None:
            break
        if o.streak_won is None:
            o.streak_won = m.won
        if m.won != o.streak_won:
            break
        o.streak += 1
    return o
