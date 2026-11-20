
#!/usr/bin/env python3
"""Phase B: one 150 s HTTP download with a seeded paper-style outage schedule."""

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

    label = {
        "plain": cc,
        "tuned": f"tuned_{cc}",
        "watchdog": f"watchdog_N{N:g}",
        "oracle": "oracle"
    }[strat]

    tag = f"b_{label}_{speed}_{mode}_loss{loss}_seed{seed}"
    base = os.path.abspath(os.path.join(out, tag))

    marker = base + ".marker"
    start_file = base + ".start"

    for path in (marker, start_file):
        if os.path.exists(path):
            os.remove(path)

    sched = make_schedule(seed, speed, mode, run_len)

    set_link(None, None, loss)

    route = [
        "ip", "route", "replace", "10.0.0.0/24",
        "dev", "veth-s", "scope", "link"
    ]

    sh(ns("srv", route + (
        ["rto_min", f"{rto_min}ms"] if strat == "tuned" else []
    )))

    srv = subprocess.Popen(
        ns("srv", [
            "python3", os.path.join(HERE, "http_range_server.py"),
            "--cc", cc, "--size-mb", str(mb)
        ]),
        stdout=DEVNULL,
        stderr=DEVNULL
    )

    cap = None
    cli = None

    try:
        time.sleep(1.0)

        if pcap:
            cap = subprocess.Popen(
                ns("cli", [
                    "tcpdump", "-i", "veth-c", "-U", "-s", "96",
                    "-w", base + ".pcap"
                ]),
                stdout=DEVNULL,
                stderr=DEVNULL
            )
            time.sleep(0.5)

        cs = strat if strat in ("watchdog", "oracle") else "plain"

        cli = subprocess.Popen(
            ns("cli", [
                "python3", os.path.join(HERE, "stall_client.py"),
                "--strategy", cs,
                "--N", str(N),
                "--marker", marker,
                "--start-file", start_file,
                "--out", base + ".json",
                "--max-time", str(run_len)
            ])
        )

        # Wait until the client records its own monotonic start time.
        deadline = time.monotonic() + 10.0

        while not os.path.exists(start_file) and time.monotonic() < deadline:
            time.sleep(0.005)

        if not os.path.exists(start_file):
            cli.kill()
            cli.wait()
            raise RuntimeError("Client did not create its start-time marker")

        with open(start_file) as f:
            t_start = float(f.read())

        # Allow the connection to establish before the first outage.
        time.sleep(2.0)

        with open(base + ".ss", "w") as f:
            f.write(
                subprocess.run(
                    ns("srv", ["ss", "-tin"]),
                    capture_output=True,
                    text=True
                ).stdout
            )

        realized = []

        for ev in sched:
            # Schedule against the client's monotonic clock.
            target = t_start + ev["start"]
            time.sleep(max(0.0, target - time.monotonic()))

            # Record the interval surrounding outage activation.
            t_down_begin = time.monotonic()
            set_link(100, 100, loss)
            t_down_done = time.monotonic()

            time.sleep(ev["dur"])

            # Record the interval surrounding link restoration.
            t_up_begin = time.monotonic()
            set_link(None, None, loss)
            t_up_done = time.monotonic()

            # Tell the oracle strategy that the link has returned.
            with open(marker, "w") as f:
                f.write(str(time.monotonic()))

            realized.append(dict(
                ev,
                t_down=round(t_down_begin - t_start, 4),
                t_down_done=round(t_down_done - t_start, 4),
                t_up_begin=round(t_up_begin - t_start, 4),
                t_up=round(t_up_done - t_start, 4),
                sleep_s=round(t_up_begin - t_down_done, 4),
                down_apply_s=round(t_down_done - t_down_begin, 4),
                restore_apply_s=round(t_up_done - t_up_begin, 4),
                actual_dur=round(t_up_done - t_down_begin, 4)
            ))

        try:
            cli.wait(timeout=run_len + 40)
        except subprocess.TimeoutExpired:
            cli.kill()
            cli.wait()

    finally:
        if cli is not None and cli.poll() is None:
            cli.terminate()
            cli.wait()

        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()

        if cap:
            cap.terminate()
            try:
                cap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cap.kill()
                cap.wait()

        sh(ns("srv", route))

    meta = dict(
        label=label,
        cc=cc,
        strategy=strat,
        N=N,
        seed=seed,
        speed=speed,
        mode=mode,
        loss=loss,
        mb=mb,
        run_len=run_len,
        events=realized,
        t_start=t_start,
        rto_min_ms=rto_min if strat == "tuned" else None,
        uname=subprocess.check_output(
            ["uname", "-r"], text=True
        ).strip()
    )

    with open(base + ".meta", "w") as f:
        json.dump(meta, f, indent=2)

    print("done", tag, f"({len(realized)} outages)", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()

    ap.add_argument("--cc", default="cubic", choices=["cubic", "bbr"])
    ap.add_argument("--strategy", default="plain",
                    choices=["plain", "tuned", "watchdog", "oracle"])
    ap.add_argument("--N", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--speed", default="350", choices=["350", "300"])
    ap.add_argument("--mode", default="fitted",
                    choices=["fitted", "fixed", "none"])
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
        run(
            a.cc, a.strategy, a.N, a.seed, a.speed, a.mode,
            a.loss, a.mb, a.out, a.rto_min, a.run_len, a.pcap
        )
    else:
        for s in range(1, 4):
            for cc in ("cubic", "bbr"):
                run(
                    cc, "plain", 1.0, s, a.speed, "none",
                    a.loss, a.mb, a.out, a.rto_min, a.run_len
                )

        configs = [
            ("cubic", "plain"),
            ("bbr", "plain"),
            ("cubic", "tuned"),
            ("cubic", "watchdog"),
            ("cubic", "oracle")
        ]

        for s in range(1, a.reps + 1):
            for cc, strat in configs:
                run(
                    cc, strat, 1.0, s, a.speed, a.mode,
                    a.loss, a.mb, a.out, a.rto_min, a.run_len
                )
