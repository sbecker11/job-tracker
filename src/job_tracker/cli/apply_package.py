"""CLI: evaluate one stored lead with the LLM (CLAUDE.md's JD Match Framework)
and, on a "pursue" verdict, generate + save a tailored résumé and cover
letter as real .docx files.

Also updates a matching record in a comparison JSONL file (if one is given
and a matching company/title line exists) so this can slot into the
manual-comparison workflow instead of being a separate, disconnected tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT, generate_package, render_jd_review
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect


def _find_lead(conn, company: str, title: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM job_leads WHERE company = ? AND title = ?",
        (company, title),
    ).fetchone()
    return dict(row) if row else None


def _update_comparison_jsonl(path: Path, *, company: str, title: str, result) -> bool:
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]

    updated = False
    for d in lines:
        if d.get("company") == company and d.get("title") == title:
            d["claude_ai_response"] = result.evaluation.rationale
            d["claude_ai_verdict"] = result.evaluation.verdict
            if result.resume_path:
                d["resume-path"] = str(result.resume_path)
            if result.cover_letter_path:
                d["cover-letter-path"] = str(result.cover_letter_path)
            eval_m = result.evaluation.metrics
            d["eval_input_tokens"] = eval_m.input_tokens if eval_m else None
            d["eval_output_tokens"] = eval_m.output_tokens if eval_m else None
            d["eval_time_s"] = round(eval_m.elapsed_s, 2) if eval_m else None
            d["eval_cost_usd"] = round(eval_m.cost_usd, 5) if eval_m and eval_m.cost_usd is not None else None
            gen_m = result.generate_metrics
            d["generate_input_tokens"] = gen_m.input_tokens if gen_m else None
            d["generate_output_tokens"] = gen_m.output_tokens if gen_m else None
            d["generate_time_s"] = round(gen_m.elapsed_s, 2) if gen_m else None
            d["generate_cost_usd"] = round(gen_m.cost_usd, 5) if gen_m and gen_m.cost_usd is not None else None
            d["total_input_tokens"] = result.total_input_tokens
            d["total_output_tokens"] = result.total_output_tokens
            d["total_time_s"] = round(result.total_elapsed_s, 2)
            d["total_cost_usd"] = round(result.total_cost_usd, 5) if result.total_cost_usd is not None else None
            updated = True

    if updated:
        with path.open("w", encoding="utf-8") as f:
            for d in lines:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return updated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Evaluate a stored lead against CLAUDE.md's JD Match Framework via the "
        "Anthropic API, and on a pursue verdict generate a tailored résumé + cover letter."
    )
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id/alias (default: {DEFAULT_MODEL})")
    ap.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Artifacts land in <root>/<Company>_<Title>/ (JobDescription.docx, LLM_Review.docx, and on a "
        f"pursue verdict the résumé + cover letter too) (default: {DEFAULT_OUTPUT_ROOT})",
    )
    ap.add_argument(
        "--comparison-jsonl",
        type=Path,
        help="If given and a matching company/title line exists, update it with the result",
    )
    ap.add_argument("--json", action="store_true", help="Print the full result as JSON")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    lead = _find_lead(conn, args.company, args.title)
    conn.close()
    if lead is None:
        print(f"No stored lead found for company={args.company!r} title={args.title!r}", file=sys.stderr)
        return 1
    if not lead.get("jd_text"):
        print("That lead has no stored jd_text to evaluate.", file=sys.stderr)
        return 1

    result = generate_package(
        lead["jd_text"],
        company=args.company,
        title=args.title,
        model=args.model,
        output_root=args.output_root,
    )

    if args.comparison_jsonl:
        found = _update_comparison_jsonl(
            args.comparison_jsonl, company=args.company, title=args.title, result=result
        )
        if not found:
            print(f"(note: no matching line for {args.company}/{args.title} in {args.comparison_jsonl})", file=sys.stderr)

    def _metrics_dict(m):
        if m is None:
            return None
        return {
            "step": m.step,
            "model": m.model,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "time_s": round(m.elapsed_s, 2),
            "cost_usd": round(m.cost_usd, 5) if m.cost_usd is not None else None,
        }

    if args.json:
        print(
            json.dumps(
                {
                    "verdict": result.evaluation.verdict,
                    "match_pct": result.evaluation.match_pct,
                    "job_summary": result.evaluation.job_summary,
                    "dealbreaker_checks": result.evaluation.dealbreaker_checks,
                    "skills_alignment": result.evaluation.skills_alignment,
                    "flags": result.evaluation.flags,
                    "rationale": result.evaluation.rationale,
                    "framing_guidance": result.evaluation.framing_guidance,
                    "jd_path": str(result.jd_path) if result.jd_path else None,
                    "review_path": str(result.review_path) if result.review_path else None,
                    "resume_path": str(result.resume_path) if result.resume_path else None,
                    "cover_letter_path": str(result.cover_letter_path) if result.cover_letter_path else None,
                    "warnings": result.warnings,
                    "evaluate_metrics": _metrics_dict(result.evaluation.metrics),
                    "generate_metrics": _metrics_dict(result.generate_metrics),
                    "total_input_tokens": result.total_input_tokens,
                    "total_output_tokens": result.total_output_tokens,
                    "total_time_s": round(result.total_elapsed_s, 2),
                    "total_cost_usd": round(result.total_cost_usd, 5) if result.total_cost_usd is not None else None,
                },
                indent=2,
            )
        )
        return 0

    print(render_jd_review(result.evaluation, company=args.company, title=args.title))
    if result.jd_path:
        print(f"(job description saved to: {result.jd_path})")
    if result.review_path:
        print(f"(review saved to: {result.review_path})")

    def _fmt_cost(v):
        return f"${v:.4f}" if v is not None else "n/a"

    em = result.evaluation.metrics
    if em:
        print(
            f"\n[evaluate]  {em.input_tokens} in / {em.output_tokens} out tokens, "
            f"{em.elapsed_s:.1f}s, {_fmt_cost(em.cost_usd)}"
        )

    if result.evaluation.verdict == "pursue":
        print(f"\nRésumé saved to:       {result.resume_path}")
        print(f"Cover letter saved to: {result.cover_letter_path}")
        gm = result.generate_metrics
        if gm:
            print(
                f"[generate]  {gm.input_tokens} in / {gm.output_tokens} out tokens, "
                f"{gm.elapsed_s:.1f}s, {_fmt_cost(gm.cost_usd)}"
            )
        print(
            f"[total]     {result.total_input_tokens} in / {result.total_output_tokens} out tokens, "
            f"{result.total_elapsed_s:.1f}s, {_fmt_cost(result.total_cost_usd)}"
        )
        if result.warnings:
            print(f"\n⚠ WARNING — house-rule issues found in generated content: {result.warnings}")
    else:
        print("\nNo package generated (verdict was not 'pursue').")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
