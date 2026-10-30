#!/usr/bin/env python3
"""Render captured requests, endpoint readiness and Pod/IP lifetimes on one timeline.

Requires matplotlib. Usage: python plot-chaos.py evidence/part2/<run>
"""
import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory
    meta = json.loads((directory / "run.json").read_text())
    if meta["status"] != "captured":
        raise ValueError("Capture is incomplete")
    origin = dt.datetime.fromisoformat(meta["started"])

    def seconds(value):
        return (dt.datetime.fromisoformat(value) - origin).total_seconds()

    end = seconds(meta["ended"])
    requests = {key: rows(directory / f"{key}.jsonl") for key in ["dns", "nodeport"]}
    snapshots = rows(directory / "snapshots.jsonl")
    deletions = rows(directory / "deletions.jsonl")
    if not all(requests.values()) or not snapshots or not deletions:
        raise ValueError("Missing requests, snapshots or deletions")
    initial = {p["uid"] for p in snapshots[0]["pods"]}
    lifetimes = {}
    for snapshot in snapshots:
        for pod in snapshot["pods"]:
            if not pod["ip"]:
                continue
            item = lifetimes.setdefault(pod["uid"], dict(pod, first=seconds(snapshot["ts"])))
            item["last"] = seconds(snapshot["ts"])
    ready = [sum(e.get("conditions", {}).get("ready") is True for e in s["endpoints"]) for s in snapshots]
    summary = {"started": meta["started"], "ended": meta["ended"], "requests": {},
        "deletions": len(deletions), "observed_pods": len(lifetimes),
        "new_pods": len(set(lifetimes) - initial),
        "unique_pod_ips": len({p["ip"] for p in lifetimes.values()}),
        "ready_endpoints_min": min(ready), "ready_endpoints_max": max(ready),
        "ready_endpoints_final": ready[-1], "dns_generator_restarts": meta["dns_generator_restarts"]}
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(3, 1, figsize=(14, 7 + len(lifetimes) * 0.22), sharex=True,
                            gridspec_kw={"height_ratios": [2, 1.3, max(2.5, len(lifetimes) * 0.20)]})
    failure_axis = axes[0].twinx()
    max_failures = 0
    for label, color in [("dns", "#0077b6"), ("nodeport", "#6a4c93")]:
        records = requests[label]
        times = [seconds(r["ts"]) for r in records]
        failures, accumulated = 0, []
        for r in records:
            failures += not (r["result"] == "ok" and r["status"].startswith("2") and r.get("curl_exit", 0) == 0)
            accumulated.append(failures)
        max_failures = max(max_failures, failures)
        gaps = [b - a for a, b in zip(times, times[1:])]
        summary["requests"][label] = {"attempts": len(records), "failed": failures,
            "max_gap_seconds": max(gaps, default=0), "first": records[0]["ts"], "last": records[-1]["ts"]}
        axes[0].step(times, range(1, len(times) + 1), where="post", color=color,
                     label=f"{label}: {len(records)} attempts; {failures} failed")
        failure_axis.step(times, accumulated, where="post", color="#d62828",
                          linestyle="--" if label == "dns" else ":", linewidth=2,
                          label=f"{label} failures")
    axes[0].set_ylabel("Cumulative client attempts")
    axes[0].legend(loc="upper left")
    failure_axis.set_ylabel("Cumulative failed requests", color="#d62828")
    failure_axis.set_ylim(-0.05, max(1, max_failures) + 0.2)
    failure_axis.yaxis.set_major_locator(MaxNLocator(integer=True))
    failure_axis.legend(loc="upper right")
    axes[1].step([seconds(s["ts"]) for s in snapshots], ready, where="post",
                 color="#2a9d8f", label="EndpointSlice ready endpoints")
    for i, event in enumerate(deletions):
        axes[1].axvline(seconds(event["ts"]), color="#e76f51", alpha=0.5,
                       label=f"API Pod deletions ({len(deletions)})" if i == 0 else None)
    axes[1].set_ylabel("Ready endpoints")
    axes[1].set_ylim(0, max(ready) + 1)
    axes[1].yaxis.set_major_locator(MaxNLocator(integer=True))
    axes[1].legend(loc="lower left")
    names = []
    nodes = sorted({p["node"] for p in lifetimes.values()})
    colors = dict(zip(nodes, ["#0077b6", "#6a4c93", "#2a9d8f"]))
    for i, pod in enumerate(lifetimes.values()):
        axes[2].plot([pod["first"], pod["last"]], [i, i], linewidth=4, color=colors[pod["node"]])
        names.append(f"{pod['name'].rsplit('-', 1)[-1]}  {pod['ip']}")
    axes[2].set_yticks(range(len(names)), names)
    axes[2].invert_yaxis()
    axes[2].set_ylabel("Observed API Pod / IP\n(sampled approximately every second)")
    axes[2].set_xlabel("Seconds since experiment start")
    for ax in axes:
        ax.grid(axis="x", alpha=0.2)
        ax.set_xlim(0, end)
        ax.axvspan(seconds(meta["chaos_started"]), seconds(meta["chaos_started"]) + meta["chaos_seconds"],
                   color="#ffb703", alpha=0.08)
    local_start = origin.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S MSK")
    fig.suptitle(f"Shop continuity under Pod chaos — {local_start}\n"
                 f"{len(deletions)} deletions · {summary['new_pods']} new Pods · "
                 f"{summary['unique_pod_ips']} distinct Pod IPs · no client retries", fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(directory / "continuity.png", dpi=140)
    fig.savefig(directory / "continuity.svg")
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
