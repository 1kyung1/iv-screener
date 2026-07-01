import os
import io
import glob
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
    # ✅ [개선] 텔레그램 4096자 제한 대응: 줄바꿈 기준으로 분할 전송
    TELEGRAM_MAX_LEN = 4000
    chunks = []
    remain = message
    while len(remain) > TELEGRAM_MAX_LEN:
        cut = remain.rfind("\n", 0, TELEGRAM_MAX_LEN)
        if cut <= 0:
            cut = TELEGRAM_MAX_LEN
        chunks.append(remain[:cut])
        remain = remain[cut:].lstrip("\n")
    chunks.append(remain)

    for chunk in chunks:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=10)
            if resp.status_code != 200:
                print(f"⚠️ 텔레그램 전송 실패: HTTP {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"⚠️ 텔레그램 전송 실패: {e}")

# ====================================================
# ✅ CSV 파일 경로 헬퍼 (반기별 파일명)
# ====================================================
def csv_path_for(base_name: str, d: date) -> str:
    half = "H1" if d.month <= 6 else "H2"
    return f"{base_name}_{d.year}_{half}.csv"

def date_already_collected(d: date) -> bool:
    """해당 날짜 데이터가 이미 CSV에 있는지 확인 (지연 수집 시 덮어쓰기 방지)"""
    fp = csv_path_for("iv_data", d)
    if not os.path.exists(fp):
        return False
    try:
        existing = pd.read_csv(fp, usecols=["date"])
        return bool((existing["date"] == d.strftime("%Y-%m-%d")).any())
    except Exception:
        return False

# ====================================================
# ✅ [수정 1] 장중(ET 9:30~16:00) 실행 차단
#    - 장중 실행 시 yfinance 일봉에 미완성 봉이 포함되고,
#      옵션 거래량도 부분치가 전일 날짜로 저장되는 오염 발생.
#    - 옵션 거래량은 장마감 후 수집해야 '하루 전체' 값이 됨.
#    - 불가피하게 강제 실행하려면 환경변수 FORCE_INTRADAY_RUN=1
# ====================================================
_run_day_is_session = is_market_open(now_et.date())
_in_session = (
    _run_day_is_session
    and (now_et.hour, now_et.minute) >= (9, 30)
    and now_et.hour < 16
)
if _in_session and not os.getenv("FORCE_INTRADAY_RUN"):
    print("🚫 현재 미국 정규장 시간(ET 9:30~16:00)입니다. 데이터 오염 방지를 위해 중단합니다.")
    send_telegram(
        f"🚫 <b>IV 수집 중단</b>\n"
        f"현재 미국 정규장 시간(ET {now_et.strftime('%H:%M')})입니다.\n"
        f"장중 실행 시 미완성 봉·부분 거래량이 섞여 데이터가 오염됩니다.\n"
        f"→ 장마감(ET 16:00) 이후 다시 실행하세요."
    )
    exit(0)

# 장마감(ET 16:00) 전이면 하루 전 기준
et_date = now_et.date()
if now_et.hour < 16:
    et_date -= timedelta(days=1)

# ====================================================
# ✅ [수정 2] 휴장일이면 '직전 거래일'까지 롤백
#    - 기존: 월요일 새벽 실행 → 기준일=일요일 → 스킵 (금요일 데이터 유실)
#    - 변경: 직전 거래일까지 거슬러 올라가되,
#      이미 수집된 날짜면 스킵(정상 수집분 덮어쓰기 방지),
#      미수집이면 '지연 수집' 경고와 함께 진행.
# ====================================================
rolled_back = False
_roll_start = et_date
for _ in range(7):
    if is_market_open(et_date):
        break
    et_date -= timedelta(days=1)
    rolled_back = True
else:
    print(f"📅 {_roll_start} 기준 7일 내 거래일을 찾지 못했습니다. 스킵합니다.")
    exit(0)

today_date = et_date
today      = today_date.strftime("%Y-%m-%d")

print(f"🕐 ET 현재시각: {now_et.strftime('%Y-%m-%d %H:%M')} | 수집 기준일: {today}"
      + (" (직전 거래일로 롤백됨)" if rolled_back else ""))

if rolled_back:
    if date_already_collected(today_date):
        print(f"📅 {today} 데이터는 이미 수집되어 있습니다. (휴장일 실행 → 스킵)")
        exit(0)
    send_telegram(
        f"📅 <b>{today} 지연 수집 시작</b>\n"
        f"⚠️ 해당 거래일 직후가 아닌 {now_et.strftime('%m-%d %H:%M')}(ET)에 수집합니다.\n"
        f"→ 주가·OI는 정상이나, <b>옵션 거래량은 리셋되어 부정확할 수 있음</b>\n"
        f"→ 이 날짜의 거래량 기반 지표는 참고용으로만 사용 권장"
    )

# ✅ 실행일과 기준일이 다르면(다음날 아침 수집 등) 거래량 신뢰도 경고
stale_volume_risk = False
if now_et.date() > today_date and is_market_open(now_et.date()):
    stale_volume_risk = True
    print(f"⚠️ 기준일({today})과 실행일({now_et.date()})이 다릅니다. 옵션 거래량이 리셋됐을 수 있습니다.")

print(f"✅ 수집 기준일({today}) 장 운영일 확인")

# ====================================================
# ✅ API 초기화
# ====================================================
API_KEY      = os.getenv("ALPACA_API_KEY")
SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")

# ✅ [수정 3-1] 키 미설정 시 즉시 중단 (500종목 전부 실패 + 60초 대기 반복 방지)
if not API_KEY or not SECRET_KEY:
    print("🚨 ALPACA_API_KEY / ALPACA_SECRET_KEY 환경변수가 없습니다. 중단합니다.")
    send_telegram(
        f"🚨 <b>IV 수집 중단</b> — {today}\n"
        f"ALPACA API 키 환경변수가 설정되지 않았습니다."
    )
    exit(1)

opt_client   = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ✅ [수정 3-2] 시작 시 1회 테스트 호출로 인증 확인
#    (키 오류(403)를 Rate Limit으로 오인해 몇 시간 낭비하는 것 방지)
try:
    _auth_req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=7),
    )
    _ = stock_client.get_stock_bars(_auth_req)
    print("✅ Alpaca 인증 확인 완료")
