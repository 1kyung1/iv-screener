import os
import requests
import pandas as pd
import yfinance as yf
import numpy as np
from datetime import datetime, date
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
import time

print("=== IV Data Collector START ===")

# ====================================================
# ✅ 주말/공휴일 체크
# ====================================================
today_date = date.today()
today      = today_date.strftime("%Y-%m-%d")
weekday    = today_date.weekday()

if weekday >= 5:
    print("📅 오늘은 주말이라 스킵합니다.")
    exit(0)

US_HOLIDAYS = [
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
    "2026-05-25","2026-07-03","2026-09-07","2026-11-26",
    "2026-11-27","2026-12-25",
]
if today in US_HOLIDAYS:
    print("🎉 오늘은 미국 공휴일이라 스킵합니다.")
    exit(0)

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
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
# ====================================================
def get_sp500_symbols():
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        sp500   = pd.read_html(resp.text, flavor="html5lib")[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"✅ S&P 500 종목 수: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"❌ S&P500 로드 실패: {e}")
        return ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","JPM","TSLA","UNH","V"]

# ====================================================
# ✅ HV 계산 (10일/20일/60일)
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
# ✅ 만기일 파싱
# ====================================================
def days_to_expiry(symbol: str) -> int:
    try:
        exp_str  = symbol[-15:-9]
        exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        return (exp_date - today_date).days
    except Exception:
        return -1

# ====================================================
# ✅ Max Pain 계산
# ====================================================
def calc_max_pain(options: list):
    try:
        strikes = {}
        for opt in options:
            strike   = getattr(opt, "strike_price", None)
            oi       = getattr(opt, "open_interest", None)
            opt_type = opt.symbol[-9]
            if not strike or not oi:
                continue
            if strike not in strikes:
                strikes[strike] = {"call_oi": 0, "put_oi": 0}
            if opt_type == "C":
                strikes[strike]["call_oi"] += oi
            elif opt_type == "P":
                strikes[strike]["put_oi"] += oi

        if not strikes:
            return None

        strike_list   = sorted(strikes.keys())
        min_pain      = float("inf")
        max_pain_price = None

        for test_price in strike_list:
            pain = 0
            for s, oi_data in strikes.items():
                if test_price > s:
                    pain += (test_price - s) * oi_data["call_oi"]
                if test_price < s:
                    pain += (s - test_price) * oi_data["put_oi"]
            if pain < min_pain:
                min_pain       = pain
                max_pain_price = test_price

        return max_pain_price
    except Exception:
        return None

# ====================================================
# ✅ GEX 계산 (Gamma Exposure)
# ====================================================
def calc_gex(options: list, spot_price: float):
    """
    GEX = Σ gamma × OI × 100 × spot²  × 0.01
    콜은 양수(+), 풋은 음수(-) → 합산
    양수 GEX: 시장조성자가 변동성 억제
    음수 GEX: 시장조성자가 변동성 증폭
    """
    try:
        gex_total     = 0.0
        gex_call      = 0.0
        gex_put       = 0.0
        valid_count   = 0

        for opt in options:
            greeks   = getattr(opt, "greeks", None)
            oi       = getattr(opt, "open_interest", None)
            opt_type = opt.symbol[-9]

            if not greeks or not oi:
                continue

            gamma = getattr(greeks, "gamma", None)
            if gamma is None:
                continue

            gex_val = float(gamma) * float(oi) * 100 * (spot_price ** 2) * 0.01

            if opt_type == "C":
                gex_call  += gex_val
                gex_total += gex_val
            elif opt_type == "P":
                gex_put   += gex_val
                gex_total -= gex_val   # 풋은 음수
            valid_count += 1

        if valid_count == 0:
            return None, None, None

        return (
            round(gex_total, 2),
            round(gex_call,  2),
            round(gex_put,   2),
        )
    except Exception:
        return None, None, None

# ====================================================
# ✅ 단일 종목 전체 데이터 수집
# ====================================================
def collect_data(symbol: str):
    try:
        # ── yfinance 데이터 ───────────────────────────────
        yf_symbol = symbol.replace("-", ".")
        ticker    = yf.Ticker(yf_symbol)
        hist      = ticker.history(period="1y")

        hv10 = hv20 = hv60 = None
        rsi  = beta = week52_pos = vol_ratio = None
        ma20 = ma50 = ma200 = price_vs_ma200 = None
        golden_cross = None
        ret_1d = ret_5d = ret_20d = None
        atr14  = None
        cur_price     = None
        days_to_earn  = None

        if len(hist) > 60:
            closes  = hist["Close"]
            volumes = hist["Volume"]
            high    = hist["High"]
            low     = hist["Low"]
            cur_price = round(float(closes.iloc[-1]), 4)

            # HV
            hv10 = calc_hv(closes, 10)
            hv20 = calc_hv(closes, 20)
            hv60 = calc_hv(closes, 60)

            # RSI 14일
            delta_p = closes.diff()
            gain    = delta_p.clip(lower=0).rolling(14).mean()
            loss    = (-delta_p.clip(upper=0)).rolling(14).mean()
            rsi     = round(float((100 - (100 / (1 + gain / loss))).iloc[-1]), 2)

            # 52주 위치
            high52 = closes.tail(252).max()
            low52  = closes.tail(252).min()
            if high52 != low52:
                week52_pos = round((cur_price - low52) / (high52 - low52) * 100, 2)

            # 거래량 이상
            avg_vol   = volumes.tail(20).mean()
            vol_ratio = round(float(volumes.iloc[-1] / avg_vol), 2) if avg_vol > 0 else None

            # 이동평균
            if len(closes) >= 200:
                ma20  = round(float(closes.tail(20).mean()),  2)
                ma50  = round(float(closes.tail(50).mean()),  2)
                ma200 = round(float(closes.tail(200).mean()), 2)
                price_vs_ma200 = round((cur_price - ma200) / ma200 * 100, 2)
                golden_cross   = int(ma50 > ma200)

            # 수익률
            ret_1d  = round(float(closes.pct_change(1).iloc[-1]  * 100), 2)
            ret_5d  = round(float(closes.pct_change(5).iloc[-1]  * 100), 2)
            ret_20d = round(float(closes.pct_change(20).iloc[-1] * 100), 2)

            # ATR 14일
            tr   = pd.concat([
                high - low,
                (high - closes.shift()).abs(),
                (low  - closes.shift()).abs()
            ], axis=1).max(axis=1)
            atr14 = round(float(tr.rolling(14).mean().iloc[-1]), 4)

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

        # ── Alpaca 옵션 체인 ──────────────────────────────
        alpaca_symbol = symbol.replace("-", ".")
        req     = OptionChainRequest(underlying_symbol=alpaca_symbol)
        chain   = client.get_option_chain(req)
        options = list(chain.values())
        if not options:
            return None

        # 30~45일 만기 필터
        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
        if not filtered:
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]
        if not filtered:
            return None

        # ── 그릭스 + IV 수집 ──────────────────────────────
        call_ivs    = []
        put_ivs     = []
        call_deltas = []
        put_deltas  = []
        gammas      = []
        thetas      = []
        vegas       = []
        rhos        = []

        call_oi_total  = put_oi_total  = 0
        call_vol_total = put_vol_total = 0

        for opt in filtered:
            iv       = getattr(opt, "implied_volatility", None)
            oi       = getattr(opt, "open_interest", None) or 0
            vol      = getattr(opt, "volume", None) or 0
            opt_type = opt.symbol[-9]
            greeks   = getattr(opt, "greeks", None)

            # OI / Volume 합산
            if opt_type == "C":
                call_oi_total  += oi
                call_vol_total += vol
            elif opt_type == "P":
                put_oi_total  += oi
                put_vol_total += vol

            # 그릭스
            if greeks:
                delta = getattr(greeks, "delta", None)
                gamma = getattr(greeks, "gamma", None)
                theta = getattr(greeks, "theta", None)
                vega  = getattr(greeks, "vega",  None)
                rho   = getattr(greeks, "rho",   None)

                if gamma is not None:
                    gammas.append(float(gamma))
                if theta is not None:
                    thetas.append(float(theta))
                if vega is not None:
                    vegas.append(float(vega))
                if rho is not None:
                    rhos.append(float(rho))

                # ATM 필터 (delta 0.4~0.6)
                if delta is not None and (0.4 <= abs(float(delta)) <= 0.6):
                    if iv:
                        if opt_type == "C":
                            call_ivs.append(float(iv))
                            call_deltas.append(float(delta))
                        elif opt_type == "P":
                            put_ivs.append(float(iv))
                            put_deltas.append(abs(float(delta)))
            else:
                # 그릭스 없으면 delta 대신 IV만 수집
                if iv:
                    if opt_type == "C":
                        call_ivs.append(float(iv))
                    elif opt_type == "P":
                        put_ivs.append(float(iv))

        all_ivs = call_ivs + put_ivs
        if not all_ivs:
            all_ivs = [float(opt.implied_volatility)
                       for opt in filtered if getattr(opt, "implied_volatility", None)]
        if not all_ivs:
            return None

        avg_call   = round(sum(call_ivs) / len(call_ivs), 4) if call_ivs else None
        avg_put    = round(sum(put_ivs)  / len(put_ivs),  4) if put_ivs  else None
        avg_iv     = round(sum(all_ivs)  / len(all_ivs),  4)
        skew       = round(avg_put - avg_call, 4) if (avg_call and avg_put) else None
        iv_hv_diff = round(avg_iv - hv20, 4)      if hv20                  else None

        # 그릭스 평균
        avg_gamma = round(sum(gammas) / len(gammas), 6) if gammas else None
        avg_theta = round(sum(thetas) / len(thetas), 6) if thetas else None
        avg_vega  = round(sum(vegas)  / len(vegas),  6) if vegas  else None
        avg_rho   = round(sum(rhos)   / len(rhos),   6) if rhos   else None
        avg_delta = round(
            (sum(call_deltas) + sum(put_deltas)) /
            (len(call_deltas) + len(put_deltas)), 4
        ) if (call_deltas or put_deltas) else None

        # ── IV Term Structure ─────────────────────────────
        iv_30 = iv_45 = iv_60 = None
        bucket = {30: [], 45: [], 60: []}
        for opt in options:
            dte = days_to_expiry(opt.symbol)
            iv  = getattr(opt, "implied_volatility", None)
            if not iv:
                continue
            if 25 <= dte <= 35:   bucket[30].append(float(iv))
            elif 40 <= dte <= 50: bucket[45].append(float(iv))
            elif 55 <= dte <= 65: bucket[60].append(float(iv))
        if bucket[30]: iv_30 = round(sum(bucket[30]) / len(bucket[30]), 4)
        if bucket[45]: iv_45 = round(sum(bucket[45]) / len(bucket[45]), 4)
        if bucket[60]: iv_60 = round(sum(bucket[60]) / len(bucket[60]), 4)

        # IV Term Structure 기울기 (contango/backwardation)
        iv_term_slope = None
        if iv_30 and iv_60:
            iv_term_slope = round(iv_60 - iv_30, 4)   # 양수=정상, 음수=역전

        # ── Put/Call Ratio ────────────────────────────────
        pcr_oi  = round(put_oi_total  / call_oi_total,  4) if call_oi_total  > 0 else None
        pcr_vol = round(put_vol_total / call_vol_total, 4) if call_vol_total > 0 else None

        # ── Max Pain ──────────────────────────────────────
        max_pain = calc_max_pain(filtered)

        # ── GEX (전체 옵션 기준) ──────────────────────────
        spot = cur_price or 0
        gex_total, gex_call, gex_put = calc_gex(options, spot) if spot else (None, None, None)

        # max_pain 대비 현재가 괴리
        pain_diff = None
        if max_pain and cur_price:
            pain_diff = round((cur_price - max_pain) / cur_price * 100, 2)

        return {
            "date":           today,
            "symbol":         symbol,
            "dte_range":      "30-45",

            # 현재가
            "cur_price":      cur_price,

            # IV
            "avg_iv":         avg_iv,
            "atm_call_iv":    avg_call,
            "atm_put_iv":     avg_put,
            "skew":           skew,
            "iv_hv_diff":     iv_hv_diff,

            # Term Structure
            "iv_30d":         iv_30,
            "iv_45d":         iv_45,
            "iv_60d":         iv_60,
            "iv_term_slope":  iv_term_slope,    # 양수=정상, 음수=역전

            # HV
            "hv10":           hv10,
            "hv20":           hv20,
            "hv60":           hv60,

            # 그릭스 (ATM 평균)
            "avg_delta":      avg_delta,
            "avg_gamma":      avg_gamma,
            "avg_theta":      avg_theta,
            "avg_vega":       avg_vega,
            "avg_rho":        avg_rho,

            # GEX
            "gex":            gex_total,        # 전체 GEX (양수=억제, 음수=증폭)
            "gex_call":       gex_call,         # 콜 GEX
            "gex_put":        gex_put,          # 풋 GEX

            # Put/Call
            "pcr_oi":         pcr_oi,
            "pcr_vol":        pcr_vol,
            "call_oi":        call_oi_total,
            "put_oi":         put_oi_total,

            # Max Pain
            "max_pain":       max_pain,
            "pain_diff":      pain_diff,        # 현재가 vs max_pain 괴리 %

            # 주가 지표
            "rsi14":          rsi,
            "beta":           beta,
            "week52_pos":     week52_pos,
            "vol_ratio":      vol_ratio,
            "ret_1d":         ret_1d,
            "ret_5d":         ret_5d,
            "ret_20d":        ret_20d,
            "atr14":          atr14,

            # 이동평균
            "ma20":           ma20,
            "ma50":           ma50,
            "ma200":          ma200,
            "price_vs_ma200": price_vs_ma200,
            "golden_cross":   golden_cross,

            # 이벤트
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
    "XLK": "tech", "XLF": "fin", "XLV": "health",
    "XLE": "energy", "XLI": "indus", "XLY": "cons_disc",
    "XLP": "cons_stap", "XLU": "util", "XLB": "material",
    "XLRE": "realestate", "XLC": "comm",
}

