import os
import io
import requests
import pandas as pd
import yfinance as yf
import numpy as np
from datetime import datetime, date, timedelta
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import exchange_calendars as xcals
import pytz
import time

print("=== IV Data Collector START ===")

# ====================================================
# ✅ 날짜 기준: UTC → ET(뉴욕) 변환 후 직전 거래일 사용
# ====================================================
ET_TZ  = pytz.timezone("America/New_York")
now_et = datetime.now(pytz.utc).astimezone(ET_TZ)

def is_market_open(check_date: date) -> bool:
    try:
        nyse = xcals.get_calendar("XNYS")
        return nyse.is_session(check_date.strftime("%Y-%m-%d"))
    except Exception:
        return check_date.weekday() < 5

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ 텔레그램 토큰/채팅ID 미설정 - 메시지 전송 안 함")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")

# 장마감(ET 16:00) 전이면 하루 전 기준
et_date = now_et.date()
if now_et.hour < 16:
    et_date -= timedelta(days=1)

today_date = et_date
today      = today_date.strftime("%Y-%m-%d")

print(f"🕐 ET 현재시각: {now_et.strftime('%Y-%m-%d %H:%M')} | 수집 기준일: {today}")

if not is_market_open(today_date):
    if today_date.weekday() >= 5:
        print(f"📅 {today}은 주말(NYSE 휴장)입니다. 스킵합니다.")
        send_telegram(
            f"📅 <b>{today}</b>\n"
            f"🛌 오늘은 미국 증시 주말 휴장일입니다.\n"
            f"→ IV 데이터 수집을 건너뜁니다."
        )
    else:
        print(f"📅 {today}은 NYSE 휴장일(공휴일)입니다. 스킵합니다.")
        send_telegram(
            f"📅 <b>{today}</b>\n"
            f"🎌 오늘은 미국 증시 휴장일이라 저장된 파일이 없습니다.\n"
            f"→ IV 데이터 수집을 건너뜁니다."
        )
    exit(0)

print(f"✅ 오늘({today}) 장 운영일 확인")

# ====================================================
# ✅ API 초기화
# ====================================================
API_KEY      = os.getenv("ALPACA_API_KEY")
SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")
opt_client   = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


# ====================================================
# ✅ 주가 데이터 수집 (yfinance 실패 시 Alpaca 폴백)
# ====================================================
def get_price_history(symbol: str, period_days: int = 365):
    yf_symbol = symbol.replace("-", ".")

    # 1차: yfinance
    try:
        hist = yf.Ticker(yf_symbol).history(period=f"{period_days}d")
        if not hist.empty and len(hist) > 10:
            print(f"    [주가] {symbol} yfinance OK ({len(hist)}일)")
            return hist, "yfinance"
    except Exception as e:
        print(f"    [주가] {symbol} yfinance 실패: {e}")

    # 2차: Alpaca Stock API
    try:
        start_dt = datetime.now() - timedelta(days=period_days + 10)
        req  = StockBarsRequest(
            symbol_or_symbols=symbol.replace("-", "."),
            timeframe=TimeFrame.Day,
            start=start_dt,
        )
        bars = stock_client.get_stock_bars(req).df
        if bars.empty:
            return None, None
        bars = bars.reset_index()
        if "symbol" in bars.columns:
            bars = bars.drop(columns=["symbol"])
        bars = bars.rename(columns={
            "open":   "Open",
            "high":   "High",
            "low":    "Low",
            "close":  "Close",
            "volume": "Volume",
        })
        bars = bars.set_index("timestamp")
        bars.index = bars.index.tz_localize(None)
        print(f"    [주가] {symbol} Alpaca 폴백 OK ({len(bars)}일)")
        return bars, "alpaca"
    except Exception as e:
        print(f"    [주가] {symbol} Alpaca도 실패: {e}")
        return None, None


def get_intraday(symbol: str):
    yf_symbol = symbol.replace("-", ".")

    # 1차: yfinance
    try:
        intraday = yf.Ticker(yf_symbol).history(interval="1m", period="1d")
        if not intraday.empty:
            return intraday
    except Exception:
        pass

    # 2차: Alpaca 1분봉
    try:
        start_dt = datetime.now() - timedelta(days=1)
        req  = StockBarsRequest(
            symbol_or_symbols=symbol.replace("-", "."),
            timeframe=TimeFrame.Minute,
            start=start_dt,
        )
        bars = stock_client.get_stock_bars(req).df.reset_index()
        if "symbol" in bars.columns:
            bars = bars.drop(columns=["symbol"])
        bars = bars.rename(columns={
            "open": "Open", "high": "High",
            "low":  "Low",  "close": "Close", "volume": "Volume",
        })
        bars = bars.set_index("timestamp")
        bars.index = bars.index.tz_localize(None)
        return bars
    except Exception:
        return None


def get_ticker_info(symbol: str, field: str, default=None):
    try:
        info = yf.Ticker(symbol.replace("-", ".")).info
        return info.get(field, default)
    except Exception:
        return default


def get_earnings_date(symbol: str):
    try:
        ticker = yf.Ticker(symbol.replace("-", "."))
        cal = ticker.calendar
        if cal is not None and "Earnings Date" in cal:
            earn_date = cal["Earnings Date"]
            if isinstance(earn_date, list) and earn_date:
                return (pd.Timestamp(earn_date[0]).date() - today_date).days
            if isinstance(earn_date, pd.Timestamp):
                return (earn_date.date() - today_date).days
        ed = ticker.earnings_dates
        if ed is not None and not ed.empty:
            future = ed[ed.index.tz_localize(None) > pd.Timestamp.now()]
            if not future.empty:
                return (future.index[0].date() - today_date).days
    except Exception:
        pass
    return None

# ====================================================
# ✅ S&P 500 리스트
# ====================================================
EXCLUDE_SYMBOLS = {"BRK-B", "BF-B", "NVR"}

def get_sp500_symbols():
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        sp500   = pd.read_html(io.StringIO(resp.text), flavor="html5lib")[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]
        print(f"✅ S&P 500 종목 수: {len(symbols)} (제외: {len(EXCLUDE_SYMBOLS)}개)")
        return symbols
    except Exception as e:
        print(f"❌ S&P500 로드 실패: {e}")
        return ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "JPM", "TSLA", "UNH", "V"]

# ====================================================
# ✅ 보조 지표 계산 함수
# ====================================================
def calc_hv(closes: pd.Series, period: int):
    try:
        if len(closes) < period + 1:
            return None
        log_returns = np.log(closes / closes.shift(1)).dropna().tail(period)
        return round(float(log_returns.std() * np.sqrt(252)), 4)
    except Exception:
        return None

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

def safe_pct(s: pd.Series, n: int):
    try:
        v = s.dropna().pct_change(n, fill_method=None).iloc[-1]
        return round(float(v) * 100, 2) if not np.isnan(v) else None
    except Exception:
        return None

def days_to_expiry(symbol: str) -> int:
    try:
        exp_str  = symbol[-15:-9]
        exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        return (exp_date - today_date).days
    except Exception:
        return -1