except Exception as e:
    _es = str(e).lower()
    if any(k in _es for k in ["401", "403", "forbidden", "unauthorized"]):
        print(f"🚨 Alpaca 인증 실패(API 키 확인 필요): {e}")
        send_telegram(
            f"🚨 <b>Alpaca 인증 실패</b> — {today}\n"
            f"API 키가 잘못됐거나 만료된 것으로 보입니다.\n"
            f"→ 수집을 중단합니다.\n{str(e)[:200]}"
        )
        exit(1)
    print(f"⚠️ Alpaca 연결 테스트 경고(계속 진행): {e}")

# ✅ Alpaca 에러 분류 헬퍼 (인증 오류와 Rate Limit을 구분)
def classify_alpaca_error(e: Exception) -> str:
    s = str(e).lower()
    if any(k in s for k in ["401", "403", "forbidden", "unauthorized"]):
        return "ALPACA_AUTH"
    if any(k in s for k in ["429", "rate limit", "too many", "503", "502", "timeout"]):
        return "ALPACA_RATELIMIT"
    return "UNKNOWN"


# ====================================================
# ✅ 주가 데이터 수집 (yfinance 실패 시 Alpaca 폴백)
# ====================================================
def clip_to_basedate(hist):
    """✅ [수정 1 보강] 수집 기준일 이후의 봉 제거 — 미완성/다음날 봉 오염 방지"""
    try:
        if hist is None or hist.empty:
            return hist
        return hist[[d <= today_date for d in hist.index.date]]
    except Exception:
        return hist


