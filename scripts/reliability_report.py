#!/usr/bin/env python3
"""Print the TASK-005 §12 reliability report for a run-manifest root.

Reads only: run manifests, audit logs, budget ledgers, and the GPU
lease registry under --root. Prints the JSON report from
h3_reliability.compute_reliability.

Every ratio metric in the JSON carries both the legacy
`confidence_interval_95` list and a `wilson_95_ci` {"lower",
"upper"} dict (TASK-RELEASE-V032 R3), plus the `a_vs_f_comparison`
side-by-side summary, the `f_95_status` honest conclusion, the
`startup_mode_breakdown`, and the metric-F `lease_cross_check`.

With --summary, a short human-readable summary (A vs F, F_95 status,
lease cross-check, startup-mode breakdown) is also printed to
stderr; stdout stays pure JSON either way.

Example:
    python3 scripts/reliability_report.py --root ~/workspace/Muse_Colab_H3_Drive_v3
    python3 scripts/reliability_report.py --root <root> --summary
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from h3_reliability import compute_reliability


def _fmt_rate(name: str, metric: dict) -> str:
    ci = metric.get("wilson_95_ci")
    ci_s = f"[{ci['lower']}, {ci['upper']}]" if ci else "n/a"
    return (f"{name}: rate={metric['rate']} "
            f"({metric['numerator']}/{metric['denominator']}), "
            f"Wilson 95% CI={ci_s}, status={metric['status']}")


def _print_summary(report: dict) -> None:
    """Human-readable A-vs-F / F_95 / lease / startup-mode summary."""
    metrics = report["metrics"]
    lines = ["reliability summary (A vs F, side by side):"]
    lines.append("  " + _fmt_rate("first_attempt_success_rate (A)",
                                  metrics["first_attempt_success_rate"]))
    lines.append("  " + _fmt_rate("deadline_bound_e2e_success_rate (F)",
                                  metrics["deadline_bound_e2e_success_rate"]))
    lines.append("  " + report["a_vs_f_comparison"]["note"])
    lines.append(f"  {report['f_95_status']}: {report['f_95_evidence']}")
    lease = metrics["deadline_bound_e2e_success_rate"].get(
        "lease_cross_check", {})
    lines.append(
        f"  lease cross-check: registry={lease.get('registry')}, "
        f"leases_checked={lease.get('leases_checked')}, "
        f"outstanding={len(lease.get('outstanding_leases') or [])}")
    lines.append("  startup_mode_breakdown:")
    for mode, bd in report["startup_mode_breakdown"].items():
        lines.append(
            f"    {mode}: runs={bd['runs']}, completed={bd['completed']}, "
            f"first_attempt={bd['first_attempt_success_rate']['rate']} "
            f"({bd['first_attempt_success_rate']['numerator']}/"
            f"{bd['first_attempt_success_rate']['denominator']}), "
            f"eventual={bd['eventual_task_success_rate']['rate']} "
            f"({bd['eventual_task_success_rate']['numerator']}/"
            f"{bd['eventual_task_success_rate']['denominator']})")
    print("\n".join(lines), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True,
                        help="production root holding model_cache/run_manifests/")
    parser.add_argument("--task", default=None,
                        help="only runs for this production_task_id")
    parser.add_argument("--since", default=None,
                        help="only runs created at or after this UTC timestamp")
    parser.add_argument("--until", default=None,
                        help="only runs created at or before this UTC timestamp")
    parser.add_argument("--output-root", default=None,
                        help="directory holding <run_id>/<variant>.mp4 files "
                             "(metric C also verifies the local file)")
    parser.add_argument("--summary", action="store_true",
                        help="also print a human-readable A-vs-F / F_95 / "
                             "lease / startup-mode summary to stderr "
                             "(stdout stays pure JSON)")
    args = parser.parse_args()
    report = compute_reliability(
        args.root, production_task_id=args.task,
        since=args.since, until=args.until,
        output_root=args.output_root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.summary:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
