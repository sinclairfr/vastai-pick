#!/usr/bin/env python3
"""
vastai-pick — smart GPU picker for ComfyUI (medo edition)

Usage:
  vastai-pick                                   # interactive GPU picker, no volume
  vastai-pick --volume V.1234                   # filter to volume's machine, mount on /workspace
  vastai-pick --gpu RTX_4090                    # skip GPU picker
  vastai-pick --gpu A100_SXM4 --launch          # auto-launch best offer
  vastai-pick --gpu RTX_4090 --min-disk 250     # no volume, big local disk
  vastai-pick --max-price 1.5 --top 5

Volume mode:
  When --volume is passed, the script:
  - fetches the volume's machine_id from `vastai show volumes`
  - restricts search to that machine only
  - sets --disk 30 (container only, models live on the volume)
  - adds -v V.<id>:/workspace to the launch env
  - boosts disk_bw weight in scoring (fast local I/O matters more than network pull)

No-volume mode (default):
  - searches all machines matching GPU + disk + price filters
  - uses --disk <min_disk> (default 250) for local model storage
  - R2 pull at boot, so inet_down + disk_bw both matter
"""

import argparse
import json
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

# ── defaults ─────────────────────────────────────────────────────────────────
DEFAULT_MIN_DISK       = 250      # GB — for no-volume mode
VOLUME_CONTAINER_DISK  = 30       # GB — container only when volume is mounted
DEFAULT_MAX_PRICE      = 2.0      # $/hr
DEFAULT_MAX_BWCOST     = 10.0     # $/TB
DEFAULT_MIN_RELIABILITY = 0.985
COMFYUI_IMAGE          = "vastai/comfy:v0.19.3-cuda-12.9-py312"
DEFAULT_TEMPLATE_HASH  = "feb2230956433009f0087e1af9c81d21"
BOOT_SCRIPT_URL        = "https://raw.githubusercontent.com/sinclairfr/medo-comfyui-vastai/main/boot_vast.sh"
ON_START_URL           = "https://raw.githubusercontent.com/sinclairfr/medo-comfyui-vastai/main/on_start.sh"

GPU_MENU = [
    # ── consumer / prosumer ──────────────────────────────────────────────────
    ("RTX 4090",      "24GB  — best price/perf, FLUX inference & LoRA"),
    ("RTX 5090",      "32GB  — latest gen, excellent for large models"),
    ("RTX 5080",      "16GB  — budget next-gen"),
    ("RTX 5070 Ti",   "16GB  — mid-range next-gen"),
    ("RTX 5070",      "12GB  — cheapest next-gen, limited for FLUX"),
    ("RTX 3090",      "24GB  — older, often very cheap"),
    ("RTX 4070S Ti",  "16GB  — budget option"),
    ("RTX 4070S",     "12GB  — budget, limited for large models"),
    # ── workstation ──────────────────────────────────────────────────────────
    ("RTX PRO 6000 WS", "96GB  — massive VRAM, no quantization needed"),
    ("RTX PRO 6000 S",  "48GB  — workstation grade"),
    ("RTX PRO 5000",    "32GB  — mid workstation"),
    ("RTX PRO 4000",    "20GB  — entry workstation"),
    ("RTX 6000Ada",     "48GB  — Ada Lovelace, solid for training"),
    # ── data center ──────────────────────────────────────────────────────────
    ("A100 SXM4",  "80GB  — LoRA training without quantization"),
    ("H100 SXM",   "80GB  — fastest training, premium price"),
    ("H100 NVL",   "94GB  — NVLink variant"),
    ("H200",       "141GB — top tier, very expensive"),
    ("H200 NVL",   "141GB — NVLink variant"),
    ("B200",       "192GB — Blackwell, bleeding edge"),
]

# ── scoring weights ───────────────────────────────────────────────────────────
# Two profiles: volume mode (disk_bw critical, storage cost matters) vs no-volume

