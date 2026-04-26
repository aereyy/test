import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import yfinance as yf
from openai import OpenAI

DB_PATH = "portfolio.db"
CURRENCIES = ["USD", "EUR", "CZK", "GBP"]
DEFAULT_TICKER_MAP = {"VUSA": "VUSA.L", "VUAG": "VUAG.L", "HBH": "HBH.DE"}


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
        "currency (total)",
        "exchange rate",
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
        "realized_gains": {},
        "dividend_income": {},
        "lending_income": {},
        "interest_on_cash": {},
        "total_deposits": {},
        "withholding_tax": {},
    }

    def add_summary_amount(key, currency, amount):
        if not currency:
            currency = "USD"
        summary[key][currency] = summary[key].get(currency, 0.0) + amount

    def amount_from_row(row):
        return to_float(row[normalized["total"]])

    for _, row in csv_df.iterrows():
        action = str(row[normalized["action"]]).strip().lower()
        ticker = str(row[normalized["ticker"]]).strip().upper()
        name = str(row[normalized["name"]]).strip()
        shares = to_float(row[normalized["no. of shares"]])
        price = to_float(row[normalized["price / share"]])
        currency_total = str(row[normalized["currency (total)"]]).strip().upper()
        currency = currency_total or str(row[normalized["currency"]]).strip().upper() or "USD"
        amount = amount_from_row(row)
        withholding_tax = to_float(row[normalized["withholding tax"]])

        if withholding_tax:
            add_summary_amount("withholding_tax", currency, abs(withholding_tax))

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
                    "original_currency": currency,
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
                    "original_currency": currency,
                    "realized_gains": 0.0,
                }
            held_shares = records[ticker]["shares"]
            sell_shares = min(shares, held_shares) if held_shares > 0 else shares
            proceeds = abs(amount) if amount != 0 else sell_shares * price
            avg_cost = records[ticker]["avg_cost"]
            cost_of_sold = avg_cost * sell_shares
            realized = proceeds - cost_of_sold
            records[ticker]["realized_gains"] += realized
            add_summary_amount("realized_gains", currency, realized)
            records[ticker]["shares"] = max(held_shares - sell_shares, 0.0)
            records[ticker]["cost_basis"] = max(records[ticker]["cost_basis"] - cost_of_sold, 0.0)
            if records[ticker]["shares"] > 0:
                records[ticker]["avg_cost"] = records[ticker]["cost_basis"] / records[ticker]["shares"]
            else:
                records[ticker]["avg_cost"] = 0.0
        elif "dividend" in action:
            add_summary_amount("dividend_income", currency, amount)
        elif action == "lending interest":
            add_summary_amount("lending_income", currency, amount)
        elif action == "interest on cash":
            add_summary_amount("interest_on_cash", currency, amount)
        elif action == "deposit":
            add_summary_amount("total_deposits", currency, amount)
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
                    "purchase_currency": rec["original_currency"] if rec["original_currency"] in CURRENCIES else "USD",
                    "purchase_date": None,
                    "purchase_fx_to_usd": 1.0,
                    "cost_basis_local": rec["cost_basis"],
                    "original_total": rec["cost_basis"],
                    "original_currency": rec["original_currency"] if rec["original_currency"] in CURRENCIES else "USD",
                }
            )

    holdings_df = pd.DataFrame(holdings)
    return holdings_df, summary


@st.cache_data(ttl=3600, show_spinner=False)
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

    inverse = _direct_rate(quote_currency, base_currency)
    if inverse not in (None, 0):
        return 1 / inverse

    # Fallback bridge through USD for pairs that may not exist directly.
    to_usd = _direct_rate(base_currency, "USD")
    usd_to_target = _direct_rate("USD", quote_currency)
    if to_usd is not None and usd_to_target is not None:
        return to_usd * usd_to_target

    usd_to_base = _direct_rate("USD", base_currency)
    target_to_usd = _direct_rate(quote_currency, "USD")
    if usd_to_base not in (None, 0) and target_to_usd not in (None, 0):
        return (1 / usd_to_base) * (1 / target_to_usd)

    return None


def parse_manual_ticker_mapping(raw_text):
    mapping = {}
    if not raw_text:
        return mapping
    parts = raw_text.replace("\n", ",").split(",")
    for part in parts:
        if "=" not in part:
            continue
        left, right = part.split("=", 1)
        left = left.strip().upper()
        right = right.strip().upper()
        if left and right:
            mapping[left] = right
    return mapping


