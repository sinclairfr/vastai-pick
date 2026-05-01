#!/usr/bin/env python3
"""
vastai-pick — smart GPU picker for ComfyUI
Usage:
  vastai-pick                          # interactive: pick GPU type then top 3 offers
  vastai-pick --launch                 # auto-launch best offer without prompting
  vastai-pick --gpu RTX_4090           # skip GPU picker, go straight to offers
  vastai-pick --gpu A100_PCIE --min-disk 500 --max-price 2.0
"""

import argparse
import json
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

# ── defaults ────────────────────────────────────────────────────────────────
DEFAULT_MIN_DISK = 150  # GB
DEFAULT_MAX_PRICE = 2.0  # $/hr per GPU
DEFAULT_MAX_BWCOST = 10.0  # $/TB — real field: internet_down_cost_per_tb
DEFAULT_MIN_RELIABILITY = 0.985  # 98.5%
COMFYUI_IMAGE = "yanwk/comfyui-boot:latest"
DEFAULT_TEMPLATE_HASH = "feb2230956433009f0087e1af9c81d21"

# GPU menu shown when --gpu is not provided
# Names must match Vast.ai internal gpu_name field exactly (spaces included)
GPU_MENU = [
    # ── consumer / prosumer ─────────────────────────────────────────────────
    ("RTX 4090", "24GB  — best price/perf, FLUX inference & LoRA"),
    ("RTX 5090", "32GB  — latest gen, excellent for large models"),
    ("RTX 5080", "16GB  — budget next-gen"),
    ("RTX 5070 Ti", "16GB  — mid-range next-gen"),
    ("RTX 5070", "12GB  — cheapest next-gen, limited for FLUX"),
    ("RTX 3090", "24GB  — older, often very cheap"),
    ("RTX 4070S Ti", "16GB  — budget option"),
    ("RTX 4070S", "12GB  — budget, limited for large models"),
    # ── workstation ─────────────────────────────────────────────────────────
    ("RTX PRO 6000 WS", "96GB  — massive VRAM, no quantization needed"),
    ("RTX PRO 6000 S", "48GB  — workstation grade"),
    ("RTX PRO 5000", "32GB  — mid workstation"),
    ("RTX PRO 4000", "20GB  — entry workstation"),
    ("RTX 6000Ada", "48GB  — Ada Lovelace, solid for training"),
    # ── data center ─────────────────────────────────────────────────────────
    ("A100 SXM4", "80GB  — LoRA training without quantization"),
    ("H100 SXM", "80GB  — fastest training, premium price"),
    ("H100 NVL", "94GB  — NVLink variant"),
    ("H200", "141GB — top tier, very expensive"),
    ("H200 NVL", "141GB — NVLink variant"),
    ("B200", "192GB — Blackwell, bleeding edge"),
]

# scoring weights — tweak to taste
W_PRICE = 0.35  # lower is better
W_BW_COST = 0.20  # lower bandwidth cost is better
W_BW_DOWN = 0.07  # higher net_down is better
W_DISK = 0.08  # more disk is better
W_SCORE = 0.10  # platform DLP/$ score
W_RELIABILITY = 0.20  # higher machine reliability is better

# Vast.ai search field uses $/GB; CLI option in this script is $/TB.
GB_PER_TB = 1000.0

# ── helpers ─────────────────────────────────────────────────────────────────


