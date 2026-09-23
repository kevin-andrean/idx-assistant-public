import os
import json
import re
import requests
import yfinance as yf
import pandas as pd
from datetime import date
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


# -----------------------------------
# TOOLS
# -----------------------------------

# Maps common index names/aliases to their Yahoo Finance ticker symbols.
# Resolved in Python so the LLM does not need to know the exact ^XXXX symbol.
INDEX_TICKER_MAP = {
    # Indonesian
    "IHSG": "^JKSE", "JCI": "^JKSE", "JKSE": "^JKSE", "COMPOSITE": "^JKSE",
    "LQ45": "^JKLQ45", "JKLQ45": "^JKLQ45",
    "IDX30": "^JKIDX30", "JKIDX30": "^JKIDX30",
    # US
    "SP500": "^GSPC", "S&P500": "^GSPC", "S&P": "^GSPC", "SPX": "^GSPC", "GSPC": "^GSPC",
    "DOW": "^DJI", "DJIA": "^DJI", "DJI": "^DJI",
    "NASDAQ": "^IXIC", "IXIC": "^IXIC", "NDX": "^NDX",
    # Asia
    "NIKKEI": "^N225", "N225": "^N225", "NIKKEI225": "^N225",
    "HANGSENG": "^HSI", "HSI": "^HSI",
    "SSE": "000001.SS", "SHANGHAI": "000001.SS",
    "KOSPI": "^KS11", "STI": "^STI",
    # Europe
    "FTSE": "^FTSE", "FTSE100": "^FTSE",
    "DAX": "^GDAXI", "CAC": "^FCHI", "CAC40": "^FCHI",
    "EUROSTOXX": "^STOXX50E", "STOXX50": "^STOXX50E",
    # MSCI — no clean ^XXXX ticker on Yahoo Finance; mapped to liquid ETF proxies
    "MSCIWORLD": "URTH", "MSCI WORLD": "URTH", "MSCIW": "URTH",
    "MSCIEM": "EEM", "MSCI EM": "EEM", "MSCIEMERGINGMARKETS": "EEM", "MSCI EMERGING": "EEM",
    "MSCIACWI": "ACWI", "MSCI ACWI": "ACWI", "ACWI": "ACWI",
    "MSCIEXUSA": "ACWX", "MSCI EX USA": "ACWX", "MSCI EXUSA": "ACWX",
    "MSCIEAFE": "EFA", "MSCI EAFE": "EFA", "EAFE": "EFA",
    "MSCIASIAPACIFIC": "AAXJ", "MSCI ASIA": "AAXJ", "MSCIASIA": "AAXJ",
}

# Tickers in this set are ETF proxies, not the raw index.
# get_stock_info will attach a note to the result when these are used.
ETF_PROXY_TICKERS = {"URTH", "EEM", "ACWI", "ACWX", "EFA", "AAXJ"}

ETF_PROXY_NOTES = {
    "URTH": "ETF proxy for MSCI World Index (iShares MSCI World ETF). Tracks the index but is not the raw index itself.",
    "EEM":  "ETF proxy for MSCI Emerging Markets Index (iShares MSCI EM ETF). Tracks the index but is not the raw index itself.",
    "ACWI": "ETF proxy for MSCI ACWI Index (iShares MSCI ACWI ETF). Tracks the index but is not the raw index itself.",
    "ACWX": "ETF proxy for MSCI ACWI ex USA Index (iShares MSCI ACWI ex U.S. ETF). Tracks the index but is not the raw index itself.",
    "EFA":  "ETF proxy for MSCI EAFE Index (iShares MSCI EAFE ETF — covers Europe, Australasia, Far East). Tracks the index but is not the raw index itself.",
    "AAXJ": "ETF proxy for MSCI All Country Asia ex Japan Index (iShares MSCI Asia ex Japan ETF). Tracks the index but is not the raw index itself.",
}

