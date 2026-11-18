# Options Reversal Zones - Pine Script Generator (Streamlit + Upstox)

Generates ready-to-paste TradingView Pine scripts for Nifty, BankNifty,
Sensex and Godrejprop from the **previous trading day's closing option
premiums** (not live LTP), pulled from Upstox's Historical Candle API.
Nifty/Sensex use the nearest weekly expiry; BankNifty/Godrejprop use the
monthly expiry.

## Get your Upstox token (once, ~30 seconds, valid 1 year, read-only)

1. Go to https://account.upstox.com/developer/apps → **Analytics** tab.
2. Click **Generate Token** → Confirm → copy it.

No OAuth, client secret or redirect URI needed.

## Deploy (GitHub + Streamlit Community Cloud)

1. Push `streamlit_app.py` and `requirements.txt` to a GitHub repo.
2. https://share.streamlit.io → New app → pick the repo → Main file path
   `streamlit_app.py` → Deploy.
3. (Recommended) App → **Settings → Secrets**:

   ```toml
   UPSTOX_ANALYTICS_TOKEN = "your_token_here"
   ```

   Without this the app asks you to paste the token on the page.

## Run locally

```
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Notes

- Always compare the **raw data table** the app shows with Upstox's own
  option chain before trusting a generated script.
- If far-from-ATM strikes have never traded (common for stock options),
  the app automatically narrows the range and tells you.
- Your app URL is public by default; restrict viewers under Settings →
  Sharing, or deploy from a private repo.
- Written without access to a live Upstox account - test the first output.