# ====================================================
# ✅ yfinance OI/PCR/MaxPain
# ====================================================
def calc_oi_metrics(yf_ticker):
    try:
        exps = yf_ticker.options
        if not exps:
            return None

        target_exp = None
        for exp in exps:
            dte = (pd.Timestamp(exp).date() - today_date).days
            if 30 <= dte <= 45:
                target_exp = exp
                break
        if not target_exp:
            for exp in exps:
                dte = (pd.Timestamp(exp).date() - today_date).days
                if 25 <= dte <= 50:
                    target_exp = exp
                    break
        if not target_exp:
            return None

        chain    = yf_ticker.option_chain(target_exp)
        calls    = chain.calls
        puts     = chain.puts
        call_oi  = int(calls["openInterest"].fillna(0).sum())
        put_oi   = int(puts["openInterest"].fillna(0).sum())
        call_vol = int(calls["volume"].fillna(0).sum())
        put_vol  = int(puts["volume"].fillna(0).sum())

        print(f"    [yf OI] call_oi={call_oi} put_oi={put_oi} "
              f"call_vol={call_vol} put_vol={put_vol} exp={target_exp}")

        pcr_oi  = round(put_oi  / call_oi,  4) if call_oi  > 0 else None
        pcr_vol = round(put_vol / call_vol, 4) if call_vol > 0 else None

        # Max Pain
        max_pain = None
        try:
            strikes = {}
            for _, row in calls.iterrows():
                s  = row["strike"]
                oi = float(row["openInterest"]) if not pd.isna(row["openInterest"]) else 0
                strikes.setdefault(s, {"call_oi": 0, "put_oi": 0})
                strikes[s]["call_oi"] += oi
            for _, row in puts.iterrows():
                s  = row["strike"]
                oi = float(row["openInterest"]) if not pd.isna(row["openInterest"]) else 0
                strikes.setdefault(s, {"call_oi": 0, "put_oi": 0})
                strikes[s]["put_oi"] += oi

            min_pain = float("inf")
            for test_price in sorted(strikes.keys()):
                pain = (
                    sum((test_price - s) * d["call_oi"] if test_price > s else 0
                        for s, d in strikes.items())
                    + sum((s - test_price) * d["put_oi"] if test_price < s else 0
                          for s, d in strikes.items())
                )
                if pain < min_pain:
                    min_pain = pain
                    max_pain = test_price
        except Exception:
            max_pain = None

        return {
            "call_oi":  call_oi, "put_oi":   put_oi,
            "call_vol": call_vol, "put_vol":  put_vol,
            "pcr_oi":   pcr_oi,  "pcr_vol":  pcr_vol,
            "max_pain": max_pain,
        }
    except Exception as e:
        print(f"    [yf OI 실패] {e}")
        return None


# ====================================================
# ✅ 단기(근월물) 콜 거래량/OI — yfinance
#    (기존 calc_oi_metrics는 30~45일 만기 "하나"만 보므로
#     뉴스 선반영 성격의 근월물 콜 급등은 잡지 못함)
# ====================================================
NEAR_TERM_MAX_DTE = 21   # 0~21일 남은 만기를 "단기"로 간주 (필요시 조정)
NEAR_TERM_MIN_DTE = 0

def calc_near_term_oi_metrics(yf_ticker, min_dte: int = NEAR_TERM_MIN_DTE, max_dte: int = NEAR_TERM_MAX_DTE):
    """
    min_dte~max_dte 범위에서 '가장 가까운 만기 하나'를 골라 콜/풋 거래량·OI를 계산.
    ⚠️ 이전 버전은 범위 내 모든 만기를 합산했는데, 그러면 종목마다 근월물
    만기 개수(위클리 유무 등)가 달라서 종목 간 비교가 왜곡됨.
    또한 이 함수가 고른 만기(exp_key)를 analyze_near_term_call_flow에도
    그대로 넘겨서 두 지표(거래량 vs 매수비율)가 같은 대상을 가리키게 함.
    """
    try:
        exps = yf_ticker.options
        if not exps:
            return None

        target_exp = None
        target_dte = None
        for exp in exps:   # yfinance는 만기를 날짜순으로 정렬해서 반환
            dte = (pd.Timestamp(exp).date() - today_date).days
            if min_dte <= dte <= max_dte:
                target_exp = exp
                target_dte = dte
                break

        if not target_exp:
            return None

        chain = yf_ticker.option_chain(target_exp)
        calls = chain.calls
        puts  = chain.puts
        call_oi  = int(calls["openInterest"].fillna(0).sum())
        put_oi   = int(puts["openInterest"].fillna(0).sum())
        call_vol = int(calls["volume"].fillna(0).sum())
        put_vol  = int(puts["volume"].fillna(0).sum())

        pcr_vol = round(put_vol / call_vol, 4) if call_vol > 0 else None
        exp_key = pd.Timestamp(target_exp).strftime("%y%m%d")  # Alpaca 심볼의 만기 포맷과 동일

        return {
            "call_oi_st":   call_oi,
            "put_oi_st":    put_oi,
            "call_vol_st":  call_vol,
            "put_vol_st":   put_vol,
            "pcr_vol_st":   pcr_vol,
            "target_exp_st": target_exp,
            "exp_key_st":   exp_key,
            "dte_st":       target_dte,
        }
    except Exception as e:
        print(f"    [yf 단기 OI 실패] {e}")
        return None


# ====================================================
# ✅ Alpaca bid/ask 기반 매수주도(buy-side) 근사 판단
#    - Alpaca 무료(indicative) feed의 latest_quote/latest_trade 사용
#    - "오늘 하루 전체 체결"이 아니라 "장마감 시점 마지막 체결가"가
#      bid/ask 중 어디에 더 가까웠는지를 보는 근사치임 (한계 있음)
# ====================================================
def classify_trade_side(bid: float, ask: float, trade_price: float,
                         buy_zone: float = 0.6, sell_zone: float = 0.4):
    """
    체결가가 스프레드(bid~ask) 안에서 어디에 위치하는지로 매수/매도 압력 근사.
    pos=1.0 → ask에서 체결(매수 주도), pos=0.0 → bid에서 체결(매도 주도)
    """
    try:
        if bid is None or ask is None or trade_price is None:
            return None
        spread = ask - bid
        if spread <= 0:
            return None
        pos = (trade_price - bid) / spread
        if pos >= buy_zone:
            return "buy"
        elif pos <= sell_zone:
            return "sell"
        else:
            return "mid"
    except Exception:
        return None