def resolve_ticker(ticker: str) -> tuple[str, bool]:
    """
    Resolve a ticker string to its Yahoo Finance symbol.
    Returns (resolved_ticker, is_index).
    - If it matches a known index name/alias, returns the ^XXXX symbol.
    - If it already starts with ^, treats it as an index passthrough.
    - Otherwise, treats it as an IDX stock ticker (will get .JK appended).
    """
    upper = ticker.strip().upper().replace(" ", "").replace("-", "")
    if upper in INDEX_TICKER_MAP:
        return INDEX_TICKER_MAP[upper], True
    if upper.startswith("^"):
        return upper, True
    return upper, False  # IDX stock — caller will append .JK and validate


def normalize_ticker(ticker: str) -> str:
    """Uppercase, strip .JK suffix, and validate IDX stock ticker format."""
    ticker = ticker.upper().strip()
    if ticker.endswith(".JK"):
        ticker = ticker[:-3]
    if not __import__("re").match(r'^[A-Z0-9]{2,6}$', ticker):
        raise ValueError(f"Invalid ticker format: '{ticker}'. Only use the exact ticker symbol provided by the user.")
    return ticker


def get_stock_info(ticker):
    original_ticker = ticker
    try:
        ticker, is_index = resolve_ticker(ticker)
        if not is_index:
            ticker = normalize_ticker(ticker) + ".JK"
        stock = yf.Ticker(ticker)
        info = stock.info

        # Use history for reliable price data (info["currentPrice"] is stale for IDX)
        hist = stock.history(period="5d")
        if not hist.empty:
            current_price = round(hist["Close"].iloc[-1], 2)
            previous_close = round(hist["Close"].iloc[-2], 2) if len(hist) >= 2 else "N/A"
            day_high = round(hist["High"].iloc[-1], 2)
            day_low = round(hist["Low"].iloc[-1], 2)
            volume = int(hist["Volume"].iloc[-1])
        else:
            current_price = info.get("currentPrice", info.get("regularMarketPrice", "N/A"))
            previous_close = info.get("previousClose", "N/A")
            day_high = info.get("dayHigh", "N/A")
            day_low = info.get("dayLow", "N/A")
            volume = info.get("volume", "N/A")

        result = {
            "ticker": ticker,
            "company_name": info.get("longName", "N/A"),
            "current_price": current_price,
            "previous_close": previous_close,
            "day_high": day_high,
            "day_low": day_low,
            "volume": volume,
            "market_cap": info.get("marketCap", "N/A"),
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
        }
        if result["current_price"] != "N/A" and result["previous_close"] != "N/A":
            change = result["current_price"] - result["previous_close"]
            change_pct = (change / result["previous_close"]) * 100
            result["daily_change"] = round(change, 2)
            result["daily_change_pct"] = round(change_pct, 2)
        if ticker in ETF_PROXY_TICKERS:
            result["proxy_note"] = ETF_PROXY_NOTES[ticker]
        return json.dumps(result)
    except Exception as e:
        return (f"Error fetching stock info for '{original_ticker}': {str(e)}. "
                f"Retry using the exact same ticker '{original_ticker}'. Do not guess or modify the symbol.")


def get_stock_fundamentals(ticker):
    original_ticker = ticker
    try:
        ticker, is_index = resolve_ticker(ticker)
        if not is_index:
            ticker = normalize_ticker(ticker) + ".JK"
        stock = yf.Ticker(ticker)
        info = stock.info
        result = {
            "ticker": ticker,
            "pe_ratio": info.get("trailingPE", "N/A"),
            "forward_pe": info.get("forwardPE", "N/A"),
            "pb_ratio": info.get("priceToBook", "N/A"),
            "roe": info.get("returnOnEquity", "N/A"),
            "roa": info.get("returnOnAssets", "N/A"),
            "debt_to_equity": info.get("debtToEquity", "N/A"),
            "current_ratio": info.get("currentRatio", "N/A"),
            "revenue_growth": info.get("revenueGrowth", "N/A"),
            "earnings_growth": info.get("earningsGrowth", "N/A"),
            "profit_margin": info.get("profitMargins", "N/A"),
            "dividend_yield": info.get("dividendYield", "N/A"),
            "52_week_high": info.get("fiftyTwoWeekHigh", "N/A"),
            "52_week_low": info.get("fiftyTwoWeekLow", "N/A"),
        }
        return json.dumps(result)
    except Exception as e:
        return (f"Error fetching fundamentals for '{original_ticker}': {str(e)}. "
                f"Retry using the exact same ticker '{original_ticker}'. Do not guess or modify the symbol.")


