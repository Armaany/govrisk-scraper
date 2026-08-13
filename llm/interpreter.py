# Claude-based interpretation layer for parsed Devex opportunities.
import json

import anthropic

from config import Config
from llm.validator import validate_llm_response


class LLMInterpreter:
    """Generates BD interpretation fields from parsed opportunity data."""

    def __init__(self, config: Config):
        """Initialize Anthropic client and store runtime config."""
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def interpret(self, parsed: dict) -> dict:
        """Interpret parsed opportunity payload via Claude and return validated JSON."""
        title = parsed.get("opportunity_title")
        funder = parsed.get("funder_organisation")
        country = parsed.get("country_region")
        deadline = parsed.get("deadline")
        contract_value = parsed.get("contract_value")
        matched_keywords = parsed.get("matched_keywords")
        description_snippet = parsed.get("description_snippet")

        prompt = f"""SYSTEM: You are a senior business development analyst for GovRisk, an international development consulting firm specialising in anti-corruption, AML, justice reform, illicit financial flows, and human trafficking prevention in Latin America and the Caribbean.

Python has already extracted and structured the opportunity data below. Your job is to interpret it and add judgment. Return ONLY valid JSON.
No preamble, no explanation, no markdown.

USER: Interpret this opportunity and return JSON:
{{
  "summary": "3-4 sentence plain English summary for the BD team",
  "relevance_score": "high|medium|low|unclear",
  "relevance_reason": "one sentence explanation",
  "bid_recommendation": "pursue|monitor|pass|insufficient_info",
  "risk_flags": "notable risks or null if none",
  "llm_confidence": "high|medium|low"
}}

Scoring guide:
  high   — directly matches GovRisk expertise and LATAM geography
  medium — right sector but geography unclear or vice versa
  low    — tangential match
  unclear — not enough information

Bid recommendation guide:
  pursue           — strong match, recommend pursuit
  monitor          — partial match, watch for more
  pass             — poor fit
  insufficient_info — cannot assess

Already-extracted data from Python:
Title: {title}
Funder: {funder}
Country: {country}
Deadline: {deadline}
Value: {contract_value}
Matched keywords: {matched_keywords}
Description: {description_snippet}"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract text from the response content blocks
            text_blocks = [
                block.text
                for block in response.content
                if getattr(block, "type", "") == "text"
            ]
            response_text = "\n".join(text_blocks).strip()

            # Debug: show raw response before any parsing
            print(f"[LLM] Raw response (first 300 chars): {repr(response_text[:300])}")

            # Strip markdown code fences if Claude wrapped the JSON despite instructions
            # Handles: ```json\n...\n``` and ```\n...\n``` and lone backtick lines
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                # Drop first line (```json or ```) and last line (```)
                inner_lines = lines[1:]
                if inner_lines and inner_lines[-1].strip() == "```":
                    inner_lines = inner_lines[:-1]
                response_text = "\n".join(inner_lines).strip()
                print(f"[LLM] After fence strip: {repr(response_text[:200])}")

            if not response_text:
                raise ValueError("Claude returned an empty response")

            raw_json = json.loads(response_text)
            result = validate_llm_response(raw_json)
            print(f"[LLM] OK — relevance={result.get('relevance_score')} bid={result.get('bid_recommendation')}")
            return result

        except Exception as exc:
            import traceback
            print(f"[LLM] ERROR interpreting '{title}'")
            print(f"[LLM] API key loaded: {'yes' if self.config.anthropic_api_key else 'NO — KEY MISSING'}")
            if self.config.anthropic_api_key:
                print(f"[LLM] Key prefix: {self.config.anthropic_api_key[:16]}...")
            print(f"[LLM] Exception type: {type(exc).__name__}")
            print(f"[LLM] Exception message: {exc}")
            print("[LLM] Full traceback:")
            traceback.print_exc()
            return {
                "summary": None,
                "relevance_score": None,
                "relevance_reason": None,
                "bid_recommendation": None,
                "risk_flags": None,
                "llm_confidence": "error",
            }
