import os
import requests
import pandas as pd
import yfinance as yf
import numpy as np
from datetime import datetime, date
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
import exchange_calendars as xcals
import time

print("=== IV Data Collector START ===")

# ====================================================
# ✅ 주말/공휴일 체크 (NYSE 캘린더 자동)
# ====================================================
today_date = date.today()
today      = today_date.strftime("%Y-%m-%d")

def is_market_open(check_date: date) -> bool:
    try:
        nyse = xcals.get_calendar("XNYS")
        return nyse.is_session(check_date.strftime("%Y-%m-%d"))
    except Exception:
        return check_date.weekday() < 5

if not is_market_open(today_date):
    print(f"📅 오늘({today})은 NYSE 휴장일입니다. 스킵합니다.")
    exit(0)

print(f"✅ 오늘({today}) 장 운영일 확인")

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")

# ====================================================
# ✅ API 초기화
# ====================================================
API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client     = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

# ====================================================
# ✅ S&P 500 리스트
# ✅ 수집 불가 종목 제거 (yfinance 미지원 또는 옵션 없음)
# ====================================================
EXCLUDE_SYMBOLS = {
    "BRK-B",   # yfinance 미지원
    "BF-B",    # yfinance 미지원
    "NVR",     # 옵션 데이터 없음
}

def get_sp500_symbols():
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        import io
        sp500   = pd.read_html(io.StringIO(resp.text), flavor="html5lib")[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]
        print(f"✅ S&P 500 종목 수: {len(symbols)} (제외: {len(EXCLUDE_SYMBOLS)}개)")
        return symbols
    except Exception as e:
        print(f"❌ S&P500 로드 실패: {e}")
        return ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","JPM","TSLA","UNH","V"]

# ====================================================
# ✅ HV 계산
# ====================================================
def calc_hv(closes: pd.Series, period: int):
    try:
        if len(closes) < period + 1:
            return None
        log_returns = np.log(closes / closes.shift(1)).dropna().tail(period)
        return round(float(log_returns.std() * np.sqrt(252)), 4)
    except Exception:
        return None

# ====================================================
# ✅ RSI 계산 (NaN 방지)
# ====================================================
def calc_rsi(closes: pd.Series, period: int = 14):
    try:
        delta = closes.diff().dropna()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss
        rsi   = (100 - (100 / (1 + rs))).iloc[-1]
        return round(float(rsi), 2) if not np.isnan(rsi) else None
    except Exception:
        return None

# ====================================================
# ✅ 만기일 파싱 → DTE 계산
# ====================================================
def days_to_expiry(symbol: str) -> int:
    try:
        exp_str  = symbol[-15:-9]
        exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        return (exp_date - today_date).days
    except Exception:
        return -1