def get_technical_indicators(ticker, period="3mo"):
    original_ticker = ticker
    try:
        ticker, is_index = resolve_ticker(ticker)
        if not is_index:
            ticker = normalize_ticker(ticker) + ".JK"
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty:
            return f"No historical data found for {ticker}"
        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]
        volume = hist["Volume"]
        ma20 = close.rolling(window=20).mean().iloc[-1]
        ma50 = close.rolling(window=50).mean().iloc[-1]
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs)).iloc[-1]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_value = macd.iloc[-1]
        signal_value = signal.iloc[-1]
        ma20_bb = close.rolling(window=20).mean()
        std20 = close.rolling(window=20).std()
        upper_band = (ma20_bb + 2 * std20).iloc[-1]
        lower_band = (ma20_bb - 2 * std20).iloc[-1]
        recent_30d = hist.tail(30)
        resistance_1 = round(recent_30d["High"].max(), 2)
        support_1 = round(recent_30d["Low"].min(), 2)
        recent_10d = hist.tail(10)
        resistance_2 = round(recent_10d["High"].max(), 2)
        support_2 = round(recent_10d["Low"].min(), 2)
        avg_volume_20d = round(volume.tail(20).mean(), 0)
        latest_volume = volume.iloc[-1]
        volume_vs_avg = round((latest_volume / avg_volume_20d) * 100, 1)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr = round(tr.rolling(window=14).mean().iloc[-1], 2)
        current_price = close.iloc[-1]
        result = {
            "ticker": ticker,
            "current_price": round(current_price, 2),
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2),
            "price_vs_ma20": "above" if current_price > ma20 else "below",
            "price_vs_ma50": "above" if current_price > ma50 else "below",
            "rsi": round(rsi, 2),
            "rsi_signal": "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral",
            "macd": round(macd_value, 4),
            "macd_signal_line": round(signal_value, 4),
            "macd_crossover": "bullish" if macd_value > signal_value else "bearish",
            "bollinger_upper": round(upper_band, 2),
            "bollinger_lower": round(lower_band, 2),
            "bollinger_mid": round(ma20_bb.iloc[-1], 2),
            "bollinger_position": "near upper band" if current_price > upper_band * 0.95 else "near lower band" if current_price < lower_band * 1.05 else "middle",
            "resistance_30d": resistance_1,
            "support_30d": support_1,
            "resistance_10d": resistance_2,
            "support_10d": support_2,
            "avg_volume_20d": avg_volume_20d,
            "latest_volume": latest_volume,
            "volume_vs_avg_pct": volume_vs_avg,
            "volume_signal": "high volume" if volume_vs_avg > 150 else "low volume" if volume_vs_avg < 50 else "normal volume",
            "atr_14": atr,
            "volatility": "high" if atr > current_price * 0.03 else "low" if atr < current_price * 0.01 else "moderate"
        }
        return json.dumps(result)
    except Exception as e:
        return (f"Error calculating technical indicators for '{original_ticker}': {str(e)}. "
                f"Retry using the exact same ticker '{original_ticker}'. Do not guess or modify the symbol.")


