"""
Предрасчёт префлоп-таблицы: 169 стартовых рук × 1–9 соперников × диапазоны.

Проход 1 — против любых рук; по нему строится рейтинг рук (он же задаёт диапазоны).
Проход 2 — против средних и крепких диапазонов. Результат — preflop.json.

    python build_preflop.py [итераций на точку, по умолчанию 30000]
"""

import json
import sys
import time
from multiprocessing import Pool

import engine

ITERS = int(sys.argv[1]) if len(sys.argv) > 1 else 30000
MAX_OPP = 9


def _init(order):
    if order:
        engine.set_order(order)


def _job(args):
    rng, cls, opp = args
    a, b = engine.class_combos(cls)[0]
    r = engine.simulate([engine.ALL_CARDS[a], engine.ALL_CARDS[b]], [], opp, rng, ITERS)
    return rng, cls, opp, [round(r["win"], 4), round(r["tie"], 4), round(r["eq"], 4)]


def run(ranges, order):
    tasks = [(rng, cls, opp) for rng in ranges for cls in engine.CLASSES for opp in range(1, MAX_OPP + 1)]
    out = {rng: {cls: [None] * MAX_OPP for cls in engine.CLASSES} for rng in ranges}
    with Pool(initializer=_init, initargs=(order,)) as pool:
        for i, (rng, cls, opp, vals) in enumerate(pool.imap_unordered(_job, tasks, chunksize=8), 1):
            out[rng][cls][opp - 1] = vals
            if i % 300 == 0:
                print(f"{i}/{len(tasks)}", flush=True)
    return out


def main():
    t = time.time()
    table = run(["any"], None)
    # Рейтинг: средний ранг руки против 1 и против 4 соперников —
    # учитывает и силу старшей карты, и играбельность в многопотовых раздачах
    rank1 = sorted(engine.CLASSES, key=lambda c: -table["any"][c][0][2])
    rank4 = sorted(engine.CLASSES, key=lambda c: -table["any"][c][3][2])
    order = sorted(engine.CLASSES, key=lambda c: rank1.index(c) + rank4.index(c))
    table.update(run([r for r in engine.RANGES if r != "any"], order))
    table["order"] = order
    engine.TABLE_PATH.write_text(json.dumps(table, separators=(",", ":")))
    print(f"готово за {time.time() - t:.0f} с, топ-10: {order[:10]}")


if __name__ == "__main__":
    main()
