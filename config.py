# Environment configuration loading, parsing, and strict startup validation.
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Spanish-language sector keyword equivalents — always merged into SECTOR_KEYWORDS
# regardless of the env value, so no .env change is required to activate them.
_SPANISH_SECTOR_KEYWORDS: list[str] = [
    # Money laundering
    "lavado de dinero",
    "lavado de activos",
    "lavado",
    # Corruption
    "anticorrupcion",
    "anticorrupción",
    "corrupcion",
    "corrupción",
    # Illicit flows
    "flujos ilicitos",
    "flujos ilícitos",
    "ilicito",
    "ilícito",
    # Trafficking — compound phrases (specific)
    "trata de personas",
    "trata de niños",
    "trata de blancas",
    "tráfico de personas",
    "trafico de personas",
    "tráfico de órganos",
    "trafico de organos",
    "tráfico de niños",
    "trafico de ninos",
    "tráfico infantil",
    "trafico infantil",
    "tráfico humano",
    "trafico humano",
    # Exploitation
    "explotación sexual",
    "explotacion sexual",
    "trabajo forzado",
    "trabajo infantil",
    "esclavitud moderna",
    # English trafficking equivalents
    "smuggling",
    "trafficking",
    "modern slavery",
    "forced labour",
    "forced labor",
    "child labour",
    "child labor",
    "sexual exploitation",
    # Governance / rule of law
    "justicia",
    "gobernanza",
    "transparencia",
    "transparency",     # English — catches grants.gov titles
    "governance",       # English — catches grants.gov titles
    "integrity",        # English
    "integridad",
    "cumplimiento",
    "crimen organizado",
    "crimen",
    "narcotrafico",
    "narcotráfico",
    "financiamiento ilicito",
    "financiamiento ilícito",
    "fortalecimiento institucional",
    "estado de derecho",
]


@dataclass
class Config:
    """Validated configuration for scraper runtime and integrations."""

    devex_email: str
    devex_password: str
    devex_session_path: str = "./devex_session.json"
    anthropic_api_key: str = ""
    store_type: str = "sheets"
    google_sheets_id: Optional[str] = None
    sheets_tab_name: str = "Opportunities"
    service_account_json: Optional[str] = None
    airtable_api_key: Optional[str] = None
    airtable_base_id: Optional[str] = None
    airtable_table_name: str = "Opportunities"
    sector_keywords: list[str] = None
    target_countries: list[str] = None
    max_results: int = 50
    run_mode: str = "dry_run"
    headless: bool = True
    log_level: str = "INFO"
    notification_email: str = ""
    admin_alert_email: str = ""
    devex_enabled: bool = True
    undp_enabled: bool = True
    iadb_enabled: bool = True
    oecd_enabled: bool = True
    worldbank_enabled: bool = True
    usaid_enabled: bool = True
    samgov_api_key: Optional[str] = None
    samgov_enabled: bool = False
    perplexity_api_key: Optional[str] = None
    perplexity_enabled: bool = False


def _get_required(name: str) -> str:
    """Return required environment value or raise a clear missing-var error."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _parse_csv_env(name: str) -> list[str]:
    """Parse a comma-separated env variable into a cleaned string list."""
    raw = _get_required(name)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError(f"Environment variable {name} must contain at least one value.")
    return values


def _parse_bool_env(name: str, default: bool) -> bool:
    """Parse a boolean env value using true/false semantics."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value for {name}: {raw}")


