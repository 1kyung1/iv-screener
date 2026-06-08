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
today = today_date.strftime("%Y-%m-%d")
weekday = today_date.weekday()

if weekday >= 5:
    print(f"📅 오늘은 주말이라 스킵합니다.")
    exit(0)

US_HOLIDAYS = [
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
    "2026-05-25","2026-07-03","2026-09-07","2026-11-26",
    "2026-11-27","2026-12-25",
]
if today in US_HOLIDAYS:
    print(f"🎉 오늘은 미국 공휴일이라 스킵합니다.")
    exit(0)

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")

# ====================================================
# ✅ API 초기화
# ====================================================
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

# ====================================================
# ✅ S&P 500 리스트
# ====================================================
def get_sp500_symbols():
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        sp500 = pd.read_html(resp.text, flavor="html5lib")[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"✅ S&P 500 종목 수: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"❌ S&P500 로드 실패: {e}")
        return ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","JPM","TSLA","UNH","V"]

# ====================================================
# ✅ HV 계산 (10일/20일/60일)
# ====================================================
def calc_hv(closes: pd.Series, period: int) -> float | None:
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
        exp_str = symbol[-15:-9]
        exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        return (exp_date - today_date).days
    except Exception:
        return -1

# ====================================================
# ✅ Max Pain 계산
# ====================================================
def calc_max_pain(options: list) -> float | None:
    try:
        # 행사가별 OI 취합
        strikes = {}
        for opt in options:
            strike = getattr(opt, "strike_price", None)
            oi = getattr(opt, "open_interest", None)
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

        strike_list = sorted(strikes.keys())
        min_pain = float("inf")
        max_pain_price = None

        for test_price in strike_list:
            pain = 0
            for s, oi_data in strikes.items():
                # 콜 손실: 주가가 행사가 위면 콜 매도자 손실
                if test_price > s:
                    pain += (test_price - s) * oi_data["call_oi"]
                # 풋 손실: 주가가 행사가 아래면 풋 매도자 손실
                if test_price < s:
                    pain += (s - test_price) * oi_data["put_oi"]
            if pain < min_pain:
                min_pain = pain
                max_pain_price = test_price

        return max_pain_price
    except Exception:
        return None

# ====================================================
# ✅ 단일 종목 전체 데이터 수집
# ====================================================
def collect_data(symbol: str) -> dict | None:
    try:
        # ── yfinance 데이터 (HV, RSI, 베타, 52주, 거래량) ──
        yf_symbol = symbol.replace("-", ".")
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="1y")

        hv10 = hv20 = hv60 = None
        rsi = beta = week52_pos = vol_ratio = None

        if len(hist) > 60:
            closes = hist["Close"]

            # HV 10/20/60일
            hv10 = calc_hv(closes, 10)
            hv20 = calc_hv(closes, 20)
            hv60 = calc_hv(closes, 60)

            # RSI 14일
            delta_p = closes.diff()
            gain = delta_p.clip(lower=0).rolling(14).mean()
            loss = (-delta_p.clip(upper=0)).rolling(14).mean()
            rs = gain / loss
            rsi_series = 100 - (100 / (1 + rs))
            rsi = round(float(rsi_series.iloc[-1]), 2)

            # 52주 고저 위치 %
            high52 = closes.tail(252).max()
            low52  = closes.tail(252).min()
            cur    = closes.iloc[-1]
            if high52 != low52:
                week52_pos = round((cur - low52) / (high52 - low52) * 100, 2)

            # 거래량 이상 (오늘 거래량 / 20일 평균)
            volumes = hist["Volume"]
            avg_vol = volumes.tail(20).mean()
            if avg_vol > 0:
                vol_ratio = round(float(volumes.iloc[-1] / avg_vol), 2)

        # 베타 (yfinance info에서)
        try:
            info = ticker.fast_info
            beta = getattr(info, "beta3_year", None) or getattr(info, "beta", None)
            if beta:
                beta = round(float(beta), 3)
        except Exception:
            beta = None

        # ── Alpaca 옵션 체인 ──────────────────────────────
        alpaca_symbol = symbol.replace("-", ".")
        req = OptionChainRequest(underlying_symbol=alpaca_symbol)
        chain = client.get_option_chain(req)
        options = list(chain.values())
        if not options:
            return None

        # ── 30~45일 만기 필터 ────────────────────────────
        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
        if not filtered:
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]
        if not filtered:
            return None

        # ── ATM IV (콜/풋) ────────────────────────────────
        call_ivs, put_ivs = [], []
        call_oi_total = put_oi_total = 0
        call_vol_total = put_vol_total = 0

        for opt in filtered:
            iv = getattr(opt, "implied_volatility", None)
            oi = getattr(opt, "open_interest", None) or 0
            vol = getattr(opt, "volume", None) or 0
            opt_type = opt.symbol[-9]

            if opt_type == "C":
                call_oi_total  += oi
                call_vol_total += vol
            elif opt_type == "P":
                put_oi_total  += oi
                put_vol_total += vol

            if not iv:
                continue
            delta = getattr(opt, "delta", None)
            if delta is not None and not (0.4 <= abs(float(delta)) <= 0.6):
                continue
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

        avg_call = round(sum(call_ivs)/len(call_ivs), 4) if call_ivs else None
        avg_put  = round(sum(put_ivs) /len(put_ivs),  4) if put_ivs  else None
        avg_iv   = round(sum(all_ivs) /len(all_ivs),  4)
        skew     = round(avg_put - avg_call, 4) if (avg_call and avg_put) else None
        iv_hv_diff = round(avg_iv - hv20, 4) if hv20 else None

        # ── IV Term Structure (30/45/60일 각각) ──────────
        iv_30 = iv_45 = iv_60 = None
        bucket = {30: [], 45: [], 60: []}
        for opt in options:
            dte = days_to_expiry(opt.symbol)
            iv  = getattr(opt, "implied_volatility", None)
            if not iv:
                continue
            if 25 <= dte <= 35:
                bucket[30].append(float(iv))
            elif 40 <= dte <= 50:
                bucket[45].append(float(iv))
            elif 55 <= dte <= 65:
                bucket[60].append(float(iv))
        if bucket[30]: iv_30 = round(sum(bucket[30])/len(bucket[30]), 4)
        if bucket[45]: iv_45 = round(sum(bucket[45])/len(bucket[45]), 4)
        if bucket[60]: iv_60 = round(sum(bucket[60])/len(bucket[60]), 4)

        # ── Put/Call Ratio ────────────────────────────────
        pcr_oi  = round(put_oi_total  / call_oi_total,  4) if call_oi_total  > 0 else None
        pcr_vol = round(put_vol_total / call_vol_total, 4) if call_vol_total > 0 else None

        # ── Max Pain ──────────────────────────────────────
        max_pain = calc_max_pain(filtered)

        return {
            "date":         today,
            "symbol":       symbol,
            "dte_range":    "30-45",
            # IV
            "avg_iv":       avg_iv,
            "atm_call_iv":  avg_call,
            "atm_put_iv":   avg_put,
            "skew":         skew,
            # HV
            "hv10":         hv10,
            "hv20":         hv20,
            "hv60":         hv60,
            "iv_hv_diff":   iv_hv_diff,   # IV - HV20
            # Term Structure
            "iv_30d":       iv_30,
            "iv_45d":       iv_45,
            "iv_60d":       iv_60,
            # Put/Call
            "pcr_oi":       pcr_oi,       # OI 기준 P/C ratio
            "pcr_vol":      pcr_vol,      # 거래량 기준 P/C ratio
            # OI
            "call_oi":      call_oi_total,
            "put_oi":       put_oi_total,
            # Max Pain
            "max_pain":     max_pain,
            # 주가 지표
            "rsi14":        rsi,
            "beta":         beta,
            "week52_pos":   week52_pos,   # 52주 고저 위치 %
            "vol_ratio":    vol_ratio,    # 거래량 이상 비율
            "sample_count": len(all_ivs),
        }

    except Exception as e:
        print(f"  ❌ {symbol} 에러: {e}")
        return None