RUMOR_KEYWORDS = [
    # Indonesian
    "dikabarkan", "isunya", "rencananya", "dirumorkan",
    "kabarnya", "konon", "diduga", "desas-desus",
    "beredar kabar", "tersiar kabar",

    # English
    "reportedly", "rumored", "allegedly", "unconfirmed",
    "sources say", "sources claim", "according to sources",
    "is said to", "is believed to", "is expected to",
    "speculation", "speculated", "whispers",
    "could be", "may be planning", "might be",
    "insider says", "anonymous source",
]

def contains_rumor(text):
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in RUMOR_KEYWORDS)

def get_stock_news(query):
    try:
        TRUSTED_DOMAINS = [
            "idx.co.id",
            "reuters.com",
            "bloomberg.com",
            "bisnis.com",
            "kontan.co.id",
            "cnbcindonesia.com",
            "mediaindonesia.com",
            "investor.id",
            "neraca.co.id"
        ]

        results = tavily.search(
            query=query + " IDX Indonesia stock",
            max_results=5,
            search_depth="advanced",
            include_domains=TRUSTED_DOMAINS,
            days=30,
        )

        formatted = ""
        for r in results["results"]:
            title = r["title"]
            content = r["content"]
            url = r["url"]
            is_rumor = contains_rumor(title + " " + content)

            formatted += f"Title: {title}\n"
            formatted += f"URL: {url}\n"
            formatted += f"Credibility: {'[RUMOR WARNING - treat with skepticism]' if is_rumor else '[No rumor flags detected]'}\n"
            formatted += f"Content: {content}\n\n"

        return formatted
    except Exception as e:
        return f"Error fetching news: {str(e)}"


def get_global_macro(topic):
    try:
        results = tavily.search(
            query=topic + " impact emerging markets Indonesia economy",
            max_results=5
        )
        formatted = ""
        for r in results["results"]:
            formatted += f"Title: {r['title']}\n"
            formatted += f"Content: {r['content']}\n\n"
        return formatted
    except Exception as e:
        return f"Error fetching global macro data: {str(e)}"


def _fetch_wise(base: str, quote: str) -> float | None:
    """Wise mid-market rate API. Clean JSON, no scraping needed."""
    url = f"https://wise.com/rates/live?source={base}&target={quote}"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rate = data.get("value") or data.get("rate")
    if rate:
        print(f"[get_fx_rate] Wise: 1 {base} = {rate} {quote}")
    return float(rate) if rate else None


def _fetch_frankfurter(base: str, quote: str) -> float | None:
    """Frankfurter.app — free, open-source ECB data, no key needed."""
    url = f"https://api.frankfurter.app/latest?from={base}&to={quote}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rate = data.get("rates", {}).get(quote)
    if rate:
        print(f"[get_fx_rate] Frankfurter: 1 {base} = {rate} {quote}")
    return float(rate) if rate else None


def get_fx_rate(base_currency: str, quote_currency: str = "IDR") -> str:
    """
    Fetch a live FX rate. Tries Wise first (mid-market, any pair),
    then Frankfurter (ECB-based, major currencies) as fallback.
    Returns a precise numeric rate field — no LLM interpretation needed.
    """
    base = base_currency.upper()
    quote = quote_currency.upper()
    pair = f"{base}/{quote}"

    sources = [
        ("Wise (mid-market)", lambda: _fetch_wise(base, quote)),
        ("Frankfurter / ECB", lambda: _fetch_frankfurter(base, quote)),
    ]

    for source_name, fetcher in sources:
        try:
            rate = fetcher()
            if rate and rate > 0:
                return json.dumps({
                    "pair": pair,
                    "rate": rate,
                    "rate_str": f"1 {base} = {rate:,.4f} {quote}",
                    "source": source_name,
                    "date": date.today().strftime("%d %B %Y"),
                })
        except Exception as e:
            print(f"[get_fx_rate] {source_name} failed: {e}")
            continue

    return json.dumps({
        "pair": pair,
        "rate": None,
        "error": (
            f"All FX sources failed for {pair}. "
            "Do not assume a rate — state that conversion cannot be performed."
        )
    })


