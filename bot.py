import os
import asyncio
import requests
import yfinance as yf
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import json
import redis as redis_lib

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=30.0)

try:
    redis_client = redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
    redis_client.ping()
except Exception:
    redis_client = None

def cache_get(key):
    if redis_client is None:
        return None
    try:
        val = redis_client.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None

def cache_set(key, value, ttl=180):
    if redis_client is None:
        return
    try:
        redis_client.set(key, json.dumps(value), ex=ttl)
    except Exception:
        pass

async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    if redis_client is None:
        return
    try:
        import datetime as dt
        from zoneinfo import ZoneInfo
        redis_client.set("heartbeat:traderbot", dt.datetime.now(ZoneInfo("UTC")).isoformat(), ex=600)
    except Exception:
        pass

async def startup_notification(context: ContextTypes.DEFAULT_TYPE):
    owner_id = os.environ.get("OWNER_CHAT_ID")
    if not owner_id:
        return
    try:
        await context.bot.send_message(
            chat_id=int(owner_id),
            text="\u2705 traderbot just started up"
        )
    except Exception:
        pass

_CIK_CACHE = {}

def get_price(ticker: str):
    cache_key = f"price:{ticker}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    data = yf.Ticker(ticker).history(period="1d")
    if data.empty:
        return None
    price = round(data['Close'].iloc[-1], 2)
    cache_set(cache_key, price, ttl=180)
    return price
def get_pct_change(ticker: str):
    cache_key = f"pct:{ticker}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    data = yf.Ticker(ticker).history(period="2d")
    if len(data) < 2:
        return None
    prev_close = data['Close'].iloc[-2]
    last_close = data['Close'].iloc[-1]
    pct = round((last_close - prev_close) / prev_close * 100, 2)
    cache_set(cache_key, pct, ttl=180)
    return pct

def get_news(ticker: str, limit: int = 6):
    cache_key = f"news:{ticker}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    headlines = []
    for item in items[:limit]:
        content = item.get("content", item)
        title = content.get("title") or item.get("title")
        if title:
            headlines.append(title)
    cache_set(cache_key, headlines, ttl=300)
    return headlines

def get_cik(ticker: str):
    global _CIK_CACHE
    if not _CIK_CACHE:
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": "trader-bot contact@example.com"},
                timeout=10,
            )
            data = resp.json()
            for entry in data.values():
                _CIK_CACHE[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)
        except Exception:
            return None
    return _CIK_CACHE.get(ticker.upper())

def get_recent_filings(ticker: str, limit: int = 5):
    cache_key = f"filings:{ticker}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    cik = get_cik(ticker)
    if not cik:
        return []
    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": "trader-bot contact@example.com"},
            timeout=10,
        )
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        filings = []
        for i in range(min(limit, len(forms))):
            filings.append({"form": forms[i], "date": dates[i]})
        cache_set(cache_key, filings, ttl=1800)
        return filings
    except Exception:
        return []

def generate_report(ticker: str) -> str:
    price = get_price(ticker)
    pct = get_pct_change(ticker)
    news = get_news(ticker, limit=6)
    filings = get_recent_filings(ticker, limit=5)

    if price is None:
        return f"Couldn't find any data for {ticker}. Double check the ticker symbol."

    data_summary = f"Ticker: {ticker}\n"
    data_summary += f"Current price: ${price}\n"
    data_summary += f"Today's move: {pct}%\n" if pct is not None else "Today's move: unavailable\n"
    if filings:
        data_summary += "Recent SEC filings (most recent first):\n"
        data_summary += "\n".join(f"- {f['form']} filed {f['date']}" for f in filings) + "\n"
    else:
        data_summary += "Recent SEC filings: none found\n"
    if news:
        data_summary += "Recent headlines:\n" + "\n".join(f"- {h}" for h in news)
    else:
        data_summary += "Recent headlines: none found"

    prompt = (
        "You are a sharp, experienced stock trader writing a full briefing on this stock for a colleague at your desk. "
        "Using ONLY the data below, write it out the way a real trader would \u2014 in your own words, natural and direct, "
        "no corporate report formatting, no headers, no bullet points, no fixed structure. "
        "Cover what actually matters here: the price action and what it means, the most recent SEC filing and why it's relevant "
        "if it is, and how the recent news fits into the picture. Give your honest read on what's going on with this name, "
        "not just a recitation of facts. Write as much as the situation actually warrants \u2014 don't pad it, but don't cut it short either. "
        "Do not invent any facts not in the data below. If something is missing, just don't mention it, don't apologize for it.\n\n"
        f"{data_summary}"
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return f"Report generation failed for {ticker}: {e}"

    for block in message.content:
        if block.type == "text":
            return block.text
    return "Something went wrong generating this report — try again."

def generate_comparison(ticker1: str, ticker2: str) -> str:
    def gather(t):
        price = get_price(t)
        pct = get_pct_change(t)
        news = get_news(t, limit=4)
        filings = get_recent_filings(t, limit=3)
        block = f"--- {t} ---\n"
        if price is None:
            return block + "no data found\n"
        block += f"Price: ${price}\n"
        block += f"Today's move: {pct}%\n" if pct is not None else "Today's move: unavailable\n"
        if filings:
            block += "Recent filings: " + "; ".join(f"{f['form']} ({f['date']})" for f in filings) + "\n"
        else:
            block += "Recent filings: none found\n"
        if news:
            block += "Headlines: " + " | ".join(news) + "\n"
        else:
            block += "Headlines: none found\n"
        return block

    data1 = gather(ticker1)
    data2 = gather(ticker2)

    prompt = (
        "You are a sharp, experienced stock trader giving a direct head-to-head comparison of two stocks for a colleague. "
        "Using ONLY the data below, compare them directly against each other \u2014 which one looks stronger right now and why, "
        "how their price action, filings, and news differ, and your honest read on which one you'd rather be in if you had to "
        "pick just one today. Write naturally, in your own words \u2014 no headers, no bullet points, no fixed structure. "
        "Do not invent any facts not in the data below. If something is missing, don't mention it or apologize for it.\n\n"
        f"{data1}\n{data2}"
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return f"Comparison failed: {e}"

    for block in message.content:
        if block.type == "text":
            return block.text
    return "Something went wrong generating this comparison \u2014 try again."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "VKC Trader Bot\n\n"
        "Send /report TICKER and I'll pull the price action, latest SEC filings, and recent news, "
        "then give you a full trader-style briefing.\n\n"
        "You can also do multiple at once: /report EDBL AAPL TSLA\n\n"
        "/compare TICKER1 TICKER2 - head-to-head comparison of two stocks\n\n"
        "Example: /report EDBL"
    )

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /report TICKER [TICKER2 TICKER3 ...]")
        return
    tickers = [t.upper() for t in context.args]
    if len(tickers) > 1:
        await update.message.reply_text(f"Pulling reports on {', '.join(tickers)}...")
    else:
        await update.message.reply_text(f"Pulling the latest on {tickers[0]}...")
    for ticker in tickers:
        text = await asyncio.to_thread(generate_report, ticker)
        await update.message.reply_text(text)

async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /compare TICKER1 TICKER2")
        return
    ticker1, ticker2 = context.args[0].upper(), context.args[1].upper()
    await update.message.reply_text(f"Comparing {ticker1} vs {ticker2}...")
    text = await asyncio.to_thread(generate_comparison, ticker1, ticker2)
    await update.message.reply_text(text)

app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("report", report))
app.add_handler(CommandHandler("compare", compare))

app.job_queue.run_once(startup_notification, when=3)
app.job_queue.run_repeating(heartbeat_job, interval=120, first=5)

app.run_polling()