def analyze_near_term_call_flow(options: list, exp_key: str = None,
                                 min_dte: int = NEAR_TERM_MIN_DTE,
                                 max_dte: int = NEAR_TERM_MAX_DTE):
    """
    Alpaca 옵션체인(options)에서 근월물 콜만 골라
    latest_quote(bid/ask) vs latest_trade(price)로 매수/매도 주도 여부를 집계.

    exp_key(YYMMDD, 예: "260710")를 넘기면 calc_near_term_oi_metrics가 고른
    만기와 '동일한 만기'만 대상으로 삼아 call_vol_st와 buy_ratio_st가
    같은 대상을 가리키도록 함. exp_key가 없으면 DTE 범위 전체를 봄.

    call_no_quote_cnt_st: bid/ask 또는 체결가가 없어서 판단 불가했던 계약 수.
    이 값이 크면 Alpaca 무료(indicative) feed 특성상 quote 데이터 자체가
    없는 경우이니, "매수비율=None" 이 버그가 아니라 데이터 공백 때문임을 알 수 있음.
    """
    buy_cnt = sell_cnt = mid_cnt = no_quote_cnt = 0
    buy_notional = sell_notional = 0.0
    checked = 0
    bid_sum = ask_sum = 0.0
    bid_ask_cnt = 0

    for opt in options:
        dte = days_to_expiry(opt.symbol)
        if not (min_dte <= dte <= max_dte):
            continue
        opt_type = opt.symbol[-9]
        if opt_type != "C":
            continue
        if exp_key is not None:
            sym_exp_key = opt.symbol[-15:-9]
            if sym_exp_key != exp_key:
                continue

        quote = getattr(opt, "latest_quote", None)
        trade = getattr(opt, "latest_trade", None)
        bid   = getattr(quote, "bid_price", None) if quote is not None else None
        ask   = getattr(quote, "ask_price", None) if quote is not None else None
        price = getattr(trade, "price", None) if trade is not None else None
        size  = (getattr(trade, "size", None) or 0) if trade is not None else 0

        if bid is not None and ask is not None and ask > bid:
            bid_sum += bid
            ask_sum += ask
            bid_ask_cnt += 1

        side = classify_trade_side(bid, ask, price)
        if side is None:
            no_quote_cnt += 1
            continue

        checked += 1
        notional = (price or 0) * size * 100  # 옵션 1계약 = 100주

        if side == "buy":
            buy_cnt += 1
            buy_notional += notional
        elif side == "sell":
            sell_cnt += 1
            sell_notional += notional
        else:
            mid_cnt += 1

    directional = buy_cnt + sell_cnt
    buy_ratio_st = round(buy_cnt / directional, 4) if directional > 0 else None
    avg_bid = round(bid_sum / bid_ask_cnt, 4) if bid_ask_cnt > 0 else None
    avg_ask = round(ask_sum / bid_ask_cnt, 4) if bid_ask_cnt > 0 else None
    avg_spread_pct = None
    if avg_bid is not None and avg_ask is not None and (avg_bid + avg_ask) > 0:
        avg_spread_pct = round((avg_ask - avg_bid) / ((avg_ask + avg_bid) / 2) * 100, 2)

    return {
        "call_buy_cnt_st":       buy_cnt,
        "call_sell_cnt_st":      sell_cnt,
        "call_mid_cnt_st":       mid_cnt,
        "call_checked_cnt_st":   checked,
        "call_no_quote_cnt_st":  no_quote_cnt,
        "call_buy_ratio_st":     buy_ratio_st,
        "call_buy_notional_st":  round(buy_notional, 2),
        "call_avg_bid_st":       avg_bid,
        "call_avg_ask_st":       avg_ask,
        "call_avg_spread_pct_st": avg_spread_pct,
    }