def run_vastai(args: list[str]) -> dict | list:
    cmd = ["vastai"] + args + ["--raw"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return parse_json_output(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"[error] vastai command failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"[error] couldn't parse vastai output", file=sys.stderr)
        sys.exit(1)


def parse_json_output(raw: str) -> dict | list:
    """Parse Vast.ai JSON even when warnings are prepended to stdout."""
    text = raw.strip()
    if not text:
        raise json.JSONDecodeError("empty output", raw, 0)

    # Fast path: pure JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: find first JSON object/array in mixed output
    first_obj = text.find("{")
    first_arr = text.find("[")

    starts = [i for i in (first_obj, first_arr) if i != -1]
    if not starts:
        raise json.JSONDecodeError("no JSON start token found", raw, 0)

    start = min(starts)
    return json.loads(text[start:])


def fetch_offers(
    gpu: str,
    min_disk: float,
    max_price: float,
    max_bw_cost: float,
    min_reliability: float,
) -> list[dict]:
    max_bw_cost_gb = max_bw_cost / GB_PER_TB
    query = (
        f'gpu_name="{gpu}" '
        f"num_gpus=1 "
        f"disk_space>={min_disk} "
        f"dph<={max_price} "
        f"inet_down_cost<={max_bw_cost_gb} "
        f"reliability>={min_reliability} "
        f"rented=False "
        f"verified=True"
    )
    offers = run_vastai(["search", "offers", query, "--order", "dph"])
    return offers if isinstance(offers, list) else []


def normalize(values: list[float], invert=False) -> list[float]:
    """Min-max normalize; invert=True means lower raw value → higher score."""
    mn, mx = min(values), max(values)
    if mx == mn:
        return [0.5] * len(values)
    normed = [(v - mn) / (mx - mn) for v in values]
    return [1 - n for n in normed] if invert else normed


def score_offers(offers: list[dict]) -> list[dict]:
    if not offers:
        return []

    prices = [o.get("dph_total", 99) for o in offers]
    bw_costs = [
        (
            o.get("internet_down_cost_per_tb", 0)
            or ((o.get("inet_down_cost", 0) or 0) * GB_PER_TB)
        )
        for o in offers
    ]
    bw_downs = [o.get("inet_down", 0) for o in offers]
    disks = [o.get("disk_space", 0) for o in offers]
    dlp_usd = [o.get("dlperf_usd", 0) or 0 for o in offers]
    reliabilities = [
        o.get("reliability2", o.get("reliability", 0)) or 0 for o in offers
    ]

    n_price = normalize(prices, invert=True)
    n_bw_cost = normalize(bw_costs, invert=True)
    n_bw = normalize(bw_downs, invert=False)
    n_disk = normalize(disks, invert=False)
    n_dlp = normalize(dlp_usd, invert=False)
    n_rel = normalize(reliabilities, invert=False)

    for i, o in enumerate(offers):
        o["_score"] = (
            W_PRICE * n_price[i]
            + W_BW_COST * n_bw_cost[i]
            + W_BW_DOWN * n_bw[i]
            + W_DISK * n_disk[i]
            + W_SCORE * n_dlp[i]
            + W_RELIABILITY * n_rel[i]
        )

    return sorted(offers, key=lambda x: x["_score"], reverse=True)


def fmt_offer(rank: int, o: dict) -> str:
    gpu_name = o.get("gpu_name", "?")
    num_gpus = o.get("num_gpus", 1)
    gpu_price = o.get("dph_base", 0)
    stor_price = o.get("storage_total_cost", 0)
    total_price = o.get("dph_total", 0)
    disk = o.get("disk_space", 0)
    bw_down = o.get("inet_down", 0)
    bw_cost = o.get("internet_down_cost_per_tb", 0) or 0
    country = o.get("geolocation", "?")
    reliability = o.get("reliability2", 0) * 100
    vram = o.get("gpu_ram", 0)
    score = o.get("_score", 0)
    oid = o.get("id", "?")
    cuda = o.get("cuda_max_good", "?")

    bw_cost_str = f"${bw_cost:.2f}/TB" if bw_cost else "free"

    return (
        f"\n  {'★' if rank == 1 else f'#{rank}'}  ID {oid}  —  {num_gpus}x {gpu_name}  "
        f"({vram:.0f}MB VRAM, CUDA {cuda})\n"
        f"     Price     : ${total_price:.4f}/hr  "
        f"(GPU ${gpu_price:.4f} + storage ${stor_price:.4f})\n"
        f"     Bandwidth : ↓{bw_down:.0f} Mbps  (cost: {bw_cost_str})\n"
        f"     Disk      : {disk:.0f} GB\n"
        f"     Location  : {country}\n"
        f"     Reliability: {reliability:.1f}%\n"
        f"     Composite score: {score:.3f}"
    )


def extract_template_hash(value: str | None) -> str | None:
    """Accept either a raw template hash or a Vast URL containing template_id."""
    if not value:
        return None

    text = value.strip()
    if "template_id=" not in text:
        return text

    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    template_ids = query.get("template_id")
    if template_ids and template_ids[0]:
        return template_ids[0]
    return text


def launch_instance(offer_id: int, disk: float, image: str, template_hash: str | None):
    template_hash = extract_template_hash(template_hash)
    launch_target = f"template: {template_hash}" if template_hash else f"image: {image}"
    print(
        f"\n[launch] Renting instance {offer_id} ({disk:.0f}GB disk, {launch_target}) ..."
    )
    cmd = [
        "vastai",
        "create",
        "instance",
        str(offer_id),
        "--disk",
        str(int(disk)),
        "--ssh",
        "--direct",
    ]

    if template_hash:
        cmd.extend(["--template_hash", template_hash])
    else:
        cmd.extend(["--image", image])

    print(f"[cmd] {' '.join(cmd)}\n")
    subprocess.run(cmd)


# ── main ─────────────────────────────────────────────────────────────────────


def pick_gpu() -> str:
    """Interactive GPU selector shown when --gpu is not provided."""
    print("\n  Select GPU type:\n")
    for i, (name, desc) in enumerate(GPU_MENU, 1):
        print(f"  [{i}] {name:<14} {desc}")
    print()
    while True:
        choice = input("  GPU choice [1 = RTX_4090]: ").strip()
        if choice == "":
            return GPU_MENU[0][0]
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(GPU_MENU):
                return GPU_MENU[idx][0]
        except ValueError:
            pass
        print(f"  Invalid — enter a number between 1 and {len(GPU_MENU)}")


def main():
    parser = argparse.ArgumentParser(
        description="Pick the best Vast.ai GPU offer for ComfyUI"
    )
    parser.add_argument(
        "--gpu", default=None, help="GPU model — skips interactive picker"
    )
    parser.add_argument(
        "--min-disk",
        type=float,
        default=DEFAULT_MIN_DISK,
        help="Min disk GB (default: 200)",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=DEFAULT_MAX_PRICE,
        help="Max $/hr (default: 2.0)",
    )
    parser.add_argument(
        "--max-bwcost",
        type=float,
        default=DEFAULT_MAX_BWCOST,
        help="Max bandwidth $/TB (default: 10.0)",
    )
    parser.add_argument(
        "--min-reliability",
        type=float,
        default=DEFAULT_MIN_RELIABILITY,
        help="Minimum host reliability 0..1 (default: 0.985)",
    )
    parser.add_argument("--image", default=COMFYUI_IMAGE, help="Docker image to use")
    parser.add_argument(
        "--template-hash",
        default=DEFAULT_TEMPLATE_HASH,
        help="Vast template hash or full URL containing template_id",
    )
    parser.add_argument(
        "--launch", action="store_true", help="Auto-launch best offer without prompting"
    )
    parser.add_argument(
        "--top", type=int, default=3, help="How many offers to show (default: 3)"
    )
    args = parser.parse_args()

    # GPU picker — only when --gpu not explicitly passed
    if args.gpu is None:
        args.gpu = pick_gpu()

    print(
        f"\n[search] GPU={args.gpu}  disk≥{args.min_disk}GB  price≤${args.max_price}/hr  bw_cost≤${args.max_bwcost}/TB  reliability≥{args.min_reliability:.3f}"
    )
    offers = fetch_offers(
        args.gpu,
        args.min_disk,
        args.max_price,
        args.max_bwcost,
        args.min_reliability,
    )

    if not offers:
        print("[error] No offers found — try relaxing --max-price or --max-bwcost")
        sys.exit(1)

    ranked = score_offers(offers)
    top = ranked[: args.top]

    print(f"\n{'─'*60}")
    print(f"  Found {len(offers)} offers — showing top {len(top)}")
    print(f"{'─'*60}")

    for i, o in enumerate(top, 1):
        print(fmt_offer(i, o))

    print(f"\n{'─'*60}")
    best = top[0]

    if args.launch:
        # Non-interactive: just launch the best
        launch_instance(
            best["id"],
            max(args.min_disk, best.get("disk_space", args.min_disk)),
            args.image,
            args.template_hash,
        )
        return

    # Interactive prompt
    print(f"\nOptions:")
    for i, o in enumerate(top, 1):
        print(f"  [{i}] Rent #{i} (ID {o['id']}, ${o['dph_total']:.4f}/hr)")
    print(f"  [q] Quit")

    try:
        choice = input("\nYour choice: ").strip().lower()
    except EOFError:
        print("\nNo interactive input available. Exiting without launching.")
        return

    if choice == "q":
        print("Cancelled.")
        return

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(top):
            selected = top[idx]
            disk_to_use = max(args.min_disk, selected.get("disk_space", args.min_disk))
            # Don't over-allocate — cap at what's available or a sane max
            disk_to_use = min(disk_to_use, 500)
            launch_instance(selected["id"], disk_to_use, args.image, args.template_hash)
        else:
            print("Invalid choice.")
    except ValueError:
        print("Invalid input.")


if __name__ == "__main__":
    main()
