"""
Options Reversal Zones - Streamlit app (Upstox, token-only)
=============================================================
Pulls the PREVIOUS DAY'S official closing premium (not live LTP) for every
strike from Upstox's Historical Candle API, then generates ready-to-paste
TradingView Pine scripts for Nifty, BankNifty, Sensex and Godrejprop
(one per instrument, or ONE combined script with an Instrument dropdown).

AUTH: Upstox Analytics Token - a 1-year, read-only token you generate with
one click (no OAuth, no redirect_uri):
  account.upstox.com/developer/apps -> Analytics tab -> Generate Token.

DEPLOY (GitHub + Streamlit Community Cloud)
  1. Push streamlit_app.py and requirements.txt to a GitHub repo.
  2. share.streamlit.io -> New app -> Main file path: streamlit_app.py.
  3. (Recommended) App -> Settings -> Secrets:
         UPSTOX_ANALYTICS_TOKEN = "your_token_here"
     Without it, the app asks you to paste the token on the page.

Not tested against a live Upstox account from where this was written -
compare the raw-data table in the app with Upstox's own option chain before
trusting the generated script.
"""

import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests
import streamlit as st

BASE = "https://api.upstox.com/v2"
BASE_V3 = "https://api.upstox.com/v3"

# key = Upstox instrument_key of the UNDERLYING (index / stock)
INSTRUMENTS = {
    "NIFTY":      {"key": "NSE_INDEX|Nifty 50",  "expiry_type": "Weekly",  "strikes_each_side": 10},
    "BANKNIFTY":  {"key": "NSE_INDEX|Nifty Bank", "expiry_type": "Monthly", "strikes_each_side": 10},
    "SENSEX":     {"key": "BSE_INDEX|SENSEX",     "expiry_type": "Weekly",  "strikes_each_side": 10},
    # Stock options have a narrow liquid band: far strikes can exist but never
    # trade. fetch_option_data probes outward from ATM and stops at the real
    # liquid edge on each side, so this is only the requested range.
    "GODREJPROP": {"key": "NSE_EQ|INE484J01027",  "expiry_type": "Monthly", "strikes_each_side": 10},
}

st.set_page_config(page_title="Options Reversal Zones - Pine Generator", page_icon="📈")


# ═══════════════════════════════════════════════════════════════════
#  UPSTOX DATA FETCHING
# ═══════════════════════════════════════════════════════════════════
def get_contracts(sess, instrument_key):
    r = sess.get(f"{BASE}/option/contract", params={"instrument_key": instrument_key})
    r.raise_for_status()
    contracts = r.json().get("data", [])
    if not contracts:
        raise RuntimeError("No option contracts returned - check the token / instrument key.")
    return contracts


def pick_expiry(contracts, expiry_type):
    """Weekly -> nearest upcoming expiry. Monthly -> the last expiry date in a
    month (the true monthly contract), nearest upcoming one."""
    expiries = sorted({c["expiry"] for c in contracts})
    today = date.today().isoformat()

    if expiry_type == "Weekly":
        upcoming = [e for e in expiries if e >= today]
        return upcoming[0] if upcoming else expiries[-1]

    by_month = {}
    for e in expiries:
        y, mo, _ = e.split("-")
        by_month.setdefault((y, mo), []).append(e)
    monthly = sorted(max(v) for v in by_month.values())
    upcoming = [e for e in monthly if e >= today]
    return upcoming[0] if upcoming else monthly[-1]


def get_prev_close(sess, instrument_key, max_retries=5):
    """Previous completed session's close from the Historical Candle API.
    (The option-chain close_price field is unreliable intraday.)"""
    today = date.today()
    from_date = (today - timedelta(days=10)).isoformat()
    to_date = today.isoformat()
    url = f"{BASE_V3}/historical-candle/{quote(instrument_key, safe='')}/days/1/{to_date}/{from_date}"

    for attempt in range(max_retries):
        r = sess.get(url)
        if r.status_code == 429:
            time.sleep(1.0 * (attempt + 1))
            continue
        r.raise_for_status()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return None
        # most-recent-first: [timestamp, open, high, low, close, volume, oi]
        for c in candles:
            if c[0][:10] < to_date:
                return c[4]
        return candles[0][4]
    raise RuntimeError("Upstox rate limit hit repeatedly - wait a moment and try again.")


