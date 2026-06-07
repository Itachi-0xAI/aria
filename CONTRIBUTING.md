# Contributing to ARIA

## Local Setup

1. Clone the repo and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Run the dashboard:
   ```bash
   streamlit run aria.py
   ```

3. Open `http://localhost:8501` in your browser.

## Running Tests

```bash
pytest tests/ -v
```

Current: **23 passing tests**. Run tests before submitting PRs.

## Demo Mode

No API keys required. Enable in `config/aria_config.yaml`:
```yaml
demo_mode: true
```

Synthetic data is loaded for all 6 modules and 7 domains. Perfect for testing UI changes or new domain configs.

## Project Structure

The 6 core modules live in `/modules`:
- **DKSM**: Data Knowledge State Monitor
- **LCI**: LLM Context Integrity  
- **PP**: Prompt Plausibility
- **AVL**: Anomaly Validation Layer
- **FLE**: Freshness Label Engine
- **ASGC**: Auto-Stale Groundtruth Classifier

Domain configs are in `/domains` (YAML + CSV; no code changes needed to add a domain).

## Adding a New Domain

1. Create `/domains/your_domain/config.yaml` with freshness rules and detection thresholds.
2. Add a CSV in `/domains/your_domain/groundtruth.csv` with known-good data samples.
3. Restart ARIA. All 6 modules will auto-detect and probe your new domain.

Zero code changes. Zero imports. Pure configuration.

## Guidelines

- **Tests first**: Failing test → green test → code. Write tests for new modules.
- **No cross-module imports**: Use the event bus (`aria/event_bus.py`) to communicate between modules.
- **No secrets in code**: API keys go in environment variables or `config/secrets.yaml` (gitignored).
- **Keep modules isolated**: Changes to one module should not affect others.

## Good First Issues

- Add a new domain config (Healthcare, Finance, etc.) with rules and groundtruth data.
- Add a test case for an existing module (e.g., DKSM staleness edge case).
- Improve module docstrings with examples.

Questions? Open an issue or start a discussion.
