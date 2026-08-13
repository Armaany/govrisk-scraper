# Dataclasses and enums for normalized opportunity records and serialization.
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


class RelevanceScore(str, Enum):
    """Allowed relevance scores returned by LLM interpretation."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCLEAR = "unclear"


class BidRecommendation(str, Enum):
    """Allowed bid recommendations returned by LLM interpretation."""

    PURSUE = "pursue"
    MONITOR = "monitor"
    PASS_ = "pass_"
    INSUFFICIENT_INFO = "insufficient_info"


class LLMConfidence(str, Enum):
    """Allowed confidence labels for LLM output quality."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    ERROR = "error"


class ReviewStatus(str, Enum):
    """Review workflow status for internal business development processing."""

    PENDING_REVIEW = "pending_review"
    REVIEWED = "reviewed"
    BID = "bid"
    NO_BID = "no_bid"
    NEEDS_MORE_INFO = "needs_more_info"


@dataclass
class OpportunityRecord:
    """Represents one opportunity across scrape, filter, LLM, and review workflow."""

    devex_opportunity_id: str
    opportunity_title: str
    funder_organisation: str
    country_region: str
    deadline: Optional[date]
    contract_value: Optional[str]
    opportunity_link: str
    description_snippet: str
    matched_keywords: list[str] = field(default_factory=list)
    summary: Optional[str] = None
    relevance_score: Optional[RelevanceScore] = None
    relevance_reason: Optional[str] = None
    bid_recommendation: Optional[BidRecommendation] = None
    risk_flags: list[str] = field(default_factory=list)
    llm_confidence: Optional[LLMConfidence] = None
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    llm_called: bool = False
    anna_benchmark: bool = False
    scraped_at: datetime = field(default_factory=datetime.now)
    source_portal: str = "devex"

    def to_dict(self) -> dict[str, Any]:
        """Return a flat dictionary for storage adapters such as Google Sheets."""
        return {
           "portal_source": self.source_portal,
        "opportunity_title": self.opportunity_title or "",
        "funder_organisation": self.funder_organisation or "",
        "country_region": self.country_region or "",
        "deadline": self.deadline.isoformat() if self.deadline else "",
        "contract_value": self.contract_value or "",
        "opportunity_link": self.opportunity_link or "",
        "summary": self.summary or "",
        "relevance_score": self.relevance_score.value if self.relevance_score else "",
        "bid_recommendation": self.bid_recommendation.value if self.bid_recommendation else "",
        "risk_flags": ", ".join(self.risk_flags) if self.risk_flags else "",
        "review_status": self.review_status.value if self.review_status else "pending_review",
    }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpportunityRecord":
        """Create an OpportunityRecord from a dictionary payload."""
        deadline_value = data.get("deadline")
        scraped_at_value = data.get("scraped_at")
        matched_keywords_value = data.get("matched_keywords", [])
        risk_flags_value = data.get("risk_flags", [])

        if isinstance(matched_keywords_value, str):
            matched_keywords = [k.strip() for k in matched_keywords_value.split(",") if k.strip()]
        else:
            matched_keywords = list(matched_keywords_value or [])

        if isinstance(risk_flags_value, str):
            risk_flags = [k.strip() for k in risk_flags_value.split(",") if k.strip()]
        else:
            risk_flags = list(risk_flags_value or [])

        return cls(
            devex_opportunity_id=str(data.get("devex_opportunity_id", "")),
            opportunity_title=str(data.get("opportunity_title", "")),
            funder_organisation=str(data.get("funder_organisation", "")),
            country_region=str(data.get("country_region", "")),
            deadline=date.fromisoformat(deadline_value) if deadline_value else None,
            contract_value=data.get("contract_value"),
            opportunity_link=str(data.get("opportunity_link", "")),
            description_snippet=str(data.get("description_snippet", "")),
            matched_keywords=matched_keywords,
            summary=data.get("summary"),
            relevance_score=RelevanceScore(data["relevance_score"]) if data.get("relevance_score") else None,
            relevance_reason=data.get("relevance_reason"),
            bid_recommendation=BidRecommendation(data["bid_recommendation"]) if data.get("bid_recommendation") else None,
            risk_flags=risk_flags,
            llm_confidence=LLMConfidence(data["llm_confidence"]) if data.get("llm_confidence") else None,
            review_status=ReviewStatus(data.get("review_status", ReviewStatus.PENDING_REVIEW.value)),
            llm_called=bool(data.get("llm_called", False)),
            anna_benchmark=bool(data.get("anna_benchmark", False)),
            scraped_at=datetime.fromisoformat(scraped_at_value) if scraped_at_value else datetime.now(),
            source_portal=str(data.get("source_portal", "devex")),
        )
