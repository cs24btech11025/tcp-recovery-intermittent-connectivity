#!/usr/bin/env python3
"""Paper-style outage schedules for Phase B (150 s runs).

Rates and mean durations: Table 3 of Wang et al., arXiv 1812.04823v1 (preprint). The plan says
VERIFY against the published MobiCom PDF, and whether the table values are means or medians.
ASSUMPTION used here: they are MEANS.

Durations are lognormal (heavy-tailed) with the mean matched to the table and sigma chosen by
us from the quantile statements in the preprint (section 5.2, Fig. 12). This is OUR assumption:
  HO  (type I):  mean 0.21 s and "85% finish within 100 ms" force sigma ~ 2.6 (median ~ 6.5 ms:
                 most events are shorter than tc can inject; a few last 0.5-3 s).
  RLF (type II): sigma 1.0: median > 1 s ("more than half last > 1 s"), P75 ~ 2.3 s (paper: ~2 s).
  NAS (type III): sigma 1.4755 gives P75 = 5 s with mean 5.49 s (paper: top 25% exceed 5 s).
  The 300 km/h entries reuse the same sigmas (unchecked).
Durations are clamped to [MIN_DUR, MAX_DUR]; the clamp lowers the effective mean (see --check).
Event counts per run are Poisson around the table rate. Events are sorted by time and pushed
later so that each starts at least MIN_GAP after the previous one ends; events that no longer fit
in the run are dropped (this slightly lowers the realized rate). Same seed -> same schedule for
every strategy. mode "fixed" uses the mean for every event (the "average-only" sensitivity case);
mode "none" gives an empty schedule (baseline).

  python3 outage_schedule.py --check             # implied distribution statistics
  python3 outage_schedule.py --seed 3            # print one schedule
"""
import argparse
import json
import math
import random

# (mean duration s, events per 150 s trace, lognormal sigma)
RATES = {
    "350": {"HO": (0.21, 14.70, 2.6), "RLF": (1.91, 2.00, 1.0), "NAS": (5.49, 1.49, 1.4755)},
    "300": {"HO": (0.16, 9.36, 2.6), "RLF": (2.09, 0.96, 1.0), "NAS": (3.98, 0.84, 1.4755)},
}
MIN_DUR, MAX_DUR = 0.05, 15.0
WARMUP, MIN_GAP = 5.0, 2.0


def poisson(rng, lam):
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def draw(rng, mean, sigma, mode):
    if mode == "fixed":
        return mean
    d = rng.lognormvariate(math.log(mean) - sigma ** 2 / 2, sigma)
    return min(MAX_DUR, max(MIN_DUR, d))


def make_schedule(seed, speed="350", mode="fitted", run_len=150.0):
    if mode == "none":
        return []
    rng = random.Random(f"phaseb-{speed}-{seed}")
    ev = []
    scale = run_len / 150.0
    for typ, (mean, rate, sigma) in RATES[speed].items():
        for _ in range(poisson(rng, rate * scale)):
            ev.append([typ, rng.uniform(WARMUP, run_len - 5.0), draw(rng, mean, sigma, mode)])
    ev.sort(key=lambda e: e[1])
    out, free = [], WARMUP
    for typ, start, dur in ev:
        start = max(start, free)
        if start + dur > run_len - 2.0:
            continue
        out.append(dict(type=typ, start=round(start, 3), dur=round(dur, 3)))
        free = start + dur + MIN_GAP
    return out


def check(speed):
    rng = random.Random(0)
    print(f"speed {speed} km/h  (clamp {MIN_DUR}-{MAX_DUR} s)")
    print("type  paper_mean  eff_mean  median  frac<0.1s  frac>1s   P75    P95   events/run  outage_s/run")
    total = 0.0
    for typ, (mean, rate, sigma) in RATES[speed].items():
        xs = sorted(draw(rng, mean, sigma, "fitted") for _ in range(20000))
        q = lambda p: xs[int(p * len(xs))]
        eff = sum(xs) / len(xs)
        total += rate * eff
        print(f"{typ:4s} {mean:10.2f} {eff:9.2f} {q(.5):7.3f} {sum(x < .1 for x in xs) / len(xs):10.2f} "
              f"{sum(x > 1 for x in xs) / len(xs):8.2f} {q(.75):6.2f} {q(.95):6.2f} {rate:11.2f} {rate * eff:13.1f}")
    print(f"expected total commanded outage per 150 s run: {total:.1f} s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--speed", default="350", choices=list(RATES))
    ap.add_argument("--mode", default="fitted", choices=["fitted", "fixed", "none"])
    a = ap.parse_args()
    if a.check:
        check(a.speed)
    else:
        print(json.dumps(make_schedule(a.seed, a.speed, a.mode), indent=1))
