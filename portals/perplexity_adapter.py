"""Perplexity portal adapter — uses sonar-pro for real-time web search discovery."""
import hashlib
import json
import logging
import re

import httpx

from portals.base_adapter import (
    CATEGORY_CONNECTION,
    CATEGORY_HTTP_STATUS,
    CATEGORY_RESPONSE_PARSE,
    CATEGORY_TIMEOUT,
    OP_LISTING_FETCH,
    OP_RESPONSE_PARSE,
    BasePortalAdapter,
    PortalFetchError,
    _safe_cause_label,
)

logger = logging.getLogger(__name__)

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"


def _deterministic_hash(link: str) -> str:
    """Return a 12-char hex digest of the SHA-256 hash of the given link."""
    return hashlib.sha256(link.encode()).hexdigest()[:12]


class PerplexityAdapter(BasePortalAdapter):
    """Adapter that queries Perplexity sonar-pro for procurement opportunities."""

    portal_name = "perplexity"

    async def fetch_opportunities(self) -> list[dict]:
        if not self.config.perplexity_enabled:
            return []

        prompt = self._build_prompt()
        headers = {
            "Authorization": f"Bearer {self.config.perplexity_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(PERPLEXITY_API_URL, json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Logs status + sanitized URL only; never the Authorization header.
                self._log_http_error(exc)
                raise PortalFetchError(
                    "Perplexity",
                    OP_LISTING_FETCH,
                    CATEGORY_HTTP_STATUS,
                    attempts=1,
                    http_status=exc.response.status_code,
                ) from exc
            except httpx.TimeoutException as exc:
                self._log_error(exc, detail="request timed out")
                raise PortalFetchError(
                    "Perplexity",
                    OP_LISTING_FETCH,
                    CATEGORY_TIMEOUT,
                    attempts=1,
                    reason=_safe_cause_label(exc),
                ) from exc
            except httpx.RequestError as exc:
                self._log_error(exc, detail="request failed")
                raise PortalFetchError(
                    "Perplexity",
                    OP_LISTING_FETCH,
                    CATEGORY_CONNECTION,
                    attempts=1,
                    reason=_safe_cause_label(exc),
                ) from exc

        # Decode the HTTP response body. A body that is not valid JSON is a
        # whole-response parse failure → typed, credential-safe PortalFetchError.
        try:
            data = response.json()
        except Exception as exc:
            self._log_parse_error(exc)
            raise PortalFetchError(
                "Perplexity",
                OP_RESPONSE_PARSE,
                CATEGORY_RESPONSE_PARSE,
                attempts=1,
                reason=_safe_cause_label(exc),
            ) from exc

        return self._parse_response(data)

    def _build_prompt(self) -> str:
        keywords = ", ".join(self.config.sector_keywords)
        countries = ", ".join(self.config.target_countries)
        return (
            f"Search for current open procurement opportunities, tenders, and grants "
            f"related to: {keywords}. Focus on opportunities in: {countries}. "
            f"Return a JSON array of up to {self.config.max_results} opportunities. "
            f"Each object must have these exact keys: opportunity_title, funder_organisation, "
            f"country_region, deadline (ISO date or null), opportunity_link (URL), "
            f"description_snippet (max 500 chars). Return ONLY the JSON array, no other text."
        )

    def _parse_response(self, data: dict) -> list[dict]:
        try:
            text = data["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            items = json.loads(text)
            results = []
            for item in items:
                link = item.get("opportunity_link", "")
                results.append({
                    "opportunity_id": f"perplexity-{_deterministic_hash(link)}",
                    "source_portal": "perplexity",
                    "matched_keywords": [],
                    "contract_value": None,  # not available from Perplexity responses
                    **{k: item.get(k) for k in [
                        "opportunity_title",
                        "funder_organisation",
                        "country_region",
                        "deadline",
                        "opportunity_link",
                        "description_snippet",
                    ]},
                })
            return results
        except Exception as exc:
            self._log_parse_error(exc)
            raise PortalFetchError(
                "Perplexity",
                OP_RESPONSE_PARSE,
                CATEGORY_RESPONSE_PARSE,
                attempts=1,
                reason=_safe_cause_label(exc),
            ) from exc
