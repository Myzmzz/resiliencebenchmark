"""Submit one L0 x <case> run to the new-cluster Stage-2 service, wait for it, and save its results.

Usage (the tunnel must be up on 127.0.0.1:28080):
    python3 run_dx.py --case D1 --harness codex --model qwen3.8-max
    python3 run_dx.py --case D7 --variant A --harness codex      # D7/D8 need A or B

Differences from the Lx round's run_lx.py:
- The level is always L0.  The Lx API only accepts a D case together with the
  complete L0 prompt (lx.LxRunRequest.validate_case_matches_level), so the case
  is the only variable of a run.
- D7/D8 carry tool_substitution_variant (A names the legal alternative after an
  honest help request, B gives a neutral hint).  The platform accepts it only
  from the fix branch codex/stage2-dx-round-fixes-20260911 onwards.
- Before submitting, it waits until no other Lx run is still active, so a run
  left over from a laptop sleep or a crash can never overlap the next one.
- It polls until the run is terminal instead of giving up on a short wall-clock
  deadline; a sleep must not make the chain submit the next run too early.
- Distinct exit codes let the chain tell a refused case (skip) from a broken
  platform (stop).

Exit codes:
    0   the run reached a terminal state and its results were saved
    10  the API refused the submission (HTTP 4xx); the reason is printed
    20  the API stayed unreachable (or 503) past the submit deadline
    30  the run did not become terminal within --max-hours
    40  another run stayed active past --max-wait-minutes; nothing was submitted
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Prompt variants of the Lx round, kept next to this script.
LX_ROUND_VARIANTS = HERE / "variants.json"
CAPABILITY_LOSS_CASES = ("D7", "D8")

EXIT_REFUSED = 10
EXIT_UNREACHABLE = 20
EXIT_NOT_TERMINAL = 30
EXIT_BLOCKED = 40
# The run ended with platform_status RESET_FAILED or BLOCKED: the platform
# itself is broken, so the chain must stop (chain_dx.sh stops on any code
# other than 0 and 10).
EXIT_PLATFORM_BROKEN = 50


def log(message: str) -> None:
    """Print one timestamped progress line (UTC) and flush it for the chain log."""
    print(f"  {time.strftime('%H:%M:%S', time.gmtime())} {message}", flush=True)


def request_json(method: str, url: str, body: dict | None = None, headers: dict | None = None,
                 timeout: float = 60.0) -> tuple[int, dict, dict]:
    """Return (status, parsed JSON body or {}, response headers) without raising on HTTP errors.

    Status 0 means the tunnel is not reachable right now; every caller retries it.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode(errors="replace")[:500]}
        return error.code, parsed, dict(error.headers)
    except (urllib.error.URLError, OSError) as error:
        return 0, {"error": f"{type(error).__name__}: {error}"}, {}


def l0_prompt(variants_file: Path) -> tuple[str, str]:
    """The verbatim copy-ready L0 prompt and the variant set id it belongs to."""
    variants = json.loads(variants_file.read_text())
    for variant in variants["variants"]:
        if variant.get("level") == "L0":
            # The variant API names the verbatim text "prompt"; the autonomy
            # cases API calls it "copy_ready_prompt". Accept either.
            prompt = variant.get("copy_ready_prompt") or variant.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise SystemExit(f"L0 has no prompt text; keys={sorted(variant)}")
            return prompt, variants["variant_set_id"]
    raise SystemExit(f"L0 not in {variants_file}")


def wait_until_idle(base: str, max_wait_minutes: int) -> bool:
    """Wait until no Lx run is non-terminal; False if one is still active at the cap."""
    deadline = time.time() + max_wait_minutes * 60
    announced = ""
    while True:
        status, body, _ = request_json("GET", f"{base}/api/v1/stage2/lx/runs")
        if status == 200:
            active = [run.get("run_id") for run in body.get("runs") or [] if run.get("terminal") is False]
            if not active:
                return True
            if ",".join(active) != announced:
                log(f"waiting for active run(s) to finish: {', '.join(active)}")
                announced = ",".join(active)
        if time.time() >= deadline:
            return False
        time.sleep(30)