def resolve_market_ticker(ticker, manual_map):
    t = str(ticker).strip().upper()
    if t in manual_map:
        return manual_map[t]
    return DEFAULT_TICKER_MAP.get(t, t)


@st.cache_data(ttl=900, show_spinner=False)
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
    raw_news = stock.news or []
    if isinstance(raw_news, list):
        for item in raw_news[:5]:
            if not isinstance(item, dict):
                continue
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


def convert_currency_dict_to_display(amount_by_currency, display_currency):
    total = 0.0
    for currency, amount in amount_by_currency.items():
        fx = fetch_fx_rate(currency, display_currency) or 1.0
        total += amount * fx
    return total


def main():
    st.set_page_config(page_title="Portfolio Analyzer", layout="wide")
    st.markdown(
        """
        <style>
        .stApp {background: linear-gradient(180deg, #0b1020 0%, #090d18 100%);}
        .block-container {padding-top: 1.2rem;}
        .hero-title {font-size: 2.1rem; font-weight: 700; margin-bottom: 0.1rem;}
        .hero-sub {color: #9aa4b2; margin-bottom: 1rem;}
        div[data-testid="stMetric"] {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 12px 14px;
        }
        </style>
        <div class="hero-title">💹 Portfolio Intelligence Dashboard</div>
        <div class="hero-sub">Track holdings, income, taxes, and research in one premium view.</div>
        """,
        unsafe_allow_html=True,
    )

    init_db()

    st.subheader("Import Trading212 CSV")
    uploaded_csv_files = st.sidebar.file_uploader(
        "Import CSV",
        type=["csv"],
        accept_multiple_files=True,
    )
    imported_holdings_df = None
    imported_summary = None
    transaction_history_df = None
    if uploaded_csv_files:
        try:
            csv_frames = [pd.read_csv(file) for file in uploaded_csv_files]
            csv_df = pd.concat(csv_frames, ignore_index=True) if csv_frames else pd.DataFrame()
            raw_rows = len(csv_df)

            normalized = {col.strip().lower(): col for col in csv_df.columns}
            id_col = normalized.get("id")
            time_col = normalized.get("time")
            action_col = normalized.get("action")
            ticker_col = normalized.get("ticker")
            total_col = normalized.get("total")
            shares_col = normalized.get("no. of shares")

            if id_col:
                with_id = csv_df[csv_df[id_col].notna() & (csv_df[id_col].astype(str).str.strip() != "")]
                without_id = csv_df[~(csv_df[id_col].notna() & (csv_df[id_col].astype(str).str.strip() != ""))]
                with_id = with_id.drop_duplicates(subset=[id_col], keep="first")

                fallback_cols = [c for c in [action_col, time_col, ticker_col, total_col, shares_col] if c]
                if fallback_cols:
                    without_id = without_id.drop_duplicates(subset=fallback_cols, keep="first")
                csv_df = pd.concat([with_id, without_id], ignore_index=True)
            else:
                fallback_cols = [c for c in [action_col, time_col, ticker_col, total_col, shares_col] if c]
                if fallback_cols:
                    csv_df = csv_df.drop_duplicates(subset=fallback_cols, keep="first")

            if time_col:
                csv_df["_parsed_time"] = pd.to_datetime(csv_df[time_col], errors="coerce")
                csv_df = csv_df.sort_values(by="_parsed_time", na_position="last")
            transaction_history_df = csv_df.copy()

            imported_holdings_df, imported_summary = parse_trading212_transactions(csv_df)

            total_transactions = len(csv_df)
            duplicates_removed = raw_rows - total_transactions
            date_range = "N/A"
            if "_parsed_time" in csv_df.columns and csv_df["_parsed_time"].notna().any():
                min_date = csv_df["_parsed_time"].min().date().isoformat()
                max_date = csv_df["_parsed_time"].max().date().isoformat()
                date_range = f"{min_date} to {max_date}"

            st.success("Trading212 CSV files parsed successfully. Portfolio rebuilt from merged transactions.")
            st.caption(
                f"Uploaded files: {len(uploaded_csv_files)} | "
                f"Raw rows: {raw_rows} | "
                f"Duplicates removed: {duplicates_removed} | "
                f"Final rows used: {total_transactions} | "
                f"Date range: {date_range}"
            )
        except Exception as exc:
            st.error(f"Failed to parse CSV: {exc}")

    st.sidebar.markdown("### Personal Stock\nPortfolio Analyzer")
    selected_nav = st.sidebar.radio(
        "Navigation",
        ["Overview", "Holdings", "Transactions", "Income & Taxes", "Stock Research", "Settings"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    st.sidebar.header("Portfolio Tools")
    with st.sidebar.expander("Add Portfolio Position", expanded=True):
        ticker_input = st.text_input("Ticker", placeholder="AAPL")
        shares_input = st.number_input("Shares", min_value=0.0, value=1.0, step=1.0)
        purchase_price_input = st.number_input("Purchase Price", min_value=0.0, value=0.0, step=1.0)
        purchase_currency_input = st.selectbox("Purchase Currency", CURRENCIES, index=0)
        purchase_date_input = st.date_input("Purchase Date")
    with st.sidebar.expander("Ticker Mapping", expanded=False):
        manual_map_text = st.text_input("Manual ticker map (e.g. HBH=HBH.PR)", value="")
    manual_ticker_map = parse_manual_ticker_mapping(manual_map_text)

    if st.sidebar.button("Add Position", use_container_width=True):
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

    portfolio_df["market_ticker"] = portfolio_df["ticker"].map(lambda t: resolve_market_ticker(t, manual_ticker_map))
    tickers = sorted(portfolio_df["market_ticker"].unique().tolist())

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

    missing_price_tickers = [
        t for t in tickers if snapshots.get(t, {}).get("current_price") in (None, 0)
    ]
    if missing_price_tickers:
        st.warning(f"Ticker mapping required: {', '.join(missing_price_tickers)}")

    ctrl_c1, ctrl_c2, ctrl_c3 = st.columns([1.2, 1.2, 1])
    display_currency = ctrl_c1.selectbox("Display Currency", CURRENCIES, index=0)
    ctrl_c2.date_input("As of", value=datetime.utcnow().date())
    ctrl_c3.toggle("Dark Mode", value=True)
    usd_to_display = fetch_fx_rate("USD", display_currency)
    if usd_to_display is None:
        st.warning("Could not fetch current FX for display currency. Falling back to USD.")
        display_currency = "USD"
        usd_to_display = 1.0

    portfolio_df["current_price"] = portfolio_df["market_ticker"].map(
        lambda t: snapshots.get(t, {}).get("current_price")
    )
    portfolio_df["stock_currency"] = portfolio_df["market_ticker"].map(
        lambda t: snapshots.get(t, {}).get("stock_currency", "USD")
    )

    if "original_total" in portfolio_df.columns:
        portfolio_df["original_cost_local"] = portfolio_df["original_total"].fillna(0.0)
        portfolio_df["original_currency"] = portfolio_df["original_currency"].fillna(
            portfolio_df["purchase_currency"]
        )
        portfolio_df["original_to_display"] = portfolio_df["original_currency"].map(
            lambda c: fetch_fx_rate(c, display_currency) or 1.0
        )
        portfolio_df["original_cost_display"] = (
            portfolio_df["original_cost_local"] * portfolio_df["original_to_display"]
        )
    else:
        portfolio_df["original_cost_local"] = portfolio_df["shares"] * portfolio_df["purchase_price"].fillna(0)
        portfolio_df["original_currency"] = portfolio_df["purchase_currency"].fillna("USD")
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

    realized_display = 0.0
    dividend_display = 0.0
    lending_display = 0.0
    deposits_display = 0.0
    tax_display = 0.0
    cash_interest_display = 0.0
    if imported_summary is not None:
        realized_display = convert_currency_dict_to_display(imported_summary["realized_gains"], display_currency)
        dividend_display = convert_currency_dict_to_display(imported_summary["dividend_income"], display_currency)
        lending_display = convert_currency_dict_to_display(imported_summary["lending_income"], display_currency)
        deposits_display = convert_currency_dict_to_display(imported_summary["total_deposits"], display_currency)
        tax_display = convert_currency_dict_to_display(imported_summary["withholding_tax"], display_currency)
        cash_interest_display = convert_currency_dict_to_display(imported_summary["interest_on_cash"], display_currency)

    total_original = portfolio_df["original_cost_display"].sum()
    total_current = portfolio_df["current_value_display"].sum()
    total_gain = total_current - total_original
    total_gain_pct = (total_gain / total_original) if total_original else 0

    table_cols = [
        "ticker",
        "market_ticker",
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
    if selected_nav == "Overview":
        top_left, top_mid, top_right = st.columns([3, 2, 2])
        with top_left:
            st.markdown("## Portfolio Overview")
            st.caption("All values displayed in selected currency")
        with top_mid:
            date_filter = st.date_input("Date filter", value=datetime.utcnow().date())
        with top_right:
            if st.button("Refresh Data", use_container_width=True):
                st.cache_data.clear()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Portfolio Value", format_currency(total_current, display_currency))
        k2.metric("Total Return", format_currency(total_gain, display_currency))
        k3.metric("Total Return %", f"{total_gain_pct * 100:.2f}%")
        k4.metric("Total Deposits", format_currency(deposits_display, display_currency))
        k5, k6, k7, k8 = st.columns(4)
        k5.metric("Dividend Income", format_currency(dividend_display, display_currency))
        k6.metric("Realized Gains", format_currency(realized_display, display_currency))
        k7.metric("Lending Income", format_currency(lending_display, display_currency))
        k8.metric("Tax Paid", format_currency(tax_display, display_currency))

        center_col, right_col = st.columns([2.25, 1])
        with center_col:
            st.markdown("### Portfolio Value Over Time")
            if transaction_history_df is not None and "_parsed_time" in transaction_history_df.columns:
                perf_df = transaction_history_df.dropna(subset=["_parsed_time"]).copy()
                normalized_cols = {col.strip().lower(): col for col in perf_df.columns}
                action_col = normalized_cols.get("action")
                total_col = normalized_cols.get("total")
                if action_col and total_col and not perf_df.empty:
                    perf_df["amount"] = perf_df[total_col].map(to_float)
                    perf_df["action_l"] = perf_df[action_col].astype(str).str.lower()
                    perf_df["cashflow"] = perf_df["amount"]
                    perf_df.loc[perf_df["action_l"].str.contains("market buy", na=False), "cashflow"] *= -1
                    perf_df = perf_df.sort_values("_parsed_time")
                    perf_df["portfolio_flow"] = perf_df["cashflow"].cumsum()
                    perf_df["deposits_line"] = perf_df["cashflow"].where(
                        perf_df["action_l"].str.contains("deposit", na=False), 0
                    ).cumsum()
                    st.plotly_chart(
                        {
                            "data": [
                                {
                                    "x": perf_df["_parsed_time"],
                                    "y": perf_df["portfolio_flow"],
                                    "type": "scatter",
                                    "mode": "lines",
                                    "name": "Portfolio Value",
                                    "line": {"shape": "spline", "width": 3, "color": "#8b5cf6"},
                                },
                                {
                                    "x": perf_df["_parsed_time"],
                                    "y": perf_df["deposits_line"],
                                    "type": "scatter",
                                    "mode": "lines",
                                    "name": "Total Deposits",
                                    "line": {"shape": "spline", "width": 2, "dash": "dot", "color": "#64748b"},
                                },
                            ],
                            "layout": {
                                "paper_bgcolor": "rgba(0,0,0,0)",
                                "plot_bgcolor": "rgba(0,0,0,0)",
                                "margin": {"l": 20, "r": 20, "t": 20, "b": 20},
                                "xaxis": {"gridcolor": "rgba(255,255,255,0.06)"},
                                "yaxis": {"gridcolor": "rgba(255,255,255,0.06)"},
                            },
                        },
                        use_container_width=True,
                    )
            else:
                st.info("Upload transaction history with dates to render performance chart.")

        with right_col:
            st.markdown("### Asset Allocation")
            alloc = portfolio_df.groupby("ticker", as_index=False)["current_value_display"].sum()
            if alloc["current_value_display"].sum() > 0:
                st.plotly_chart(
                    {
                        "data": [
                            {
                                "labels": alloc["ticker"],
                                "values": alloc["current_value_display"],
                                "type": "pie",
                                "hole": 0.62,
                            }
                        ],
                        "layout": {
                            "paper_bgcolor": "rgba(0,0,0,0)",
                            "plot_bgcolor": "rgba(0,0,0,0)",
                            "margin": {"l": 20, "r": 20, "t": 20, "b": 20},
                        },
                    },
                    use_container_width=True,
                )
            st.markdown("### Income & Taxes Summary")
            st.metric("Dividend Income", format_currency(dividend_display, display_currency))
            st.metric("Lending Income", format_currency(lending_display, display_currency))
            st.metric("Tax Paid", format_currency(tax_display, display_currency))

        st.markdown("### Top Holdings")
        holdings_view = portfolio_df[table_cols].copy()
        holdings_view = holdings_view.sort_values(by="current_value_display", ascending=False)
        holdings_view["allocation_pct"] = holdings_view["current_value_display"] / holdings_view["current_value_display"].sum()
        st.dataframe(
            holdings_view.rename(
                columns={
                    "ticker": "Ticker",
                    "shares": "Shares",
                    "purchase_price": "Avg Price",
                    "current_price": "Current Price",
                    "current_value_display": "Market Value",
                    "unrealized_gain_loss": "Gain/Loss",
                    "unrealized_gain_loss_pct": "Return %",
                    "allocation_pct": "Allocation %",
                }
            ),
            column_config={
                "Allocation %": st.column_config.ProgressColumn("Allocation %", min_value=0, max_value=1),
                "Return %": st.column_config.ProgressColumn("Return %", min_value=-1.0, max_value=1.0),
            },
            use_container_width=True,
            hide_index=True,
        )

    elif selected_nav == "Holdings":
        st.subheader("Holdings")
        st.dataframe(portfolio_df[table_cols], use_container_width=True, hide_index=True)

    elif selected_nav == "Transactions":
        st.subheader("Transactions")
        debug_cols = [
            "ticker",
            "market_ticker",
            "shares",
            "original_cost_local",
            "original_currency",
            "current_price",
            "current_value_display",
        ]
        debug_df = portfolio_df[debug_cols].rename(
            columns={
                "market_ticker": "mapped ticker",
                "original_cost_local": "original total",
                "original_currency": "original currency",
                "current_value_display": f"current value ({display_currency})",
            }
        )
        with st.expander("Debug transaction table", expanded=False):
            st.dataframe(debug_df, use_container_width=True, hide_index=True)
    elif selected_nav == "Income & Taxes":
        st.subheader("Income & Taxes")
        i1, i2, i3 = st.columns(3)
        i1.metric("Dividend Income", format_currency(dividend_display, display_currency))
        i2.metric("Lending Income", format_currency(lending_display, display_currency))
        i3.metric("Interest on Cash", format_currency(cash_interest_display, display_currency))
        i4, i5 = st.columns(2)
        i4.metric("Total Deposits", format_currency(deposits_display, display_currency))
        i5.metric("Withholding Tax", format_currency(tax_display, display_currency))
    elif selected_nav == "Stock Research":
        st.subheader("Stock Research")
        selected = st.selectbox("Pick a stock", tickers)
        s = snapshots[selected]
        logo_url = None
        try:
            logo_url = yf.Ticker(selected).info.get("logo_url")
        except Exception:
            logo_url = None
        if logo_url:
            st.image(logo_url, width=48)

        c1, c2, c3 = st.columns(3)
        c1.metric("Market Cap", format_number(s["market_cap"]))
        c2.metric("P/E Ratio", format_number(s["pe_ratio"]))
        c3.metric("Debt to Equity", format_number(s["debt_to_equity"]))

        c4, c5, c6 = st.columns(3)
        c4.metric("Revenue Growth", format_number(s["revenue_growth"], pct=True))
        c5.metric("Profit Margins", format_number(s["profit_margins"], pct=True))
        c6.metric("Analyst Recommendation", str(s["analyst_recommendation"]).upper())

        st.markdown(f"#### Recent News: {selected}")
        if news_by_ticker[selected]:
            for item in news_by_ticker[selected]:
                st.markdown(f"- [{item['title']}]({item['link']})")
        else:
            st.write("No recent headlines found.")

        st.markdown("#### AI Investment Analysis")
        if st.button(f"Generate analysis for {selected}"):
            with st.spinner("Calling OpenAI..."):
                ai_text = summarize_with_openai(s)
            st.markdown(ai_text)
    else:
        st.subheader("Settings")
        st.write("Theme and dashboard preferences will appear here.")


if __name__ == "__main__":
    main()