WEIGHTS_NO_VOLUME = dict(
    price       = 0.35,   # lower dph = better
    bw_cost     = 0.15,   # lower inet_down_cost = better (R2 egress free, but still)
    inet_down   = 0.10,   # higher net speed = faster R2 pull
    disk_bw     = 0.10,   # faster local disk = faster model load
    storage_cost= 0.05,   # minor: local disk storage cost while stopped
    reliability = 0.20,   # uptime
    dlp_score   = 0.05,   # platform perf/$ score
)

WEIGHTS_VOLUME = dict(
    price       = 0.35,   # still the main driver
    bw_cost     = 0.05,   # barely matters — no R2 pull
    inet_down   = 0.03,   # barely matters — no R2 pull
    disk_bw     = 0.22,   # critical — all model I/O goes through this
    storage_cost= 0.10,   # volume storage billed permanently
    reliability = 0.22,   # critical — volume machine must stay online
    dlp_score   = 0.03,
)

GB_PER_TB = 1000.0


# ── vastai helpers ────────────────────────────────────────────────────────────

def run_vastai(args: list[str]) -> dict | list:
    cmd = ["vastai"] + args + ["--raw"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return _parse_json(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"[error] vastai command failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("[error] couldn't parse vastai output", file=sys.stderr)
        sys.exit(1)


def _parse_json(raw: str) -> dict | list:
    text = raw.strip()
    if not text:
        raise json.JSONDecodeError("empty", raw, 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # strip prepended warnings
    for start_char in ("{", "["):
        idx = text.find(start_char)
        if idx != -1:
            return json.loads(text[idx:])
    raise json.JSONDecodeError("no JSON found", raw, 0)


def resolve_volume(volume_arg: str) -> tuple[int, int]:
    """
    Accept 'V.1234', '1234', or plain int.
    Returns (volume_id, machine_id).
    Exits if volume not found.
    """
    vol_id_str = volume_arg.lstrip("Vv.")
    try:
        vol_id = int(vol_id_str)
    except ValueError:
        print(f"[error] invalid volume id: {volume_arg!r} — expected V.<int> or <int>")
        sys.exit(1)

    print(f"[volume] fetching info for V.{vol_id} ...")
    volumes = run_vastai(["show", "volumes"])
    if not isinstance(volumes, list):
        volumes = volumes.get("volumes", [])

    for v in volumes:
        if v.get("id") == vol_id:
            machine_id = v.get("machine_id")
            size = v.get("size", "?")
            name = v.get("name", "")
            print(f"[volume] V.{vol_id}  name={name!r}  size={size}GB  machine_id={machine_id}")
            if not machine_id:
                print("[error] volume has no machine_id — is it attached to a running instance?")
                sys.exit(1)
            return vol_id, machine_id

    print(f"[error] volume V.{vol_id} not found in `vastai show volumes`")
    sys.exit(1)


def fetch_offers(
    gpu: str,
    min_disk: float,
    max_price: float,
    max_bw_cost: float,
    min_reliability: float,
    machine_id: int | None = None,
) -> list[dict]:
    max_bw_cost_gb = max_bw_cost / GB_PER_TB
    query_parts = [
        f'gpu_name="{gpu}"',
        "num_gpus=1",
        f"dph<={max_price}",
        f"inet_down_cost<={max_bw_cost_gb}",
        f"reliability>={min_reliability}",
        "rented=False",
        "verified=True",
    ]

    if machine_id is not None:
        # volume mode: don't filter by disk (volume provides storage)
        query_parts.append(f"machine_id={machine_id}")
        print(f"[search] locked to machine_id={machine_id} (volume mode, no disk filter)")
    else:
        query_parts.append(f"disk_space>={min_disk}")

    query = " ".join(query_parts)
    offers = run_vastai(["search", "offers", query, "--order", "dph"])
    return offers if isinstance(offers, list) else []


# ── scoring ───────────────────────────────────────────────────────────────────

def normalize(values: list[float], invert=False) -> list[float]:
    mn, mx = min(values), max(values)
    if mx == mn:
        return [0.5] * len(values)
    normed = [(v - mn) / (mx - mn) for v in values]
    return [1 - n for n in normed] if invert else normed


def score_offers(offers: list[dict], volume_mode: bool) -> list[dict]:
    if not offers:
        return []

    W = WEIGHTS_VOLUME if volume_mode else WEIGHTS_NO_VOLUME

    def field(o, key, default=0):
        v = o.get(key)
        return v if v is not None else default

    prices        = [field(o, "dph_total", 99) for o in offers]
    bw_costs      = [
        field(o, "internet_down_cost_per_tb") or
        (field(o, "inet_down_cost") * GB_PER_TB)
        for o in offers
    ]
    inet_downs    = [field(o, "inet_down") for o in offers]
    disk_bws      = [field(o, "disk_bw") for o in offers]
    stor_costs    = [field(o, "storage_total_cost") for o in offers]
    reliabilities = [field(o, "reliability2") or field(o, "reliability") for o in offers]
    dlp_scores    = [field(o, "dlperf_usd") for o in offers]

    n_price    = normalize(prices,        invert=True)
    n_bw_cost  = normalize(bw_costs,      invert=True)
    n_inet     = normalize(inet_downs,    invert=False)
    n_disk_bw  = normalize(disk_bws,      invert=False)
    n_stor     = normalize(stor_costs,    invert=True)
    n_rel      = normalize(reliabilities, invert=False)
    n_dlp      = normalize(dlp_scores,    invert=False)

    for i, o in enumerate(offers):
        o["_score"] = (
            W["price"]        * n_price[i]   +
            W["bw_cost"]      * n_bw_cost[i] +
            W["inet_down"]    * n_inet[i]     +
            W["disk_bw"]      * n_disk_bw[i] +
            W["storage_cost"] * n_stor[i]     +
            W["reliability"]  * n_rel[i]      +
            W["dlp_score"]    * n_dlp[i]
        )
        o["_volume_mode"] = volume_mode

    return sorted(offers, key=lambda x: x["_score"], reverse=True)


# ── formatting ────────────────────────────────────────────────────────────────

def fmt_offer(rank: int, o: dict, volume_mode: bool, vol_id: int | None) -> str:
    gpu      = o.get("gpu_name", "?")
    n_gpus   = o.get("num_gpus", 1)
    dph_gpu  = o.get("dph_base", 0)
    dph_stor = o.get("storage_total_cost", 0)
    dph_tot  = o.get("dph_total", 0)
    disk     = o.get("disk_space", 0)
    disk_bw  = o.get("disk_bw", 0)
    inet_dn  = o.get("inet_down", 0)
    bw_cost  = o.get("internet_down_cost_per_tb", 0) or 0
    country  = o.get("geolocation", "?")
    rel      = (o.get("reliability2") or o.get("reliability") or 0) * 100
    vram     = o.get("gpu_ram", 0)
    score    = o.get("_score", 0)
    oid      = o.get("id", "?")
    cuda     = o.get("cuda_max_good", "?")
    machine  = o.get("machine_id", "?")

    # storage cost for 200 GB over a month (volume or local disk)
    stc_per_gb_hr = dph_stor / max(disk, 1)
    monthly_200 = stc_per_gb_hr * 200 * 24 * 30

    bw_cost_str = f"${bw_cost:.2f}/TB" if bw_cost else "free"
    vol_str = f"  (volume V.{vol_id} → /workspace)" if volume_mode and vol_id else ""
    marker  = "★" if rank == 1 else f"#{rank}"

    # pull time estimate from R2 (200GB @ inet_down Mbps), only in no-volume mode
    pull_str = ""
    if not volume_mode and inet_dn > 0:
        pull_min = (200 * 1024) / (inet_dn / 8) / 60
        pull_str = f"\n     R2 pull est : ~{pull_min:.1f} min for 200GB"

    return (
        f"\n  {marker}  ID {oid}  —  {n_gpus}x {gpu}  "
        f"({vram:.0f}MB VRAM, CUDA {cuda}){vol_str}\n"
        f"     Price      : ${dph_tot:.4f}/hr  "
        f"(GPU ${dph_gpu:.4f} + storage ${dph_stor:.5f})\n"
        f"     Storage    : ${monthly_200:.2f}/mois for 200GB\n"
        f"     Disk speed : {disk_bw:.0f} MB/s local  |  net ↓{inet_dn:.0f} Mbps (cost: {bw_cost_str})\n"
        f"     Disk space : {disk:.0f} GB\n"
        f"     Location   : {country}  |  machine_id={machine}\n"
        f"     Reliability: {rel:.1f}%\n"
        f"     Score      : {score:.3f}"
        f"{pull_str}"
    )


# ── launch ────────────────────────────────────────────────────────────────────

def extract_template_hash(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if "template_id=" not in text:
        return text
    parsed = urlparse(text)
    ids = parse_qs(parsed.query).get("template_id")
    return ids[0] if ids else text


def build_env(volume_mode: bool, vol_id: int | None) -> str:
    """Build the -e / -v env string for --env argument."""
    ports = "-p 8188:8188 -p 8081:8081 -p 5055:5055 -p 1111:1111"
    vol_mount = f"-v V.{vol_id}:/workspace " if volume_mode and vol_id else ""
    env_vars = (
        f'-e BOOT_SCRIPT="{BOOT_SCRIPT_URL}" '
        f'-e MEDO_ON_START_URL="{ON_START_URL}" '
        f'-e ON_START_WAIT_SUPERVISORD_SECONDS=200 '
        f'-e OPEN_BUTTON_PORT="1111" '
        f'-e DATA_DIRECTORY="/workspace/" '
        f'-e MODELS_ROOT=/workspace/ComfyUI/models'
    )
    return f"{ports} {vol_mount}{env_vars}"


def launch_instance(
    offer_id: int,
    disk: float,
    image: str,
    template_hash: str | None,
    volume_mode: bool,
    vol_id: int | None,
):
    template_hash = extract_template_hash(template_hash)
    launch_target = f"template: {template_hash}" if template_hash else f"image: {image}"
    disk_int = VOLUME_CONTAINER_DISK if volume_mode else int(disk)

    print(f"\n[launch] Renting instance {offer_id}  disk={disk_int}GB  {launch_target}")
    if volume_mode:
        print(f"[launch] Volume V.{vol_id} will be mounted at /workspace")

    cmd = [
        "vastai", "create", "instance", str(offer_id),
        "--disk", str(disk_int),
        "--env", build_env(volume_mode, vol_id),
        "--onstart-cmd", "entrypoint.sh",
        "--ssh", "--direct",
    ]

    if template_hash:
        cmd.extend(["--template_hash", template_hash])
    else:
        cmd.extend(["--image", image])

    print(f"\n[cmd] {' '.join(cmd)}\n")
    subprocess.run(cmd)


# ── main ──────────────────────────────────────────────────────────────────────

def pick_gpu() -> str:
    print("\n  Select GPU type:\n")
    for i, (name, desc) in enumerate(GPU_MENU, 1):
        print(f"  [{i:>2}] {name:<20} {desc}")
    print()
    while True:
        choice = input("  GPU choice [1 = RTX 4090]: ").strip()
        if choice == "":
            return GPU_MENU[0][0]
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(GPU_MENU):
                return GPU_MENU[idx][0]
        except ValueError:
            pass
        print(f"  Invalid — enter 1..{len(GPU_MENU)}")


def main():
    parser = argparse.ArgumentParser(
        description="Pick the best Vast.ai GPU offer for ComfyUI (medo edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--gpu", default=None,
                        help="GPU model — skips interactive picker")
    parser.add_argument("--volume", default=None, metavar="V.<ID>",
                        help="Vast volume id (V.1234 or 1234) — locks to that machine, mounts /workspace")
    parser.add_argument("--min-disk", type=float, default=DEFAULT_MIN_DISK,
                        help=f"Min disk GB for no-volume mode (default: {DEFAULT_MIN_DISK})")
    parser.add_argument("--max-price", type=float, default=DEFAULT_MAX_PRICE,
                        help=f"Max $/hr (default: {DEFAULT_MAX_PRICE})")
    parser.add_argument("--max-bwcost", type=float, default=DEFAULT_MAX_BWCOST,
                        help=f"Max bandwidth $/TB (default: {DEFAULT_MAX_BWCOST})")
    parser.add_argument("--min-reliability", type=float, default=DEFAULT_MIN_RELIABILITY,
                        help=f"Min reliability 0..1 (default: {DEFAULT_MIN_RELIABILITY})")
    parser.add_argument("--image", default=COMFYUI_IMAGE,
                        help="Docker image")
    parser.add_argument("--template-hash", default=DEFAULT_TEMPLATE_HASH,
                        help="Vast template hash or URL")
    parser.add_argument("--launch", action="store_true",
                        help="Auto-launch best offer without prompting")
    parser.add_argument("--top", type=int, default=3,
                        help="Offers to show (default: 3)")
    args = parser.parse_args()

    # resolve volume first — determines machine_id and scoring profile
    vol_id = None
    machine_id = None
    volume_mode = args.volume is not None

    if volume_mode:
        vol_id, machine_id = resolve_volume(args.volume)

    # GPU picker
    if args.gpu is None:
        args.gpu = pick_gpu()

    mode_str = f"volume=V.{vol_id} machine={machine_id}" if volume_mode else f"disk≥{args.min_disk}GB (no volume)"
    print(
        f"\n[search] GPU={args.gpu}  {mode_str}  "
        f"price≤${args.max_price}/hr  bw_cost≤${args.max_bwcost}/TB  "
        f"reliability≥{args.min_reliability:.3f}"
    )

    offers = fetch_offers(
        args.gpu,
        args.min_disk,
        args.max_price,
        args.max_bwcost,
        args.min_reliability,
        machine_id=machine_id,
    )

    if not offers:
        if volume_mode:
            print(
                f"[error] No offers on machine {machine_id} matching GPU={args.gpu!r}.\n"
                f"        That machine may not have this GPU available right now.\n"
                f"        Try without --volume to search all machines."
            )
        else:
            print("[error] No offers found — try relaxing --max-price or --min-reliability")
        sys.exit(1)

    ranked = score_offers(offers, volume_mode)
    top = ranked[: args.top]

    W = WEIGHTS_VOLUME if volume_mode else WEIGHTS_NO_VOLUME
    profile = "volume mode (disk_bw + reliability boosted)" if volume_mode else "no-volume mode (price + inet_down boosted)"
    print(f"\n{'─'*64}")
    print(f"  Found {len(offers)} offers — top {len(top)} — scoring: {profile}")
    print(f"{'─'*64}")

    for i, o in enumerate(top, 1):
        print(fmt_offer(i, o, volume_mode, vol_id))

    print(f"\n{'─'*64}")

    if args.launch:
        best = top[0]
        launch_instance(best["id"], args.min_disk, args.image, args.template_hash, volume_mode, vol_id)
        return

    # interactive
    print("\nOptions:")
    for i, o in enumerate(top, 1):
        print(f"  [{i}] Rent #{i}  ID {o['id']}  ${o['dph_total']:.4f}/hr  score={o['_score']:.3f}")
    print("  [q] Quit")

    try:
        choice = input("\nYour choice: ").strip().lower()
    except EOFError:
        print("\nNo interactive input. Exiting.")
        return

    if choice == "q":
        print("Cancelled.")
        return

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(top):
            selected = top[idx]
            disk_to_use = min(max(args.min_disk, selected.get("disk_space", args.min_disk)), 500)
            launch_instance(selected["id"], disk_to_use, args.image, args.template_hash, volume_mode, vol_id)
        else:
            print("Invalid choice.")
    except ValueError:
        print("Invalid input.")


if __name__ == "__main__":
    main()