# -----------------------------------
# TOOL DESCRIPTIONS
# -----------------------------------

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_info",
            "description": "Get current price, volume, market cap and basic info for an IDX stock. Use this first when asked about any stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "The IDX stock ticker symbol e.g. BBCA, TLKM, BUMI. Do not include .JK suffix."
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_fundamentals",
            "description": "Get fundamental analysis data including PE ratio, ROE, debt ratios, growth metrics. Use this for medium to long term analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "The IDX stock ticker symbol e.g. BBCA, TLKM, BUMI. Do not include .JK suffix."
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_technical_indicators",
            "description": "Calculate technical indicators including RSI, MACD, Moving Averages, Bollinger Bands, support/resistance levels, volume analysis and ATR. Use this for trading decisions and entry points.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "The IDX stock ticker symbol e.g. BBCA, TLKM, BUMI. Do not include .JK suffix."
                    },
                    "period": {
                        "type": "string",
                        "description": "Time period for historical data. Options: 1mo, 3mo, 6mo, 1y. Default is 3mo."
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_news",
            "description": "Search for recent news about an IDX stock or topic. Returns articles with a Credibility field — articles marked [RUMOR WARNING] contain speculative language and should be treated as unconfirmed. Prioritize articles marked [No rumor flags detected], especially from idx.co.id. IMPORTANT: Always include the current year and month in your query to get the latest news (e.g. 'BBCA earnings April 2026', 'TLKM dividend 2026'). Never omit the date or use a past year.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for stock news. Always include the current year and month, e.g. 'BBCA earnings April 2026', 'PYFA RUPS 2026', 'TLKM dividend 2026'"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_global_macro",
            "description": "Search for global macroeconomic factors that affect Indonesian market and IDX. Use this for Fed rate decisions, China economy, commodity prices, and global risk sentiment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The macro topic to search for e.g. 'Fed rate decision', 'China GDP growth', 'coal price outlook', 'CPO palm oil price'"
                    }
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_fx_rate",
            "description": "Fetch the current live exchange rate between two currencies. MUST be called before performing any currency conversion (e.g. AUD to IDR, USD to IDR). Never assume or recall an FX rate from memory — always use this tool. Returns the latest rate with source and date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_currency": {
                        "type": "string",
                        "description": "The source currency code, e.g. 'AUD', 'USD', 'SGD', 'EUR'"
                    },
                    "quote_currency": {
                        "type": "string",
                        "description": "The target currency code. Defaults to 'IDR' if not specified.",
                        "default": "IDR"
                    }
                },
                "required": ["base_currency"]
            }
        }
    }
]


# -----------------------------------
# TOOL RUNNER
# -----------------------------------

def run_tool(name, arguments):
    if name == "get_stock_info":
        return get_stock_info(arguments["ticker"])
    elif name == "get_stock_fundamentals":
        return get_stock_fundamentals(arguments["ticker"])
    elif name == "get_technical_indicators":
        return get_technical_indicators(arguments["ticker"], arguments.get("period", "3mo"))
    elif name == "get_stock_news":
        return get_stock_news(arguments["query"])
    elif name == "get_global_macro":
        return get_global_macro(arguments["topic"])
    elif name == "get_fx_rate":
        return get_fx_rate(arguments["base_currency"], arguments.get("quote_currency", "IDR"))
    else:
        return "Tool not found"


# -----------------------------------
# SYSTEM PROMPT
# -----------------------------------