# ====================================================
# ✅ yfinance로 OI/PCR/MaxPain 계산
# ====================================================
def calc_oi_metrics(ticker, target_dte_min=25, target_dte_max=50):
    """
    yfinance option_chain으로 OI/Volume/PCR/MaxPain 계산
    Alpaca 무료플랜은 OI 미제공 → yfinance로 대체
    """
    try:
        exps = ticker.options
        if not exps:
            return None

        # 30~45일 만기 찾기
        target_exp = None
        for exp in exps:
            dte = (pd.Timestamp(exp).date() - today_date).days
            if target_dte_min <= dte <= target_dte_max:
                target_exp = exp
                break

        if not target_exp:
            return None

        chain    = ticker.option_chain(target_exp)
        calls    = chain.calls
        puts     = chain.puts

        call_oi  = int(calls["openInterest"].fillna(0).sum())
        put_oi   = int(puts["openInterest"].fillna(0).sum())
        call_vol = int(calls["volume"].fillna(0).sum())
        put_vol  = int(puts["volume"].fillna(0).sum())

        pcr_oi  = round(put_oi  / call_oi,  4) if call_oi  > 0 else None
        pcr_vol = round(put_vol / call_vol, 4) if call_vol > 0 else None

        # Max Pain 계산
        max_pain = None
        try:
            strikes = {}
            for _, row in calls.iterrows():
                s = row["strike"]
                oi = row["openInterest"] if not pd.isna(row["openInterest"]) else 0
                strikes.setdefault(s, {"call_oi": 0, "put_oi": 0})
                strikes[s]["call_oi"] += oi
            for _, row in puts.iterrows():
                s = row["strike"]
                oi = row["openInterest"] if not pd.isna(row["openInterest"]) else 0
                strikes.setdefault(s, {"call_oi": 0, "put_oi": 0})
                strikes[s]["put_oi"] += oi

            min_pain = float("inf")
            for test_price in sorted(strikes.keys()):
                pain = 0
                for s, d in strikes.items():
                    if test_price > s: pain += (test_price - s) * d["call_oi"]
                    if test_price < s: pain += (s - test_price) * d["put_oi"]
                if pain < min_pain:
                    min_pain  = pain
                    max_pain  = test_price
        except Exception:
            max_pain = None

        return {
            "call_oi":  call_oi,
            "put_oi":   put_oi,
            "call_vol": call_vol,
            "put_vol":  put_vol,
            "pcr_oi":   pcr_oi,
            "pcr_vol":  pcr_vol,
            "max_pain": max_pain,
        }
    except Exception:
        return None

# ====================================================
# ✅ GEX 계산 (Alpaca 그릭스 + yfinance OI 조합)
# ====================================================
def calc_gex(options: list, spot_price: float):
    try:
        gex_total = gex_call = gex_put = 0.0
        valid = 0
        for opt in options:
            greeks   = getattr(opt, "greeks", None)
            oi_raw   = getattr(opt, "open_interest", None)
            opt_type = opt.symbol[-9]
            if greeks is None or oi_raw is None:
                continue
            gamma = getattr(greeks, "gamma", None)
            if gamma is None:
                continue
            oi      = int(oi_raw)
            gex_val = float(gamma) * oi * 100 * (spot_price ** 2) * 0.01
            if opt_type == "C":
                gex_call  += gex_val
                gex_total += gex_val
            elif opt_type == "P":
                gex_put   += gex_val
                gex_total -= gex_val
            valid += 1
        if valid == 0:
            return None, None, None
        return round(gex_total, 2), round(gex_call, 2), round(gex_put, 2)
    except Exception:
        return None, None, None

