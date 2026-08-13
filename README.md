<!-- Setup instructions and project overview for the GovRisk Devex scraper. -->
# GovRisk Devex Scraper

Stubbed project scaffold based on `SCRAPER_SPEC.md`.

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Install Playwright browser binaries:
   - `playwright install`
4. Copy `.env.example` to `.env` and fill in required values.
5. Run in stub mode:
   - `python main.py`

## Project Structure

- `main.py` — orchestrator sequence
- `models.py` — shared dataclasses
- `config.py` — env loading and validation
- `auth/` — authentication
- `scraper/` — search, parse, and filtering
- `llm/` — interpretation and validation
- `store/` — output adapters
- `utils/` — audit and notifications
- `tests/` — placeholder test modules
