import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import yfinance as yf
from openai import OpenAI

DB_PATH = "portfolio.db"
CURRENCIES = ["USD", "EUR", "CZK", "GBP"]


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
            purchase_currency TEXT NOT NULL DEFAULT 'USD',
            purchase_date TEXT,
            purchase_fx_to_usd REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute("PRAGMA table_info(portfolio)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "purchase_currency" not in existing_cols:
        cur.execute("ALTER TABLE portfolio ADD COLUMN purchase_currency TEXT NOT NULL DEFAULT 'USD'")
    if "purchase_date" not in existing_cols:
        cur.execute("ALTER TABLE portfolio ADD COLUMN purchase_date TEXT")
    if "purchase_fx_to_usd" not in existing_cols:
        cur.execute("ALTER TABLE portfolio ADD COLUMN purchase_fx_to_usd REAL")
    conn.commit()
    conn.close()


def add_position(ticker, shares, purchase_price, purchase_currency, purchase_date, purchase_fx_to_usd):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO portfolio (
            ticker, shares, purchase_price, purchase_currency, purchase_date, purchase_fx_to_usd, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker.upper(),
            shares,
            purchase_price,
            purchase_currency,
            purchase_date,
            purchase_fx_to_usd,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_portfolio():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT ticker, shares, purchase_price, purchase_currency, purchase_date, purchase_fx_to_usd, created_at
        FROM portfolio
        """,
        conn,
    )
    conn.close()
    return df


def to_float(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    cleaned = (
        text.replace(",", "")
        .replace("£", "")
        .replace("$", "")
        .replace("€", "")
        .replace("Kč", "")
    )
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_trading212_transactions(csv_df):
    normalized = {col.strip().lower(): col for col in csv_df.columns}

    required_columns = [
        "action",
        "ticker",
        "name",
        "no. of shares",
        "price / share",
        "currency",
        "total",
        "withholding tax",
        "currency conversion",
    ]
    for col in required_columns:
        if col not in normalized:
            csv_df[col] = None
            normalized[col] = col

    records = {}
    summary = {
        "realized_gains": 0.0,
        "dividend_income": 0.0,
        "lending_income": 0.0,
        "interest_on_cash": 0.0,
        "total_deposits": 0.0,
        "withholding_tax": 0.0,
    }

    def amount_from_row(row):
        converted = to_float(row[normalized["currency conversion"]])
        if converted != 0:
            return converted
        return to_float(row[normalized["total"]])

    for _, row in csv_df.iterrows():
        action = str(row[normalized["action"]]).strip().lower()
        ticker = str(row[normalized["ticker"]]).strip().upper()
        name = str(row[normalized["name"]]).strip()
        shares = to_float(row[normalized["no. of shares"]])
        price = to_float(row[normalized["price / share"]])
        currency = str(row[normalized["currency"]]).strip().upper() or "USD"
        amount = amount_from_row(row)
        withholding_tax = to_float(row[normalized["withholding tax"]])

        if withholding_tax:
            summary["withholding_tax"] += abs(withholding_tax)

        if action == "market buy":
            if not ticker or shares <= 0:
                continue
            if ticker not in records:
                records[ticker] = {
                    "ticker": ticker,
                    "name": name,
                    "shares": 0.0,
                    "cost_basis": 0.0,
                    "avg_cost": 0.0,
                    "currency": currency,
                    "realized_gains": 0.0,
                }
            buy_cost = abs(amount) if amount != 0 else shares * price
            records[ticker]["cost_basis"] += buy_cost
            records[ticker]["shares"] += shares
            if records[ticker]["shares"] > 0:
                records[ticker]["avg_cost"] = records[ticker]["cost_basis"] / records[ticker]["shares"]
        elif action == "market sell":
            if not ticker or shares <= 0:
                continue
            if ticker not in records:
                records[ticker] = {
                    "ticker": ticker,
                    "name": name,
                    "shares": 0.0,
                    "cost_basis": 0.0,
                    "avg_cost": 0.0,
                    "currency": currency,
                    "realized_gains": 0.0,
                }
            held_shares = records[ticker]["shares"]
            sell_shares = min(shares, held_shares) if held_shares > 0 else shares
            proceeds = abs(amount) if amount != 0 else sell_shares * price
            avg_cost = records[ticker]["avg_cost"]
            cost_of_sold = avg_cost * sell_shares
            realized = proceeds - cost_of_sold
            records[ticker]["realized_gains"] += realized
            summary["realized_gains"] += realized
            records[ticker]["shares"] = max(held_shares - sell_shares, 0.0)
            records[ticker]["cost_basis"] = max(records[ticker]["cost_basis"] - cost_of_sold, 0.0)
            if records[ticker]["shares"] > 0:
                records[ticker]["avg_cost"] = records[ticker]["cost_basis"] / records[ticker]["shares"]
            else:
                records[ticker]["avg_cost"] = 0.0
        elif action == "dividend":
            summary["dividend_income"] += amount
        elif action == "lending interest":
            summary["lending_income"] += amount
        elif action == "interest on cash":
            summary["interest_on_cash"] += amount
        elif action == "deposit":
            summary["total_deposits"] += amount
        else:
            continue

    holdings = []
    for rec in records.values():
        if rec["shares"] > 0:
            holdings.append(
                {
                    "ticker": rec["ticker"],
                    "name": rec["name"],
                    "shares": rec["shares"],
                    "purchase_price": rec["avg_cost"],
                    "purchase_currency": rec["currency"] if rec["currency"] in CURRENCIES else "USD",
                    "purchase_date": None,
                    "purchase_fx_to_usd": 1.0,
                    "cost_basis_local": rec["cost_basis"],
                }
            )

    holdings_df = pd.DataFrame(holdings)
    return holdings_df, summary


def fetch_fx_rate(base_currency, quote_currency, at_date=None):
    if base_currency == quote_currency:
        return 1.0

    def _direct_rate(src, dst):
        pair = f"{src}{dst}=X"
        fx_ticker = yf.Ticker(pair)

        if at_date:
            start_date = datetime.fromisoformat(at_date)
            end_date = start_date + timedelta(days=7)
            hist = fx_ticker.history(
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
            )
            if not hist.empty:
                return float(hist["Close"].iloc[0])

        recent = fx_ticker.history(period="5d")
        if not recent.empty:
            return float(recent["Close"].iloc[-1])
        return None

    direct = _direct_rate(base_currency, quote_currency)
    if direct is not None:
        return direct

    # Fallback bridge through USD for pairs that may not exist directly.
    to_usd = _direct_rate(base_currency, "USD")
    usd_to_target = _direct_rate("USD", quote_currency)
    if to_usd is not None and usd_to_target is not None:
        return to_usd * usd_to_target

    return None


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
        "stock_currency": info.get("currency") or "USD",
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


def format_currency(value, currency):
    symbols = {"USD": "$", "EUR": "€", "CZK": "Kč", "GBP": "£"}
    if value is None:
        return "N/A"
    return f"{symbols.get(currency, '')}{value:,.2f} {currency}"


def main():
    st.set_page_config(page_title="Portfolio Analyzer", layout="wide")
    st.title("📊 Personal Stock Portfolio Analyzer")

    init_db()

    st.subheader("Import Trading212 CSV")
    uploaded_csv = st.file_uploader("Upload Trading212 transaction export", type=["csv"])
    imported_holdings_df = None
    imported_summary = None
    if uploaded_csv is not None:
        try:
            csv_df = pd.read_csv(uploaded_csv)
            imported_holdings_df, imported_summary = parse_trading212_transactions(csv_df)
            st.success("Trading212 CSV parsed successfully. Portfolio rebuilt from transactions.")
        except Exception as exc:
            st.error(f"Failed to parse CSV: {exc}")

    st.sidebar.header("Add Portfolio Position")
    ticker_input = st.sidebar.text_input("Ticker", placeholder="AAPL")
    shares_input = st.sidebar.number_input("Shares", min_value=0.0, value=1.0, step=1.0)
    purchase_price_input = st.sidebar.number_input("Purchase Price", min_value=0.0, value=0.0, step=1.0)
    purchase_currency_input = st.sidebar.selectbox("Purchase Currency", CURRENCIES, index=0)
    purchase_date_input = st.sidebar.date_input("Purchase Date")

    if st.sidebar.button("Add Position"):
        if ticker_input.strip():
            purchase_date_str = purchase_date_input.strftime("%Y-%m-%d")
            hist_fx = fetch_fx_rate(purchase_currency_input, "USD", at_date=purchase_date_str)
            if hist_fx is None:
                st.sidebar.error("Could not fetch historical FX rate for purchase date.")
            else:
                add_position(
                    ticker_input.strip(),
                    shares_input,
                    purchase_price_input,
                    purchase_currency_input,
                    purchase_date_str,
                    hist_fx,
                )
                st.sidebar.success(
                    f"Added {ticker_input.upper()} with historical FX ({purchase_currency_input}->USD): {hist_fx:.4f}"
                )
        else:
            st.sidebar.error("Please enter a ticker")

    if imported_holdings_df is not None:
        portfolio_df = imported_holdings_df.copy()
    else:
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
                    "stock_currency": "USD",
                }
                news_by_ticker[ticker] = []

    display_currency = st.selectbox("Display Currency", CURRENCIES, index=0)
    usd_to_display = fetch_fx_rate("USD", display_currency)
    if usd_to_display is None:
        st.warning("Could not fetch current FX for display currency. Falling back to USD.")
        display_currency = "USD"
        usd_to_display = 1.0

    portfolio_df["current_price"] = portfolio_df["ticker"].map(
        lambda t: snapshots.get(t, {}).get("current_price")
    )
    portfolio_df["stock_currency"] = portfolio_df["ticker"].map(
        lambda t: snapshots.get(t, {}).get("stock_currency", "USD")
    )

    portfolio_df["original_cost_local"] = portfolio_df["shares"] * portfolio_df["purchase_price"].fillna(0)
    portfolio_df["original_cost_usd"] = (
        portfolio_df["original_cost_local"] * portfolio_df["purchase_fx_to_usd"].fillna(1.0)
    )
    portfolio_df["original_cost_display"] = portfolio_df["original_cost_usd"] * usd_to_display

    portfolio_df["stock_to_usd_now"] = portfolio_df["stock_currency"].map(
        lambda c: fetch_fx_rate(c, "USD") or 1.0
    )
    portfolio_df["current_value_usd"] = (
        portfolio_df["shares"] * portfolio_df["current_price"].fillna(0) * portfolio_df["stock_to_usd_now"]
    )
    portfolio_df["current_value_display"] = portfolio_df["current_value_usd"] * usd_to_display
    portfolio_df["unrealized_gain_loss"] = (
        portfolio_df["current_value_display"] - portfolio_df["original_cost_display"]
    )
    portfolio_df["unrealized_gain_loss_pct"] = portfolio_df["unrealized_gain_loss"] / portfolio_df[
        "original_cost_display"
    ].replace(0, pd.NA)

    if imported_summary is not None:
        st.subheader("Current Holdings")
    else:
        st.subheader("Portfolio Table")
    table_cols = [
        "ticker",
        "shares",
        "purchase_price",
        "purchase_currency",
        "purchase_date",
        "current_price",
        "stock_currency",
        "original_cost_display",
        "current_value_display",
        "unrealized_gain_loss",
        "unrealized_gain_loss_pct",
    ]
    st.dataframe(portfolio_df[table_cols], use_container_width=True)

    if imported_summary is not None:
        st.subheader("Realized gains")
        st.metric("Realized Gains", format_currency(imported_summary["realized_gains"], display_currency))

        st.subheader("Dividend income")
        st.metric("Dividend Income", format_currency(imported_summary["dividend_income"], display_currency))

        st.subheader("Lending income")
        st.metric("Lending Income", format_currency(imported_summary["lending_income"], display_currency))

        st.subheader("Total deposits")
        st.metric("Total Deposits", format_currency(imported_summary["total_deposits"], display_currency))

        st.subheader("Tax summary")
        st.metric("Tax Summary (Withholding)", format_currency(imported_summary["withholding_tax"], display_currency))

        st.subheader("Additional cash interest")
        st.metric("Interest on Cash", format_currency(imported_summary["interest_on_cash"], display_currency))

    total_original = portfolio_df["original_cost_display"].sum()
    total_current = portfolio_df["current_value_display"].sum()
    total_gain = total_current - total_original
    total_gain_pct = (total_gain / total_original) if total_original else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Original Invested Value", format_currency(total_original, display_currency))
    k2.metric("Current Value", format_currency(total_current, display_currency))
    k3.metric("Unrealized Gain/Loss", format_currency(total_gain, display_currency))
    k4.metric("Unrealized Gain/Loss %", f"{total_gain_pct * 100:.2f}%")

    st.subheader("Allocation Chart")
    alloc = portfolio_df.groupby("ticker", as_index=False)["current_value_display"].sum()
    if alloc["current_value_display"].sum() > 0:
        st.plotly_chart(
            {
                "data": [
                    {
                        "labels": alloc["ticker"],
                        "values": alloc["current_value_display"],
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