# ====================================================
# ✅ 단일 종목 전체 데이터 수집
# ====================================================
def collect_data(symbol: str):
    try:
        yf_symbol = symbol.replace("-", ".")
        ticker    = yf.Ticker(yf_symbol)
        hist      = ticker.history(period="1y")

        hv10 = hv20 = hv60 = None
        rsi  = beta = week52_pos = vol_ratio = None
        ma20 = ma50 = ma200 = price_vs_ma200 = golden_cross = None
        ret_1d = ret_5d = ret_20d = atr14 = cur_price = None
        days_to_earn = None

        if len(hist) > 60:
            closes  = hist["Close"]
            volumes = hist["Volume"]
            high    = hist["High"]
            low     = hist["Low"]

            # ✅ dropna()로 NaN 방지
            closes_clean = closes.dropna()
            if len(closes_clean) == 0:
                return None

            cur_price = round(float(closes_clean.iloc[-1]), 4)

            hv10 = calc_hv(closes_clean, 10)
            hv20 = calc_hv(closes_clean, 20)
            hv60 = calc_hv(closes_clean, 60)
            rsi  = calc_rsi(closes_clean, 14)

            high52 = closes_clean.tail(252).max()
            low52  = closes_clean.tail(252).min()
            if high52 != low52:
                week52_pos = round((cur_price - low52) / (high52 - low52) * 100, 2)

            avg_vol   = volumes.tail(20).mean()
            vol_ratio = round(float(volumes.iloc[-1] / avg_vol), 2) if avg_vol > 0 else None

            if len(closes_clean) >= 200:
                ma20  = round(float(closes_clean.tail(20).mean()),  2)
                ma50  = round(float(closes_clean.tail(50).mean()),  2)
                ma200 = round(float(closes_clean.tail(200).mean()), 2)
                price_vs_ma200 = round((cur_price - ma200) / ma200 * 100, 2)
                golden_cross   = int(ma50 > ma200)

            # ✅ FutureWarning 수정 + NaN 체크
            def safe_pct(s, n):
                v = s.pct_change(n, fill_method=None).iloc[-1]
                return round(float(v) * 100, 2) if not np.isnan(v) else None

            ret_1d  = safe_pct(closes_clean, 1)
            ret_5d  = safe_pct(closes_clean, 5)
            ret_20d = safe_pct(closes_clean, 20)

            tr    = pd.concat([
                high - low,
                (high - closes.shift()).abs(),
                (low  - closes.shift()).abs()
            ], axis=1).max(axis=1)
            atr_val = tr.rolling(14).mean().iloc[-1]
            atr14   = round(float(atr_val), 4) if not np.isnan(atr_val) else None

        # 베타
        try:
            info = ticker.fast_info
            beta = getattr(info, "beta3_year", None) or getattr(info, "beta", None)
            beta = round(float(beta), 3) if beta else None
        except Exception:
            beta = None

        # 어닝까지 남은 일수
        try:
            cal = ticker.calendar
            if cal is not None and "Earnings Date" in cal:
                earn_date = cal["Earnings Date"]
                if isinstance(earn_date, list) and earn_date:
                    days_to_earn = (pd.Timestamp(earn_date[0]).date() - today_date).days
        except Exception:
            days_to_earn = None

        # ✅ yfinance OI/PCR/MaxPain
        oi_metrics = calc_oi_metrics(ticker)
        call_oi  = oi_metrics["call_oi"]  if oi_metrics else 0
        put_oi   = oi_metrics["put_oi"]   if oi_metrics else 0
        call_vol = oi_metrics["call_vol"] if oi_metrics else 0
        put_vol  = oi_metrics["put_vol"]  if oi_metrics else 0
        pcr_oi   = oi_metrics["pcr_oi"]   if oi_metrics else None
        pcr_vol  = oi_metrics["pcr_vol"]  if oi_metrics else None
        max_pain = oi_metrics["max_pain"] if oi_metrics else None

        # ── Alpaca 옵션 체인 (IV + 그릭스) ──────────────
        alpaca_symbol = symbol.replace("-", ".")
        req     = OptionChainRequest(underlying_symbol=alpaca_symbol)
        chain   = client.get_option_chain(req)
        options = list(chain.values())
        if not options:
            return None

        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
        if not filtered:
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]
        if not filtered:
            return None

        # ── 그릭스 + IV 수집 ──────────────────────────
        call_ivs = []
        put_ivs  = []
        call_deltas = []
        put_deltas  = []
        gammas = []
        thetas = []
        vegas  = []
        rhos   = []

        for opt in filtered:
            iv       = getattr(opt, "implied_volatility", None)
            opt_type = opt.symbol[-9]
            greeks   = getattr(opt, "greeks", None)

            if greeks is not None:
                delta = getattr(greeks, "delta", None)
                gamma = getattr(greeks, "gamma", None)
                theta = getattr(greeks, "theta", None)
                vega  = getattr(greeks, "vega",  None)
                rho   = getattr(greeks, "rho",   None)

                if gamma is not None: gammas.append(float(gamma))
                if theta is not None: thetas.append(float(theta))
                if vega  is not None: vegas.append(float(vega))
                if rho   is not None: rhos.append(float(rho))

                if delta is not None and (0.4 <= abs(float(delta)) <= 0.6):
                    if iv:
                        if opt_type == "C":
                            call_ivs.append(float(iv))
                            call_deltas.append(float(delta))
                        elif opt_type == "P":
                            put_ivs.append(float(iv))
                            put_deltas.append(abs(float(delta)))
            else:
                if iv:
                    if opt_type == "C": call_ivs.append(float(iv))
                    elif opt_type == "P": put_ivs.append(float(iv))

        all_ivs = call_ivs + put_ivs
        if not all_ivs:
            all_ivs = [
                float(opt.implied_volatility)
                for opt in filtered
                if getattr(opt, "implied_volatility", None)
            ]
        if not all_ivs:
            return None

        avg_call = round(sum(call_ivs)/len(call_ivs), 4) if call_ivs else None
        avg_put  = round(sum(put_ivs) /len(put_ivs),  4) if put_ivs  else None
        avg_iv   = round(sum(all_ivs) /len(all_ivs),  4)
        skew     = round(avg_put - avg_call, 4) if (avg_call and avg_put) else None
        iv_hv_diff = round(avg_iv - hv20, 4) if hv20 else None

        avg_gamma = round(sum(gammas)/len(gammas), 6) if gammas else None
        avg_theta = round(sum(thetas)/len(thetas), 6) if thetas else None
        avg_vega  = round(sum(vegas) /len(vegas),  6) if vegas  else None
        avg_rho   = round(sum(rhos)  /len(rhos),   6) if rhos   else None
        avg_delta = None
        if call_deltas or put_deltas:
            all_d = call_deltas + put_deltas
            avg_delta = round(sum(all_d)/len(all_d), 4)

        # IV Term Structure
        iv_30 = iv_45 = iv_60 = None
        bucket = {30: [], 45: [], 60: []}
        for opt in options:
            dte = days_to_expiry(opt.symbol)
            iv  = getattr(opt, "implied_volatility", None)
            if not iv: continue
            if 25 <= dte <= 35:   bucket[30].append(float(iv))
            elif 40 <= dte <= 50: bucket[45].append(float(iv))
            elif 55 <= dte <= 65: bucket[60].append(float(iv))
        if bucket[30]: iv_30 = round(sum(bucket[30])/len(bucket[30]), 4)
        if bucket[45]: iv_45 = round(sum(bucket[45])/len(bucket[45]), 4)
        if bucket[60]: iv_60 = round(sum(bucket[60])/len(bucket[60]), 4)
        iv_term_slope = round(iv_60 - iv_30, 4) if (iv_30 and iv_60) else None

        # Max Pain 대비 현재가
        pain_diff = None
        if max_pain and cur_price:
            pain_diff = round((cur_price - max_pain) / cur_price * 100, 2)

        # GEX (Alpaca 그릭스 기반 - OI 있을 때만 계산됨)
        spot = cur_price or 0.0
        gex_total, gex_call, gex_put = calc_gex(options, spot) if spot > 0 else (None, None, None)

        return {
            "date":           today,
            "symbol":         symbol,
            "dte_range":      "30-45",
            "cur_price":      cur_price,
            "avg_iv":         avg_iv,
            "atm_call_iv":    avg_call,
            "atm_put_iv":     avg_put,
            "skew":           skew,
            "iv_hv_diff":     iv_hv_diff,
            "iv_30d":         iv_30,
            "iv_45d":         iv_45,
            "iv_60d":         iv_60,
            "iv_term_slope":  iv_term_slope,
            "hv10":           hv10,
            "hv20":           hv20,
            "hv60":           hv60,
            "avg_delta":      avg_delta,
            "avg_gamma":      avg_gamma,
            "avg_theta":      avg_theta,
            "avg_vega":       avg_vega,
            "avg_rho":        avg_rho,
            "gex":            gex_total,
            "gex_call":       gex_call,
            "gex_put":        gex_put,
            "pcr_oi":         pcr_oi,
            "pcr_vol":        pcr_vol,
            "call_oi":        call_oi,
            "put_oi":         put_oi,
            "max_pain":       max_pain,
            "pain_diff":      pain_diff,
            "rsi14":          rsi,
            "beta":           beta,
            "week52_pos":     week52_pos,
            "vol_ratio":      vol_ratio,
            "ret_1d":         ret_1d,
            "ret_5d":         ret_5d,
            "ret_20d":        ret_20d,
            "atr14":          atr14,
            "ma20":           ma20,
            "ma50":           ma50,
            "ma200":          ma200,
            "price_vs_ma200": price_vs_ma200,
            "golden_cross":   golden_cross,
            "days_to_earn":   days_to_earn,
            "sample_count":   len(all_ivs),
        }

    except Exception as e:
        print(f"  ❌ {symbol} 에러: {e}")
        return None