def fetch_option_data(sess, instrument_name, strikes_each_side):
    cfg = INSTRUMENTS[instrument_name]
    contracts = get_contracts(sess, cfg["key"])
    expiry = pick_expiry(contracts, cfg["expiry_type"])
    expiry_contracts = [c for c in contracts if c["expiry"] == expiry]

    by_strike_type = {(c["strike_price"], c["instrument_type"]): c["instrument_key"] for c in expiry_contracts}
    strikes = sorted({k[0] for k in by_strike_type})
    diffs = [round(b - a, 4) for a, b in zip(strikes, strikes[1:])]
    step = max(set(diffs), key=diffs.count) if diffs else None

    # spot price (for ATM) - one lightweight option/chain call; its close_price is NOT used
    r = sess.get(f"{BASE}/option/chain", params={"instrument_key": cfg["key"], "expiry_date": expiry})
    r.raise_for_status()
    chain_data = r.json().get("data", [])
    if not chain_data:
        raise RuntimeError(f"Empty option chain for {instrument_name} / {expiry}.")
    spot = chain_data[0]["underlying_spot_price"]
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))

    margin = 2  # Boundary Line needs neighbours 2 strikes beyond the display range
    requested_half = strikes_each_side + margin

    def probe(idx):
        s = strikes[idx]
        ce_key, pe_key = by_strike_type.get((s, "CE")), by_strike_type.get((s, "PE"))
        if not ce_key or not pe_key:
            return None
        ce = get_prev_close(sess, ce_key)
        time.sleep(0.1)
        pe = get_prev_close(sess, pe_key)
        time.sleep(0.1)
        if ce is None or pe is None:
            return None
        return ce, pe

    center = probe(atm_idx)
    if center is None:
        raise RuntimeError(f"ATM strike {strikes[atm_idx]} has no historical data for this expiry.")

    right, left = {}, {}
    for off in range(1, requested_half + 1):
        i = atm_idx + off
        if i >= len(strikes):
            break
        res = probe(i)
        if res is None:
            break
        right[off] = res
    for off in range(1, requested_half + 1):
        i = atm_idx - off
        if i < 0:
            break
        res = probe(i)
        if res is None:
            break
        left[off] = res

    effective_half = min(len(left), len(right))
    if effective_half < margin + 1:
        raise RuntimeError(
            f"Only {effective_half} liquid strike(s) each side of ATM ({strikes[atm_idx]}) - "
            f"not enough for a display range plus the {margin}-strike margin."
        )
    effective_each_side = effective_half - margin

    window_strikes = ([strikes[atm_idx - o] for o in range(effective_half, 0, -1)]
                      + [strikes[atm_idx]]
                      + [strikes[atm_idx + o] for o in range(1, effective_half + 1)])
    ext_ce = ([left[o][0] for o in range(effective_half, 0, -1)] + [center[0]]
              + [right[o][0] for o in range(1, effective_half + 1)])
    ext_pe = ([left[o][1] for o in range(effective_half, 0, -1)] + [center[1]]
              + [right[o][1] for o in range(1, effective_half + 1)])

    return {
        "expiry": expiry,
        "spot": spot,
        "step": step,
        "margin": margin,
        "ext_strikes": window_strikes,
        "ext_ce": ext_ce,
        "ext_pe": ext_pe,
        "requested_strikes_each_side": strikes_each_side,
        "effective_strikes_each_side": effective_each_side,
        "shrunk": effective_each_side < strikes_each_side,
    }


