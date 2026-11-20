#!/usr/bin/env python3
"""Mitigation experiments: one HTTP download with one injected outage per run.

Needs setup_link.sh already run (buffer/loss config), run_one.py in the same folder
(imports its link helpers), http_range_server.py and stall_client.py in the same folder.

One run:
  sudo python3 run_mitig.py --cc cubic --strategy watchdog --N 1 --outage 3 --seed 1
  strategies: plain (vanilla, --cc cubic|bbr), tuned (lower rto_min on the server route,
              cubic), watchdog (stall-aware client, threshold --N seconds), oracle
  --outage 0 means no outage (baseline download time)

Sweep (about 200 runs, ~1 h with the defaults):
  sudo python3 run_mitig.py --sweep --reps 5 --loss 0
Outputs data/m_<label>_out<L>_loss<P>_seed<S>.{json,meta,ss}  (+ .pcap with --pcap)
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time

from run_one import ns, sh, set_link

HERE = os.path.dirname(os.path.abspath(__file__))
DEVNULL = subprocess.DEVNULL


def run(cc, strat, N, outage, seed, loss, mb, out, rto_min, pcap=False):
    os.makedirs(out, exist_ok=True)
    label = {"plain": cc, "tuned": f"tuned_{cc}", "watchdog": f"watchdog_N{N:g}",
             "oracle": "oracle"}[strat]
    tag = f"m_{label}_out{outage}_loss{loss}_seed{seed}"
    base = os.path.abspath(os.path.join(out, tag))
    marker = base + ".marker"
    if os.path.exists(marker):
        os.remove(marker)
    rng = random.Random(f"{seed}-{outage}")
    t_cut = rng.uniform(3.0, 6.0)

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
                   "--N", str(N), "--marker", marker, "--out", base + ".json"]))

    time.sleep(2.0)  # confirm the congestion control actually in use
    with open(base + ".ss", "w") as f:
        f.write(subprocess.run(ns("srv", ["ss", "-tin"]), capture_output=True, text=True).stdout)

    t_down = t_up = None
    if outage > 0:
        time.sleep(max(0.0, t_start + t_cut - time.time()))
        t_down = time.time()
        set_link(100, 100, loss)
        time.sleep(outage)
        t_up = time.time()
        set_link(None, None, loss)
        with open(marker, "w") as f:  # oracle's signal: the link is back
            f.write(str(time.time()))

    try:
        cli.wait(timeout=170)
    except subprocess.TimeoutExpired:
        cli.kill()
    srv.terminate()
    if cap:
        cap.terminate()
    sh(ns("srv", route))  # reset rto_min

    meta = dict(label=label, cc=cc, strategy=strat, N=N, outage=outage, seed=seed, loss=loss,
                mb=mb, rto_min_ms=rto_min if strat == "tuned" else None,
                t_start=t_start, t_cut_cmd=t_down, t_restore_cmd=t_up,
                uname=subprocess.check_output(["uname", "-r"], text=True).strip())
    json.dump(meta, open(base + ".meta", "w"), indent=2)
    print("done", tag, flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc", default="cubic", choices=["cubic", "bbr"])
    ap.add_argument("--strategy", default="plain", choices=["plain", "tuned", "watchdog", "oracle"])
    ap.add_argument("--N", type=float, default=1.0)
    ap.add_argument("--outage", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--mb", type=int, default=15)
    ap.add_argument("--rto-min", type=int, default=50)
    ap.add_argument("--out", default="data")
    ap.add_argument("--pcap", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--lengths", default="0.5,1,2,3,5")
    a = ap.parse_args()

    if not a.sweep:
        run(a.cc, a.strategy, a.N, a.outage, a.seed, a.loss, a.mb, a.out, a.rto_min, a.pcap)
        sys.exit(0)

    lengths = [float(x) for x in a.lengths.split(",")]
    configs = [("cubic", "plain", 1.0), ("bbr", "plain", 1.0), ("cubic", "tuned", 1.0),
               ("cubic", "watchdog", 0.5), ("cubic", "watchdog", 1.0),
               ("cubic", "watchdog", 2.0), ("cubic", "oracle", 1.0)]
    for s in range(1, a.reps + 1):  # baselines (no outage) for the two plain configs
        for cc in ("cubic", "bbr"):
            run(cc, "plain", 1.0, 0.0, s, a.loss, a.mb, a.out, a.rto_min)
    for s in range(1, a.reps + 1):
        for L in lengths:
            for cc, strat, N in configs:
                run(cc, strat, N, L, s, a.loss, a.mb, a.out, a.rto_min)