MARKET_TICKERS = {
    "^VIX": "vix", "^VIX9D": "vix9d", "^VIX3M": "vix3m",
    "SPY": "spy", "QQQ": "qqq", "IWM": "iwm",
    "TLT": "tlt", "^TNX": "tnx", "UUP": "uup", "GLD": "gld",
}

def collect_market_data():
    try:
        row = {"date": today}

        for ticker_sym, col in MARKET_TICKERS.items():
            try:
                hist = yf.Ticker(ticker_sym).history(period="60d")
                if hist.empty:
                    continue
                closes = hist["Close"]
                cur    = round(float(closes.iloc[-1]), 4)
                row[f"{col}_close"] = cur
                row[f"{col}_ret1d"] = round(float(closes.pct_change(1).iloc[-1] * 100), 2)
                row[f"{col}_ret5d"] = round(float(closes.pct_change(5).iloc[-1] * 100), 2)
            except Exception:
                pass

        # VIX 파생 지표
        v9d = row.get("vix9d_close")
        v   = row.get("vix_close")
        v3m = row.get("vix3m_close")
        if v9d and v and v3m:
            row["vix_term_spread"]    = round(v3m - v9d, 4)
            row["vix_backwardation"]  = int(v9d > v)
            row["vix_above20"]        = int(v >= 20)
            row["vix_above30"]        = int(v >= 30)

        # SPY 이평선
        try:
            spy_hist = yf.Ticker("SPY").history(period="1y")["Close"]
            ma50     = spy_hist.tail(50).mean()
            ma200    = spy_hist.tail(200).mean()
            row["spy_golden_cross"]    = int(ma50 > ma200)
            row["spy_price_vs_ma200"]  = round((spy_hist.iloc[-1] - ma200) / ma200 * 100, 2)
        except Exception:
            pass

        # SPY P/C Ratio
        try:
            spy   = yf.Ticker("SPY")
            chain = spy.option_chain(spy.options[0])
            row["spy_pcr_vol"] = round(
                chain.puts["volume"].sum() / chain.calls["volume"].sum(), 4
            )
        except Exception:
            pass

        # 섹터 ETF
        for etf, name in SECTOR_ETFS.items():
            try:
                hist = yf.Ticker(etf).history(period="60d")["Close"]
                row[f"sec_{name}_ret1d"] = round(float(hist.pct_change(1).iloc[-1] * 100), 2)
                row[f"sec_{name}_ret5d"] = round(float(hist.pct_change(5).iloc[-1] * 100), 2)
            except Exception:
                pass

        return row
    except Exception as e:
        print(f"❌ 시장 데이터 수집 실패: {e}")
        return None

