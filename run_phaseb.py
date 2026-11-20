#!/usr/bin/env python3
"""Phase B: one 150 s HTTP download with a seeded paper-style outage schedule.

Needs setup_link.sh already run, plus run_one.py, http_range_server.py, stall_client.py and
outage_schedule.py in the same folder. Outages use drop mode (100% loss both directions), which
the plan calls the pessimistic bound for types I and II.

One run:
  sudo python3 run_phaseb.py --cc cubic --strategy watchdog --N 1 --seed 3
  strategies: plain (--cc cubic|bbr), tuned (rto_min 50 ms, cubic), watchdog, oracle
  --mode fitted (heavy-tailed durations) | fixed (every event at the table mean) | none (baseline)

Sweep (baselines 3 seeds x 2 cc, then --reps seeds x 5 strategies; ~2.6 min per run):
  sudo python3 run_phaseb.py --sweep --reps 6
Outputs data/b_<label>_<speed>_<mode>_loss<P>_seed<S>.{json,meta,ss}
Metric: goodput over the whole run = bytes received / elapsed time (the 200 MB file never finishes).
"""
import argparse
import json
import os
import subprocess
import time

from outage_schedule import make_schedule
from run_one import ns, sh, set_link

HERE = os.path.dirname(os.path.abspath(__file__))
DEVNULL = subprocess.DEVNULL


def run(cc, strat, N, seed, speed, mode, loss, mb, out, rto_min, run_len, pcap=False):
    os.makedirs(out, exist_ok=True)
    label = {"plain": cc, "tuned": f"tuned_{cc}", "watchdog": f"watchdog_N{N:g}",
             "oracle": "oracle"}[strat]
    tag = f"b_{label}_{speed}_{mode}_loss{loss}_seed{seed}"
    base = os.path.abspath(os.path.join(out, tag))
    marker = base + ".marker"
    if os.path.exists(marker):
        os.remove(marker)
    sched = make_schedule(seed, speed, mode, run_len)

    set_link(None, None, loss)
    route = ["ip", "route", "replace", "10.0.0.0/24", "dev", "veth-s", "scope", "link"]
    sh(ns("srv", route + (["rto_min", f"{rto_min}ms"] if strat == "tuned" else [])))

    srv = subprocess.Popen(
        ns("srv", ["python3", os.path.join(HERE, "http_range_server.py"),
                   "--cc", cc, "--size-mb", str(mb)]), stdout=DEVNULL, stderr=DEVNULL)
    time.sleep(1.0)
    cap = None
    if pcap:
        cap = subprocess.Popen(ns("cli", ["tcpdump", "-i", "veth-c", "-U", "-s", "96",
                                          "-w", base + ".pcap"]), stdout=DEVNULL, stderr=DEVNULL)
        time.sleep(0.5)

    cs = strat if strat in ("watchdog", "oracle") else "plain"
    t_start = time.time()
    cli = subprocess.Popen(
        ns("cli", ["python3", os.path.join(HERE, "stall_client.py"), "--strategy", cs,
                   "--N", str(N), "--marker", marker, "--out", base + ".json",
                   "--max-time", str(run_len)]))

    time.sleep(2.0)  # confirm the congestion control in use (first outage starts after 5 s)
    with open(base + ".ss", "w") as f:
        f.write(subprocess.run(ns("srv", ["ss", "-tin"]), capture_output=True, text=True).stdout)

    realized = []
    for ev in sched:
        time.sleep(max(0.0, t_start + ev["start"] - time.time()))
        t_down = time.time()
        set_link(100, 100, loss)
        time.sleep(ev["dur"])
        t_up = time.time()
        set_link(None, None, loss)
        with open(marker, "w") as f:  # oracle's signal: the link is back
            f.write(str(time.time()))
        realized.append(dict(ev, t_down=round(t_down - t_start, 3), t_up=round(t_up - t_start, 3)))

    try:
        cli.wait(timeout=run_len + 40)
    except subprocess.TimeoutExpired:
        cli.kill()
    srv.terminate()
    if cap:
        cap.terminate()
    sh(ns("srv", route))

    meta = dict(label=label, cc=cc, strategy=strat, N=N, seed=seed, speed=speed, mode=mode,
                loss=loss, mb=mb, run_len=run_len, events=realized, t_start=t_start,
                rto_min_ms=rto_min if strat == "tuned" else None,
                uname=subprocess.check_output(["uname", "-r"], text=True).strip())
    json.dump(meta, open(base + ".meta", "w"), indent=2)
    print("done", tag, f"({len(realized)} outages)", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc", default="cubic", choices=["cubic", "bbr"])
    ap.add_argument("--strategy", default="plain", choices=["plain", "tuned", "watchdog", "oracle"])
    ap.add_argument("--N", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--speed", default="350", choices=["350", "300"])
    ap.add_argument("--mode", default="fitted", choices=["fitted", "fixed", "none"])
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--mb", type=int, default=200)
    ap.add_argument("--rto-min", type=int, default=50)
    ap.add_argument("--run-len", type=float, default=150.0)
    ap.add_argument("--out", default="data")
    ap.add_argument("--pcap", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--reps", type=int, default=6)
    a = ap.parse_args()

    if not a.sweep:
        run(a.cc, a.strategy, a.N, a.seed, a.speed, a.mode, a.loss, a.mb, a.out, a.rto_min,
            a.run_len, a.pcap)
    else:
        for s in range(1, 4):  # baselines: no outages
            for cc in ("cubic", "bbr"):
                run(cc, "plain", 1.0, s, a.speed, "none", a.loss, a.mb, a.out, a.rto_min, a.run_len)
        configs = [("cubic", "plain"), ("bbr", "plain"), ("cubic", "tuned"),
                   ("cubic", "watchdog"), ("cubic", "oracle")]
        for s in range(1, a.reps + 1):
            for cc, strat in configs:
                run(cc, strat, 1.0, s, a.speed, a.mode, a.loss, a.mb, a.out, a.rto_min, a.run_len)