def get_price_history(symbol: str, period_days: int = 365, ticker=None):
    yf_symbol = symbol.replace("-", ".")

    # 1차: yfinance (✅ [개선] 이미 만든 Ticker 객체가 있으면 재사용)
    try:
        yft  = ticker if ticker is not None else yf.Ticker(yf_symbol)
        hist = clip_to_basedate(yft.history(period=f"{period_days}d"))
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
        bars = clip_to_basedate(bars)
        print(f"    [주가] {symbol} Alpaca 폴백 OK ({len(bars)}일)")
        return bars, "alpaca"
    except Exception as e:
        print(f"    [주가] {symbol} Alpaca도 실패: {e}")
        return None, None


def get_intraday(symbol: str, ticker=None):
    yf_symbol = symbol.replace("-", ".")

    # 1차: yfinance
    try:
        yft      = ticker if ticker is not None else yf.Ticker(yf_symbol)
        intraday = clip_to_basedate(yft.history(interval="1m", period="1d"))
        if intraday is not None and not intraday.empty:
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


def get_ticker_info(symbol: str, field: str, default=None, ticker=None):
    try:
        yft  = ticker if ticker is not None else yf.Ticker(symbol.replace("-", "."))
        info = yft.info
        return info.get(field, default)
    except Exception:
        return default


def get_earnings_date(symbol: str, ticker=None):
    try:
        yft = ticker if ticker is not None else yf.Ticker(symbol.replace("-", "."))
        cal = yft.calendar
        if cal is not None and "Earnings Date" in cal:
            earn_date = cal["Earnings Date"]
            if isinstance(earn_date, list) and earn_date:
                return (pd.Timestamp(earn_date[0]).date() - today_date).days
            if isinstance(earn_date, pd.Timestamp):
                return (earn_date.date() - today_date).days
        ed = yft.earnings_dates
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
MINIMAL_FALLBACK = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "JPM", "TSLA", "UNH", "V"]