# ====================================================
# ✅ CSV 파일명 (연도별 분리 → GitHub 100MB 제한 대응)
# ====================================================
def get_file_path(base_name: str) -> str:
    year = today_date.year
    return f"{base_name}_{year}.csv"

def save_csv(results: list, col_order: list, base_name: str):
    file_path = get_file_path(base_name)
    df_new    = pd.DataFrame(results)

    # 컬럼 순서 맞추기 (없는 컬럼은 None)
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
        df_new      = pd.concat([df_existing, df_new], ignore_index=True)

    df_new.to_csv(file_path, index=False)
    print(f"✅ 저장 완료: {file_path} ({len(df_new)}행)")

# ====================================================
# ✅ 컬럼 순서 정의
# ====================================================
IV_COL_ORDER = [
    "date", "symbol", "dte_range", "cur_price",
    # IV
    "avg_iv", "atm_call_iv", "atm_put_iv", "skew", "iv_hv_diff",
    # Term Structure
    "iv_30d", "iv_45d", "iv_60d", "iv_term_slope",
    # HV
    "hv10", "hv20", "hv60",
    # 그릭스
    "avg_delta", "avg_gamma", "avg_theta", "avg_vega", "avg_rho",
    # GEX
    "gex", "gex_call", "gex_put",
    # Put/Call
    "pcr_oi", "pcr_vol", "call_oi", "put_oi",
    # Max Pain
    "max_pain", "pain_diff",
    # 주가 지표
    "rsi14", "beta", "week52_pos", "vol_ratio",
    "ret_1d", "ret_5d", "ret_20d", "atr14",
    # 이동평균
    "ma20", "ma50", "ma200", "price_vs_ma200", "golden_cross",
    # 이벤트
    "days_to_earn",
    "sample_count",
]

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
            f"skew={row['skew']} | gex={row['gex']} | "
            f"gamma={row['avg_gamma']} | pcr={row['pcr_oi']} | "
            f"pain={row['max_pain']} | rsi={row['rsi14']}"
        )
    else:
        failed.append(symbol)
    time.sleep(0.3)

