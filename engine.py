"""
Покерный движок: Монте-Карло на eval7, предрасчитанная префлоп-таблица,
диапазоны рук соперников и подсчёт аутов.

Карты — строки вида "As", "Th", "2c". Функции модуля вызываются в отдельных
процессах (ProcessPoolExecutor), поэтому принимают и возвращают простые типы.
"""

import json
import os
import random
from itertools import combinations
from pathlib import Path

import eval7

RANKS = "AKQJT98765432"
SUITS = "shdc"
ALL_CARDS = [r + s for r in RANKS for s in SUITS]
IDX = {c: i for i, c in enumerate(ALL_CARDS)}
EV = [eval7.Card(c) for c in ALL_CARDS]

# Типы комбинаций eval7 от слабой к сильной
TYPES = ["High Card", "Pair", "Two Pair", "Trips", "Straight",
         "Flush", "Full House", "Quads", "Straight Flush"]
TYPE_IDX = {t: i for i, t in enumerate(TYPES)}
MADE_HAND = TYPE_IDX["Straight"]  # стрит и выше — «настоящее» усиление даже при спаренном столе

# Диапазоны соперников: доля лучших стартовых рук, с которыми они играют
RANGES = {"any": 1.0, "mid": 0.40, "tight": 0.20}

SIMULATIONS = int(os.getenv("SIMULATIONS", "20000"))
TABLE_PATH = Path(__file__).with_name("preflop.json")


# ---------- стартовые руки ----------

def hand_class(a: str, b: str) -> str:
    """Класс стартовой руки: "AA", "AKs", "T9o"."""
    ra, rb = RANKS.index(a[0]), RANKS.index(b[0])
    if ra > rb:
        a, b = b, a
    if a[0] == b[0]:
        return a[0] * 2
    return a[0] + b[0] + ("s" if a[1] == b[1] else "o")


CLASSES = [RANKS[i] + RANKS[j] + suf if i != j else RANKS[i] * 2
           for i in range(13) for j in range(i, 13)
           for suf in (("",) if i == j else ("s", "o"))]


def class_combos(cls: str) -> list:
    if len(cls) == 2:
        return [(IDX[cls[0] + s1], IDX[cls[1] + s2]) for s1, s2 in combinations(SUITS, 2)]
    if cls[2] == "s":
        return [(IDX[cls[0] + s], IDX[cls[1] + s]) for s in SUITS]
    return [(IDX[cls[0] + s1], IDX[cls[1] + s2]) for s1 in SUITS for s2 in SUITS if s1 != s2]


def range_combos(order: list, share: float) -> list:
    """Комбинации лучших `share` стартовых рук по рейтингу `order`."""
    target, out = share * 1326, []
    for cls in order:
        if len(out) >= target:
            break
        out += class_combos(cls)
    return out


_table = None
_ranges = {"any": None}


def set_order(order: list) -> None:
    for name, share in RANGES.items():
        if share < 1:
            _ranges[name] = range_combos(order, share)


def _load_table() -> None:
    global _table
    if TABLE_PATH.exists():
        _table = json.loads(TABLE_PATH.read_text())
        set_order(_table["order"])


_load_table()


def available_ranges() -> list:
    return [name for name in RANGES if name in _ranges]


# ---------- расчёт ----------

def simulate(hand, board, opp, rng="any", iters=SIMULATIONS) -> dict:
    """Монте-Карло против `opp` соперников из диапазона `rng`."""
    hero = [IDX[c] for c in hand]
    brd = [IDX[c] for c in board]
    dead0 = set(hero) | set(brd)
    deck = [i for i in range(52) if i not in dead0]
    need = 5 - len(brd)
    hero_ev = [EV[i] for i in hero]
    brd_ev = [EV[i] for i in brd]

    combos = _ranges.get(rng)
    usable = [c for c in combos if c[0] not in dead0 and c[1] not in dead0] if combos else None

    sample, choice = random.sample, random.choice
    evaluate, handtype = eval7.evaluate, eval7.handtype
    wins = ties = 0
    equity = 0.0
    dist = [0] * len(TYPES)

    for _ in range(iters):
        if not usable:
            d = sample(deck, 2 * opp + need)
            opps = [(d[2 * k], d[2 * k + 1]) for k in range(opp)]
            run = d[2 * opp:]
        else:
            dead = set(dead0)
            opps = []
            for _k in range(opp):
                for _t in range(30):
                    a, b = choice(usable)
                    if a not in dead and b not in dead:
                        break
                else:  # диапазон «выбит» мёртвыми картами — берём любые
                    a, b = sample([i for i in deck if i not in dead], 2)
                dead.add(a)
                dead.add(b)
                opps.append((a, b))
            run = []
            while len(run) < need:
                i = choice(deck)
                if i not in dead:
                    dead.add(i)
                    run.append(i)

        full = brd_ev + [EV[i] for i in run]
        mine = evaluate(hero_ev + full)
        dist[TYPE_IDX[handtype(mine)]] += 1
        best, cnt = -1, 0
        for a, b in opps:
            s = evaluate([EV[a], EV[b]] + full)
            if s > best:
                best, cnt = s, 1
            elif s == best:
                cnt += 1
        if mine > best:
            wins += 1
            equity += 1
        elif mine == best:
            ties += 1
            equity += 1 / (cnt + 1)

    return {
        "win": wins / iters,
        "tie": ties / iters,
        "eq": equity / iters,
        "dist": {TYPES[i]: n / iters for i, n in enumerate(dist) if n},
    }


def hand_type(cards) -> str:
    return eval7.handtype(eval7.evaluate([EV[IDX[c]] for c in cards]))


def outs(hand, board) -> dict:
    """Карты, которые на следующей улице усиливают именно вашу руку (не только стол)."""
    known = set(hand) | set(board)
    cur = TYPE_IDX[hand_type(hand + board)]
    board_old = TYPE_IDX[hand_type(board)]
    by_type, cards = {}, []
    for c in ALL_CARDS:
        if c in known:
            continue
        new = TYPE_IDX[hand_type(hand + board + [c])]
        board_new = TYPE_IDX[hand_type(board + [c])]
        if new <= cur or new <= board_new:
            continue
        # Пара/две пары за счёт спаривания стола — не настоящий аут
        if board_new > board_old and new < MADE_HAND:
            continue
        by_type[TYPES[new]] = by_type.get(TYPES[new], 0) + 1
        cards.append(c)
    unseen = 52 - len(known)
    n = len(cards)
    if len(board) == 3:  # две карты впереди
        hit = 1 - (unseen - n) * (unseen - n - 1) / (unseen * (unseen - 1))
    else:
        hit = n / unseen
    return {"count": n, "by": by_type, "cards": cards, "hit": hit}


def analyze(hand, board, opp, rng="any") -> dict:
    """Полный анализ ситуации. opp — число соперников в раздаче."""
    res = {}
    if len(hand) == 2 and len(board) >= 3:
        res["type"] = hand_type(hand + board)
    if len(hand) < 2 or len(board) in (1, 2):
        return res
    if not board and _table and rng in _table:
        win, tie, eq = _table[rng][hand_class(*hand)][opp - 1]
        res.update(win=win, tie=tie, eq=eq, exact=True)
    else:
        res.update(simulate(hand, board, opp, rng))
    if len(board) in (3, 4):
        res["outs"] = outs(hand, board)
    return res
