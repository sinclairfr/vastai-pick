# vastai-pick

Smart CLI tool to find and rent the best Vast.ai GPU offer for ComfyUI workloads. Filters by price, bandwidth cost, and disk space — then scores and ranks results so you don't have to eyeball 60 rows of offers.

## Why

Vast.ai's marketplace has hundreds of offers. The sticker price means nothing without knowing the bandwidth cost — a $0.30/hr GPU with $20/TB egress will wreck you when pulling FLUX models. This tool surfaces the real cost upfront and picks the optimal offer automatically.

## Requirements

- Python 3.10+
- [`vastai` CLI](https://github.com/vast-ai/vast-cli) installed and authenticated

```bash
brew install pipx
pipx install vastai
vastai set api-key <your_key>   # from https://cloud.vast.ai/manage-keys/
```

## Install

```bash
curl -sO https://raw.githubusercontent.com/YOUR_USERNAME/vastai-pick/main/vastai-pick.py
mv vastai-pick.py ~/.local/bin/vastai-pick
chmod +x ~/.local/bin/vastai-pick
```

Make sure `~/.local/bin` is in your `$PATH`. If not, add to `~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Usage

```bash
# Interactive — pick GPU type, then choose from top 3 offers
vastai-pick

# Auto-launch the best offer without any prompt
vastai-pick --launch

# Skip GPU picker, go straight to offers
vastai-pick --gpu "RTX 4090"

# LoRA training setup — A100, more disk, higher price ceiling
vastai-pick --gpu "A100 SXM4" --min-disk 500 --max-price 2.5

# Tight budget
vastai-pick --gpu "RTX 3090" --max-price 0.4 --max-bwcost 0.05

# Show top 5 instead of 3
vastai-pick --top 5

# Use a custom Docker image
vastai-pick --image pytorch/pytorch:latest --launch
```

## Demo

```
$ vastai-pick

  Select GPU type:

  [1] RTX 4090          24GB  — best price/perf, FLUX inference & LoRA
  [2] RTX 5090          32GB  — latest gen, excellent for large models
  [3] RTX 5080          16GB  — budget next-gen
  ...
  [14] A100 SXM4        80GB  — LoRA training without quantization
  [15] H100 SXM         80GB  — fastest training, premium price
  ...

  GPU choice [1 = RTX 4090]: 1

[search] GPU=RTX 4090  disk≥200GB  price≤$2.0/hr  bw_cost≤$0.15/TB
────────────────────────────────────────────────────────────
  Found 56 offers — showing top 3
────────────────────────────────────────────────────────────

  ★  ID 20847015  —  1x RTX 4090  (24564MB VRAM, CUDA 13.1)
     Price     : $0.3681/hr
     Bandwidth : ↓7284 Mbps  (cost: $0.009/TB)
     Disk      : 626 GB
     Location  : Spain, ES
     Reliability: 99.9%
     Composite score: 0.724

  #2  ID 31825455  —  1x RTX 4090  (24564MB VRAM, CUDA 12.8)
     Price     : $0.3752/hr
     Bandwidth : ↓6569 Mbps  (cost: $0.003/TB)
     Disk      : 1324 GB
     Location  : Texas, US
     Reliability: 99.7%
     Composite score: 0.707

  #3  ID 16314541  —  1x RTX 4090  (24564MB VRAM, CUDA 13.1)
     Price     : $0.3481/hr
     Bandwidth : ↓6352 Mbps  (cost: $0.009/TB)
     Disk      : 548 GB
     Location  : Spain, ES
     Reliability: 99.9%
     Composite score: 0.703

────────────────────────────────────────────────────────────
Options:
  [1] Rent #1 (ID 20847015, $0.3681/hr)
  [2] Rent #2 (ID 31825455, $0.3752/hr)
  [3] Rent #3 (ID 16314541, $0.3481/hr)
  [q] Quit

Your choice: 1

[launch] Renting instance 20847015 ...
```

## Scoring

Offers are ranked by a weighted composite score — all values are min-max normalized before weighting so units don't interfere:

| Factor | Weight | Direction |
|---|---|---|
| Price ($/hr) | 50% | lower is better |
| Download bandwidth (Mbps) | 25% | higher is better |
| Disk space (GB) | 15% | more is better |
| DLP/$ platform score | 10% | higher is better |

Tweak the weights at the top of the script (`W_PRICE`, `W_BW_DOWN`, `W_DISK`, `W_SCORE`) to match your priorities.

## Supported GPUs

All 19 GPUs currently available on Vast.ai, from RTX 3090 to B200. GPU names are kept in sync with Vast.ai's internal `gpu_name` field (spaces matter — `RTX 4090` not `RTX_4090`).

## Options

| Flag | Default | Description |
|---|---|---|
| `--gpu` | interactive | GPU model name — skips picker if provided |
| `--min-disk` | 200 | Minimum disk space in GB |
| `--max-price` | 2.0 | Maximum $/hr |
| `--max-bwcost` | 0.15 | Maximum bandwidth cost in $/TB |
| `--image` | `vastai/comfyui:latest` | Docker image to use |
| `--launch` | false | Auto-launch best offer, no prompt |
| `--top` | 3 | Number of offers to display |

## License

MIT
