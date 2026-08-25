"""Explicit Agent portfolio runner. Deterministic mode is offline; live modes are opt-in."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from server.core.agent.policy import POLICY_VERSION
from server.core.agent.runtime_openai import AGENTS_SDK_VERSION
from server.core.storage.agent_runs import RESPONSE_SCHEMA_VERSION
from tests.evals.portfolio_v2 import SCORER_VERSION, score_portfolio


ROOT = Path(__file__).resolve().parent


def _load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def deterministic_report(*, generated_at: str | None = None) -> dict[str, Any]:
    instant = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return score_portfolio(
        _load("agent_portfolio_v2.json"),
        _load("agent_baseline_v1.json"),
        _load("observed_fake_runs_v1.json"),
        _load("observed_fake_runs_v2.json"),
        report_metadata={
            "model": "deterministic-fixtures",
            "endpoint_type": "none",
            "sdk_version": AGENTS_SDK_VERSION,
            "policy_version": POLICY_VERSION,
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
            "generated_at": instant,
        },
    )


def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    from tests.evals.run_portfolio_live import run_live_portfolio

    api_key = (
        os.environ.get("OPENAI_API_KEY", "")
        if args.source == "openai"
        else os.environ.get("CHESS_AGENT_API_KEY", "")
    )
    if not api_key:
        variable = "OPENAI_API_KEY" if args.source == "openai" else "CHESS_AGENT_API_KEY"
        raise SystemExit(f"{variable} is required for an explicit {args.source} eval")
    if args.source == "custom" and not args.base_url:
        raise SystemExit("--base-url is required for a custom endpoint eval")
    with tempfile.TemporaryDirectory(prefix="chesscoach-agent-live-eval-") as data_dir:
        return run_live_portfolio(
            source=args.source,
            model=args.model,
            base_url=args.base_url,
            api_key=api_key,
            data_dir=data_dir,
            certificate_data_dir=args.certificate_data_dir,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("deterministic", "openai", "custom"), default="deterministic")
    parser.add_argument("--model", default=os.environ.get("CHESS_AGENT_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("CHESS_AGENT_BASE_URL", ""))
    parser.add_argument("--certificate-data-dir", default=os.environ.get("CHESSCOACH_DATA_DIR", ""))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.source != "deterministic" and not args.model:
        raise SystemExit("--model or CHESS_AGENT_MODEL is required for a live eval")
    report = deterministic_report() if args.source == "deterministic" else _run_live(args)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