def get_sp500_symbols():
    # 1차: Wikipedia (✅ [수정 4-1] timeout 추가 — 무한 대기 방지)
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        sp500   = pd.read_html(io.StringIO(resp.text), flavor="html5lib")[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        symbols = [s for s in symbols if s not in EXCLUDE_SYMBOLS]
        if len(symbols) < 400:
            raise ValueError(f"파싱 결과 이상: {len(symbols)}개 (구조 변경 의심)")
        print(f"✅ S&P 500 종목 수: {len(symbols)} (제외: {len(EXCLUDE_SYMBOLS)}개)")
        return symbols
    except Exception as e:
        print(f"❌ S&P500 Wikipedia 로드 실패: {e}")
        wiki_err = str(e)[:150]

    # ✅ [수정 4-2] 2차 폴백: 직전 수집분 CSV의 심볼 목록 재사용
    #    (10종목 미니 리스트로 조용히 축소되어 하루치가 무의미해지는 것 방지)
    try:
        prev_symbols = []
        prev_date    = None
        for fp in sorted(glob.glob("iv_data_*_H*.csv"), reverse=True):
            df_prev = pd.read_csv(fp, usecols=["date", "symbol"])
            if df_prev.empty:
                continue
            prev_date    = df_prev["date"].max()
            prev_symbols = df_prev.loc[df_prev["date"] == prev_date, "symbol"].dropna().unique().tolist()
            if prev_symbols:
                break
        if len(prev_symbols) >= 100:
            print(f"♻️ 폴백: 직전 수집분({prev_date})의 {len(prev_symbols)}개 심볼 재사용")
            send_telegram(
                f"⚠️ <b>S&P500 목록 로드 실패</b> — {today}\n"
                f"Wikipedia 오류: {wiki_err}\n"
                f"→ 직전 수집분({prev_date})의 {len(prev_symbols)}개 심볼로 대체 진행\n"
                f"→ 지수 편입/제외 변동은 반영되지 않음"
            )
            return prev_symbols
    except Exception as e2:
        print(f"❌ CSV 심볼 폴백도 실패: {e2}")

    # 3차: 최소 목록 (✅ [수정 4-3] 조용히 축소되지 않도록 강한 경보)
    send_telegram(
        f"🚨 <b>S&P500 목록 전체 실패</b> — {today}\n"
        f"Wikipedia 오류: {wiki_err}\n"
        f"→ 대형주 {len(MINIMAL_FALLBACK)}개만 수집됩니다.\n"
        f"→ <b>오늘 데이터는 사실상 불완전</b>하니 원인 확인 필요"
    )
    return MINIMAL_FALLBACK

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
    """✅ [개선] 표준 Wilder 평활(EWM) 방식 RSI.
    기존 단순 rolling mean 버전과 값이 약간 다르며(보통 ±수 포인트),
    일반 차트 플랫폼의 RSI와 일치함. 기존 저장분과의 미세한 불연속은 감안할 것."""
    try:
        delta = closes.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/period, min_periods=period).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, min_periods=period).mean()
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
        oi_dte     = None
        for exp in exps:
            dte = (pd.Timestamp(exp).date() - today_date).days
            if 30 <= dte <= 45:
                target_exp, oi_dte = exp, dte
                break
        if not target_exp:
            for exp in exps:
                dte = (pd.Timestamp(exp).date() - today_date).days
                if 25 <= dte <= 50:
                    target_exp, oi_dte = exp, dte
                    break

        # ✅ [개선] 만기 공백 폴백 — 매달 월간만기 직전 1~1.5주간
        #    위클리 없는 종목(S&P 절반)은 25~50일 범위에 만기가 없어
        #    거래량/PCR/MaxPain이 통째로 결측되던 문제 해결.
        #    Alpaca 쪽(auto:Nd)과 동일하게 최근접 미래 만기로 강등 사용.
        #    (DTE 7일 이상을 우선, 없으면 그냥 최근접 미래 만기)
        if not target_exp:
            future = []
            for exp in exps:
                dte = (pd.Timestamp(exp).date() - today_date).days
                if dte > 0:
                    future.append((dte, exp))
            preferred = [t for t in future if t[0] >= 7]
            pick = min(preferred) if preferred else (min(future) if future else None)
            if pick:
                oi_dte, target_exp = pick
                print(f"    [yf OI 만기폴백] {target_exp} (DTE {oi_dte}일) 사용")
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
            "oi_exp_dte": oi_dte,   # ✅ 어떤 만기(DTE)를 사용했는지 기록 (폴백 추적용)
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
STALE_TRADE_SECONDS = 900   # quote 시각과 체결 시각이 이보다 더 벌어지면 "오래된 체결"로 간주해 제외
MIN_CONTRACT_PRICE  = 0.05  # ask가 이보다 낮은 로또성 딥아웃오브더머니 계약은 노이즈가 커서 제외

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

    ⚠️ 필터링 근거 (실제 AAPL 테스트에서 발견된 문제):
    - 딥아웃오브더머니 로또성 계약(bid=0, ask=0.01~0.03)은 latest_trade가
      latest_quote보다 훨씬 예전 체결인 경우가 흔해서, 체결가가 현재
      ask보다도 높게 나오는(pos>1) 왜곡된 결과가 나옴. → MIN_CONTRACT_PRICE로 제외.
    - quote 시각과 trade 시각이 크게 벌어진 경우도 같은 이유로 신뢰 불가.
      → STALE_TRADE_SECONDS로 제외.

    call_no_quote_cnt_st: bid/ask 또는 체결가가 없거나(quote 자체 부재),
    로또성/오래된 체결이라 판단 불가했던 계약 수. 이 값이 크면 데이터가
    없는 게 아니라 "신뢰할 수 없어서 제외"한 것일 수 있음.
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
        if quote is None or trade is None:
            no_quote_cnt += 1
            continue

        bid   = getattr(quote, "bid_price", None)
        ask   = getattr(quote, "ask_price", None)
        price = getattr(trade, "price", None)
        size  = getattr(trade, "size", None) or 0

        # ✅ 로또성 딥아웃오브더머니 계약 제외 (bid/ask 왜곡이 심함)
        if ask is None or ask < MIN_CONTRACT_PRICE:
            no_quote_cnt += 1
            continue

        # ✅ 체결이 현재 호가보다 너무 오래됐으면 제외 (stale trade)
        quote_ts = getattr(quote, "timestamp", None)
        trade_ts = getattr(trade, "timestamp", None)
        if quote_ts is not None and trade_ts is not None:
            try:
                age_sec = abs((quote_ts - trade_ts).total_seconds())
                if age_sec > STALE_TRADE_SECONDS:
                    no_quote_cnt += 1
                    continue
            except Exception:
                pass

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
    total_notional = buy_notional + sell_notional
    buy_notional_ratio_st = round(buy_notional / total_notional, 4) if total_notional > 0 else None
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
        "call_buy_notional_ratio_st": buy_notional_ratio_st,  # ✅ 계약수 대신 금액(계약수×체결가×100) 기준 비율
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
        # ✅ [개선] 종목당 yf.Ticker를 1회만 생성해 재사용 (기존 5~6회 생성 → 호출량·차단 위험 감소)
        yft = yf.Ticker(symbol.replace("-", "."))

        hist, price_source = get_price_history(symbol, period_days=365, ticker=yft)

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
                intraday = get_intraday(symbol, ticker=yft)
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

        beta_val = get_ticker_info(symbol, "beta", ticker=yft)
        beta = round(float(beta_val), 3) if beta_val is not None else None

        days_to_earn = get_earnings_date(symbol, ticker=yft)

        yf_ticker = yft
        oi_m       = calc_oi_metrics(yf_ticker)
        call_oi    = oi_m["call_oi"]  if oi_m else 0
        put_oi     = oi_m["put_oi"]   if oi_m else 0
        call_vol   = oi_m["call_vol"] if oi_m else 0  # ✅ 변수 보존
        put_vol    = oi_m["put_vol"]  if oi_m else 0  # ✅ 변수 보존
        pcr_oi     = oi_m["pcr_oi"]   if oi_m else None
        pcr_vol    = oi_m["pcr_vol"]  if oi_m else None
        max_pain   = oi_m["max_pain"] if oi_m else None
        oi_exp_dte = oi_m.get("oi_exp_dte") if oi_m else None

        # ✅ 단기(근월물) 콜/풋 거래량·OI (yfinance, 가장 가까운 만기 하나)
        oi_st       = calc_near_term_oi_metrics(yf_ticker)
        call_oi_st  = oi_st["call_oi_st"]  if oi_st else 0
        call_vol_st = oi_st["call_vol_st"] if oi_st else 0
        put_vol_st  = oi_st["put_vol_st"]  if oi_st else 0
        pcr_vol_st  = oi_st["pcr_vol_st"]  if oi_st else None
        exp_key_st  = oi_st["exp_key_st"]  if oi_st else None
        exp_st      = oi_st["target_exp_st"] if oi_st else None  # ✅ OI 전일比 비교 시 같은 만기인지 확인용

        alpaca_symbol = symbol.replace("-", ".")
        try:
            req     = OptionChainRequest(underlying_symbol=alpaca_symbol)
            chain   = opt_client.get_option_chain(req)
            options = list(chain.values())
        except Exception as e:
            # ✅ [수정 3-3] 인증 오류(401/403)를 Rate Limit과 분리해 분류
            return {"_fail_reason": classify_alpaca_error(e), "_fail_detail": str(e)}

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

        skew       = round(avg_put - avg_call, 4) if (avg_call is not None and avg_put is not None) else None
        iv_hv_diff = round(avg_iv - hv20, 4)      if hv20 is not None                              else None

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

        iv_term_slope = round(iv_60 - iv_30, 4) if (iv_30 is not None and iv_60 is not None) else None

        pain_diff = None
        if max_pain is not None and cur_price:
            pain_diff = round((cur_price - max_pain) / cur_price * 100, 2)

        return {
            "date":           today,
            "symbol":         symbol,
            "price_source":   price_source,
            "dte_range":      dte_range_used,
            "oi_exp_dte":     oi_exp_dte,   # ✅ yf OI 계산에 사용된 만기 DTE (폴백 여부 추적)
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
            "exp_st":         exp_st,         # ✅ 근월물 만기일 (OI 전일比 비교 시 동일 만기 확인용)
            "call_buy_ratio_st":    flow_st["call_buy_ratio_st"],    # ✅ 매수주도 비율(근사, 계약수 기준)
            "call_buy_notional_ratio_st": flow_st["call_buy_notional_ratio_st"],  # ✅ 금액 기준 매수비율
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
        reason = classify_alpaca_error(e)
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
                    h = clip_to_basedate(yf.Ticker(ticker_sym).history(period="60d"))
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
                        bars.index = bars.index.tz_localize(None)
                        hist = clip_to_basedate(bars)
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
        if v9d is not None and v is not None and v3m is not None:
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
                    h = clip_to_basedate(yf.Ticker(etf).history(period="60d"))
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
                        bars.index = bars.index.tz_localize(None)
                        hist = clip_to_basedate(bars)
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
    "date", "symbol", "price_source", "dte_range", "oi_exp_dte",   # ✅ oi_exp_dte 추가
    "open", "high", "low", "close", "volume", "vwap", "vwap_diff",
    "cur_price",
    "avg_iv", "atm_call_iv", "atm_put_iv", "skew", "iv_hv_diff",
    "iv_30d", "iv_45d", "iv_60d", "iv_term_slope",
    "hv10", "hv20", "hv60",
    "avg_delta", "avg_gamma", "avg_theta", "avg_vega", "avg_rho",
    "gex", "gex_call", "gex_put", "dex",
    "pcr_oi", "pcr_vol", "call_oi", "put_oi",
    "call_vol", "put_vol",   # ✅ 추가
    "call_oi_st", "call_vol_st", "put_vol_st", "pcr_vol_st", "exp_st",  # ✅ exp_st 추가
    "call_buy_ratio_st", "call_buy_notional_ratio_st", "call_buy_cnt_st", "call_sell_cnt_st",  # ✅ 매수주도 근사
    "call_checked_cnt_st", "call_no_quote_cnt_st", "call_buy_notional_st",
    "call_avg_bid_st", "call_avg_ask_st", "call_avg_spread_pct_st",    # ✅ 실제 bid/ask 값
    "max_pain", "pain_diff",
    "rsi14", "beta", "week52_pos", "vol_ratio",
    "ret_1d", "ret_5d", "ret_20d", "atr14",
    "ma20", "ma50", "ma200", "price_vs_ma200", "golden_cross",
    "days_to_earn", "sample_count",
]

