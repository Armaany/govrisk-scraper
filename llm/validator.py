# Validation helpers for cleaning and normalizing LLM response fields.


def validate_llm_response(raw: dict) -> dict:
    """Normalize LLM output values to allowed enums and fallback defaults."""
    cleaned = dict(raw or {})

    allowed_relevance = {"high", "medium", "low", "unclear"}
    relevance = str(cleaned.get("relevance_score", "")).strip().lower()
    if relevance not in allowed_relevance:
        cleaned["relevance_score"] = "unclear"
    else:
        cleaned["relevance_score"] = relevance

    allowed_bid = {"pursue", "monitor", "pass", "pass_", "insufficient_info"}
    bid = str(cleaned.get("bid_recommendation", "")).strip().lower()
    if bid not in allowed_bid:
        cleaned["bid_recommendation"] = "insufficient_info"
    else:
        # Normalise "pass" → "pass_" to match the BidRecommendation enum value
        cleaned["bid_recommendation"] = "pass_" if bid == "pass" else bid

    allowed_confidence = {"high", "medium", "low", "error"}
    confidence = str(cleaned.get("llm_confidence", "")).strip().lower()
    if confidence not in allowed_confidence:
        cleaned["llm_confidence"] = "low"
    else:
        cleaned["llm_confidence"] = confidence

    summary = cleaned.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        cleaned["summary"] = "No summary generated — review source listing"
    else:
        cleaned["summary"] = summary.strip()

    return cleaned
