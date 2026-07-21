import os
import asyncio
import requests
import yfinance as yf
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

_CIK_CACHE = {}

def get_price(ticker: str):
    data = yf.Ticker(ticker).history(period="1d")
    if data.empty:
        return None
    return round(data['Close'].iloc[-1], 2)

def get_pct_change(ticker: str):
    data = yf.Ticker(ticker).history(period="2d")
    if len(data) < 2:
        return None
    prev_close = data['Close'].iloc[-2]
    last_close = data['Close'].iloc[-1]
    return round((last_close - prev_close) / prev_close * 100, 2)

def get_news(ticker: str, limit: int = 3):
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

def get_latest_filing(ticker: str):
    cik = get_cik(ticker)
    if not cik:
        return None
    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": "trader-bot contact@example.com"},
            timeout=10,
        )
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        if not forms:
            return None
        return {"form": forms[0], "date": dates[0]}
    except Exception:
        return None

def generate_report(ticker: str) -> str:
    price = get_price(ticker)
    pct = get_pct_change(ticker)
    news = get_news(ticker)
    filing = get_latest_filing(ticker)

    if price is None:
        return f"Couldn't find any data for {ticker}. Double check the ticker symbol."

    data_summary = f"Ticker: {ticker}\n"
    data_summary += f"Current price: ${price}\n"
    data_summary += f"Today's move: {pct}%\n" if pct is not None else "Today's move: unavailable\n"
    if filing:
        data_summary += f"Most recent SEC filing: {filing['form']} filed on {filing['date']}\n"
    else:
        data_summary += "Most recent SEC filing: none found\n"
    if news:
        data_summary += "Recent headlines:\n" + "\n".join(f"- {h}" for h in news)
    else:
        data_summary += "Recent headlines: none found"

    prompt = (
        "You are a sharp, experienced stock trader giving a quick verbal briefing to a colleague. "
        "Using ONLY the data below, write a natural, conversational briefing on this stock in 4-6 sentences. "
        "Sound like a trader talking, not a report. Mention the price action, the most recent SEC filing if there is one, "
        "and weave in the news naturally. Do not invent any facts not in the data. "
        "If data is missing, just don't mention it, don't apologize for it.\n\n"
        f"{data_summary}"
    )

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "VKC Trader Bot\n\n"
        "Send /report TICKER and I'll pull the price action, latest SEC filing, and recent news, "
        "then give you a trader-style briefing.\n\n"
        "Example: /report EDBL"
    )

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /report TICKER")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"Pulling the latest on {ticker}...")
    text = await asyncio.to_thread(generate_report, ticker)
    await update.message.reply_text(text)

app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("report", report))

app.run_polling()