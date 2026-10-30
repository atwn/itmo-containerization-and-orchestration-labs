#!/usr/bin/env python3
"""Capture one bounded lab5 experiment; preserve failed runs as evidence too."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--chaos", type=int, default=180)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument("--rps", type=float, default=5)
    args = parser.parse_args()
    if min(args.chaos, args.warmup, args.cooldown, args.rps) <= 0:
        parser.error("durations and rps must be positive")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    root = Path(__file__).resolve().parent.parent
    context = os.environ.get("KUBE_CONTEXT", "kind-lab5")
    namespace = os.environ.get("NAMESPACE", "shop")
    kubectl = ["kubectl", "--context", context, "--request-timeout=10s", "-n", namespace]

    def kube(*arguments):
        return subprocess.check_output([*kubectl, *arguments], text=True, timeout=20)

    service = json.loads(kube("get", "svc", "shop-api", "-o", "json"))
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    node = next(n for n in nodes if "node-role.kubernetes.io/control-plane" not in n["metadata"]["labels"])
    node_ip = next(a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP")
    port = service["spec"]["ports"][0]["nodePort"]
    url = f"http://{node_ip}:{port}/health"
    generators = json.loads(kube("get", "pods", "-l", "app.kubernetes.io/name=chaos-load", "-o", "json"))["items"]
    if len(generators) != 1 or generators[0]["status"]["phase"] != "Running":
        raise RuntimeError("Expected one running chaos-load pod")
    generator = generators[0]
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "3", "-o", "/dev/null", url], check=True)
    args.directory.mkdir(parents=True, exist_ok=False)
    args.directory = args.directory.resolve()
    total = args.warmup + args.chaos + args.cooldown
    metadata = {
        "context": context, "namespace": namespace, "nodeport_url": url,
        "dns_generator": generator["metadata"]["name"],
        "dns_generator_uid": generator["metadata"]["uid"],
        "dns_generator_initial_restarts": [c["restartCount"] for c in generator["status"].get("containerStatuses", [])],
        "dns_generator_env": generator["spec"]["containers"][0].get("env", []),
        "warmup_seconds": args.warmup, "chaos_seconds": args.chaos,
        "cooldown_seconds": args.cooldown, "nodeport_target_rps": args.rps,
        "started": timestamp(), "status": "running",
    }
    metadata["service"] = {key: service["spec"].get(key) for key in
                           ["type", "clusterIP", "ports", "externalTrafficPolicy", "internalTrafficPolicy"]}
    deployment = json.loads(kube("get", "deployment", "shop-api", "-o", "json"))
    pod_spec = deployment["spec"]["template"]["spec"]
    metadata["api_shutdown"] = {
        "lifecycle": pod_spec["containers"][0].get("lifecycle", {}),
        "terminationGracePeriodSeconds": pod_spec["terminationGracePeriodSeconds"],
        "image": pod_spec["containers"][0]["image"],
        "replicas": deployment["spec"]["replicas"],
    }
    meta_path = args.directory / "run.json"
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n")
    processes = []
    files = []

    def launch(script, duration, filename, extra_env=None):
        output = (args.directory / f"{filename}.jsonl").open("w")
        errors = (args.directory / f"{filename}.stderr").open("w")
        files.extend([output, errors])
        env = dict(os.environ, KUBE_CONTEXT=context, NAMESPACE=namespace,
                   DURATION_SECONDS=str(duration), **(extra_env or {}))
        command = ["bash", str(root / "scripts" / script)]
        if script == "load.sh":
            command.append(str(args.rps))
        process = subprocess.Popen(command, env=env, stdout=output, stderr=errors, start_new_session=True)
        processes.append(process)
        return process

    start_clock = time.monotonic()
    chaos = None
    try:
        load = launch("load.sh", total, "nodeport", {"URL": url})
        with (args.directory / "snapshots.jsonl").open("w") as snapshots:
            while time.monotonic() - start_clock < total:
                elapsed = time.monotonic() - start_clock
                if chaos is None and elapsed >= args.warmup:
                    metadata["chaos_started"] = timestamp()
                    chaos = launch("chaos.sh", args.chaos, "deletions")
                    print("Chaos started", metadata["chaos_started"], flush=True)
                if load.poll() is not None and elapsed < total - 2:
                    raise RuntimeError(f"Load generator exited early: {load.returncode}")
                if chaos is not None and chaos.poll() not in (None, 0):
                    raise RuntimeError(f"Chaos script failed: {chaos.returncode}")
                observed = timestamp()
                state = json.loads(kube("get", "pods,endpointslices", "-o", "json"))
                pods, endpoints = [], []
                for item in state["items"]:
                    if item["kind"] == "Pod" and item["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "api":
                        pods.append({"name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
                            "ip": item["status"].get("podIP"), "node": item["spec"].get("nodeName"),
                            "deletion": item["metadata"].get("deletionTimestamp"),
                            "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in item["status"].get("conditions", []))})
                    elif item["kind"] == "EndpointSlice" and item["metadata"].get("labels", {}).get("kubernetes.io/service-name") == "shop-api":
                        endpoints.extend(item.get("endpoints", []))
                snapshots.write(json.dumps({"ts": observed, "pods": pods, "endpoints": endpoints}) + "\n")
                snapshots.flush()
                time.sleep(1)
        load.wait(timeout=5)
        if chaos is not None:
            chaos.wait(timeout=15)
        metadata["ended"] = timestamp()
        raw = kube("logs", "pod/" + metadata["dns_generator"], "--since-time=" + metadata["started"])
        (args.directory / "dns.raw.log").write_text(raw)
        with (args.directory / "dns.jsonl").open("w") as output:
            for line in raw.splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "result" in row and dt.datetime.fromisoformat(metadata["started"]) <= dt.datetime.fromisoformat(row["ts"]) <= dt.datetime.fromisoformat(metadata["ended"]):
                    output.write(json.dumps(row) + "\n")
        final_generator = json.loads(kube("get", "pod", metadata["dns_generator"], "-o", "json"))
        metadata["dns_generator_restarts"] = [c["restartCount"] for c in final_generator["status"].get("containerStatuses", [])]
        metadata["status"] = "captured"
    except KeyboardInterrupt:
        metadata["status"] = "interrupted"
        metadata["ended"] = timestamp()
        raise
    except Exception as error:
        metadata["status"] = "error"
        metadata["ended"] = timestamp()
        metadata["error"] = str(error)
        raise
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for file in files:
            file.close()
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print("Captured:", args.directory, flush=True)


if __name__ == "__main__":
    main()