def _parse_int_env(name: str, default: int) -> int:
    """Parse an integer env value or return default when not set."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer value for {name}: {raw}") from exc


def load_config() -> Config:
    """Load, validate, and parse all runtime configuration from .env."""
    load_dotenv()

    devex_email = _get_required("DEVEX_EMAIL")
    devex_password = _get_required("DEVEX_PASSWORD")
    anthropic_api_key = _get_required("ANTHROPIC_API_KEY")
    store_type = _get_required("STORE_TYPE").lower()
    run_mode = _get_required("RUN_MODE").lower()
    notification_email = _get_required("NOTIFICATION_EMAIL")
    admin_alert_email = _get_required("ADMIN_ALERT_EMAIL")

    if store_type not in {"sheets", "airtable"}:
        raise ValueError("STORE_TYPE must be either 'sheets' or 'airtable'.")
    if run_mode not in {"dry_run", "live"}:
        raise ValueError("RUN_MODE must be either 'dry_run' or 'live'.")

    google_sheets_id = os.getenv("GOOGLE_SHEETS_ID", "").strip() or None
    service_account_json = os.getenv("SERVICE_ACCOUNT_JSON", "").strip() or None
    airtable_api_key = os.getenv("AIRTABLE_API_KEY", "").strip() or None
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip() or None

    if store_type == "sheets":
        if not google_sheets_id:
            raise ValueError("Missing required environment variable for sheets mode: GOOGLE_SHEETS_ID")
        if not service_account_json:
            raise ValueError("Missing required environment variable for sheets mode: SERVICE_ACCOUNT_JSON")
    if store_type == "airtable":
        if not airtable_api_key:
            raise ValueError("Missing required environment variable for airtable mode: AIRTABLE_API_KEY")
        if not airtable_base_id:
            raise ValueError("Missing required environment variable for airtable mode: AIRTABLE_BASE_ID")

    devex_enabled = _parse_bool_env("DEVEX_ENABLED", True)
    undp_enabled = _parse_bool_env("UNDP_ENABLED", True)
    iadb_enabled = _parse_bool_env("IADB_ENABLED", True)
    oecd_enabled = _parse_bool_env("OECD_ENABLED", True)
    worldbank_enabled = _parse_bool_env("WORLDBANK_ENABLED", True)
    usaid_enabled = _parse_bool_env("USAID_ENABLED", True)
    samgov_enabled = _parse_bool_env("SAM_GOV_ENABLED", False)
    samgov_api_key = os.getenv("SAM_GOV_API_KEY", "").strip() or None
    perplexity_enabled = _parse_bool_env("PERPLEXITY_ENABLED", False)
    perplexity_api_key = os.getenv("PERPLEXITY_API_KEY", "").strip() or None

    if samgov_enabled and not samgov_api_key:
        raise ValueError("Missing required environment variable: SAM_GOV_API_KEY")
    if perplexity_enabled and not perplexity_api_key:
        raise ValueError("Missing required environment variable: PERPLEXITY_API_KEY")

    base_sector_keywords = _parse_csv_env("SECTOR_KEYWORDS")
    sector_keywords = base_sector_keywords + [
        kw for kw in _SPANISH_SECTOR_KEYWORDS if kw not in base_sector_keywords
    ]

    return Config(
        devex_email=devex_email,
        devex_password=devex_password,
        devex_session_path=os.getenv("DEVEX_SESSION_PATH", "./devex_session.json").strip() or "./devex_session.json",
        anthropic_api_key=anthropic_api_key,
        store_type=store_type,
        google_sheets_id=google_sheets_id,
        sheets_tab_name=os.getenv("SHEETS_TAB_NAME", "Opportunities").strip() or "Opportunities",
        service_account_json=service_account_json,
        airtable_api_key=airtable_api_key,
        airtable_base_id=airtable_base_id,
        airtable_table_name=os.getenv("AIRTABLE_TABLE_NAME", "Opportunities").strip() or "Opportunities",
        sector_keywords=sector_keywords,
        target_countries=_parse_csv_env("TARGET_COUNTRIES"),
        max_results=_parse_int_env("MAX_RESULTS", 50),
        run_mode=run_mode,
        headless=_parse_bool_env("HEADLESS", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
        notification_email=notification_email,
        admin_alert_email=admin_alert_email,
        devex_enabled=devex_enabled,
        undp_enabled=undp_enabled,
        iadb_enabled=iadb_enabled,
        oecd_enabled=oecd_enabled,
        worldbank_enabled=worldbank_enabled,
        usaid_enabled=usaid_enabled,
        samgov_api_key=samgov_api_key,
        samgov_enabled=samgov_enabled,
        perplexity_api_key=perplexity_api_key,
        perplexity_enabled=perplexity_enabled,
    )
