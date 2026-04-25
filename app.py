import os
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
import yfinance as yf
from openai import OpenAI

DB_PATH = "portfolio.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            shares REAL NOT NULL,
            purchase_price REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def add_position(ticker, shares, purchase_price):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO portfolio (ticker, shares, purchase_price, created_at) VALUES (?, ?, ?, ?)",
        (ticker.upper(), shares, purchase_price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_portfolio():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT ticker, shares, purchase_price, created_at FROM portfolio", conn)
    conn.close()
    return df


def fetch_stock_snapshot(ticker):
    stock = yf.Ticker(ticker)
    info = stock.info or {}

    rec = "N/A"
    if info.get("recommendationKey"):
        rec = info.get("recommendationKey")

    snapshot = {
        "ticker": ticker.upper(),
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE"),
        "revenue_growth": info.get("revenueGrowth"),
        "profit_margins": info.get("profitMargins"),
        "debt_to_equity": info.get("debtToEquity"),
        "analyst_recommendation": rec,
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
    }

    news_items = []
    for item in stock.news[:5]:
        title = item.get("title")
        link = item.get("link")
        if title and link:
            news_items.append({"title": title, "link": link})

    return snapshot, news_items


def summarize_with_openai(snapshot):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "Set OPENAI_API_KEY in your environment to generate AI analysis."

    client = OpenAI(api_key=api_key)

    prompt = f"""
You are an investment research assistant.
Given the stock data below, provide a concise analysis with these headings:
1) Bull Thesis
2) Bear Thesis
3) Valuation Concerns
4) Rating (Buy/Hold/Sell)

Stock data:
{snapshot}

Keep it short and beginner-friendly. Do not provide financial advice disclaimers.
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            temperature=0.4,
        )
        return response.output_text
    except Exception as exc:
        return f"OpenAI request failed: {exc}"


def format_number(value, pct=False):
    if value is None:
        return "N/A"
    if pct:
        return f"{value * 100:.2f}%"
    if isinstance(value, (int, float)):
        if abs(value) >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        if abs(value) >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        return f"{value:.2f}"
    return str(value)


def main():
    st.set_page_config(page_title="Portfolio Analyzer", layout="wide")
    st.title("📊 Personal Stock Portfolio Analyzer")

    init_db()

    st.sidebar.header("Add Portfolio Position")
    ticker_input = st.sidebar.text_input("Ticker", placeholder="AAPL")
    shares_input = st.sidebar.number_input("Shares", min_value=0.0, value=1.0, step=1.0)
    purchase_price_input = st.sidebar.number_input("Purchase Price", min_value=0.0, value=0.0, step=1.0)

    if st.sidebar.button("Add Position"):
        if ticker_input.strip():
            add_position(ticker_input.strip(), shares_input, purchase_price_input)
            st.sidebar.success(f"Added {ticker_input.upper()} to portfolio")
        else:
            st.sidebar.error("Please enter a ticker")

    portfolio_df = get_portfolio()

    if portfolio_df.empty:
        st.info("No positions yet. Add a stock from the sidebar.")
        return

    tickers = sorted(portfolio_df["ticker"].unique().tolist())

    snapshots = {}
    news_by_ticker = {}

    with st.spinner("Fetching market data..."):
        for ticker in tickers:
            try:
                snap, news = fetch_stock_snapshot(ticker)
                snapshots[ticker] = snap
                news_by_ticker[ticker] = news
            except Exception:
                snapshots[ticker] = {
                    "ticker": ticker,
                    "market_cap": None,
                    "pe_ratio": None,
                    "revenue_growth": None,
                    "profit_margins": None,
                    "debt_to_equity": None,
                    "analyst_recommendation": "N/A",
                    "current_price": None,
                }
                news_by_ticker[ticker] = []

    portfolio_df["current_price"] = portfolio_df["ticker"].map(
        lambda t: snapshots.get(t, {}).get("current_price")
    )
    portfolio_df["position_value"] = portfolio_df["shares"] * portfolio_df["current_price"].fillna(0)

    st.subheader("Portfolio Table")
    st.dataframe(portfolio_df, use_container_width=True)

    st.subheader("Allocation Chart")
    alloc = portfolio_df.groupby("ticker", as_index=False)["position_value"].sum()
    if alloc["position_value"].sum() > 0:
        st.plotly_chart(
            {
                "data": [
                    {
                        "labels": alloc["ticker"],
                        "values": alloc["position_value"],
                        "type": "pie",
                    }
                ],
                "layout": {"margin": {"l": 20, "r": 20, "t": 20, "b": 20}},
            },
            use_container_width=True,
        )
    else:
        st.write("Not enough live pricing data to build allocation chart yet.")

    st.subheader("Stock Detail Cards")
    selected = st.selectbox("Pick a stock", tickers)
    s = snapshots[selected]

    c1, c2, c3 = st.columns(3)
    c1.metric("Market Cap", format_number(s["market_cap"]))
    c2.metric("P/E Ratio", format_number(s["pe_ratio"]))
    c3.metric("Debt to Equity", format_number(s["debt_to_equity"]))

    c4, c5, c6 = st.columns(3)
    c4.metric("Revenue Growth", format_number(s["revenue_growth"], pct=True))
    c5.metric("Profit Margins", format_number(s["profit_margins"], pct=True))
    c6.metric("Analyst Recommendation", str(s["analyst_recommendation"]).upper())

    st.subheader(f"Recent News: {selected}")
    if news_by_ticker[selected]:
        for item in news_by_ticker[selected]:
            st.markdown(f"- [{item['title']}]({item['link']})")
    else:
        st.write("No recent headlines found.")

    st.subheader("AI Investment Analysis")
    if st.button(f"Generate analysis for {selected}"):
        with st.spinner("Calling OpenAI..."):
            ai_text = summarize_with_openai(s)
        st.markdown(ai_text)


if __name__ == "__main__":
    main()