elapsed = round(time.time() - start_time)

# ── iv_data 저장 ──────────────────────────────────────
if results:
    save_csv(results, IV_COL_ORDER, "iv_data")

if failed:
    with open("failed_symbols.txt", "w") as f:
        f.write("\n".join(failed))

# ── market_data 저장 ──────────────────────────────────
print("\n📡 시장 전체 데이터 수집 중...")
market_row = collect_market_data()
if market_row:
    save_csv(
        [market_row],
        ["date"] + [k for k in market_row.keys() if k != "date"],
        "market_data"
    )
    print(
        f"   VIX={market_row.get('vix_close')} | "
        f"SPY={market_row.get('spy_close')} | "
        f"QQQ={market_row.get('qqq_close')} | "
        f"Term={market_row.get('vix_term_spread')}"
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
        f"📈 수집항목: IV/HV/Skew/Greeks/GEX/PCR/MaxPain/RSI/MA/ATR/어닝"
    )
    if fail_count > 0:
        msg += f"\n⚠️ 실패: {', '.join(failed[:10])}"
        if fail_count > 10:
            msg += f" 외 {fail_count-10}개"
else:
    msg = f"❌ <b>IV 데이터 수집 실패</b>\n📅 날짜: {today}"

send_telegram(msg)
print("=== IV Data Collector DONE ===")