def submit(base: str, payload: dict, idempotency_key: str, deadline: float) -> tuple[int, str]:
    """POST the run with one Idempotency-Key; retry 503 and tunnel drops until ``deadline``.

    Returns (exit code, run id or refusal text).  The same key on every retry
    guarantees a retry can never create a second run.
    """
    while True:
        status, body, headers = request_json("POST", f"{base}/api/v1/stage2/lx/runs", payload,
                                             {"Idempotency-Key": idempotency_key})
        if status in (200, 202) and body.get("run_id"):
            return 0, body["run_id"]
        if 400 <= status < 500:
            return EXIT_REFUSED, f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:800]}"
        if time.time() >= deadline:
            return EXIT_UNREACHABLE, f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:400]}"
        wait = int(headers.get("Retry-After") or 30)
        reason = "service busy (503)" if status == 503 else f"status {status}"
        log(f"submit {reason}, retry in {min(max(wait, 10), 60)}s")
        time.sleep(min(max(wait, 10), 60))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True, choices=["C0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"])
    parser.add_argument("--variant", choices=["A", "B"], default=None,
                        help="D7/D8 only: tool_substitution_variant")
    parser.add_argument("--harness", required=True, choices=["codex", "claude-code", "deepseek-harness"])
    parser.add_argument("--model", default="qwen3.8-max")
    parser.add_argument("--duration", type=int, default=300, help="fault duration disclosed by L0 (seconds)")
    parser.add_argument("--variants", type=Path, default=LX_ROUND_VARIANTS)
    parser.add_argument("--image", default="5746ecf", help="platform image tag, recorded in the run note")
    parser.add_argument("--base", default="http://127.0.0.1:28080")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    parser.add_argument("--max-wait-minutes", type=int, default=90)
    parser.add_argument("--submit-minutes", type=int, default=50)
    parser.add_argument("--max-hours", type=float, default=3.0)
    args = parser.parse_args()
    if (args.case in CAPABILITY_LOSS_CASES) != (args.variant is not None):
        parser.error("--variant is required for D7/D8 and not allowed for any other case")

    prompt, variant_set_id = l0_prompt(args.variants)
    if not wait_until_idle(args.base, args.max_wait_minutes):
        print(f"BLOCKED another run is still active after {args.max_wait_minutes} min", flush=True)
        return EXIT_BLOCKED

    label = f"{args.case}-{args.variant}" if args.variant else args.case
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    tag = f"newenv-L0-{label}-{args.harness}-{args.model}"
    payload = {
        "autonomy_level": "L0", "prompt": prompt, "application": "otel-demo",
        "harness": args.harness, "model": args.model, "llm_tag": tag,
        "duration_seconds": args.duration, "variant_set_id": variant_set_id, "case": args.case,
        "note": f"Dx round 2026-09-11, new Tencent 2-node cluster, image {args.image}",
    }
    if args.variant:
        payload["tool_substitution_variant"] = args.variant
    code, value = submit(args.base, payload, f"{tag}-{stamp}", time.time() + args.submit_minutes * 60)
    if code != 0:
        refusal = "REFUSED" if code == EXIT_REFUSED else "UNREACHABLE"
        print(f"{refusal} case={label} harness={args.harness} {value}", flush=True)
        return code
    run_id = value
    run_dir = args.out / f"L0-{label}-{args.harness}-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    log(f"run_id={run_id} case={label} harness={args.harness} model={args.model}")

    last_line = ""
    summary: dict = {}
    deadline = time.time() + args.max_hours * 3600
    while True:
        status, body, _ = request_json("GET", f"{args.base}/api/v1/stage2/lx/runs/{run_id}")
        if status == 200:
            summary = body
            progress = summary.get("progress") or {}
            counters = summary.get("counters") or {}
            line = (f"{summary.get('status')} platform={summary.get('platform_status')} "
                    f"phase={progress.get('current_phase')} events={counters.get('event_count')}")
            if line != last_line:
                log(line)
                last_line = line
            if summary.get("terminal"):
                break
        if time.time() >= deadline:
            (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
            print(f"NOT_TERMINAL case={label} harness={args.harness} run_id={run_id} "
                  f"after {args.max_hours} h", flush=True)
            return EXIT_NOT_TERMINAL
        time.sleep(20)

    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    results = {}
    for name in ("interactions", "usage", "score"):
        body = {}
        for _attempt in range(9):
            status, body, _ = request_json("GET", f"{args.base}/api/v1/stage2/lx/runs/{run_id}/{name}",
                                           timeout=120)
            if status != 0:
                break
            time.sleep(20)  # the tunnel is being re-created by the keeper
        results[name] = body
        (run_dir / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1))

    # A terminal run can still mean the platform itself is broken: RESET_FAILED
    # (the post-trial reset did not verify, e.g. the 09-11 D1 incident that
    # uninstalled OTel Demo) or BLOCKED (the environment gate refused to start).
    # Every later run would be refused the same way, so report it and let the
    # chain stop instead of burning through the case list.
    platform_status = str(summary.get("platform_status") or "")
    if platform_status in ("RESET_FAILED", "BLOCKED"):
        print(f"PLATFORM_{platform_status} case={label} harness={args.harness} run_id={run_id} "
              f"failure={json.dumps(summary.get('failure'), ensure_ascii=False)[:300]}", flush=True)
        return EXIT_PLATFORM_BROKEN

    score = results["score"]
    summary_score = score.get("score_summary") or {}
    failed = [check.get("check_id") or check.get("id") or check.get("name")
              for check in (score.get("checks") or []) if isinstance(check, dict) and check.get("passed") is False]
    interactions = results["interactions"]
    usage = results["usage"]
    print("RESULT", json.dumps({
        "run_id": run_id, "case": label, "harness": args.harness,
        "status": summary.get("status"), "platform_status": summary.get("platform_status"),
        "verdict": score.get("verdict"), "trial_validity": score.get("trial_validity"),
        "total_with_bonus": summary_score.get("total_with_bonus"), "max_score": summary_score.get("max_score"),
        "reason_codes": score.get("reason_codes"),
        "capability_loss_score": score.get("capability_loss_score"),
        "failure": summary.get("failure"),
        "failed_checks": failed,
        "interaction_count": len(interactions.get("interactions") or []) if isinstance(interactions, dict) else None,
        "usage_total_calls": (usage.get("summary") or {}).get("total_calls") if isinstance(usage, dict) else None,
        "saved": str(run_dir),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