# ====================================================
# ✅ 시장 전체 데이터 수집
# ====================================================
SECTOR_ETFS = {
    "XLK": "tech",      "XLF": "fin",       "XLV": "health",
    "XLE": "energy",    "XLI": "indus",     "XLY": "cons_disc",
    "XLP": "cons_stap", "XLU": "util",      "XLB": "material",
    "XLRE":"realestate","XLC": "comm",
}
MARKET_TICKERS = {
    "^VIX": "vix", "^VIX9D": "vix9d", "^VIX3M": "vix3m",
    "SPY":  "spy", "QQQ":    "qqq",   "IWM":    "iwm",
    "TLT":  "tlt", "^TNX":   "tnx",   "UUP":    "uup",
    "GLD":  "gld",
}

def safe_pct_val(s, n):
    try:
        v = s.dropna().pct_change(n, fill_method=None).iloc[-1]
        return round(float(v) * 100, 2) if not np.isnan(v) else None
    except Exception:
        return None

def collect_market_data():
    try:
        row = {"date": today}

        for ticker_sym, col in MARKET_TICKERS.items():
            try:
                hist = yf.Ticker(ticker_sym).history(period="60d")
                if hist.empty: continue
                closes = hist["Close"].dropna()
                if len(closes) == 0: continue
                row[f"{col}_close"] = round(float(closes.iloc[-1]), 4)
                row[f"{col}_ret1d"] = safe_pct_val(closes, 1)
                row[f"{col}_ret5d"] = safe_pct_val(closes, 5)
            except Exception:
                pass

        v9d = row.get("vix9d_close")
        v   = row.get("vix_close")
        v3m = row.get("vix3m_close")
        if v9d and v and v3m:
            row["vix_term_spread"]   = round(v3m - v9d, 4)
            row["vix_backwardation"] = int(v9d > v)
            row["vix_above20"]       = int(v >= 20)
            row["vix_above30"]       = int(v >= 30)

        try:
            spy_hist = yf.Ticker("SPY").history(period="1y")["Close"].dropna()
            ma50  = spy_hist.tail(50).mean()
            ma200 = spy_hist.tail(200).mean()
            row["spy_golden_cross"]   = int(ma50 > ma200)
            row["spy_price_vs_ma200"] = round((spy_hist.iloc[-1] - ma200) / ma200 * 100, 2)
        except Exception:
            pass

        try:
            spy   = yf.Ticker("SPY")
            chain = spy.option_chain(spy.options[0])
            row["spy_pcr_vol"] = round(
                chain.puts["volume"].sum() / chain.calls["volume"].sum(), 4
            )
        except Exception:
            pass

        for etf, name in SECTOR_ETFS.items():
            try:
                hist = yf.Ticker(etf).history(period="60d")["Close"].dropna()
                row[f"sec_{name}_ret1d"] = safe_pct_val(hist, 1)
                row[f"sec_{name}_ret5d"] = safe_pct_val(hist, 5)
            except Exception:
                pass

        return row
    except Exception as e:
        print(f"❌ 시장 데이터 수집 실패: {e}")
        return None

