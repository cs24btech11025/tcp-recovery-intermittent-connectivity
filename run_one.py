#!/usr/bin/env python3
"""One experiment run: iperf3 download + tcpdump + ss logger + outage injector.

Run as root, after setup_link.sh has built the link with the matching buffer/loss:
  sudo python3 run_one.py --algo cubic --buf 125 --outage 2.0 --seed 1 --loss 0

Outputs (in --out, default ./data), named algo_bufKB_outageS_lossP_seedN:
  .json  iperf3 per-interval results (100 ms)
  .pcap  capture at the client (use it for TRUE outage start/end and goodput)
  .ss    ss -ti samples on the server (confirms cubic/bbr; shows cwnd, rto)
  .meta  tc wall-clock times, kernel, sysctls (tc times are only a cross-check)

Sweep helper:  sudo python3 run_one.py --sweep --reps 5 --buf 125 --loss 0
(assumes setup_link.sh was already run for that buf/loss)
"""
import argparse, json, os, random, subprocess, time

LENGTHS = [0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]  # ~8-value grid, edit freely
DURATION = 30  # seconds per run
WARMUP = (8.0, 12.0)  # outage start drawn uniformly from this window


def ns(name, cmd):
    return ["ip", "netns", "exec", name] + cmd


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def netem_args(loss):
    a = ["delay", "50ms", "limit", "1000"]
    return a + (["loss", f"{loss}%"] if loss > 0 else [])


def set_link(data_loss, ack_loss, base_loss):
    """Data direction falls back to base_loss when data_loss is None."""
    dl = base_loss if data_loss is None else data_loss
    al = 0 if ack_loss is None else ack_loss
    sh(ns("srv", ["tc", "qdisc", "change", "dev", "veth-s", "root", "handle", "1:", "netem"] + netem_args(dl)))
    sh(ns("cli", ["tc", "qdisc", "change", "dev", "veth-c", "root", "netem"] + netem_args(al)))


def run(algo, buf, outage, seed, loss, out):
    os.makedirs(out, exist_ok=True)
    tag = f"{algo}_buf{buf}_out{outage}_loss{loss}_seed{seed}"
    base = os.path.join(out, tag)
    rng = random.Random(f"{seed}-{outage}")  # same seed -> same start for both algos
    t_cut = rng.uniform(*WARMUP)

    set_link(None, None, loss)  # make sure baseline is restored

    srv = subprocess.Popen(ns("srv", ["iperf3", "-s", "-1"]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    cap = subprocess.Popen(ns("cli", ["tcpdump", "-i", "veth-c", "-U", "-w", base + ".pcap"]),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sslog = open(base + ".ss", "w")
    ss = subprocess.Popen(
        ns("srv", ["bash", "-c", "while true; do date +%s.%N; ss -ti dst 10.0.0.2; sleep 0.2; done"]),
        stdout=sslog, stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    t_start = time.time()
    cli = subprocess.Popen(
        ns("cli", ["iperf3", "-c", "10.0.0.1", "-t", str(DURATION), "-i", "0.1", "-R", "-C", algo, "--json"]),
        stdout=open(base + ".json", "w"), stderr=subprocess.DEVNULL)

    time.sleep(max(0, t_start + t_cut - time.time()))
    t_down = time.time()
    set_link(100, 100, loss)  # drop mode: loss 100% both directions
    time.sleep(outage)
    t_up = time.time()
    set_link(None, None, loss)  # restore (baseline loss back on data direction)

    cli.wait()
    time.sleep(0.5)
    for p in (ss, cap):
        p.terminate()
    srv.terminate()
    sslog.close()

    meta = {
        "algo": algo, "buf_kb": buf, "outage_cmd_s": outage, "base_loss_pct": loss, "seed": seed,
        "t_client_start": t_start, "t_cut_cmd": t_down, "t_restore_cmd": t_up,
        "uname": subprocess.check_output(["uname", "-r"], text=True).strip(),
        "cc_avail": subprocess.check_output(["sysctl", "-n", "net.ipv4.tcp_available_congestion_control"], text=True).strip(),
        "tc_srv": subprocess.check_output(ns("srv", ["tc", "qdisc", "show", "dev", "veth-s"]), text=True),
    }
    json.dump(meta, open(base + ".meta", "w"), indent=2)
    print("done", tag)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo", choices=["cubic", "bbr"])
    ap.add_argument("--buf", type=int, default=125)
    ap.add_argument("--outage", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--out", default="data")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--reps", type=int, default=5)
    a = ap.parse_args()
    if a.sweep:
        for seed in range(1, a.reps + 1):
            for L in LENGTHS:
                for algo in ("cubic", "bbr"):
                    run(algo, a.buf, L, seed, a.loss, a.out)
    else:
        run(a.algo, a.buf, a.outage, a.seed, a.loss, a.out)
