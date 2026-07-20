"""Static registry of monitored trading pairs. Read-only signal engine
never trades these -- it only watches them and computes indicator-based
scores."""

REAL_PAIRS = [
    ("EUR/USD", "EURUSD"),
    ("GBP/USD", "GBPUSD"),
    ("EUR/JPY", "EURJPY"),
    ("USD/JPY", "USDJPY"),
    ("GBP/JPY", "GBPJPY"),
    ("AUD/JPY", "AUDJPY"),
    ("AUD/USD", "AUDUSD"),
]

OTC_PAIRS = [
    ("USD/BDT", "USDBDT_otc"),
    ("USD/INR", "USDINR_otc"),
    ("USD/PKR", "USDPKR_otc"),
    ("USD/BRL", "USDBRL_otc"),
    ("NZD/JPY", "NZDJPY_otc"),
    ("USD/IDR", "USDIDR_otc"),
    ("USD/MXN", "USDMXN_otc"),
    ("NZD/USD", "NZDUSD_otc"),
    ("USD/ZAR", "USDZAR_otc"),
    ("CAD/CHF", "CADCHF_otc"),
    ("USD/NGN", "USDNGN_otc"),
    ("EUR/NZD", "EURNZD_otc"),
    ("USD/ARS", "USDARS_otc"),
    ("USD/COP", "USDCOP_otc"),
    ("AUD/NZD", "AUDNZD_otc"),
    ("GBP/NZD", "GBPNZD_otc"),
    ("USD/PHP", "USDPHP_otc"),
    ("NZD/CHF", "NZDCHF_otc"),
]


def all_pairs():
    """Returns list of dicts: symbol, displayName, market."""
    pairs = []
    for display, symbol in REAL_PAIRS:
        pairs.append({"symbol": symbol, "displayName": display, "market": "real"})
    for display, symbol in OTC_PAIRS:
        pairs.append({"symbol": symbol, "displayName": display, "market": "otc"})
    return pairs
