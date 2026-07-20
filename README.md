# Quotex Signal Desk — Streamlit Edition

Dark-themed binary options signal dashboard built with Python + Streamlit.
Uses your Quotex session cookies/token to fetch live candle data and run
the 12-indicator signal logic (including 5M HTF SNR Reversal).

---

## Run Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the app
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

---

## Connect to Quotex

Go to the **Connection** page and either:
- Paste your `.env` file contents, OR
- Enter credentials manually

Required fields:
- `QX_COOKIES` — your Quotex session cookie string
- `QX_TOKEN`   — your Quotex SSID/token

Optional:
- `QX_EMAIL`, `QX_PASSWORD`

---

## Deploy to Streamlit Cloud (Free)

1. Push this folder to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Select repo → main file: `app.py`
4. Under **Advanced settings → Secrets**, add:

```toml
QX_EMAIL    = "your@email.com"
QX_PASSWORD = "yourpassword"
QX_COOKIES  = "your_full_cookie_string"
QX_TOKEN    = "your_token_string"
```

5. Click **Deploy** — live in ~60 seconds

---

## Features

| Feature | Description |
|---|---|
| Dashboard | Scan all 25 pairs, see CALL/PUT/NEUTRAL per pair with confidence |
| Generate Signal | Pick a pair + duration, get CALL/PUT + entry time + exit time + live countdown |
| Signal History | All CALL/PUT signals from the session, filterable by direction and market |
| Connection | Paste .env or enter credentials manually |
| Auto-refresh | Optional 30-second auto-scan on the Dashboard |
| Cloud ready | Works on Streamlit Cloud with secrets management |

---

## Pairs Monitored

**Real (7):** EUR/USD, GBP/USD, EUR/JPY, USD/JPY, GBP/JPY, AUD/JPY, AUD/USD

**OTC (18):** USD/BDT, USD/INR, USD/PKR, USD/BRL, NZD/JPY, USD/IDR, USD/MXN,
NZD/USD, USD/ZAR, CAD/CHF, USD/NGN, EUR/NZD, USD/ARS, USD/COP, AUD/NZD,
GBP/NZD, USD/PHP, NZD/CHF

---

## File Structure

```
streamlit_app/
├── app.py              ← main Streamlit app (all pages)
├── qx_client.py        ← sync wrapper around async Quotex API
├── signal_logic.py     ← 12-indicator signal engine (5M HTF SNR)
├── pairs.py            ← static pair registry
├── quotexapi/          ← vendored Quotex API client
├── requirements.txt    ← Python dependencies
├── .streamlit/
│   └── config.toml     ← dark theme + server config
└── README.md
```