# ====================================================
# ✅ 메인 루프
# ====================================================
symbols = get_sp500_symbols()
results, failed = [], []
start_time = time.time()

for i, symbol in enumerate(symbols):
    print(f"[{i+1}/{len(symbols)}] {symbol} 수집 중...")
    row = collect_data(symbol)
    if row:
        results.append(row)
        print(f"  ✅ iv={row['avg_iv']}, hv20={row['hv20']}, pcr={row['pcr_oi']}, pain={row['max_pain']}, rsi={row['rsi14']}")
    else:
        failed.append(symbol)
    time.sleep(0.3)

elapsed = round(time.time() - start_time)

# ====================================================
# ✅ CSV 저장
# ====================================================
file_path = "iv_data.csv"
col_order = [
    "date", "symbol", "dte_range",
    "avg_iv", "atm_call_iv", "atm_put_iv", "skew",
    "hv10", "hv20", "hv60", "iv_hv_diff",
    "iv_30d", "iv_45d", "iv_60d",
    "pcr_oi", "pcr_vol",
    "call_oi", "put_oi",
    "max_pain",
    "rsi14", "beta", "week52_pos", "vol_ratio",
    "sample_count",
]

if results:
    df_new = pd.DataFrame(results)[col_order]
    if os.path.exists(file_path):
        df_existing = pd.read_csv(file_path)
        for col in col_order:
            if col not in df_existing.columns:
                df_existing[col] = None
        df_existing = df_existing[df_existing["date"] != today]
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined.to_csv(file_path, index=False)
    else:
        df_new.to_csv(file_path, index=False)
    print(f"✅ 저장 완료: {len(df_new)}개 종목")

if failed:
    with open("failed_symbols.txt", "w") as f:
        f.write("\n".join(failed))

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
success_count = len(results)
fail_count = len(failed)

if success_count > 0:
    msg = (
        f"📊 <b>IV 데이터 수집 완료</b>\n"
        f"📅 날짜: {today}\n"
        f"✅ 성공: {success_count}개 종목\n"
        f"❌ 실패: {fail_count}개 종목\n"
        f"⏱ 소요시간: {elapsed//60}분 {elapsed%60}초\n"
        f"📈 수집: IV/HV/Skew/PCR/MaxPain/RSI/Beta/52주/거래량"
    )
    if fail_count > 0:
        msg += f"\n⚠️ 실패: {', '.join(failed[:10])}"
        if fail_count > 10:
            msg += f" 외 {fail_count-10}개"
else:
    msg = f"❌ <b>IV 데이터 수집 실패</b>\n📅 날짜: {today}"

send_telegram(msg)
print("=== IV Data Collector DONE ===")