# ====================================================
# ✅ CSV 저장 (연도별 파일 분리)
# ====================================================
IV_COL_ORDER = [
    "date", "symbol", "dte_range", "cur_price",
    "avg_iv", "atm_call_iv", "atm_put_iv", "skew", "iv_hv_diff",
    "iv_30d", "iv_45d", "iv_60d", "iv_term_slope",
    "hv10", "hv20", "hv60",
    "avg_delta", "avg_gamma", "avg_theta", "avg_vega", "avg_rho",
    "gex", "gex_call", "gex_put",
    "pcr_oi", "pcr_vol", "call_oi", "put_oi",
    "max_pain", "pain_diff",
    "rsi14", "beta", "week52_pos", "vol_ratio",
    "ret_1d", "ret_5d", "ret_20d", "atr14",
    "ma20", "ma50", "ma200", "price_vs_ma200", "golden_cross",
    "days_to_earn", "sample_count",
]

def save_csv(results: list, col_order: list, base_name: str):
    file_path = f"{base_name}_{today_date.year}.csv"
    df_new    = pd.DataFrame(results)
    for col in col_order:
        if col not in df_new.columns:
            df_new[col] = None
    df_new = df_new[col_order]
    if os.path.exists(file_path):
        df_existing = pd.read_csv(file_path)
        for col in col_order:
            if col not in df_existing.columns:
                df_existing[col] = None
        df_existing = df_existing[df_existing["date"] != today]
        df_new = pd.concat([df_existing, df_new[col_order]], ignore_index=True)
    df_new.to_csv(file_path, index=False)
    print(f"✅ 저장 완료: {file_path} ({len(df_new)}행)")