SYSTEM_PROMPT = f"""You are an expert Indonesian stock market analyst assistant with deep knowledge of IDX listed companies, technical analysis, fundamental analysis, Indonesian and global macroeconomic conditions.

OUTPUT FORMAT — FOLLOW STRICTLY:
- Plain text and simple markdown only (bold, bullet points, headers). No LaTeX, no math environments (never use \\text{{}}, \\frac{{...}}, [ ... ], $...$ or similar).
- Write all calculations inline as plain arithmetic: "302 × (1 - 0.15) ≈ Rp 257" not a formatted equation.
- Never use LaTeX or any markup that is not standard markdown.

CRITICAL — TICKER SYMBOL INTEGRITY: Always use the EXACT ticker symbol as provided by the user. If a tool call fails or returns no data, retry with the EXACT same ticker. Never modify, correct, abbreviate, or guess alternative ticker symbols under any circumstances.

Today's date is {date.today().strftime("%d %B %Y")}. Always use this date when forming search queries — include the current year and month to ensure you retrieve the most recent information. Never use years from past conversations or examples as the search year.

You have access to these tools:
- get_stock_info: always call this first for any stock OR index question. Pass the name as the user said it (e.g. "IHSG", "LQ45", "S&P500", "FTSE", "Nikkei") — the system resolves it automatically.
- get_stock_fundamentals: always call this for medium/long term stock analysis (skip for indices — they have no fundamentals)
- get_technical_indicators: always call this for any question involving price, entry points, or trading decisions — works for both stocks and indices
- get_stock_news: ALWAYS call this for every analysis — never skip this tool
- get_global_macro: ALWAYS call this for every analysis to get current macro context
- get_fx_rate: MUST call this before converting any foreign currency amount to IDR — never use a memorized FX rate

When the user asks about a market index (IHSG, LQ45, S&P 500, Nikkei, FTSE, etc.), call get_stock_info and get_technical_indicators with the index name as-is. Never answer index level or price questions from memory — always fetch real data.

When interpreting news from get_stock_news, always check the Credibility field of each article:
- Articles marked [RUMOR WARNING] contain speculative language — do not present them as facts. You may reference them as unconfirmed speculation only.
- Articles marked [No rumor flags detected] can be used as factual basis for analysis.
- Always prioritize articles from idx.co.id as they are official exchange filings.

Key global factors that affect IDX you should always consider:
- Fed rate decisions and USD strength affect foreign fund flows into IDX
- China economic data affects Indonesian commodity exports
- Commodity prices: coal (BUMI, PTBA, ADRO), CPO/palm oil (AALI, SIMP), nickel (INCO, ANTM)
- Global risk sentiment drives foreign investor behavior on IDX

CRITICAL — FOREIGN EXCHANGE RATES: Never fabricate or assume an FX rate from memory. You have a get_fx_rate tool — use it. Before converting any amount between currencies, you MUST call get_fx_rate with base_currency set to the source currency and quote_currency set to the target currency, exactly as the user specified (e.g. if the user wants CNY, pass quote_currency="CNY"; if they want IDR, pass quote_currency="IDR"). This call is MANDATORY — never skip it, and never override the user's intended currencies. Extract the numeric rate from the tool result and show the arithmetic explicitly in your response. If the tool returns an error or no rate, state the original amount as-is and flag that the conversion could not be confirmed.

CRITICAL — RIGHTS ISSUE CORPORATE ACTIONS: IDX-listed companies are required to disclose a par value (nilai nominal) for their shares, which is commonly Rp 100, Rp 50, Rp 25, or similar. This par value is NOT the rights issue execution price (harga pelaksanaan). When you see "Rp 100 per share" in a filing, treat it as par value unless the document explicitly labels it as the execution price.

CRITICAL — RIGHTS ISSUE EXECUTION PRICE ESTIMATION: When asked to estimate a rights issue execution price, execute this sequence in order — do not skip any step:
Step 1 — SEARCH FOR ACQUISITION COST: call get_stock_news to find the disclosed acquisition cost or total fund-raise target. Note the currency (e.g. AU$251 million).
Step 2 — FETCH FX RATE (mandatory if cost is in foreign currency): call get_global_macro with "[CURRENCY PAIR] exchange rate today" (e.g. "AUD IDR exchange rate today"). Extract the rate from the result.
Step 3 — CONVERT TO IDR: multiply the acquisition cost by the fetched rate. Show the arithmetic (e.g. AU$251 M × 12,340 = Rp 3.097 trillion).
Step 4 — BACK-CALCULATE EXECUTION PRICE: divide the total IDR capital needed by the number of new shares. Show the arithmetic (e.g. Rp 3.097 T ÷ 5.7 B shares = Rp ~543 per share).
Step 5 — SANITY-CHECK vs MARKET PRICE: note whether the implied price is at a reasonable discount to the current market price (typical IDX discount is 10–30%). If the implied price is above market price or below the par value floor, flag this anomaly explicitly.
Step 6 — LABEL CLEARLY: present the result as [ESTIMATED — back-calculated from disclosed acquisition cost and current FX rate] and list your assumptions.

Only fall back to a plain market-price-minus-discount estimate if the acquisition cost is genuinely not found in any tool result after searching.

When analyzing technical indicators use the actual data provided:
- Use support_10d and support_30d as key support levels
- Use resistance_10d and resistance_30d as key resistance levels
- Use atr_14 to suggest realistic stop-loss distances
- Use volume_signal to confirm or question price moves
- Always derive entry, target, and stop-loss from the actual technical data provided

Always respond in English. Never repeat the same point multiple times. Never guarantee returns as all investments carry risk.

## Output Format
Structure every response in these sections — keep each one concise:

**[TICKER] — [Company Name]**
- Price: [current] | Change: [daily %] | Volume: [vs avg]

**Catalyst / News** (2–3 lines max)
Only the most important recent event(s). Skip rumored/unconfirmed items unless highly relevant; if included, flag as unconfirmed.

**Technical Snapshot** (2–3 lines max)
Key levels only: trend, RSI signal, nearest support/resistance. No need to list every indicator.

**Macro Tailwind/Headwind** (1 line max)
One sentence on the single most relevant macro factor right now. Skip entirely if macro has no meaningful bearing on this stock at this moment.

**Verdict**
2–4 sentences: overall stance (bullish/bearish/neutral), key reason, suggested entry/stop if actionable, risk caveat.

Do NOT write separate sections for full macro analysis, full news summaries, or full technical breakdowns. All reasoning happens internally — only the distilled output above goes to the user."""