def save_csv(results: list, col_order: list, base_name: str):
    file_path = csv_path_for(base_name, today_date)
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

    # ✅ [수정 1 보강] 옵션 거래량 리셋 자가진단
    #    (다음날 아침 실행 등으로 거래량이 새 날 기준 0으로 잡힌 경우 감지)
    if "call_vol" in df.columns and df["call_vol"].notna().any():
        zero_rate = (df["call_vol"].fillna(0) == 0).mean()
        if zero_rate >= 0.8:
            alerts.append(
                f"🚨 <b>옵션 거래량 리셋 의심</b>\n"
                f"   call_vol=0 종목 비율: {zero_rate*100:.0f}%\n"
                f"   → 장마감 이후가 아닌 시각에 수집됐을 가능성\n"
                f"   → 이 날짜의 거래량/콜급등 지표는 신뢰 불가"
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
        buy_ratio_notional = row.get("call_buy_notional_ratio_st")
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
            "buy_ratio_notional": buy_ratio_notional,
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
# ✅ [개선] 축적 CSV 기반 "이례성" 감지 2종
#    인텔 사례("뉴스 전날 콜이 이례적으로 많이 들어옴")를 겨냥한 핵심 업그레이드.
#    vol/OI 스냅샷 비율만으로는 "그 종목치고 이례적인가"를 알 수 없으므로,
#    그동안 쌓아온 CSV를 활용해 자기 자신의 과거와 비교한다.
#
#    (a) 상대 거래량: 오늘 근월물 콜 거래량 ÷ 자기 최근 N일 평균
#        → 데이트레이딩 회전이 많은 종목도 "평소 대비" 기준으로 정규화됨
#    (b) OI 급증: 근월물 콜 OI가 전 거래일 대비 급증
#        → OI는 익일 반영되므로 "전일 세션에서 신규 포지션이 실제로
#          개설됐다"는 확증. 거래량만으로는 회전/매집 구분이 안 되는데
#          OI 증가는 이를 구분해준다. (같은 만기끼리만 비교)
# ====================================================
REL_VOL_LOOKBACK    = 20    # 상대 거래량 비교 기간(일)
REL_VOL_MIN_DAYS    = 3     # 최소 필요 과거 관측일 수 (데이터 쌓이기 전엔 자동 비활성)
REL_VOL_THRESHOLD   = 3.0   # 평소 대비 3배 이상
REL_VOL_MIN_VOL     = 200   # 최소 절대 거래량
OI_CHANGE_THRESHOLD = 1.5   # 전일 대비 1.5배 이상
OI_CHANGE_MIN_ABS   = 500   # 최소 절대 증가 계약수

def load_history_csv():
    """지금까지 축적된 iv_data CSV 전체 로드 (반기 파일 경계 자동 처리, 오늘 제외)"""
    need_cols = {"date", "symbol", "call_vol_st", "call_oi_st", "exp_st"}
    frames = []
    for fp in sorted(glob.glob("iv_data_*_H*.csv")):
        try:
            frames.append(pd.read_csv(fp, usecols=lambda c: c in need_cols))
        except Exception as e:
            print(f"  ⚠️ 히스토리 로드 실패({fp}): {e}")
    if not frames:
        return None
    hist = pd.concat(frames, ignore_index=True)
    hist = hist[hist["date"] < today]   # 오늘 저장분 제외
    return hist if not hist.empty else None


def detect_unusual_activity(results: list, hist_df):
    rel_surges, oi_surges = [], []
    if hist_df is None or hist_df.empty:
        return rel_surges, oi_surges

    grouped = {sym: g.sort_values("date") for sym, g in hist_df.groupby("symbol")}

    for row in results:
        sym = row["symbol"]
        h   = grouped.get(sym)
        if h is None or h.empty:
            continue

        # (a) 상대 거래량 급등
        cv = row.get("call_vol_st") or 0
        if "call_vol_st" in h.columns:
            past = h["call_vol_st"].dropna()
            past = past[past > 0].tail(REL_VOL_LOOKBACK)
            if cv >= REL_VOL_MIN_VOL and len(past) >= REL_VOL_MIN_DAYS:
                avg = float(past.mean())
                if avg > 0 and cv / avg >= REL_VOL_THRESHOLD:
                    rel_surges.append({
                        "symbol":   sym,
                        "call_vol": int(cv),
                        "avg_vol":  int(round(avg)),
                        "ratio":    round(cv / avg, 1),
                        "days":     len(past),
                        "pcr":      row.get("pcr_vol_st"),
                    })

        # (b) OI 급증 (전일 매집 확인) — 같은 만기끼리만 비교
        oi     = row.get("call_oi_st") or 0
        exp_st = row.get("exp_st")
        if exp_st and "exp_st" in h.columns and "call_oi_st" in h.columns:
            h2 = h.dropna(subset=["call_oi_st"])
            h2 = h2[h2["exp_st"] == exp_st]      # 만기가 롤오버되면 비교 무의미 → 동일 만기만
            if not h2.empty:
                prev      = h2.iloc[-1]
                prev_oi   = float(prev["call_oi_st"])
                prev_date = prev["date"]
                if (prev_oi > 0 and oi - prev_oi >= OI_CHANGE_MIN_ABS
                        and oi / prev_oi >= OI_CHANGE_THRESHOLD):
                    oi_surges.append({
                        "symbol":    sym,
                        "oi":        int(oi),
                        "prev_oi":   int(prev_oi),
                        "prev_date": prev_date,
                        "ratio":     round(oi / prev_oi, 1),
                        "exp":       exp_st,
                    })

    rel_surges.sort(key=lambda x: x["ratio"], reverse=True)
    oi_surges.sort(key=lambda x: x["ratio"], reverse=True)
    return rel_surges, oi_surges


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
    "ALPACA_AUTH":     "Alpaca 인증 오류 (API 키 확인 필요)",   # ✅ [수정 3] 분리
    "PRICE_FAIL":      "주가 데이터 수집 실패",
    "UNKNOWN":         "알 수 없는 에러",
}
failed_details = []
alpaca_consecutive_fails = 0
auth_consecutive_fails   = 0   # ✅ [수정 3] 인증 오류 연속 카운터

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

        if reason == "ALPACA_AUTH":
            # ✅ [수정 3] 인증 오류는 재시도해도 소용없음 → 3회 연속이면 즉시 중단
            auth_consecutive_fails += 1
            if auth_consecutive_fails >= 3:
                send_telegram(
                    f"🚨 <b>Alpaca 인증 오류로 수집 중단</b> — {today}\n"
                    f"{symbol} 포함 3개 연속 401/403 발생\n"
                    f"→ API 키 확인 후 재실행 필요\n"
                    f"→ 지금까지 수집된 {len(results)}개는 저장합니다."
                )
                print("\n  🚨 인증 오류 3회 연속 → 루프 중단\n")
                break
        elif reason == "ALPACA_RATELIMIT":
            auth_consecutive_fails = 0
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
            auth_consecutive_fails   = 0

        time.sleep(0.3)
        continue

    alpaca_consecutive_fails = 0
    auth_consecutive_fails   = 0
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
    if stale_volume_risk:
        msg += f"\n⚠️ 기준일 다음날 수집됨 → 옵션 거래량은 참고용"
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
            notional_str = (f"금액기준={s['buy_ratio_notional']*100:.0f}%"
                             if s["buy_ratio_notional"] is not None else "금액기준=N/A")
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"매수비율={s['buy_ratio']*100:.0f}% ({notional_str}) "
                f"(매수{s['buy_cnt']}/매도{s['sell_cnt']})  "
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

    # ====================================================
    # ✅ [개선] 축적 데이터 기반 이례성 감지 (상대 거래량 + OI 급증)
    # ====================================================
    hist_df = load_history_csv()
    rel_surges, oi_surges = detect_unusual_activity(results, hist_df)

    if rel_surges:
        lines = [f"📈 <b>평소 대비 콜 거래량 급등</b> — {today}",
                 f"(자기 자신의 최근 {REL_VOL_LOOKBACK}일 평균과 비교)\n"]
        for s in rel_surges[:15]:
            pcr_str = f"PCR={s['pcr']:.2f}" if s["pcr"] is not None else "PCR=N/A"
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"오늘={s['call_vol']:,}  평소={s['avg_vol']:,}  "
                f"<b>{s['ratio']}배</b>  {pcr_str} (표본 {s['days']}일)"
            )
        if len(rel_surges) > 15:
            lines.append(f"... 외 {len(rel_surges)-15}개")
        send_telegram("\n".join(lines))
        print(f"  📡 상대 거래량 급등 {len(rel_surges)}개 전송 완료")
    else:
        print("  ℹ️ 상대 거래량 급등 종목 없음 (또는 과거 표본 부족)")

    if oi_surges:
        lines = [f"🧲 <b>근월물 콜 OI 급증 감지</b> — {today}",
                 f"(전 거래일 대비, 동일 만기 기준 — 전일 세션의 신규 포지션 개설 확증)\n"]
        for s in oi_surges[:15]:
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"OI {s['prev_oi']:,} → {s['oi']:,} (<b>{s['ratio']}배</b>)  "
                f"만기={s['exp']} (기준일 {s['prev_date']})"
            )
        if len(oi_surges) > 15:
            lines.append(f"... 외 {len(oi_surges)-15}개")
        send_telegram("\n".join(lines))
        print(f"  📡 콜 OI 급증 {len(oi_surges)}개 전송 완료")
    else:
        print("  ℹ️ 콜 OI 급증 종목 없음 (또는 동일 만기 과거 표본 없음)")

print("=== IV Data Collector DONE ===")