# ====================================================
# ✅ 단일 종목 전체 데이터 수집
# ====================================================
def collect_data(symbol: str):
    try:
        hist, price_source = get_price_history(symbol, period_days=365)

        hv10 = hv20 = hv60 = None
        rsi  = beta = week52_pos = vol_ratio = None
        ma20 = ma50 = ma200 = price_vs_ma200 = golden_cross = None
        ret_1d = ret_5d = ret_20d = atr14 = cur_price = None
        days_to_earn = None
        open_price = high_price = low_price = volume = None
        vwap = vwap_diff = None

        if hist is not None and len(hist) > 60:
            closes  = hist["Close"].dropna()
            volumes = hist["Volume"]
            high    = hist["High"]
            low     = hist["Low"]

            if len(closes) == 0:
                return {"_fail_reason": "PRICE_FAIL", "_fail_detail": "주가 Close 데이터 없음"}

            cur_price  = round(float(closes.iloc[-1]),       4)
            open_price = round(float(hist["Open"].iloc[-1]), 4)
            high_price = round(float(hist["High"].iloc[-1]), 4)
            low_price  = round(float(hist["Low"].iloc[-1]),  4)
            volume     = int(hist["Volume"].iloc[-1])

            # VWAP
            try:
                intraday = get_intraday(symbol)
                if intraday is not None and not intraday.empty:
                    typical  = (intraday["High"] + intraday["Low"] + intraday["Close"]) / 3
                    vwap_val = (typical * intraday["Volume"]).cumsum() / intraday["Volume"].cumsum()
                    vwap     = round(float(vwap_val.iloc[-1]), 4)
                    vwap_diff = round((cur_price - vwap) / vwap * 100, 2)
            except Exception:
                pass

            hv10 = calc_hv(closes, 10)
            hv20 = calc_hv(closes, 20)
            hv60 = calc_hv(closes, 60)
            rsi  = calc_rsi(closes, 14)

            high52 = closes.tail(252).max()
            low52  = closes.tail(252).min()
            if high52 != low52:
                week52_pos = round((cur_price - low52) / (high52 - low52) * 100, 2)

            avg_vol   = volumes.tail(20).mean()
            vol_ratio = round(float(volumes.iloc[-1] / avg_vol), 2) if avg_vol > 0 else None

            if len(closes) >= 200:
                ma20  = round(float(closes.tail(20).mean()),  2)
                ma50  = round(float(closes.tail(50).mean()),  2)
                ma200 = round(float(closes.tail(200).mean()), 2)
                price_vs_ma200 = round((cur_price - ma200) / ma200 * 100, 2)
                golden_cross   = int(ma50 > ma200)

            ret_1d  = safe_pct(closes, 1)
            ret_5d  = safe_pct(closes, 5)
            ret_20d = safe_pct(closes, 20)

            tr    = pd.concat([
                high - low,
                (high - hist["Close"].shift()).abs(),
                (low  - hist["Close"].shift()).abs()
            ], axis=1).max(axis=1)
            atr_v = tr.rolling(14).mean().iloc[-1]
            atr14 = round(float(atr_v), 4) if not np.isnan(atr_v) else None

        beta_val = get_ticker_info(symbol, "beta")
        beta = round(float(beta_val), 3) if beta_val else None

        days_to_earn = get_earnings_date(symbol)

        yf_ticker = yf.Ticker(symbol.replace("-", "."))
        oi_m     = calc_oi_metrics(yf_ticker)
        call_oi  = oi_m["call_oi"]  if oi_m else 0
        put_oi   = oi_m["put_oi"]   if oi_m else 0
        call_vol = oi_m["call_vol"] if oi_m else 0  # ✅ 변수 보존
        put_vol  = oi_m["put_vol"]  if oi_m else 0  # ✅ 변수 보존
        pcr_oi   = oi_m["pcr_oi"]   if oi_m else None
        pcr_vol  = oi_m["pcr_vol"]  if oi_m else None
        max_pain = oi_m["max_pain"] if oi_m else None

        # ✅ 단기(근월물) 콜/풋 거래량·OI (yfinance, 가장 가까운 만기 하나)
        oi_st       = calc_near_term_oi_metrics(yf_ticker)
        call_oi_st  = oi_st["call_oi_st"]  if oi_st else 0
        call_vol_st = oi_st["call_vol_st"] if oi_st else 0
        put_vol_st  = oi_st["put_vol_st"]  if oi_st else 0
        pcr_vol_st  = oi_st["pcr_vol_st"]  if oi_st else None
        exp_key_st  = oi_st["exp_key_st"]  if oi_st else None

        alpaca_symbol = symbol.replace("-", ".")
        try:
            req     = OptionChainRequest(underlying_symbol=alpaca_symbol)
            chain   = opt_client.get_option_chain(req)
            options = list(chain.values())
        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ["429", "rate limit", "too many", "forbidden", "503", "502"]):
                return {"_fail_reason": "ALPACA_RATELIMIT", "_fail_detail": str(e)}
            return {"_fail_reason": "ALPACA_RATELIMIT" if "timeout" in err_str else "UNKNOWN",
                    "_fail_detail": str(e)}

        if not options:
            return {"_fail_reason": "ALPACA_EMPTY", "_fail_detail": "옵션체인 응답이 비어있음"}

        # ✅ 근월물 콜 매수/매도 주도 근사 (Alpaca bid/ask, 무료 indicative feed)
        #    exp_key_st를 넘겨서 위 call_vol_st/call_oi_st와 '같은 만기'만 봄
        flow_st = analyze_near_term_call_flow(options, exp_key=exp_key_st)

        all_dtes = sorted(set(days_to_expiry(opt.symbol) for opt in options))
        print(f"    [DTE분포] {symbol}: {all_dtes[:10]}{'...' if len(all_dtes)>10 else ''}")

        dte_range_used = "30-45"
        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
        if not filtered:
            dte_range_used = "25-50"
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]
        if not filtered:
            valid = [opt for opt in options if days_to_expiry(opt.symbol) > 0]
            if not valid:
                return {"_fail_reason": "ALPACA_EMPTY",
                        "_fail_detail": "유효한 미래 만기 옵션 없음"}
            nearest_dte = min(days_to_expiry(opt.symbol) for opt in valid)
            filtered = [opt for opt in valid if days_to_expiry(opt.symbol) == nearest_dte]
            dte_range_used = f"auto:{nearest_dte}d"
            print(f"    [DTE 자동조정] {symbol}: 25~50일 공백 → {nearest_dte}일 만기 사용")

        call_ivs = []; put_ivs = []
        call_deltas = []; put_deltas = []
        gammas = []; thetas = []; vegas = []; rhos = []

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

                if delta is not None and (0.3 <= abs(float(delta)) <= 0.7):
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
            all_ivs = [float(opt.implied_volatility) for opt in filtered
                       if getattr(opt, "implied_volatility", None)]
        if not all_ivs:
            return {"_fail_reason": "ALPACA_NO_IV",
                    "_fail_detail": f"filtered {len(filtered)}개 옵션 모두 IV=None"}

        avg_call = round(sum(call_ivs) / len(call_ivs), 4) if call_ivs else None
        avg_put  = round(sum(put_ivs)  / len(put_ivs),  4) if put_ivs  else None
        avg_iv   = round(sum(all_ivs)  / len(all_ivs),  4)

        skew       = round(avg_put - avg_call, 4) if (avg_call and avg_put) else None
        iv_hv_diff = round(avg_iv - hv20, 4)      if hv20                   else None

        avg_gamma = round(sum(gammas) / len(gammas), 6) if gammas else None
        avg_theta = round(sum(thetas) / len(thetas), 6) if thetas else None
        avg_vega  = round(sum(vegas)  / len(vegas),  6) if vegas  else None
        avg_rho   = round(sum(rhos)   / len(rhos),   6) if rhos   else None
        avg_delta = None
        if call_deltas or put_deltas:
            all_d     = call_deltas + put_deltas
            avg_delta = round(sum(all_d) / len(all_d), 4)

        gex_call = gex_put = gex = None
        if avg_gamma is not None and cur_price is not None:
            gex_call = round(avg_gamma * call_oi * cur_price ** 2 * 0.01, 2)
            gex_put  = round(avg_gamma * put_oi  * cur_price ** 2 * 0.01, 2)
            gex      = round(gex_call - gex_put, 2)

        dex = None
        if avg_delta is not None and cur_price is not None:
            dex = round(avg_delta * (call_oi - put_oi) * cur_price * 100, 2)

        iv_30 = iv_45 = iv_60 = None
        bucket = {30: [], 45: [], 60: []}
        for opt in options:
            dte = days_to_expiry(opt.symbol)
            iv  = getattr(opt, "implied_volatility", None)
            if not iv: continue
            if 20 <= dte <= 37:   bucket[30].append(float(iv))
            elif 38 <= dte <= 52: bucket[45].append(float(iv))
            elif 53 <= dte <= 75: bucket[60].append(float(iv))
        if bucket[30]: iv_30 = round(sum(bucket[30]) / len(bucket[30]), 4)
        if bucket[45]: iv_45 = round(sum(bucket[45]) / len(bucket[45]), 4)
        if bucket[60]: iv_60 = round(sum(bucket[60]) / len(bucket[60]), 4)

        iv_term_slope = round(iv_60 - iv_30, 4) if (iv_30 and iv_60) else None

        pain_diff = None
        if max_pain and cur_price:
            pain_diff = round((cur_price - max_pain) / cur_price * 100, 2)

        return {
            "date":           today,
            "symbol":         symbol,
            "price_source":   price_source,
            "dte_range":      dte_range_used,
            "open":           open_price,
            "high":           high_price,
            "low":            low_price,
            "close":          cur_price,
            "volume":         volume,
            "vwap":           vwap,
            "vwap_diff":      vwap_diff,
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
            "gex":            gex,
            "gex_call":       gex_call,
            "gex_put":        gex_put,
            "dex":            dex,
            "pcr_oi":         pcr_oi,
            "pcr_vol":        pcr_vol,
            "call_oi":        call_oi,
            "put_oi":         put_oi,
            "call_vol":       call_vol,   # ✅ 추가 (콜 급등 감지에 필요)
            "put_vol":        put_vol,    # ✅ 추가
            "call_oi_st":     call_oi_st,     # ✅ 근월물(0~21일) 콜 OI
            "call_vol_st":    call_vol_st,    # ✅ 근월물 콜 거래량
            "put_vol_st":     put_vol_st,     # ✅ 근월물 풋 거래량
            "pcr_vol_st":     pcr_vol_st,     # ✅ 근월물 PCR(거래량 기준)
            "call_buy_ratio_st":    flow_st["call_buy_ratio_st"],    # ✅ 매수주도 비율(근사)
            "call_buy_cnt_st":      flow_st["call_buy_cnt_st"],
            "call_sell_cnt_st":     flow_st["call_sell_cnt_st"],
            "call_checked_cnt_st":  flow_st["call_checked_cnt_st"],
            "call_no_quote_cnt_st": flow_st["call_no_quote_cnt_st"],  # ✅ quote 없어서 판단불가한 계약수
            "call_buy_notional_st": flow_st["call_buy_notional_st"],
            "call_avg_bid_st":      flow_st["call_avg_bid_st"],       # ✅ 실제 평균 bid
            "call_avg_ask_st":      flow_st["call_avg_ask_st"],       # ✅ 실제 평균 ask
            "call_avg_spread_pct_st": flow_st["call_avg_spread_pct_st"],
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
        err_str = str(e).lower()
        if any(k in err_str for k in ["429", "rate limit", "too many", "forbidden", "503", "502"]):
            reason = "ALPACA_RATELIMIT"
        else:
            reason = "UNKNOWN"
        print(f"  ❌ {symbol} 에러: {e}")
        return {"_fail_reason": reason, "_fail_detail": str(e)}

# ====================================================
# ✅ 시장 전체 데이터 수집
# ====================================================
SECTOR_ETFS = {
    "XLK":  "tech",       "XLF":  "fin",      "XLV": "health",
    "XLE":  "energy",     "XLI":  "indus",    "XLY": "cons_disc",
    "XLP":  "cons_stap",  "XLU":  "util",     "XLB": "material",
    "XLRE": "realestate", "XLC":  "comm",
}
MARKET_TICKERS = {
    "^VIX": "vix", "^VIX9D": "vix9d", "^VIX3M": "vix3m",
    "SPY":  "spy", "QQQ":    "qqq",   "IWM":    "iwm",
    "TLT":  "tlt", "^TNX":   "tnx",   "UUP":    "uup",
    "GLD":  "gld",
}
ALPACA_FALLBACK_TICKERS = {"SPY", "QQQ", "IWM", "TLT", "UUP", "GLD"}

def collect_market_data():
    try:
        row = {"date": today}

        for ticker_sym, col in MARKET_TICKERS.items():
            try:
                hist = None
                try:
                    h = yf.Ticker(ticker_sym).history(period="60d")
                    if not h.empty:
                        hist = h
                except Exception:
                    pass

                if hist is None and ticker_sym in ALPACA_FALLBACK_TICKERS:
                    try:
                        start_dt = datetime.now() - timedelta(days=70)
                        req  = StockBarsRequest(
                            symbol_or_symbols=ticker_sym,
                            timeframe=TimeFrame.Day,
                            start=start_dt,
                        )
                        bars = stock_client.get_stock_bars(req).df.reset_index()
                        if "symbol" in bars.columns:
                            bars = bars.drop(columns=["symbol"])
                        bars = bars.rename(columns={"close": "Close"})
                        bars = bars.set_index("timestamp")
                        hist = bars
                        print(f"    [{ticker_sym}] Alpaca 폴백 사용")
                    except Exception:
                        pass

                if hist is None or hist.empty:
                    continue

                closes = hist["Close"].dropna()
                if len(closes) == 0:
                    continue
                row[f"{col}_close"] = round(float(closes.iloc[-1]), 4)
                row[f"{col}_ret1d"] = safe_pct(closes, 1)
                row[f"{col}_ret5d"] = safe_pct(closes, 5)
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
            spy_hist, _ = get_price_history("SPY", period_days=365)
            if spy_hist is not None:
                spy_closes = spy_hist["Close"].dropna()
                ma50  = spy_closes.tail(50).mean()
                ma200 = spy_closes.tail(200).mean()
                row["spy_golden_cross"]   = int(ma50 > ma200)
                row["spy_price_vs_ma200"] = round(
                    (spy_closes.iloc[-1] - ma200) / ma200 * 100, 2
                )
        except Exception:
            pass

        try:
            spy   = yf.Ticker("SPY")
            chain = spy.option_chain(spy.options[0])
            c_vol = chain.calls["volume"].fillna(0).sum()
            p_vol = chain.puts["volume"].fillna(0).sum()
            row["spy_pcr_vol"] = round(p_vol / c_vol, 4) if c_vol > 0 else None
        except Exception:
            pass

        for idx_sym, idx_col in [("SPY", "spy"), ("QQQ", "qqq"), ("IWM", "iwm")]:
            try:
                idx_price = row.get(f"{idx_col}_close")
                if not idx_price:
                    continue

                call_gex_total = 0.0
                put_gex_total  = 0.0
                calculated     = False

                yf_call_oi = {}
                yf_put_oi  = {}
                try:
                    idx_ticker = yf.Ticker(idx_sym)
                    all_exps   = idx_ticker.options or []
                    valid_exps = []
                    for exp in all_exps:
                        dte = (pd.Timestamp(exp).date() - today_date).days
                        if 7 <= dte <= 60:
                            valid_exps.append(exp)

                    if not valid_exps and all_exps:
                        valid_exps = all_exps[:3]

                    def _safe_oi(v):
                        try:
                            f = float(v)
                            return 0 if np.isnan(f) else f
                        except Exception:
                            return 0

                    for exp in valid_exps:
                        exp_key  = pd.Timestamp(exp).strftime("%y%m%d")
                        yf_chain = idx_ticker.option_chain(exp)
                        for _, r in yf_chain.calls.iterrows():
                            k = (exp_key, round(float(r["strike"]), 1))
                            yf_call_oi[k] = _safe_oi(r["openInterest"])
                        for _, r in yf_chain.puts.iterrows():
                            k = (exp_key, round(float(r["strike"]), 1))
                            yf_put_oi[k] = _safe_oi(r["openInterest"])

                    print(f"    [{idx_sym} yf OI] exps={valid_exps} "
                          f"call={len(yf_call_oi)} put={len(yf_put_oi)} (만기×strike)")
                except Exception as e:
                    print(f"    [{idx_sym} yf OI 실패] {e}")

                yf_oi_available = bool(yf_call_oi or yf_put_oi)

                try:
                    req     = OptionChainRequest(underlying_symbol=idx_sym)
                    chain   = opt_client.get_option_chain(req)
                    options = list(chain.values())

                    filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
                    if not filtered:
                        filtered = [opt for opt in options if 7 <= days_to_expiry(opt.symbol) <= 60]

                    matched = 0
                    for opt in filtered:
                        greeks = getattr(opt, "greeks", None)
                        if greeks is None: continue
                        gamma = getattr(greeks, "gamma", None)
                        if gamma is None: continue
                        try:
                            if np.isnan(float(gamma)): continue
                        except Exception:
                            continue

                        opt_type = opt.symbol[-9]
                        try:
                            exp_key = opt.symbol[-15:-9]
                            strike  = round(int(opt.symbol[-8:]) / 1000, 1)
                        except Exception:
                            continue

                        k = (exp_key, strike)
                        if yf_oi_available:
                            oi = yf_call_oi.get(k, 0) if opt_type == "C" else yf_put_oi.get(k, 0)
                        else:
                            oi = getattr(opt, "open_interest", None) or 0

                        try:
                            oi = float(oi)
                        except Exception:
                            continue
                        if oi <= 0 or np.isnan(oi): continue

                        try:
                            gex_val = float(gamma) * oi * idx_price ** 2 * 0.01
                        except Exception:
                            continue
                        if np.isnan(gex_val): continue

                        if opt_type == "C":   call_gex_total += gex_val
                        elif opt_type == "P": put_gex_total  += gex_val
                        matched += 1

                    if call_gex_total != 0.0 or put_gex_total != 0.0:
                        calculated = True
                        oi_src = "yf+alpaca" if yf_oi_available else "alpaca only"
                        print(f"    [{idx_sym} GEX({oi_src})] matched={matched} "
                              f"call={round(call_gex_total,2)} put={round(put_gex_total,2)} "
                              f"net={round(call_gex_total-put_gex_total,2)}")
                    else:
                        print(f"    [{idx_sym} GEX] matched={matched} → 계산값 없음")
                except Exception as e:
                    print(f"    [{idx_sym} Alpaca gamma 실패] {e}")

                if calculated:
                    row[f"{idx_col}_gex_call"] = round(call_gex_total, 2)
                    row[f"{idx_col}_gex_put"]  = round(put_gex_total,  2)
                    row[f"{idx_col}_gex"]      = round(call_gex_total - put_gex_total, 2)

            except Exception as e:
                print(f"    [{idx_sym} GEX 실패] {e}")

        for etf, name in SECTOR_ETFS.items():
            try:
                hist = None
                try:
                    h = yf.Ticker(etf).history(period="60d")
                    if not h.empty:
                        hist = h
                except Exception:
                    pass
                if hist is None:
                    try:
                        start_dt = datetime.now() - timedelta(days=70)
                        req  = StockBarsRequest(
                            symbol_or_symbols=etf,
                            timeframe=TimeFrame.Day,
                            start=start_dt,
                        )
                        bars = stock_client.get_stock_bars(req).df.reset_index()
                        if "symbol" in bars.columns:
                            bars = bars.drop(columns=["symbol"])
                        bars = bars.rename(columns={"close": "Close"}).set_index("timestamp")
                        hist = bars
                    except Exception:
                        pass
                if hist is not None:
                    closes = hist["Close"].dropna()
                    row[f"sec_{name}_ret1d"] = safe_pct(closes, 1)
                    row[f"sec_{name}_ret5d"] = safe_pct(closes, 5)
            except Exception:
                pass

        return row
    except Exception as e:
        print(f"❌ 시장 데이터 수집 실패: {e}")
        return None

# ====================================================
# ✅ CSV 컬럼 순서 (call_vol, put_vol 추가)
# ====================================================
IV_COL_ORDER = [
    "date", "symbol", "price_source", "dte_range",
    "open", "high", "low", "close", "volume", "vwap", "vwap_diff",
    "cur_price",
    "avg_iv", "atm_call_iv", "atm_put_iv", "skew", "iv_hv_diff",
    "iv_30d", "iv_45d", "iv_60d", "iv_term_slope",
    "hv10", "hv20", "hv60",
    "avg_delta", "avg_gamma", "avg_theta", "avg_vega", "avg_rho",
    "gex", "gex_call", "gex_put", "dex",
    "pcr_oi", "pcr_vol", "call_oi", "put_oi",
    "call_vol", "put_vol",   # ✅ 추가
    "call_oi_st", "call_vol_st", "put_vol_st", "pcr_vol_st",           # ✅ 단기(근월물, 최근접 만기)
    "call_buy_ratio_st", "call_buy_cnt_st", "call_sell_cnt_st",        # ✅ 매수주도 근사
    "call_checked_cnt_st", "call_no_quote_cnt_st", "call_buy_notional_st",
    "call_avg_bid_st", "call_avg_ask_st", "call_avg_spread_pct_st",    # ✅ 실제 bid/ask 값
    "max_pain", "pain_diff",
    "rsi14", "beta", "week52_pos", "vol_ratio",
    "ret_1d", "ret_5d", "ret_20d", "atr14",
    "ma20", "ma50", "ma200", "price_vs_ma200", "golden_cross",
    "days_to_earn", "sample_count",
]

def save_csv(results: list, col_order: list, base_name: str):
    half      = "H1" if today_date.month <= 6 else "H2"
    file_path = f"{base_name}_{today_date.year}_{half}.csv"
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
# ✅ 데이터 품질 이상 감지
# ====================================================
ALERT_FALLBACK_RATE  = 0.30
ALERT_FAIL_RATE      = 0.20
ALERT_NULL_RATE      = 0.50

def check_data_quality(results: list, yf_fallback_count: int, total: int) -> list:
    alerts = []
    if not results:
        return alerts

    df = pd.DataFrame(results)
    n  = len(df)

    fallback_rate = yf_fallback_count / total if total > 0 else 0
    if fallback_rate >= ALERT_FALLBACK_RATE:
        alerts.append(
            f"⚠️ <b>yfinance 주가 이상 의심</b>\n"
            f"   Alpaca 폴백 종목: {yf_fallback_count}/{total} "
            f"({fallback_rate*100:.0f}%)\n"
            f"   → yfinance 주가 API 구조 변경 가능성"
        )

    for col, label in [
        ("pcr_oi",   "PCR(OI)"),
        ("max_pain", "MaxPain"),
        ("call_oi",  "Call OI"),
    ]:
        if col in df.columns:
            null_rate = df[col].isna().sum() / n
            if null_rate >= ALERT_NULL_RATE:
                alerts.append(
                    f"⚠️ <b>yfinance 옵션 OI 이상 의심</b>\n"
                    f"   {label} null 비율: {null_rate*100:.0f}%\n"
                    f"   → yfinance 옵션 체인 구조 변경 가능성\n"
                    f"   → PCR/MaxPain/GEX 데이터 신뢰도 저하"
                )
                break

    if "avg_iv" in df.columns:
        null_rate = df["avg_iv"].isna().sum() / n
        if null_rate >= ALERT_NULL_RATE:
            alerts.append(
                f"⚠️ <b>Alpaca 옵션 IV 이상 의심</b>\n"
                f"   avg_iv null 비율: {null_rate*100:.0f}%\n"
                f"   → Alpaca 옵션 체인 API 문제 가능성"
            )

    if "avg_gamma" in df.columns:
        null_rate = df["avg_gamma"].isna().sum() / n
        if null_rate >= ALERT_NULL_RATE:
            alerts.append(
                f"⚠️ <b>Alpaca Greeks 이상 의심</b>\n"
                f"   avg_gamma null 비율: {null_rate*100:.0f}%\n"
                f"   → Alpaca Greeks 제공 중단 가능성\n"
                f"   → GEX/DEX 데이터 신뢰도 저하"
            )

    if "rsi14" in df.columns:
        null_rate = df["rsi14"].isna().sum() / n
        if null_rate >= ALERT_NULL_RATE:
            alerts.append(
                f"⚠️ <b>주가 기반 지표 이상 의심</b>\n"
                f"   RSI null 비율: {null_rate*100:.0f}%\n"
                f"   → yfinance/Alpaca 주가 데이터 문제 가능성"
            )

    return alerts


def check_market_data_quality(market_row: dict) -> list:
    alerts = []
    if not market_row:
        return alerts

    if market_row.get("vix_close") is None:
        alerts.append(
            f"⚠️ <b>VIX 수집 실패</b>\n"
            f"   → yfinance 지수(^VIX) 수집 불가\n"
            f"   → 시장 공포지수 데이터 공백"
        )

    if market_row.get("spy_gex") is None:
        alerts.append(
            f"⚠️ <b>SPY/QQQ GEX 수집 실패</b>\n"
            f"   → yfinance OI 또는 Alpaca Greeks 문제\n"
            f"   → 시장 GEX 데이터 공백"
        )

    sec_cols  = [k for k in market_row if k.startswith("sec_") and k.endswith("_ret1d")]
    sec_nulls = sum(1 for k in sec_cols if market_row.get(k) is None)
    if sec_cols and sec_nulls / len(sec_cols) >= 0.5:
        alerts.append(
            f"⚠️ <b>섹터 ETF 수집 이상</b>\n"
            f"   null 섹터: {sec_nulls}/{len(sec_cols)}\n"
            f"   → yfinance/Alpaca ETF 데이터 문제 가능성"
        )

    return alerts


# ====================================================
# ✅ 콜 거래량 급등 감지
# ====================================================
def detect_call_surge(results: list, vol_oi_threshold: float = 2.0, min_call_vol: int = 500) -> list:
    """
    call_vol / call_oi >= threshold 이고 call_vol >= min_call_vol 인 종목 탐지.
    비율 높은 순으로 정렬하여 반환.
    """
    surges = []
    for row in results:
        call_vol = row.get("call_vol") or 0
        call_oi  = row.get("call_oi")  or 0
        pcr_vol  = row.get("pcr_vol")

        if call_vol < min_call_vol:
            continue
        if call_oi <= 0:
            continue

        ratio = call_vol / call_oi
        if ratio < vol_oi_threshold:
            continue

        surges.append({
            "symbol":   row["symbol"],
            "call_vol": int(call_vol),
            "call_oi":  int(call_oi),
            "ratio":    round(ratio, 2),
            "pcr_vol":  pcr_vol,
        })

    surges.sort(key=lambda x: x["ratio"], reverse=True)
    return surges


# ====================================================
# ✅ 단기 콜 "매수주도" 급등 감지
#    - 근월물(0~21일) 콜 거래량이 OI 대비 이례적으로 많고
#    - Alpaca bid/ask 근사상 매수 주도(ask 쪽 체결)로 보이는 종목
#    인텔 사례처럼 "뉴스 전날 콜이 매수 우위로 많이 들어온" 패턴을 겨냥.
#    ⚠️ 참고: buy_ratio는 "장마감 시점 마지막 체결가"가 bid/ask 중
#    어디에 가까웠는지에 대한 근사치이며, 오늘 하루 전체 체결을
#    집계한 것이 아님 (Alpaca 무료 feed의 한계).
# ====================================================
def detect_buyside_call_surge(results: list, vol_oi_threshold: float = 1.5,
                               min_call_vol: int = 200, min_buy_ratio: float = 0.6,
                               min_checked: int = 3) -> list:
    surges = []
    for row in results:
        call_vol_st = row.get("call_vol_st") or 0
        call_oi_st  = row.get("call_oi_st")  or 0
        buy_ratio   = row.get("call_buy_ratio_st")
        checked     = row.get("call_checked_cnt_st") or 0
        buy_cnt     = row.get("call_buy_cnt_st") or 0
        sell_cnt    = row.get("call_sell_cnt_st") or 0
        pcr_vol_st  = row.get("pcr_vol_st")
        avg_bid     = row.get("call_avg_bid_st")
        avg_ask     = row.get("call_avg_ask_st")
        avg_spread  = row.get("call_avg_spread_pct_st")

        if call_vol_st < min_call_vol:
            continue
        if call_oi_st <= 0:
            continue
        if checked < min_checked:          # 표본이 너무 적으면 신뢰도 낮음 → 제외
            continue
        if buy_ratio is None or buy_ratio < min_buy_ratio:
            continue

        ratio = call_vol_st / call_oi_st
        if ratio < vol_oi_threshold:
            continue

        surges.append({
            "symbol":       row["symbol"],
            "call_vol_st":  int(call_vol_st),
            "call_oi_st":   int(call_oi_st),
            "ratio":        round(ratio, 2),
            "buy_ratio":    buy_ratio,
            "buy_cnt":      buy_cnt,
            "sell_cnt":     sell_cnt,
            "pcr_vol_st":   pcr_vol_st,
            "avg_bid":      avg_bid,
            "avg_ask":      avg_ask,
            "avg_spread":   avg_spread,
        })

    # 매수주도 비율 우선, 그다음 거래량/OI 비율 순
    surges.sort(key=lambda x: (x["buy_ratio"], x["ratio"]), reverse=True)
    return surges


# ====================================================
# ✅ 메인 루프
# ====================================================
symbols           = get_sp500_symbols()
results           = []
failed            = []
yf_fallback_count = 0
start_time        = time.time()

FAIL_REASON = {
    "ALPACA_EMPTY":    "Alpaca 옵션체인 비어있음",
    "ALPACA_DTE":      "DTE 범위(25~50일) 옵션 없음 (만기일 공백)",
    "ALPACA_NO_IV":    "Alpaca IV 값 없음",
    "ALPACA_RATELIMIT":"Alpaca Rate Limit / 장애",
    "PRICE_FAIL":      "주가 데이터 수집 실패",
    "UNKNOWN":         "알 수 없는 에러",
}
failed_details = []
alpaca_consecutive_fails = 0

for i, symbol in enumerate(symbols):
    print(f"[{i+1}/{len(symbols)}] {symbol} 수집 중...")
    row = collect_data(symbol)

    if row is None or "_fail_reason" in (row or {}):
        reason  = (row or {}).get("_fail_reason", "UNKNOWN")
        detail  = (row or {}).get("_fail_detail", "")
        label   = FAIL_REASON.get(reason, reason)

        print(f"  ❌ [{reason}] {label} | {detail}")
        failed.append(symbol)
        failed_details.append({"symbol": symbol, "reason": reason, "detail": detail})

        if reason == "ALPACA_RATELIMIT":
            alpaca_consecutive_fails += 1
            if alpaca_consecutive_fails >= 5:
                warn_msg = (
                    f"🚨 <b>Alpaca Rate Limit 감지</b>\n"
                    f"📅 {today} | {symbol} 포함 5개 연속 실패\n"
                    f"→ 60초 대기 후 재개"
                )
                print(f"\n  ⚠️ Alpaca 연속 {alpaca_consecutive_fails}회 실패 → 60초 대기\n")
                send_telegram(warn_msg)
                time.sleep(60)
                alpaca_consecutive_fails = 0
        else:
            alpaca_consecutive_fails = 0

        time.sleep(0.3)
        continue

    alpaca_consecutive_fails = 0
    results.append(row)
    if row.get("price_source") == "alpaca":
        yf_fallback_count += 1
    print(
        f"  ✅ [{row.get('price_source','?')}] "
        f"iv={row['avg_iv']} | hv20={row['hv20']} | "
        f"skew={row['skew']} | pcr_oi={row['pcr_oi']} | "
        f"pain={row['max_pain']} | rsi={row['rsi14']} | "
        f"gex={row['gex']} | dex={row['dex']} | "
        f"call_vol={row['call_vol']} | put_vol={row['put_vol']} | "
        f"call_vol_st={row.get('call_vol_st')} | call_oi_st={row.get('call_oi_st')} | "
        f"buy_ratio_st={row.get('call_buy_ratio_st')} | "
        f"bid={row.get('call_avg_bid_st')} | ask={row.get('call_avg_ask_st')} | "
        f"no_quote={row.get('call_no_quote_cnt_st')}"  # ✅ 로그 추가 (디버깅용)
    )
    time.sleep(0.3)

elapsed = round(time.time() - start_time)

# ====================================================
# ✅ 실패 원인 분류 요약
# ====================================================
if failed_details:
    from collections import Counter, defaultdict
    reason_counter = Counter(d["reason"] for d in failed_details)

    print("\n" + "="*55)
    print("📋 실패 원인 요약")
    print("="*55)
    for reason, count in reason_counter.most_common():
        label = FAIL_REASON.get(reason, reason)
        print(f"  {label}: {count}개")

    by_reason = defaultdict(list)
    for d in failed_details:
        by_reason[d["reason"]].append(d)

    for reason, items in by_reason.items():
        label = FAIL_REASON.get(reason, reason)
        print(f"\n  [{label}] 상위 샘플:")
        for d in items[:5]:
            print(f"    • {d['symbol']}: {d['detail']}")
    print("="*55 + "\n")

    dte_fail_count = reason_counter.get("ALPACA_DTE", 0)
    if dte_fail_count >= len(failed_details) * 0.5:
        dte_samples = [d["detail"] for d in by_reason["ALPACA_DTE"][:3]]
        send_telegram(
            f"📅 <b>만기일 공백 경보</b> — {today}\n"
            f"⚠️ DTE 25~50일 옵션 없는 종목: {dte_fail_count}개\n"
            f"→ 옵션 만기일 직후 공백 가능성\n"
            f"샘플: {' / '.join(dte_samples)}"
        )

    rate_fail_count = reason_counter.get("ALPACA_RATELIMIT", 0)
    if rate_fail_count >= 10:
        send_telegram(
            f"🚨 <b>Alpaca API 장애 의심</b> — {today}\n"
            f"Rate Limit / 장애 실패: {rate_fail_count}개\n"
            f"→ status.alpaca.markets 확인 권장"
        )

if results:
    save_csv(results, IV_COL_ORDER, "iv_data")

if failed:
    with open("failed_symbols.txt", "w") as f:
        f.write("\n".join(failed))
    if failed_details:
        pd.DataFrame(failed_details).to_csv("failed_details.csv", index=False, encoding="utf-8-sig")
        print(f"📄 실패 상세 저장: failed_details.csv ({len(failed_details)}건)")

print("\n📡 시장 전체 데이터 수집 중...")
market_row = collect_market_data()
if market_row:
    market_cols = ["date"] + [k for k in market_row.keys() if k != "date"]
    save_csv([market_row], market_cols, "market_data")
    print(
        f"   VIX={market_row.get('vix_close')} | "
        f"SPY={market_row.get('spy_close')} | "
        f"QQQ={market_row.get('qqq_close')} | "
        f"VIX Term={market_row.get('vix_term_spread')} | "
        f"SPY GEX={market_row.get('spy_gex')} | "
        f"QQQ GEX={market_row.get('qqq_gex')} | "
        f"IWM GEX={market_row.get('iwm_gex')}"
    )

# ====================================================
# ✅ 데이터 품질 이상 감지 → 텔레그램 즉시 경보
# ====================================================
total_symbols = len(symbols)
fail_count    = len(failed)
fail_rate     = fail_count / total_symbols if total_symbols > 0 else 0

if fail_rate >= ALERT_FAIL_RATE:
    send_telegram(
        f"🚨 <b>수집 실패율 경보</b>\n"
        f"📅 날짜: {today}\n"
        f"❌ 실패: {fail_count}/{total_symbols} ({fail_rate*100:.0f}%)\n"
        f"→ API 전반적 문제 또는 네트워크 오류 의심"
    )

data_alerts = check_data_quality(results, yf_fallback_count, total_symbols)
for alert in data_alerts:
    send_telegram(f"📅 {today}\n{alert}")
    print(f"  🚨 경보 발송: {alert[:50]}...")

market_alerts = check_market_data_quality(market_row or {})
for alert in market_alerts:
    send_telegram(f"📅 {today}\n{alert}")
    print(f"  🚨 경보 발송: {alert[:50]}...")

# ====================================================
# ✅ 최종 완료 텔레그램 알림
# ====================================================
success_count = len(results)
alert_count   = len(data_alerts) + len(market_alerts) + (1 if fail_rate >= ALERT_FAIL_RATE else 0)

if success_count > 0:
    msg = (
        f"📊 <b>IV 데이터 수집 완료</b>\n"
        f"📅 날짜: {today}\n"
        f"✅ 성공: {success_count}개 종목\n"
        f"❌ 실패: {fail_count}개 종목\n"
        f"🔄 Alpaca 폴백: {yf_fallback_count}개 종목\n"
        f"⏱ 소요시간: {elapsed//60}분 {elapsed%60}초\n"
        f"📈 수집항목: IV/HV/Skew/Greeks/GEX/PCR/MaxPain/RSI/Beta/MA/ATR/어닝"
    )
    if alert_count > 0:
        msg += f"\n🚨 데이터 이상 경보: {alert_count}건 (위 메시지 확인)"
    if fail_count > 0:
        msg += f"\n⚠️ 실패 종목: {', '.join(failed[:10])}"
        if fail_count > 10:
            msg += f" 외 {fail_count-10}개"
        if failed_details:
            from collections import Counter
            rc = Counter(d["reason"] for d in failed_details)
            reason_lines = [f"  • {FAIL_REASON.get(r,r)}: {c}개" for r, c in rc.most_common()]
            msg += "\n📋 실패 원인:\n" + "\n".join(reason_lines)
else:
    msg = f"❌ <b>IV 데이터 수집 실패</b>\n📅 날짜: {today}"

send_telegram(msg)

# ====================================================
# ✅ 콜 거래량 급등 감지 → 완료 메시지 다음에 별도 전송
# ====================================================
if results:
    call_surges = detect_call_surge(results)
    if call_surges:
        lines = [f"🚀 <b>콜 거래량 급등 감지</b> — {today}\n"]
        for s in call_surges[:15]:
            pcr_str = f"PCR={s['pcr_vol']:.2f}" if s["pcr_vol"] else "PCR=N/A"
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"콜거래량={s['call_vol']:,}  OI={s['call_oi']:,}  "
                f"비율={s['ratio']}x  {pcr_str}"
            )
        if len(call_surges) > 15:
            lines.append(f"... 외 {len(call_surges)-15}개")
        send_telegram("\n".join(lines))
        print(f"  📡 콜 급등 감지 {len(call_surges)}개 전송 완료")
    else:
        print("  ℹ️ 콜 거래량 급등 종목 없음")

    # ✅ 단기 콜 "매수주도" 급등 (인텔 사례처럼 뉴스 전 콜 매수 우위 패턴 겨냥)
    buyside_surges = detect_buyside_call_surge(results)
    if buyside_surges:
        lines = [f"🎯 <b>단기 콜 매수주도 급등 감지</b> — {today}",
                 f"(근월물 최근접 만기 · bid/ask 근사 · 참고용 지표)\n"]
        for s in buyside_surges[:15]:
            pcr_str    = f"PCR={s['pcr_vol_st']:.2f}" if s["pcr_vol_st"] else "PCR=N/A"
            bidask_str = (f"bid={s['avg_bid']:.2f}/ask={s['avg_ask']:.2f}"
                          f"(스프레드{s['avg_spread']}%)"
                          if s["avg_bid"] is not None and s["avg_ask"] is not None
                          else "bid/ask=N/A")
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"매수비율={s['buy_ratio']*100:.0f}% (매수{s['buy_cnt']}/매도{s['sell_cnt']})  "
                f"콜거래량={s['call_vol_st']:,}  OI={s['call_oi_st']:,}  "
                f"비율={s['ratio']}x  {pcr_str}\n"
                f"  {bidask_str}"
            )
        if len(buyside_surges) > 15:
            lines.append(f"... 외 {len(buyside_surges)-15}개")
        send_telegram("\n".join(lines))
        print(f"  📡 단기 콜 매수주도 급등 {len(buyside_surges)}개 전송 완료")
    else:
        # ✅ 진단용: 왜 못 잡았는지 콘솔에서 바로 확인 가능하도록
        checked_vals   = [r.get("call_checked_cnt_st") or 0 for r in results]
        no_quote_vals  = [r.get("call_no_quote_cnt_st") or 0 for r in results]
        total_checked  = sum(checked_vals)
        total_no_quote = sum(no_quote_vals)
        print(
            "  ℹ️ 단기 콜 매수주도 급등 종목 없음 "
            f"(전체 표본합계 checked={total_checked}, quote없음={total_no_quote} "
            f"→ quote없음 비율이 높으면 Alpaca 무료 feed의 커버리지 한계일 가능성)"
        )

print("=== IV Data Collector DONE ===")