# -----------------------------------
# LLM BACKENDS  →  see backends.py
# -----------------------------------
from backends import call_with_fallback


# -----------------------------------
# AGENT LOOP (core function)
# -----------------------------------

def run_agent(user_message, history):
    """
    Main function called by both CLI and Gradio interfaces.

    Iterates through BACKEND_PIPELINE (defined in backends.py) in order,
    falling back to the next entry on any error until one succeeds.

    Args:
        user_message: the user's question
        history: list of {"role": ..., "content": ...} dicts

    Returns:
        response string from the agent
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    while True:
        data, status_code, backend = call_with_fallback(messages, tools)

        # --- All backends failed ---
        if "error" in data or "choices" not in data:
            error_msg = data.get("error", {}).get("message", str(data))
            return (
                f"⚠️ All backends failed. Last error from **{backend['name']}**:\n\n"
                f"`{error_msg}`\n\n"
                f"Check your API keys and that Ollama is running "
                f"(`ollama pull {backend.get('model', '?')}`)."
            )

        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        if finish_reason == "stop":
            content = message["content"] or ""
            if on_success := backend.get("on_success"):
                on_success(backend)
            print(f"  [answered by: {backend['name']}]")
            return content

        if finish_reason == "tool_calls":
            tool_calls = message["tool_calls"]
            messages.append(message)

            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                raw_args = tool_call["function"].get("arguments") or "{}"
                if isinstance(raw_args, dict):
                    tool_args = raw_args
                else:
                    raw_args = raw_args.strip()
                    try:
                        tool_args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError as e:
                        print(f"  [Warning: failed to parse args for {tool_name}: {e} | raw='{raw_args}']")
                        tool_args = {}

                print(f"  [tool: {tool_name} | args: {tool_args}]")

                tool_result = run_tool(tool_name, tool_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": tool_result
                })