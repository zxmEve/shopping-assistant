"""Replay saved proposals at the validation boundary; never call a provider.

Original usage/model latency remain source measurements, not replay measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path
from uuid import uuid4

from procure_pilot.domain.requirements import RequirementSpec
from procure_pilot.evals.requirement_runner import BACKEND_ROOT, _distribution
from procure_pilot.evals.requirements import (
    extract_facts,
    load_dataset,
    score_requirement,
    summarize,
)
from procure_pilot.services.requirement_validation import RequirementSpecService


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay_requirements(
    *,
    source_path: Path,
    dataset_path: Path,
    output_root: Path,
    service=None,
    validator_path: Path | None = None,
) -> tuple[dict, Path]:
    source = json.loads(source_path.read_text())
    if source.get("mode") != "live" or source.get("report_version") != "requirement-report-v1":
        raise ValueError("Replay requires an original live requirement report")
    if sha(dataset_path) != source["manifest"]["dataset_sha256"]:
        raise ValueError("Replay dataset must be byte-identical to the live dataset")
    dataset = load_dataset(dataset_path)
    labels = {c.case_id: c for c in dataset.cases}
    scorer_path = Path(__file__).with_name("requirements.py")
    expected_scorer = source["manifest"]["source_files"].get(
        "src/procure_pilot/evals/requirements.py"
    )
    if expected_scorer != sha(scorer_path):
        raise ValueError("Replay requires the unchanged live scorer implementation")
    service = service or RequirementSpecService()
    validator_path = (
        validator_path or BACKEND_ROOT / "src/procure_pilot/services/requirement_validation.py"
    )
    artifact = output_root / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10])
    artifact.mkdir(parents=True, exist_ok=False)
    results = []
    for original in source["results"]:
        case = labels[original["case_id"]]
        if original["proposal_spec"] is None or original["error_type"]:
            raise ValueError("This replay requires a saved, successfully parsed proposal per case")
        proposed = RequirementSpec.model_validate(original["proposal_spec"])
        saved_base = original["envelope"]["requirement_spec"]
        base = RequirementSpec.model_validate(saved_base) if saved_base else None
        messages = original["input_messages"]
        before = score_requirement(
            case, proposed, messages=messages, base=base, expected_authority="PROPOSED"
        )
        if before != original["proposal"]:
            raise ValueError("Saved proposal score changed before replay")
        final = None
        fallback_used = False
        validation_error = None
        execution_error = None
        started = time.perf_counter()
        try:
            try:
                final = service.validate(
                    proposed.model_copy(deep=True),
                    message_id="msg_current",
                    text=messages["msg_current"],
                    base=base,
                )
            except ValueError as exc:
                fallback_used = True
                validation_error = str(exc)
                fallback = service.propose(
                    run_id=proposed.run_id,
                    message_id="msg_current",
                    text=messages["msg_current"],
                    base=base,
                )
                final = service.validate(
                    fallback, message_id="msg_current", text=messages["msg_current"], base=base
                )
        except Exception as exc:
            execution_error = type(exc).__name__
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        score = score_requirement(
            case,
            final,
            messages=messages,
            base=base,
            expected_authority="VALIDATED",
            error="execution_failed" if execution_error else None,
        )
        original_final = (
            RequirementSpec.model_validate(original["final_spec"])
            if original["final_spec"]
            else None
        )
        same_facts = (sorted(f.key() for f in extract_facts(final)) if final else []) == (
            sorted(f.key() for f in extract_facts(original_final)) if original_final else []
        )
        row = dict(
            case_id=case.case_id,
            repetition=original["repetition"],
            category=case.category,
            group_id=case.group_id,
            split=case.split,
            input_sha256=original["input_sha256"],
            input_messages=messages,
            proposal=before,
            proposal_spec=original["proposal_spec"],
            original_final=original["final"],
            original_fallback_used=original["fallback_used"],
            final=score,
            final_spec=final.model_dump(mode="json") if final else None,
            fallback_used=fallback_used,
            validation_error=validation_error,
            error_type=execution_error,
            validation_duration_ms=duration_ms,
            model_duration_ms=None,
            new_model_requests=0,
            original_final_score_reproduced=score == original["final"],
            original_final_facts_reproduced=same_facts,
        )
        results.append(row)
    final_summary = summarize(results, "final")
    comparison = {}
    for phase, scores in [
        ("before", [r["original_final"] for r in results]),
        ("after", [r["final"] for r in results]),
    ]:
        comparison[phase] = dict(
            passed=sum(s["passed"] for s in scores),
            omissions=sum(s["omission_count"] for s in scores),
            wrong=sum(s["wrong_count"] for s in scores),
            additions=sum(s["addition_count"] for s in scores),
        )
    worsened = [
        r["case_id"]
        for r in results
        if any(
            r["final"][k] > r["original_final"][k]
            for k in ["omission_count", "wrong_count", "addition_count"]
        )
        or (r["original_final"]["passed"] and not r["final"]["passed"])
    ]
    report = dict(
        report_version="requirement-validation-replay-v1",
        mode="validation_replay",
        generated_at=datetime.now(UTC).isoformat(),
        source_report_sha256=sha(source_path),
        source_report=str(source_path.resolve()),
        dataset_sha256=sha(dataset_path),
        scorer_sha256=sha(scorer_path),
        validator_sha256=sha(validator_path),
        source_files={
            str(p.relative_to(BACKEND_ROOT)): sha(p)
            for p in sorted((BACKEND_ROOT / "src").rglob("*.py"))
        },
        case_count=len(results),
        new_model_requests=0,
        new_model_tokens=0,
        original_live_usage_not_remeasured=source["usage"],
        original_live_latency_not_remeasured=source["latency"],
        proposal=summarize(results, "proposal"),
        final=final_summary,
        comparison=comparison,
        worsened_case_ids=worsened,
        fallback_count=sum(r["fallback_used"] for r in results),
        replay_latency=_distribution([r["validation_duration_ms"] for r in results]),
        results=results,
    )
    path = artifact / "replay.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--validator-snapshot", type=Path)
    args = parser.parse_args()
    service = None
    if args.validator_snapshot:
        source = json.loads(args.source.read_text())
        expected = source["manifest"]["source_files"][
            "src/procure_pilot/services/requirement_validation.py"
        ]
        if sha(args.validator_snapshot) != expected:
            parser.error("Historical validator snapshot must match the live source hash")
        loader = SourceFileLoader("_historical_requirement_validator", str(args.validator_snapshot))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)
        service = module.RequirementSpecService()
    report, path = replay_requirements(
        source_path=args.source,
        dataset_path=args.dataset,
        output_root=args.output_dir,
        service=service,
        validator_path=args.validator_snapshot,
    )
    print(
        json.dumps(
            dict(
                report=str(path),
                comparison=report["comparison"],
                worsened_cases=report["worsened_case_ids"],
                fallback_count=report["fallback_count"],
                historical_scores_reproduced=sum(
                    r["original_final_score_reproduced"] for r in report["results"]
                ),
                historical_facts_reproduced=sum(
                    r["original_final_facts_reproduced"] for r in report["results"]
                ),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
