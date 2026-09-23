# IDX Market Assistant

An AI-powered assistant for Indonesian stock market (IDX) analysis. Combines an LLM backend (OpenRouter, with an optional local Ollama fallback), Tavily web search, and yfinance market data, served through a Gradio UI.

This repo also includes `idx_monitor.py`, a separate monitoring script for IDX announcements and suspension tracking, which sends alerts via Telegram.

## Requirements

- Python 3.11
- pip
- (Optional) [Ollama](https://ollama.com/) installed locally, if you want the local LLM fallback instead of relying solely on OpenRouter

## Setup on a new machine

1. **Clone the repo**
   ```
   git clone <repo-url>
   cd idx-assistant
   ```

2. **Create a virtual environment**
   ```
   py -m venv env
   ```

3. **Activate it**
   - Windows: `env\Scripts\activate`
   - macOS/Linux: `source env/bin/activate`

4. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

5. **Create a `.env` file** in the project root with the following keys:
   ```
   OPENROUTER_API_KEY=your_key_here
   TAVILY_API_KEY=your_key_here
   OLLAMA_MODEL=qwen2.5:7b
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   ```

   | Variable | Used by | Notes |
   |---|---|---|
   | `OPENROUTER_API_KEY` | Assistant | Required — main LLM backend |
   | `TAVILY_API_KEY` | Assistant | Required — web search |
   | `OLLAMA_MODEL` | Assistant | Optional — only needed if using local Ollama fallback (e.g. `qwen2.5:7b`); requires Ollama installed and the model pulled locally |
   | `TELEGRAM_BOT_TOKEN` | `idx_monitor.py` | Required for monitor alerts |
   | `TELEGRAM_CHAT_ID` | `idx_monitor.py` | Required for monitor alerts |

## Running

With the virtual environment activated:

- **Main assistant:**
  ```
  python main.py
  ```
- **IDX monitor** (announcements + suspension tracking):
  ```
  python idx_monitor.py
  ```

On Windows, `run_idx_assistant.bat` and `run_idx_monitor.bat` are provided as shortcuts — they activate `env` and launch the respective script. Edit the hardcoded path at the top of each `.bat` file if your project folder location differs from the original machine.

## Notes

- `idx_monitor.py` keeps state in a `state/` directory (JSON files + lock file for deduplication). Make sure it's writable.
- Suspension tracking relies on `pandas_market_calendars` with the `"XIDX"` calendar for Indonesian trading days.
- `curl_cffi` is used to work around 403 errors from some data sources.
- Recommended `.gitignore` entries: `env/`, `.env`, `state/`, `__pycache__/`
