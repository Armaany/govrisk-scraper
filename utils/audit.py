# JSONL audit logger utilities for scraper run traceability and diagnostics.
import json
from datetime import datetime
from pathlib import Path


class AuditLogger:
    """Appends structured audit events to a local JSON-lines log file."""

    def __init__(self, log_path: str = "./audit.log"):
        """Store log path and create audit file if it does not exist."""
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)

    def log(
        self,
        event_type: str,
        detail: str,
        opportunity_id: str = None,
        title: str = None,
        llm_called: bool = False,
        confidence: str = None,
        error_message: str = None,
    ) -> None:
        """Append one structured audit event as a single JSON line."""
        try:
            event = {
                "timestamp": datetime.now().isoformat(),
                "event_type": event_type,
                "opportunity_id": opportunity_id or None,
                "title": title or None,
                "llm_called": bool(llm_called),
                "confidence": confidence or None,
                "detail": detail,
                "error_message": error_message or None,
            }
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"Audit logging failed: {exc}")

    def log_run_start(self, mode: str, max_results: int) -> None:
        """Record run start details including mode and max results."""
        detail = f"mode={mode}, max_results={max_results}"
        self.log(event_type="run_start", detail=detail)

    def log_run_complete(
        self,
        total_scraped: int,
        total_matched: int,
        total_written: int,
        llm_calls_made: int,
        duplicates_skipped: int,
        errors: int,
    ) -> None:
        """Record completion summary counts and print them to console."""
        summary = (
            f"total_scraped={total_scraped}, total_matched={total_matched}, "
            f"total_written={total_written}, llm_calls_made={llm_calls_made}, "
            f"duplicates_skipped={duplicates_skipped}, errors={errors}"
        )
        self.log(event_type="run_complete", detail=summary)
        print(f"Run complete: {summary}")

    def log_filtered_out(self, url: str) -> None:
        """Record an opportunity URL filtered out by keyword/geography checks."""
        self.log(event_type="filtered_out", detail=url)

    def log_duplicate(self, opportunity_id: str, title: str) -> None:
        """Record that a duplicate opportunity was skipped."""
        self.log(
            event_type="duplicate_skipped",
            detail="Duplicate opportunity skipped.",
            opportunity_id=opportunity_id,
            title=title,
        )

    def log_error(self, error_message: str, opportunity_id: str = None) -> None:
        """Record an error event with full error message payload."""
        self.log(
            event_type="error",
            detail="Error encountered during run.",
            opportunity_id=opportunity_id,
            error_message=error_message,
        )