# ═══════════════════════════════════════════════════════════════════
#  PINE SCRIPT GENERATION (formulas verified against your original scripts)
# ═══════════════════════════════════════════════════════════════════
def r4(x):
    return f"{x:.4f}"


def compute_display_arrays(chain):
    """Turns a fetched chain (with margin strikes on each side for Boundary
    Line) into the final display-ready lists: strikes, avg, bl, and all 4
    zone pairs - shared by both the single-instrument and combined builders."""
    m = chain["margin"]
    ext_strikes, ext_ce, ext_pe = chain["ext_strikes"], chain["ext_ce"], chain["ext_pe"]
    n_display = len(ext_strikes) - 2 * m

    strikes, ce, pe, avg, bl = [], [], [], [], []
    uce133, uce15, upe133, upe15 = [], [], [], []
    lce15, lce2, lpe15, lpe2 = [], [], [], []

    for d in range(n_display):
        ext_i = d + m
        s, c, p = ext_strikes[ext_i], ext_ce[ext_i], ext_pe[ext_i]
        strikes.append(s)
        ce.append(c)
        pe.append(p)
        avg.append((c + p) / 2)
        bl.append((ext_ce[ext_i + 2] + ext_pe[ext_i - 2]) / 2)
        uce133.append(c * 1.33)
        uce15.append(c * 1.5)
        upe133.append(p * 1.33)
        upe15.append(p * 1.5)
        lce15.append(c / 1.5)
        lce2.append(c / 3)
        lpe15.append(p / 1.5)
        lpe2.append(p / 3)

    return {
        "strikes": strikes, "avg": avg, "bl": bl,
        "uce133": uce133, "uce15": uce15, "upe133": upe133, "upe15": upe15,
        "lce15": lce15, "lce2": lce2, "lpe15": lpe15, "lpe2": lpe2,
        "default_strike": strikes[len(strikes) // 2],
    }


def build_pine_script(instrument_name, expiry_type, chain):
    d = compute_display_arrays(chain)
    strikes, avg, bl = d["strikes"], d["avg"], d["bl"]
    uce133, uce15, upe133, upe15 = d["uce133"], d["uce15"], d["upe133"], d["upe15"]
    lce15, lce2, lpe15, lpe2 = d["lce15"], d["lce2"], d["lpe15"], d["lpe2"]
    default_strike = d["default_strike"]
    strike_opts = ", ".join(f"\"{s:g}\"" for s in strikes)

    def arr(vals):
        return ", ".join(r4(v) for v in vals)

    return f'''//@version=5
indicator("Options Reversal Zones - Auto ({instrument_name}) | Expiry: {chain["expiry"]}", overlay=true, max_lines_count=500, max_labels_count=500)

// Auto-generated from Upstox (previous day's close). Spot at generation time: {chain["spot"]}
// Regenerate this file each trading day for fresh premiums.

// ── Strike Selector ──────────────────────────────────────
selected = input.string("{default_strike:g}", "Select Strike", options=[{strike_opts}])

// ── Zone Toggles ─────────────────────────────────────────
show_avg    = input.bool(true,  "Individual Average")
show_bl     = input.bool(true,  "Boundary Line")
show_uce    = input.bool(true,  "Upper Reversal Zone CE")
show_upe    = input.bool(true,  "Upper Reversal Zone PE")
show_lce    = input.bool(true,  "Lower Reversal Zone CE")
show_lpe    = input.bool(true,  "Lower Reversal Zone PE")
show_lbl    = input.bool(true,  "Show Labels")
lw          = input.int(1, "Line Width", minval=1, maxval=4)

// ── Data pulled from Upstox at generation time ────────
var string[] strike_arr  = array.from({strike_opts})
var float[]  avg_arr     = array.from({arr(avg)})
var float[]  bl_arr      = array.from({arr(bl)})
var float[]  uce133_arr  = array.from({arr(uce133)})
var float[]  uce15_arr   = array.from({arr(uce15)})
var float[]  upe133_arr  = array.from({arr(upe133)})
var float[]  upe15_arr   = array.from({arr(upe15)})
var float[]  lce15_arr   = array.from({arr(lce15)})
var float[]  lce2_arr    = array.from({arr(lce2)})
var float[]  lpe15_arr   = array.from({arr(lpe15)})
var float[]  lpe2_arr    = array.from({arr(lpe2)})

// ── Helpers ──────────────────────────────────────────────
f_line(y, col, w) =>
    line.new(bar_index-1, y, bar_index, y, extend=extend.both, color=col, width=w)

f_lbl(y, txt, col) =>
    if show_lbl
        label.new(bar_index, y, txt, xloc=xloc.bar_index, yloc=yloc.price,
                  style=label.style_label_left, color=color.new(col,80),
                  textcolor=col, size=size.small)

// ── Plot on last bar ─────────────────────────────────────
idx = array.indexof(strike_arr, selected)

if barstate.islast and idx >= 0
    s = selected
    if show_avg
        v = array.get(avg_arr, idx)
        f_line(v, color.new(color.gray, 20), lw)
        f_lbl(v, s + " Avg " + str.tostring(v, "#.00"), color.gray)
    if show_bl
        v = array.get(bl_arr, idx)
        f_line(v, color.new(color.orange, 20), lw)
        f_lbl(v, s + " BL " + str.tostring(v, "#.00"), color.orange)
    if show_uce
        v = array.get(uce133_arr, idx)
        f_line(v, color.new(color.red, 20), lw)
        f_lbl(v, s + " UCE×1.33 " + str.tostring(v, "#.00"), color.red)
        v := array.get(uce15_arr, idx)
        f_line(v, color.new(color.red, 40), lw)
        f_lbl(v, s + " UCE×1.5 " + str.tostring(v, "#.00"), color.red)
    if show_upe
        v = array.get(upe133_arr, idx)
        f_line(v, color.new(color.purple, 20), lw)
        f_lbl(v, s + " UPE×1.33 " + str.tostring(v, "#.00"), color.purple)
        v := array.get(upe15_arr, idx)
        f_line(v, color.new(color.purple, 40), lw)
        f_lbl(v, s + " UPE×1.5 " + str.tostring(v, "#.00"), color.purple)
    if show_lce
        v = array.get(lce15_arr, idx)
        f_line(v, color.new(color.teal, 20), lw)
        f_lbl(v, s + " LCE×1.5 " + str.tostring(v, "#.00"), color.teal)
        v := array.get(lce2_arr, idx)
        f_line(v, color.new(color.teal, 40), lw)
        f_lbl(v, s + " LCE×2 " + str.tostring(v, "#.00"), color.teal)
    if show_lpe
        v = array.get(lpe15_arr, idx)
        f_line(v, color.new(color.blue, 20), lw)
        f_lbl(v, s + " LPE×1.5 " + str.tostring(v, "#.00"), color.blue)
        v := array.get(lpe2_arr, idx)
        f_line(v, color.new(color.blue, 40), lw)
        f_lbl(v, s + " LPE×2 " + str.tostring(v, "#.00"), color.blue)

// ── Legend ───────────────────────────────────────────────
plot(na, "Individual Average",     color=color.gray)
plot(na, "Boundary Line",          color=color.orange)
plot(na, "Upper Reversal Zone CE", color=color.red)
plot(na, "Upper Reversal Zone PE", color=color.purple)
plot(na, "Lower Reversal Zone CE", color=color.teal)
plot(na, "Lower Reversal Zone PE", color=color.blue)
'''


def build_combined_pine_script(chains_by_instrument, expiry_types):
    """chains_by_instrument: {"NIFTY": chain, "BANKNIFTY": chain, ...} for all
    four. Produces ONE Pine script with an Instrument selector that switches
    which pre-baked array set (and which per-instrument strike dropdown) is
    active. Pine's input dropdown options must be static, so each instrument
    keeps its own "Select Strike" input - only the one matching the currently
    chosen Instrument is actually used."""
    names = list(chains_by_instrument.keys())
    disp = {name: compute_display_arrays(chains_by_instrument[name]) for name in names}

    def arr(vals):
        return ", ".join(r4(v) for v in vals)

    def strike_opts_for(name):
        return ", ".join(f"\"{s:g}\"" for s in disp[name]["strikes"])

    lc = {name: name.lower() for name in names}  # e.g. "NIFTY" -> "nifty", used as Pine variable prefixes

    # ── per-instrument data block + its own strike selector ──
    data_blocks = []
    for name in names:
        d = disp[name]
        chain = chains_by_instrument[name]
        p = lc[name]
        data_blocks.append(f'''
// ── {name} | {expiry_types[name]} Expiry: {chain["expiry"]} | Spot at generation: {chain["spot"]} ──
selected_{p} = input.string("{d["default_strike"]:g}", "Select Strike ({name})", options=[{strike_opts_for(name)}], group="{name}")
var string[] {p}_strike_arr  = array.from({strike_opts_for(name)})
var float[]  {p}_avg_arr     = array.from({arr(d["avg"])})
var float[]  {p}_bl_arr      = array.from({arr(d["bl"])})
var float[]  {p}_uce133_arr  = array.from({arr(d["uce133"])})
var float[]  {p}_uce15_arr   = array.from({arr(d["uce15"])})
var float[]  {p}_upe133_arr  = array.from({arr(d["upe133"])})
var float[]  {p}_upe15_arr   = array.from({arr(d["upe15"])})
var float[]  {p}_lce15_arr   = array.from({arr(d["lce15"])})
var float[]  {p}_lce2_arr    = array.from({arr(d["lce2"])})
var float[]  {p}_lpe15_arr   = array.from({arr(d["lpe15"])})
var float[]  {p}_lpe2_arr    = array.from({arr(d["lpe2"])})''')

    # ── ternary chain picking the active instrument's arrays/selection ──
    def ternary(field):
        parts = []
        for i, name in enumerate(names):
            val = f"{lc[name]}_{field}" if field != "selected" else f"selected_{lc[name]}"
            if i < len(names) - 1:
                parts.append(f'instrument == "{name}" ? {val} : ')
            else:
                parts.append(val)
        return "".join(parts)

    def expiry_ternary():
        parts = []
        for i, name in enumerate(names):
            chain = chains_by_instrument[name]
            val = f'"{expiry_types[name]} Expiry: {chain["expiry"]}"'
            if i < len(names) - 1:
                parts.append(f'instrument == "{name}" ? {val} : ')
            else:
                parts.append(val)
        return "".join(parts)

    instrument_options = ", ".join(f'"{n}"' for n in names)

    return f'''//@version=5
indicator("Options Reversal Zones - Auto (All Instruments)", overlay=true, max_lines_count=500, max_labels_count=500)

// Auto-generated from Upstox (previous day's close).
// Regenerate this file each trading day for fresh premiums.

// ── Instrument Selector ───────────────────────────────────
instrument = input.string("{names[0]}", "Instrument", options=[{instrument_options}])

// ── Zone Toggles ───────────────────────────────────────────
show_avg    = input.bool(true,  "Individual Average")
show_bl     = input.bool(true,  "Boundary Line")
show_uce    = input.bool(true,  "Upper Reversal Zone CE")
show_upe    = input.bool(true,  "Upper Reversal Zone PE")
show_lce    = input.bool(true,  "Lower Reversal Zone CE")
show_lpe    = input.bool(true,  "Lower Reversal Zone PE")
show_lbl    = input.bool(true,  "Show Labels")
lw          = input.int(1, "Line Width", minval=1, maxval=4)
{"".join(data_blocks)}

// ── Pick the active instrument's data ───────────────────────
strike_arr  = {ternary("strike_arr")}
avg_arr     = {ternary("avg_arr")}
bl_arr      = {ternary("bl_arr")}
uce133_arr  = {ternary("uce133_arr")}
uce15_arr   = {ternary("uce15_arr")}
upe133_arr  = {ternary("upe133_arr")}
upe15_arr   = {ternary("upe15_arr")}
lce15_arr   = {ternary("lce15_arr")}
lce2_arr    = {ternary("lce2_arr")}
lpe15_arr   = {ternary("lpe15_arr")}
lpe2_arr    = {ternary("lpe2_arr")}
selected    = {ternary("selected")}

// ── Helpers ──────────────────────────────────────────────
f_line(y, col, w) =>
    line.new(bar_index-1, y, bar_index, y, extend=extend.both, color=col, width=w)

f_lbl(y, txt, col) =>
    if show_lbl
        label.new(bar_index, y, txt, xloc=xloc.bar_index, yloc=yloc.price,
                  style=label.style_label_left, color=color.new(col,80),
                  textcolor=col, size=size.small)

// ── Plot on last bar ─────────────────────────────────────
idx = array.indexof(strike_arr, selected)

if barstate.islast and idx >= 0
    s = selected
    if show_avg
        v = array.get(avg_arr, idx)
        f_line(v, color.new(color.gray, 20), lw)
        f_lbl(v, s + " Avg " + str.tostring(v, "#.00"), color.gray)
    if show_bl
        v = array.get(bl_arr, idx)
        f_line(v, color.new(color.orange, 20), lw)
        f_lbl(v, s + " BL " + str.tostring(v, "#.00"), color.orange)
    if show_uce
        v = array.get(uce133_arr, idx)
        f_line(v, color.new(color.red, 20), lw)
        f_lbl(v, s + " UCE×1.33 " + str.tostring(v, "#.00"), color.red)
        v := array.get(uce15_arr, idx)
        f_line(v, color.new(color.red, 40), lw)
        f_lbl(v, s + " UCE×1.5 " + str.tostring(v, "#.00"), color.red)
    if show_upe
        v = array.get(upe133_arr, idx)
        f_line(v, color.new(color.purple, 20), lw)
        f_lbl(v, s + " UPE×1.33 " + str.tostring(v, "#.00"), color.purple)
        v := array.get(upe15_arr, idx)
        f_line(v, color.new(color.purple, 40), lw)
        f_lbl(v, s + " UPE×1.5 " + str.tostring(v, "#.00"), color.purple)
    if show_lce
        v = array.get(lce15_arr, idx)
        f_line(v, color.new(color.teal, 20), lw)
        f_lbl(v, s + " LCE×1.5 " + str.tostring(v, "#.00"), color.teal)
        v := array.get(lce2_arr, idx)
        f_line(v, color.new(color.teal, 40), lw)
        f_lbl(v, s + " LCE×2 " + str.tostring(v, "#.00"), color.teal)
    if show_lpe
        v = array.get(lpe15_arr, idx)
        f_line(v, color.new(color.blue, 20), lw)
        f_lbl(v, s + " LPE×1.5 " + str.tostring(v, "#.00"), color.blue)
        v := array.get(lpe2_arr, idx)
        f_line(v, color.new(color.blue, 40), lw)
        f_lbl(v, s + " LPE×2 " + str.tostring(v, "#.00"), color.blue)

// ── Legend ───────────────────────────────────────────────
plot(na, "Individual Average",     color=color.gray)
plot(na, "Boundary Line",          color=color.orange)
plot(na, "Upper Reversal Zone CE", color=color.red)
plot(na, "Upper Reversal Zone PE", color=color.purple)
plot(na, "Lower Reversal Zone CE", color=color.teal)
plot(na, "Lower Reversal Zone PE", color=color.blue)
'''


# ═══════════════════════════════════════════════════════════════════
#  APP UI
# ═══════════════════════════════════════════════════════════════════
def show_raw(chain):
    m = chain["margin"]
    n = len(chain["ext_strikes"])
    st.dataframe(
        {"Strike": chain["ext_strikes"][m:n - m],
         "CE (prev close)": chain["ext_ce"][m:n - m],
         "PE (prev close)": chain["ext_pe"][m:n - m]},
        use_container_width=True,
    )


def shrink_warning(name, chain):
    if chain["shrunk"]:
        st.warning(
            f"{name}: requested {chain['requested_strikes_each_side']} strikes each side, but only "
            f"{chain['effective_strikes_each_side']} had real trade history - using "
            f"{chain['effective_strikes_each_side']} per side."
        )


st.title("📈 Options Reversal Zones - Pine Script Generator (Upstox)")

st.subheader("1. Upstox Analytics Token")
st.caption("Generate once, valid 1 year: account.upstox.com/developer/apps → Analytics tab → Generate Token.")

try:
    token = st.secrets.get("UPSTOX_ANALYTICS_TOKEN")
except Exception:
    token = None
if not token:
    token = st.text_input("Paste your Analytics Token", type="password")
if not token:
    st.stop()

sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"})
st.success("Token set ✓")

st.subheader("2. Generate a Pine script")
for name, cfg in INSTRUMENTS.items():
    col1, col2 = st.columns([3, 2])
    col1.write(f"**{name}** ({cfg['expiry_type']} expiry)")
    if col2.button("Fetch & Generate", key=f"gen_{name}"):
        with st.spinner(f"Pulling {name} previous-close data from Upstox..."):
            try:
                chain = fetch_option_data(sess, name, cfg["strikes_each_side"])
                pine_code = build_pine_script(name, cfg["expiry_type"], chain)
            except Exception as e:
                st.error(f"{name} failed: {e}")
            else:
                st.success(f"{name}: expiry {chain['expiry']}, spot {chain['spot']}")
                shrink_warning(name, chain)
                with st.expander(f"🔍 Raw fetched data for {name} - compare with Upstox's option chain", expanded=True):
                    show_raw(chain)
                st.download_button(
                    f"Download {name} Pine script",
                    data=pine_code,
                    file_name=f"{name}_reversal_zones_{chain['expiry']}.pine",
                    mime="text/plain",
                    key=f"dl_{name}",
                )
                with st.expander(f"Preview {name}.pine"):
                    st.code(pine_code, language="text")

st.subheader("3. Or generate ONE combined script for all 4 instruments")
st.caption("Fetches all four and produces one Pine file with an Instrument dropdown.")

if st.button("Fetch & Generate Combined Script"):
    chains, expiry_types, failed = {}, {}, []
    for name, cfg in INSTRUMENTS.items():
        with st.spinner(f"Pulling {name} previous-close data from Upstox..."):
            try:
                chains[name] = fetch_option_data(sess, name, cfg["strikes_each_side"])
                expiry_types[name] = cfg["expiry_type"]
            except Exception as e:
                failed.append(f"{name}: {e}")

    if failed:
        st.error("Some instruments failed, so the combined script was not built:\n\n" + "\n".join(failed))
    else:
        combined_code = build_combined_pine_script(chains, expiry_types)
        st.success("All four fetched successfully.")
        for name, chain in chains.items():
            shrink_warning(name, chain)
        for name, chain in chains.items():
            with st.expander(f"🔍 Raw fetched data for {name}"):
                show_raw(chain)
        st.download_button(
            "Download combined Pine script (all 4 instruments)",
            data=combined_code,
            file_name=f"AllInstruments_reversal_zones_{date.today().isoformat()}.pine",
            mime="text/plain",
        )
        with st.expander("Preview combined script"):
            st.code(combined_code, language="text")