# ====================================================
# ✅ 메인 루프
# ====================================================
symbols    = get_sp500_symbols()
results    = []
failed     = []
start_time = time.time()

for i, symbol in enumerate(symbols):
    print(f"[{i+1}/{len(symbols)}] {symbol} 수집 중...")
    row = collect_data(symbol)
    if row:
        results.append(row)
        print(
            f"  ✅ iv={row['avg_iv']} | hv20={row['hv20']} | "
            f"skew={row['skew']} | pcr={row['pcr_oi']} | "
            f"pain={row['max_pain']} | rsi={row['rsi14']}"
        )
    else:
        failed.append(symbol)
    time.sleep(0.3)

elapsed = round(time.time() - start_time)

if results:
    save_csv(results, IV_COL_ORDER, "iv_data")

if failed:
    with open("failed_symbols.txt", "w") as f:
        f.write("\n".join(failed))

print("\n📡 시장 전체 데이터 수집 중...")
market_row = collect_market_data()
if market_row:
    market_cols = ["date"] + [k for k in market_row.keys() if k != "date"]
    save_csv([market_row], market_cols, "market_data")
    print(
        f"   VIX={market_row.get('vix_close')} | "
        f"SPY={market_row.get('spy_close')} | "
        f"QQQ={market_row.get('qqq_close')} | "
        f"VIX Term={market_row.get('vix_term_spread')}"
    )

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
success_count = len(results)
fail_count    = len(failed)

if success_count > 0:
    msg = (
        f"📊 <b>IV 데이터 수집 완료</b>\n"
        f"📅 날짜: {today}\n"
        f"✅ 성공: {success_count}개 종목\n"
        f"❌ 실패: {fail_count}개 종목\n"
        f"⏱ 소요시간: {elapsed//60}분 {elapsed%60}초\n"
        f"📈 수집항목: IV/HV/Skew/Greeks/PCR/MaxPain/RSI/MA/ATR/어닝"
    )
    if fail_count > 0:
        msg += f"\n⚠️ 실패: {', '.join(failed[:10])}"
        if fail_count > 10:
            msg += f" 외 {fail_count-10}개"
else:
    msg = f"❌ <b>IV 데이터 수집 실패</b>\n📅 날짜: {today}"

send_telegram(msg)
print("=== IV Data Collector DONE ===")
