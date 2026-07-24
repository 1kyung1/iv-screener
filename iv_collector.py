SCRIPT_VERSION = "v3.2 (2026-07-24)"   # ✅ 배포 확인용: 수집 완료 텔레그램에 표시됨

import os
import io
import sys
import json
import glob
import html as _html
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

_NYSE_CAL = None   # ✅ [v3.1] 캘린더 객체 1회 생성 후 재사용 (롤백 루프에서 최대 8회 생성되던 것 정리)

def is_market_open(check_date: date) -> bool:
    global _NYSE_CAL
    try:
        if _NYSE_CAL is None:
            _NYSE_CAL = xcals.get_calendar("XNYS")
        return _NYSE_CAL.is_session(check_date.strftime("%Y-%m-%d"))
    except Exception:
        return check_date.weekday() < 5

# ====================================================
# ✅ 텔레그램 알림
# ====================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def esc(s) -> str:
    """✅ [v3.1 핵심수정] 텔레그램 parse_mode=HTML용 이스케이프.
    API 에러 문자열 등에 <, >, & 가 포함되면 텔레그램이 400을 반환하고
    그 알림 전체가 조용히 유실됨 → 외부에서 온 문자열은 반드시 이걸 거칠 것."""
    return _html.escape(str(s), quote=False)


def _balance_html_chunks(chunks):
    """✅ [v3.1 핵심수정] 4000자 분할 경계에서 <b>...</b>가 갈라지면
    양쪽 chunk 모두 'can't parse entities' 400으로 실패 → 태그 짝 맞추기.
    (급등 종목이 많은 날 = 메시지가 가장 긴 날 = 가장 중요한 알림이 유실되던 구조)"""
    fixed = []
    carry_open = 0
    for ch in chunks:
        ch = ("<b>" * carry_open) + ch
        carry_open = max(ch.count("<b>") - ch.count("</b>"), 0)
        ch = ch + ("</b>" * carry_open)
        fixed.append(ch)
    return fixed


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
    chunks = _balance_html_chunks(chunks)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in chunks:
        sent = False
        # ✅ [v3.1] 1차: HTML → 실패 시 2차: 태그 제거 plain text (알림 유실 방지 최후 폴백)
        payloads = [
            {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
            {"chat_id": TELEGRAM_CHAT_ID,
             "text": chunk.replace("<b>", "").replace("</b>", "")},
        ]
        for attempt, payload in enumerate(payloads):
            try:
                resp = requests.post(url, data=payload, timeout=10)
                if resp.status_code == 200:
                    sent = True
                    break
                if resp.status_code == 429:
                    # ✅ [v3.1] 다건 연속 전송 시 rate limit → retry_after만큼 대기 후 1회 재시도
                    retry_after = 5
                    try:
                        retry_after = int(resp.json()["parameters"]["retry_after"])
                    except Exception:
                        pass
                    time.sleep(min(retry_after, 30))
                    resp = requests.post(url, data=payload, timeout=10)
                    if resp.status_code == 200:
                        sent = True
                        break
                print(f"⚠️ 텔레그램 전송 실패(시도{attempt+1}): HTTP {resp.status_code} - {resp.text[:150]}")
            except Exception as e:
                print(f"⚠️ 텔레그램 전송 실패(시도{attempt+1}): {e}")
        if not sent:
            print(f"🚨 텔레그램 메시지 최종 유실: {chunk[:80]}...")
        time.sleep(0.3)   # 연속 전송 간격

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
    sys.exit(0)

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
    sys.exit(0)

today_date = et_date
today      = today_date.strftime("%Y-%m-%d")

print(f"🕐 ET 현재시각: {now_et.strftime('%Y-%m-%d %H:%M')} | 수집 기준일: {today}"
      + (" (직전 거래일로 롤백됨)" if rolled_back else ""))

if rolled_back:
    if date_already_collected(today_date):
        print(f"📅 {today} 데이터는 이미 수집되어 있습니다. (휴장일 실행 → 스킵)")
        sys.exit(0)
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

# ✅ [v3.1 수정] 같은 날 재실행 감지 → CSV는 갱신하되 급등 알림 재전송은 억제
#    (기존엔 rolled_back일 때만 체크 → 정상 수집일 저녁에 두 번 돌리면 알림 4종이 전부 중복 발송)
ALERT_SUPPRESS = False
if not rolled_back and date_already_collected(today_date):
    ALERT_SUPPRESS = True
    print(f"♻️ {today} 데이터가 이미 존재 → 재수집 모드 (CSV 갱신, 급등 알림은 중복 방지 위해 생략)")
    send_telegram(
        f"♻️ <b>{today} 재수집 시작</b>\n"
        f"이미 수집된 날짜입니다 → CSV는 최신값으로 갱신되며,\n"
        f"급등 알림은 중복 방지를 위해 이번 실행에서 생략합니다."
    )

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
    sys.exit(1)

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
            f"→ 수집을 중단합니다.\n{esc(str(e)[:200])}"   # ✅ [v3.1] HTML 이스케이프
        )
        sys.exit(1)
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
# ✅ [v3.1 수정] yfinance .info 7일 캐시
#    .info는 yfinance에서 가장 무거운 호출인데 여기서 뽑는 값들
#    (베타/공매도잔량/시총/섹터/배당락일)은 격주~분기 단위로만 갱신됨.
#    → 7일 캐시로 하루 500회 호출을 주 1회로 축소 (429 차단 위험의 최대 원인 제거)
#    공매도잔량은 어차피 거래소가 격주 공시라 데이터 손실 없음.
# ====================================================
INFO_CACHE_FILE     = "info_cache.json"
INFO_CACHE_TTL_DAYS = 7

def _load_info_cache():
    try:
        with open(INFO_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

_info_cache       = _load_info_cache()
_info_cache_dirty = False

def get_cached_info(symbol: str, yft) -> dict:
    """TTL 이내면 캐시 반환, 아니면 .info 호출 후 필요한 키만 캐시에 저장.
    이번 호출이 실패하면 만료된 캐시라도 재사용 (없는 것보단 나음)."""
    global _info_cache_dirty
    ent = _info_cache.get(symbol)
    try:
        if ent and (today_date - date.fromisoformat(ent["ts"])).days < INFO_CACHE_TTL_DAYS:
            return ent
    except Exception:
        ent = None

    info = {}
    try:
        info = yft.info or {}
    except Exception:
        info = {}

    if not info:
        return ent or {}

    new_ent = {
        "ts":                  today,
        "beta":                info.get("beta"),
        "shortPercentOfFloat": info.get("shortPercentOfFloat"),
        "marketCap":           info.get("marketCap"),
        "sector":              info.get("sector"),
        "exDividendDate":      info.get("exDividendDate"),
    }
    _info_cache[symbol] = new_ent
    _info_cache_dirty   = True
    return new_ent


def save_info_cache():
    global _info_cache_dirty
    if not _info_cache_dirty:
        return
    try:
        tmp = INFO_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_info_cache, f)
        os.replace(tmp, INFO_CACHE_FILE)   # 원자적 교체
        print(f"💾 info 캐시 저장: {len(_info_cache)}종목")
        _info_cache_dirty = False
    except Exception as e:
        print(f"⚠️ info 캐시 저장 실패: {e}")


# ====================================================
# ✅ [v3.1 추가] 어닝 날짜 7일 캐시 (7/4 리팩토링 결정 재적용)
#    yf calendar/earnings_dates는 종목당 1~2회/일 = 500회+ 호출.
#    절대 날짜를 캐시하고 days_to_earn은 매일 재계산 → 호출 1/7로.
#    캐시된 어닝일이 지나면 즉시 재조회(다음 분기 일정 갱신).
#    한계: 어닝 일정이 변경되면 최대 7일 지연 반영.
# ====================================================
EARN_CACHE_FILE = "earnings_cache.json"

def _load_earn_cache():
    try:
        with open(EARN_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

_earn_cache       = _load_earn_cache()
_earn_cache_dirty = False

def get_cached_earnings_days(symbol: str, yft):
    global _earn_cache_dirty
    ent = _earn_cache.get(symbol)
    if ent:
        try:
            fetched_d = date.fromisoformat(ent["ts"])
            e_str = ent.get("earn_date")
            e_d   = date.fromisoformat(e_str) if e_str else None
            fresh = (today_date - fetched_d).days < INFO_CACHE_TTL_DAYS
            # 캐시가 신선하고, 어닝일이 아직 안 지났으면(또는 어닝일 없음) API 호출 생략
            if fresh and (e_d is None or e_d >= today_date):
                return (e_d - today_date).days if e_d else None
        except Exception:
            pass
    d = get_earnings_date(symbol, ticker=yft)
    e_str = (today_date + timedelta(days=d)).isoformat() if d is not None else None
    _earn_cache[symbol] = {"ts": today, "earn_date": e_str}
    _earn_cache_dirty = True
    return d


def save_earn_cache():
    global _earn_cache_dirty
    if not _earn_cache_dirty:
        return
    try:
        tmp = EARN_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_earn_cache, f)
        os.replace(tmp, EARN_CACHE_FILE)
        print(f"💾 어닝 캐시 저장: {len(_earn_cache)}종목")
        _earn_cache_dirty = False
    except Exception as e:
        print(f"⚠️ 어닝 캐시 저장 실패: {e}")


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


def yf_with_backoff(fn, retries=1, wait=20, label="yf"):
    """✅ [v3.1 추가] yfinance 429/차단 의심 시 대기 후 재시도하는 공용 래퍼.
    .info 캐시 적용 후엔 옵션 체인(종목당 3회 × 500종목 ≈ 1,500회/일)이
    yfinance 호출량의 최대 지분인데 기존엔 백오프가 주가에만 있었음.
    야후가 중간에 막으면 OI/PCR/MaxPain이 그 시점부터 전부 결측되던 구멍을 방어."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            es = str(e).lower()
            if attempt < retries and any(k in es for k in ["429", "too many", "rate limit", "crumb"]):
                print(f"    [yf백오프] {label}: 차단 의심 → {wait}초 대기 후 재시도")
                time.sleep(wait)
                continue
            raise


def get_price_history(symbol: str, period_days: int = 365, ticker=None):
    yf_symbol = symbol.replace("-", ".")

    # 1차: yfinance (✅ [개선] 이미 만든 Ticker 객체가 있으면 재사용)
    # ✅ [v3.1] 429(Too Many Requests) 감지 시 20초 대기 후 1회 재시도
    #    yfinance가 한번 차단되면 이후 종목 전부 Alpaca 폴백으로 넘어가던 것 완화
    for _attempt in range(2):
        try:
            yft  = ticker if ticker is not None else yf.Ticker(yf_symbol)
            hist = clip_to_basedate(yft.history(period=f"{period_days}d"))
            if not hist.empty and len(hist) > 10:
                print(f"    [주가] {symbol} yfinance OK ({len(hist)}일)")
                return hist, "yfinance"
            break
        except Exception as e:
            es = str(e)
            if _attempt == 0 and ("429" in es or "Too Many" in es or "rate" in es.lower()):
                print(f"    [주가] {symbol} yfinance 429 의심 → 20초 대기 후 재시도")
                time.sleep(20)
                continue
            print(f"    [주가] {symbol} yfinance 실패: {e}")
            break

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


def _clip_intraday_session(intraday):
    """✅ [v3.1 핵심수정] 분봉을 '수집 기준일의 정규장(ET 9:30~16:00)'으로 한정.
    기존 문제:
      - Alpaca 폴백은 start=now-1day라 전일 애프터마켓~당일 프리마켓 봉이 섞인 채
        cumsum VWAP을 계산 → 폴백 종목만 vwap 값이 오염됨
      - yfinance(정규장만) vs Alpaca(시간외 포함)로 같은 컬럼의 정의가 소스마다 달랐음
    → 양쪽 모두 ET-naive 인덱스로 통일 후 기준일 정규장만 남김 (VWAP 정의 일원화)"""
    try:
        if intraday is None or intraday.empty:
            return None
        mask = [(ts.date() == today_date)
                and ((ts.hour, ts.minute) >= (9, 30))
                and (ts.hour < 16)
                for ts in intraday.index]
        out = intraday[mask]
        return out if not out.empty else None
    except Exception:
        return intraday


def _to_et_naive(df):
    """tz-aware 인덱스(UTC/ET)를 ET-naive로 통일"""
    try:
        if df.index.tz is not None:
            df.index = df.index.tz_convert(ET_TZ).tz_localize(None)
    except Exception:
        pass
    return df


def get_intraday(symbol: str, ticker=None):
    yf_symbol = symbol.replace("-", ".")

    # 1차: yfinance
    try:
        yft      = ticker if ticker is not None else yf.Ticker(yf_symbol)
        intraday = yft.history(interval="1m", period="1d")
        if intraday is not None and not intraday.empty:
            intraday = _clip_intraday_session(_to_et_naive(intraday))
            if intraday is not None:
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
        # ✅ [v3.1] 기존엔 UTC를 그대로 naive화 → 날짜/시간 필터가 어긋남. ET로 변환 후 naive화.
        bars = _to_et_naive(bars)
        return _clip_intraday_session(bars)
    except Exception:
        return None


def get_ticker_info(symbol: str, field: str, default=None, ticker=None):
    try:
        yft  = ticker if ticker is not None else yf.Ticker(symbol.replace("-", "."))
        info = yft.info
        return info.get(field, default)
    except Exception:
        return default


def _chain_premium(df):
    """✅ [v3.2] 옵션 체인의 거래대금($) = Σ(거래량 × 계약가격 × 100).

    ⚠️ v3.1까지는 lastPrice만 썼는데, lastPrice는 '마지막 체결가'라
    유동성이 얕은 행사가에서는 며칠 전 가격이 그대로 남아 있음.
    실측 결과 lastPrice가 당시 [bid, ask] 밴드를 벗어난 비율이 20.9%였음.
    → (bid+ask)/2를 우선 쓰고, 호가가 없을 때만 lastPrice로 폴백한다.
    """
    def _col(name):
        # yfinance가 컬럼 구조를 바꿔도 죽지 않도록 방어
        if name not in df.columns:
            return pd.Series(0.0, index=df.index)
        return pd.to_numeric(df[name], errors="coerce").fillna(0)

    try:
        if df is None or len(df) == 0:
            return 0
        vol  = _col("volume")
        last = _col("lastPrice")
        mid  = (_col("bid") + _col("ask")) / 2
        px   = mid.where(mid > 0, last)      # 호가 없으면 lastPrice 폴백
        return int((vol * px).sum() * 100)
    except Exception:
        return None


def get_earnings_date(symbol: str, ticker=None):
    try:
        yft = ticker if ticker is not None else yf.Ticker(symbol.replace("-", "."))
        cal = yft.calendar
        if cal is not None and "Earnings Date" in cal:
            earn_date = cal["Earnings Date"]
            # ✅ [v3.2 수정] yfinance calendar는 '지난' 실적일을 그대로 담고 있는
            #    경우가 있어 v3.1까지는 days_to_earn이 음수로 저장됐음(실측 6.3%).
            #    미래 날짜만 채택하고, 없으면 아래 earnings_dates 분기로 넘긴다.
            cands = earn_date if isinstance(earn_date, list) else [earn_date]
            future = sorted(
                d for d in (pd.Timestamp(x).date() for x in cands if x is not None)
                if d >= today_date
            )
            if future:
                return (future[0] - today_date).days
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
# ✅ [v3.1] 분봉 VWAP 수집 기본 OFF
#    - 이전 결정(7/4 리팩토링)대로: 종목당 1분봉 호출 500회/일이 yfinance
#      차단 위험 대비 중요도가 낮아 제외. 이 계보 파일에 누락돼 있던 것 재적용.
#    - vwap/vwap_diff 컬럼은 스키마 유지를 위해 남기고 값만 null로 저장
#      (컬럼을 빼면 save_csv 재정렬 과정에서 기존 vwap 데이터가 삭제됨)
#    - 다시 켜려면 환경변수 COLLECT_VWAP=1
COLLECT_VWAP = os.getenv("COLLECT_VWAP", "") == "1"
if not COLLECT_VWAP:
    print("ℹ️ 분봉 VWAP 수집 OFF (차단 위험 감소, COLLECT_VWAP=1로 재활성화 가능)")

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
        wiki_err = esc(str(e)[:150])   # ✅ [v3.1] HTML 이스케이프

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
# ====================================================
# ✅ [수정] 최다 거래 콜 행사가의 bid/ask 정보 추출 (알림 표시용)
#    - 6/29 버전에 있던 top_call_* 기능이 6/30 버전에서 빠졌던 것을 복원
#    - yfinance 체인에 이미 bid/ask가 포함되어 있어 추가 API 호출 없음
#    ⚠️ yfinance의 bid/ask는 장마감 후 '마지막 호가'라 0으로 지워져
#       있을 수 있음 → 그 경우 lastPrice(마지막 체결가)를 대신 제공
# ====================================================
def _top_call_info(calls):
    try:
        if calls is None or calls.empty:
            return {}
        vol = calls["volume"].fillna(0)
        if vol.max() <= 0:
            return {}
        r = calls.loc[vol.idxmax()]

        def _f(x):
            try:
                x = float(x)
                return round(x, 2) if x > 0 else None
            except Exception:
                return None

        return {
            "strike": _f(r.get("strike")),
            "bid":    _f(r.get("bid")),
            "ask":    _f(r.get("ask")),
            "last":   _f(r.get("lastPrice")),
            "volume": int(vol.max()),
        }
    except Exception:
        return {}


OTM_NEAR_BAND = 0.15   # ✅ [조언 3] 근접 OTM 밴드: 현재가 +0~15% 구간만 '방향성 베팅'으로 인정

def _strike_zone_shares(calls, cur_price, call_vol):
    """
    ✅ [조언 2·3 반영] 콜 거래량의 행사가 구간별 비중 계산
      - otm_share:      현재가 초과 전체 (기존 지표, 백테스트 비교용으로 유지)
      - otm_near_share: 현재가 +0~15% 구간만 (딥OTM 로또콜 노이즈 제거 → 방향성 베팅 순도↑)
      - itm_share:      현재가 이하 (깊은 ITM 대량 매수 = 주식 대용의 강한 확신 신호 가능)
    """
    otm_share, otm_near_share, itm_share = None, None, None
    try:
        if cur_price and call_vol > 0:
            vol = calls["volume"].fillna(0)
            otm_share      = round(float(vol[calls["strike"] > cur_price].sum()) / call_vol, 4)
            near_mask      = (calls["strike"] > cur_price) & (calls["strike"] <= cur_price * (1 + OTM_NEAR_BAND) + 1e-6)
            otm_near_share = round(float(vol[near_mask].sum()) / call_vol, 4)
            itm_share      = round(float(vol[calls["strike"] <= cur_price].sum()) / call_vol, 4)
    except Exception:
        pass
    return otm_share, otm_near_share, itm_share


def _strike_concentration(calls):
    """
    ✅ [추가] 행사가 집중도 (허핀달 지수, 0~1)
    콜 거래량이 특정 행사가에 얼마나 몰려있는지.
    - 1에 가까움 = 한두 행사가에 집중 (특정 시나리오에 베팅하는 정보성 매수 특징)
    - 0에 가까움 = 여러 행사가에 분산 (개미 물량/일반 거래 특징)
    ※ 상장 행사가 수가 적은 종목은 자연히 높게 나오므로 절대값보다
      자기 과거 대비·종목 간 상대 비교로 사용할 것 (백테스트에서 검증 후 알림 승격 예정)
    """
    try:
        vol   = calls["volume"].fillna(0)
        total = float(vol.sum())
        if total <= 0:
            return None
        shares = vol / total
        return round(float((shares ** 2).sum()), 4)
    except Exception:
        return None


def calc_oi_metrics(yf_ticker, cur_price=None):
    try:
        exps = yf_with_backoff(lambda: yf_ticker.options, label="만기목록")   # ✅ [v3.1] 백오프
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

        chain    = yf_with_backoff(lambda: yf_ticker.option_chain(target_exp),
                                   label="월물체인")   # ✅ [v3.1] 백오프
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

        top = _top_call_info(calls)   # ✅ 최다 거래 콜 행사가 정보 (알림 표시용)

        # ✅ [추가 2·3 + 조언 2·3] 월물 콜 프리미엄 거래대금($) + 행사가 구간별 비중
        call_prem = None
        try:
            call_prem = _chain_premium(calls)   # ✅ [v3.2] 미드가 우선
            put_prem  = _chain_premium(puts)    # ✅ [v3.2] 풋 프리미엄 신규
        except Exception:
            pass
        otm_share, otm_near_share, itm_share = _strike_zone_shares(calls, cur_price, call_vol)

        # ✅ [v3.1 핵심수정] 행사가별 OI 맵 — 계약별 GEX/DEX 계산용
        #    (지수 GEX와 동일하게 (만기,행사가) 단위로 Alpaca 감마와 매칭)
        call_oi_by_strike, put_oi_by_strike = {}, {}
        try:
            for _, r in calls.iterrows():
                oi_v = 0 if pd.isna(r["openInterest"]) else float(r["openInterest"])
                call_oi_by_strike[round(float(r["strike"]), 1)] = oi_v
            for _, r in puts.iterrows():
                oi_v = 0 if pd.isna(r["openInterest"]) else float(r["openInterest"])
                put_oi_by_strike[round(float(r["strike"]), 1)] = oi_v
        except Exception:
            pass

        return {
            "call_oi":  call_oi, "put_oi":   put_oi,
            "call_vol": call_vol, "put_vol":  put_vol,
            "pcr_oi":   pcr_oi,  "pcr_vol":  pcr_vol,
            "max_pain": max_pain,
            "call_prem": call_prem,           # ✅ [추가 2] 월물 콜 프리미엄 거래대금($)
            "put_prem":  put_prem,            # ✅ [v3.2] 월물 풋 프리미엄 거래대금($)
            "pcr_prem":  (round(put_prem / call_prem, 4)
                          if call_prem and put_prem is not None else None),  # ✅ [v3.2] 금액기준 P/C
            "otm_call_share": otm_share,      # 전체 OTM 비중 (백테스트 비교용 유지)
            "otm_near_share": otm_near_share, # ✅ [조언 3] +0~15% 근접 OTM 비중
            "itm_call_share": itm_share,      # ✅ [조언 2] ITM 비중
            "strike_conc":    _strike_concentration(calls),  # ✅ 행사가 집중도(HHI)
            "oi_exp":     str(target_exp),    # ✅ [조언 4] 월물 만기일 (알림 표시용)
            "oi_exp_dte": oi_dte,   # ✅ 어떤 만기(DTE)를 사용했는지 기록 (폴백 추적용)
            "oi_exp_key": pd.Timestamp(target_exp).strftime("%y%m%d"),  # ✅ Alpaca 심볼 매칭용 만기 키
            "call_oi_by_strike": call_oi_by_strike,   # ✅ [v3.1] 계약별 GEX/DEX용 (CSV 미저장)
            "put_oi_by_strike":  put_oi_by_strike,
            "top_call_strike": top.get("strike"),
            "top_call_bid":    top.get("bid"),
            "top_call_ask":    top.get("ask"),
            "top_call_last":   top.get("last"),
            "top_call_volume": top.get("volume"),
        }
    except Exception as e:
        print(f"    [yf OI 실패] {e}")
        return None


# ====================================================
# ✅ 단기(근월물) 콜 거래량/OI — yfinance
#    (기존 calc_oi_metrics는 30~45일 만기 "하나"만 보므로
#     뉴스 선반영 성격의 근월물 콜 급등은 잡지 못함)
# ====================================================
# ✅ [조언 1 반영] 근월물 밴드: 0~21일 → 7~30일
#    - 하한 7일: 대형주 0DTE/위클리 데이트레이딩 노이즈 제거
#    - 상한 30일: 이벤트 대기 여유 + 세타 감당 가능한 '스위트스팟'(3~4주) 포함
#    ※ 밴드 변경 시점 전후로 exp_st가 달라지므로 OI 전일比 비교는
#      동일만기 조건에 의해 자동으로 안전하게 리셋됨
# ✅ [v3.2 핵심수정] 상한 30 → 45.
#    위클리가 없는 월물전용 종목은 7~30일 창이 통째로 비는 구간이 생겨
#    call_prem_st 이하 전 컬럼이 결측됐음(실측: 2026-07-13~21 결측률 51%, 256종목).
#    그러다 월물이 정확히 30DTE가 되는 날 한꺼번에 되살아나며 z-score가 폭발함.
#    45일로 넓히면 월물이 항상 잡혀 시계열이 끊기지 않는다.
NEAR_TERM_MAX_DTE = 45
NEAR_TERM_MIN_DTE = 7

def calc_near_term_oi_metrics(yf_ticker, min_dte: int = NEAR_TERM_MIN_DTE, max_dte: int = NEAR_TERM_MAX_DTE,
                              cur_price=None):
    """
    min_dte~max_dte 범위에서 '가장 가까운 만기 하나'를 골라 콜/풋 거래량·OI를 계산.
    ⚠️ 이전 버전은 범위 내 모든 만기를 합산했는데, 그러면 종목마다 근월물
    만기 개수(위클리 유무 등)가 달라서 종목 간 비교가 왜곡됨.
    또한 이 함수가 고른 만기(exp_key)를 analyze_near_term_call_flow에도
    그대로 넘겨서 두 지표(거래량 vs 매수비율)가 같은 대상을 가리키게 함.
    """
    try:
        exps = yf_with_backoff(lambda: yf_ticker.options, label="만기목록(근월)")   # ✅ [v3.1] 백오프
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

        chain = yf_with_backoff(lambda: yf_ticker.option_chain(target_exp),
                                label="근월물체인")   # ✅ [v3.1] 백오프
        calls = chain.calls
        puts  = chain.puts
        call_oi  = int(calls["openInterest"].fillna(0).sum())
        put_oi   = int(puts["openInterest"].fillna(0).sum())
        call_vol = int(calls["volume"].fillna(0).sum())
        put_vol  = int(puts["volume"].fillna(0).sum())

        pcr_vol = round(put_vol / call_vol, 4) if call_vol > 0 else None
        exp_key = pd.Timestamp(target_exp).strftime("%y%m%d")  # Alpaca 심볼의 만기 포맷과 동일

        top = _top_call_info(calls)   # ✅ 최다 거래 콜 행사가 정보 (알림 표시용)

        # ✅ [추가 2] 콜 프리미엄 거래대금($): Σ(거래량 × 마지막체결가 × 100)
        #    계약 수만으로는 $0.05 로또콜 5천계약과 $5 콜 5천계약을 구분 못함.
        #    "돈이 실린 급증"을 골라내는 필터. lastPrice는 체인에 이미 포함(추가 호출 없음)
        call_prem = None
        try:
            call_prem = _chain_premium(calls)   # ✅ [v3.2] 미드가 우선
            put_prem  = _chain_premium(puts)    # ✅ [v3.2] 풋 프리미엄 신규
        except Exception:
            pass

        # ✅ [추가 3 + 조언 2·3] 행사가 구간별 콜 거래량 비중
        otm_share, otm_near_share, itm_share = _strike_zone_shares(calls, cur_price, call_vol)

        return {
            "call_oi_st":   call_oi,
            "put_oi_st":    put_oi,
            "call_vol_st":  call_vol,
            "put_vol_st":   put_vol,
            "pcr_vol_st":   pcr_vol,
            "call_prem_st": call_prem,   # ✅ [추가 2] 근월물 콜 프리미엄 거래대금($)
            "put_prem_st":  put_prem,    # ✅ [v3.2] 근월물 풋 프리미엄 거래대금($)
            "pcr_prem_st":  (round(put_prem / call_prem, 4)
                             if call_prem and put_prem is not None else None),  # ✅ [v3.2]
            "otm_call_share_st": otm_share,            # 전체 OTM 비중 (백테스트 비교용 유지)
            "otm_near_share_st": otm_near_share,       # ✅ [조언 3] +0~15% 근접 OTM 비중
            "itm_call_share_st": itm_share,            # ✅ [조언 2] ITM 비중
            "strike_conc_st": _strike_concentration(calls),  # ✅ 행사가 집중도(HHI)
            "target_exp_st": target_exp,
            "exp_key_st":   exp_key,
            "dte_st":       target_dte,
            "top_call_strike_st": top.get("strike"),
            "top_call_bid_st":    top.get("bid"),
            "top_call_ask_st":    top.get("ask"),
            "top_call_last_st":   top.get("last"),
            "top_call_volume_st": top.get("volume"),
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

def _alpaca_topcall_quote(chain, underlying: str, exp_key: str, strike):
    """
    ✅ [수정] Alpaca 체인(dict: OCC심볼→스냅샷)에서 특정 만기/행사가 콜 계약의
    bid/ask 조회. yfinance bid/ask는 장마감 후 소멸되지만 Alpaca latest_quote는
    마지막 NBBO 스냅샷이 유지되므로 알림 표시용으로 더 신뢰할 수 있음.
    (이미 받아온 체인에서 조회하므로 추가 API 호출 없음)
    """
    try:
        if not exp_key or strike is None:
            return None, None
        root = underlying.replace("-", "").replace(".", "")
        occ  = f"{root}{exp_key}C{int(round(float(strike) * 1000)):08d}"
        snap = chain.get(occ)
        if snap is None:
            return None, None
        q   = getattr(snap, "latest_quote", None)
        bid = getattr(q, "bid_price", None) if q else None
        ask = getattr(q, "ask_price", None) if q else None
        bid = round(float(bid), 2) if bid else None   # 0 → None
        ask = round(float(ask), 2) if ask else None
        return bid, ask
    except Exception:
        return None, None


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
        "last_trade_buy_ratio_st": buy_notional_ratio_st,  # ✅ 계약수 대신 금액(계약수×체결가×100) 기준 비율
        "last_trade_buy_notional_st":  round(buy_notional, 2),
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
        gap_pct    = None   # ✅ [v3.2]
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
            # ✅ [v3.2] 전일종가 → 당일시가 갭(%).
            #    신호의 수익이 갭에서 나오면 종가 신호로는 잡을 수 없다(실행 불가).
            #    open_price는 이미 산출돼 있었는데 CSV에 실리지 않았음.
            gap_pct = None
            try:
                if len(closes) >= 2 and open_price:
                    gap_pct = round((open_price / float(closes.iloc[-2]) - 1) * 100, 4)
            except Exception:
                pass

            # VWAP (✅ [v3.1] 기본 OFF — 분봉 500회/일 제거, COLLECT_VWAP=1로 재활성화)
            if COLLECT_VWAP:
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

        # ✅ [추가 5·6] .info는 이미 베타 때문에 매일 1회 호출 중 →
        #    같은 호출에서 공매도잔량·시총·섹터·배당락일까지 추출 (추가 API 호출 0건)
        #    - short_pct_float: 콜 급증 + 높은 공매도 잔량 = 감마 스퀴즈 후보 조합
        #    - market_cap/sector: 소형주일수록 같은 신호에 크게 반응 → 정규화·그룹 분석용
        #    - days_to_exdiv: 배당락 직전 콜 매수(배당 캡처 전략) 노이즈 필터용
        info = get_cached_info(symbol, yft)   # ✅ [v3.1] .info 7일 캐시 (호출량 1/7로)
        beta_val  = info.get("beta")
        beta      = round(float(beta_val), 3) if beta_val is not None else None
        short_pct = info.get("shortPercentOfFloat")
        short_pct = round(float(short_pct), 4) if short_pct is not None else None
        market_cap = info.get("marketCap")
        market_cap = int(market_cap) if market_cap else None
        sector     = info.get("sector") or None
        days_to_exdiv = None
        try:
            exdiv_ts = info.get("exDividendDate")
            if exdiv_ts:
                exdiv_d = pd.Timestamp(exdiv_ts, unit="s").date()
                d = (exdiv_d - today_date).days
                days_to_exdiv = d if d >= 0 else None   # 과거 배당락은 제외
        except Exception:
            pass

        days_to_earn = get_cached_earnings_days(symbol, yft)   # ✅ [v3.1] 7일 캐시

        yf_ticker = yft
        oi_m       = calc_oi_metrics(yf_ticker, cur_price=cur_price)
        call_oi    = oi_m["call_oi"]  if oi_m else 0
        put_oi     = oi_m["put_oi"]   if oi_m else 0
        call_vol   = oi_m["call_vol"] if oi_m else 0  # ✅ 변수 보존
        put_vol    = oi_m["put_vol"]  if oi_m else 0  # ✅ 변수 보존
        pcr_oi     = oi_m["pcr_oi"]   if oi_m else None
        pcr_vol    = oi_m["pcr_vol"]  if oi_m else None
        max_pain   = oi_m["max_pain"] if oi_m else None
        oi_exp_dte = oi_m.get("oi_exp_dte") if oi_m else None
        call_prem      = oi_m.get("call_prem")      if oi_m else None   # ✅ [추가 2]
        put_prem       = oi_m.get("put_prem")       if oi_m else None   # ✅ [v3.2]
        pcr_prem       = oi_m.get("pcr_prem")       if oi_m else None   # ✅ [v3.2]
        otm_call_share = oi_m.get("otm_call_share") if oi_m else None   # ✅ [추가 3]
        otm_near_share = oi_m.get("otm_near_share") if oi_m else None   # ✅ [조언 3]
        itm_call_share = oi_m.get("itm_call_share") if oi_m else None   # ✅ [조언 2]
        strike_conc    = oi_m.get("strike_conc")    if oi_m else None   # ✅ 행사가 집중도
        oi_exp         = oi_m.get("oi_exp")         if oi_m else None   # ✅ [조언 4]
        # ✅ 최다 거래 콜 행사가 정보 (월물)
        top_call = {k: (oi_m.get(k) if oi_m else None)
                    for k in ("top_call_strike", "top_call_bid", "top_call_ask",
                              "top_call_last", "top_call_volume")}

        # ✅ 단기(근월물) 콜/풋 거래량·OI (yfinance, 가장 가까운 만기 하나)
        oi_st       = calc_near_term_oi_metrics(yf_ticker, cur_price=cur_price)
        call_oi_st  = oi_st["call_oi_st"]  if oi_st else 0
        put_oi_st   = oi_st["put_oi_st"]   if oi_st else 0   # ✅ [추가 1] 계산만 되고 저장 안 되던 풋 OI
        call_vol_st = oi_st["call_vol_st"] if oi_st else 0
        put_vol_st  = oi_st["put_vol_st"]  if oi_st else 0
        pcr_vol_st  = oi_st["pcr_vol_st"]  if oi_st else None
        call_prem_st      = oi_st.get("call_prem_st")      if oi_st else None   # ✅ [추가 2]
        put_prem_st       = oi_st.get("put_prem_st")       if oi_st else None   # ✅ [v3.2]
        pcr_prem_st       = oi_st.get("pcr_prem_st")       if oi_st else None   # ✅ [v3.2]
        dte_st            = oi_st.get("dte_st")            if oi_st else None   # ✅ [v3.2] 만기 이질성 추적
        otm_call_share_st = oi_st.get("otm_call_share_st") if oi_st else None   # ✅ [추가 3]
        otm_near_share_st = oi_st.get("otm_near_share_st") if oi_st else None   # ✅ [조언 3]
        itm_call_share_st = oi_st.get("itm_call_share_st") if oi_st else None   # ✅ [조언 2]
        strike_conc_st    = oi_st.get("strike_conc_st")    if oi_st else None   # ✅ 행사가 집중도
        exp_key_st  = oi_st["exp_key_st"]  if oi_st else None
        exp_st      = oi_st["target_exp_st"] if oi_st else None  # ✅ OI 전일比 비교 시 같은 만기인지 확인용
        # ✅ 최다 거래 콜 행사가 정보 (근월물)
        top_call_st = {k: (oi_st.get(k) if oi_st else None)
                       for k in ("top_call_strike_st", "top_call_bid_st", "top_call_ask_st",
                                 "top_call_last_st", "top_call_volume_st")}

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

        # ✅ [수정] 주력행사가 bid/ask를 Alpaca 호가로 우선 교체
        #    yfinance bid/ask는 장마감 후 소멸 → Alpaca latest_quote(마지막 NBBO) 우선,
        #    Alpaca에도 없으면 yfinance 값 유지, 그것도 없으면 알림에서 체결가로 폴백.
        a_bid, a_ask = _alpaca_topcall_quote(
            chain, symbol, oi_m.get("oi_exp_key") if oi_m else None,
            top_call.get("top_call_strike"))
        if a_bid is not None: top_call["top_call_bid"] = a_bid
        if a_ask is not None: top_call["top_call_ask"] = a_ask

        a_bid_st, a_ask_st = _alpaca_topcall_quote(
            chain, symbol, exp_key_st, top_call_st.get("top_call_strike_st"))
        if a_bid_st is not None: top_call_st["top_call_bid_st"] = a_bid_st
        if a_ask_st is not None: top_call_st["top_call_ask_st"] = a_ask_st

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

        # ====================================================
        # ✅ [v3.1 핵심수정] 종목별 GEX/DEX 계산 방식 교체
        #    기존: avg_gamma × 총OI — 콜/풋에 같은 감마를 곱하므로
        #          gex ≡ avg_gamma×(call_oi−put_oi)와 동치. 행사가별 감마 분포
        #          (진짜 GEX의 핵심 정보)가 전부 소실됨. DEX도 |풋델타| 평균을
        #          섞어 (call_oi−put_oi)에 곱하는 근사라 실제 DEX가 아니었음.
        #    변경: 지수(SPY/QQQ/IWM) GEX와 동일하게 (만기,행사가)별로
        #          Alpaca 감마/델타 × yfinance OI를 매칭해 계약별 합산.
        #          DEX는 풋 델타의 음수 부호를 그대로 살려 합산.
        #    ⚠️ 이 버전부터 gex/dex 값의 스케일이 과거 저장분과 불연속.
        #       백테스트 시 v3.1 이전/이후를 구분하거나 이후 데이터만 사용 권장.
        #    매칭 실패 시(만기 불일치 등) 기존 근사 방식으로 폴백해 결측 방지.
        # ====================================================
        gex_call = gex_put = gex = dex = None
        _oi_exp_key  = oi_m.get("oi_exp_key")        if oi_m else None
        _call_oi_map = (oi_m.get("call_oi_by_strike") if oi_m else None) or {}
        _put_oi_map  = (oi_m.get("put_oi_by_strike")  if oi_m else None) or {}
        if _oi_exp_key and (_call_oi_map or _put_oi_map) and cur_price is not None:
            _cg = _pg = _dex_sum = 0.0
            _gex_matched = 0
            for opt in options:
                try:
                    if opt.symbol[-15:-9] != _oi_exp_key:
                        continue
                    _otype  = opt.symbol[-9]
                    _strike = round(int(opt.symbol[-8:]) / 1000, 1)
                except Exception:
                    continue
                _greeks = getattr(opt, "greeks", None)
                if _greeks is None:
                    continue
                _oi = _call_oi_map.get(_strike, 0) if _otype == "C" else _put_oi_map.get(_strike, 0)
                if not _oi or _oi <= 0:
                    continue
                try:
                    _g = getattr(_greeks, "gamma", None)
                    if _g is not None and not np.isnan(float(_g)):
                        _val = float(_g) * _oi * cur_price ** 2 * 0.01
                        if _otype == "C":
                            _cg += _val
                        else:
                            _pg += _val
                        _gex_matched += 1
                    _d = getattr(_greeks, "delta", None)
                    if _d is not None and not np.isnan(float(_d)):
                        _dex_sum += float(_d) * _oi * cur_price * 100
                except Exception:
                    continue
            if _gex_matched > 0:
                gex_call = round(_cg, 2)
                gex_put  = round(_pg, 2)
                gex      = round(_cg - _pg, 2)
                dex      = round(_dex_sum, 2)

        # 폴백: 계약별 매칭 실패 시 기존 근사 유지 (결측보단 근사가 나음, 로그로 표시)
        if gex is None and avg_gamma is not None and cur_price is not None:
            gex_call = round(avg_gamma * call_oi * cur_price ** 2 * 0.01, 2)
            gex_put  = round(avg_gamma * put_oi  * cur_price ** 2 * 0.01, 2)
            gex      = round(gex_call - gex_put, 2)
            print(f"    [GEX] {symbol}: 계약별 매칭 실패 → 기존 근사식 폴백")
        if dex is None and avg_delta is not None and cur_price is not None:
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
            "calc_ver":       SCRIPT_VERSION.split()[0],  # ✅ [v3.1] 계산 방식 버전 기록
                              # gex/dex 등 계산식이 바뀌면 버전으로 구분 가능 →
                              # 백테스트 시 calc_ver로 필터링 (과거 행은 공란=v3.0 이전)
            "price_source":   price_source,
            "dte_range":      dte_range_used,
            "oi_exp_dte":     oi_exp_dte,   # ✅ yf OI 계산에 사용된 만기 DTE (폴백 여부 추적)
            "open":           open_price,
            "high":           high_price,
            "low":            low_price,
            "close":          cur_price,
            "open":           open_price,   # ✅ [v3.2] 당일 시가
            "gap_pct":        gap_pct,      # ✅ [v3.2] 전일종가→당일시가 갭(%)
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
            "put_oi_st":      put_oi_st,      # ✅ [추가 1] 근월물 풋 OI (콜/풋 동시증가=변동성 베팅 구분용)
            "call_vol_st":    call_vol_st,    # ✅ 근월물 콜 거래량
            "put_vol_st":     put_vol_st,     # ✅ 근월물 풋 거래량
            "pcr_vol_st":     pcr_vol_st,     # ✅ 근월물 PCR(거래량 기준)
            "call_prem_st":   call_prem_st,   # ✅ [추가 2] 근월물 콜 프리미엄 거래대금($)
            "put_prem_st":    put_prem_st,    # ✅ [v3.2] 근월물 풋 프리미엄 거래대금($)
            "pcr_prem_st":    pcr_prem_st,    # ✅ [v3.2] 근월물 금액기준 P/C
            "dte_st":         dte_st,         # ✅ [v3.2] 근월물 잔존일수
            "otm_call_share_st": otm_call_share_st,  # ✅ [추가 3] 근월물 OTM 콜 비중
            "otm_near_share_st": otm_near_share_st,  # ✅ [조언 3] 근월물 +0~15% 근접OTM 비중
            "itm_call_share_st": itm_call_share_st,  # ✅ [조언 2] 근월물 ITM 비중
            "strike_conc_st": strike_conc_st,        # ✅ 근월물 행사가 집중도(HHI)
            "call_prem":      call_prem,      # ✅ [추가 2] 월물 콜 프리미엄 거래대금($)
            "put_prem":       put_prem,       # ✅ [v3.2] 월물 풋 프리미엄 거래대금($)
            "pcr_prem":       pcr_prem,       # ✅ [v3.2] 월물 금액기준 P/C
            "otm_call_share": otm_call_share, # ✅ [추가 3] 월물 OTM 콜 비중
            "otm_near_share": otm_near_share, # ✅ [조언 3] 월물 +0~15% 근접OTM 비중
            "itm_call_share": itm_call_share, # ✅ [조언 2] 월물 ITM 비중
            "strike_conc":    strike_conc,    # ✅ 월물 행사가 집중도(HHI)
            "oi_exp":         oi_exp,         # ✅ [조언 4] 월물 만기일
            "short_pct_float": short_pct,     # ✅ [추가 5] 공매도 잔량(유통주식 대비, 격주 갱신)
            "market_cap":     market_cap,     # ✅ [추가 5] 시가총액
            "sector":         sector,         # ✅ [추가 5] 섹터
            "days_to_exdiv":  days_to_exdiv,  # ✅ [추가 6] 배당락까지 남은 일수
            "exp_st":         exp_st,         # ✅ 근월물 만기일 (OI 전일比 비교 시 동일 만기 확인용)
            **top_call,                       # ✅ 월물 최다 거래 콜: strike/bid/ask/last/volume
            **top_call_st,                    # ✅ 근월물 최다 거래 콜: strike/bid/ask/last/volume
            "call_buy_ratio_st":    flow_st["call_buy_ratio_st"],    # ✅ 매수주도 비율(근사, 계약수 기준)
            "last_trade_buy_ratio_st": flow_st["last_trade_buy_ratio_st"],  # ✅ 금액 기준 매수비율
            "call_buy_cnt_st":      flow_st["call_buy_cnt_st"],
            "call_sell_cnt_st":     flow_st["call_sell_cnt_st"],
            "call_checked_cnt_st":  flow_st["call_checked_cnt_st"],
            "call_no_quote_cnt_st": flow_st["call_no_quote_cnt_st"],  # ✅ quote 없어서 판단불가한 계약수
            "last_trade_buy_notional_st": flow_st["last_trade_buy_notional_st"],
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
                idx_oi_cover = 0.0    # ✅ [v3.2] yf OI 만기 완주율
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

                    # ✅ [v3.2 핵심수정] v3.1은 try가 만기 루프 '전체'를 감싸서
                    #    35개 만기 중 5개째에서 429가 나면 나머지를 통째로 건너뛰고도
                    #    yf_oi_available=True가 되어 '축소된 값'이 그대로 저장됐음.
                    #    (실측: SPY GEX가 날짜별로 1.7M~76M, 20배 진동)
                    #    → 만기별로 예외를 잡고, 백오프를 태우고, 완주율로 판정한다.
                    ok_exps = 0
                    for exp in valid_exps:
                        try:
                            exp_key  = pd.Timestamp(exp).strftime("%y%m%d")
                            yf_chain = yf_with_backoff(
                                lambda e=exp: idx_ticker.option_chain(e),
                                label=f"{idx_sym} {exp}")
                            for _, r in yf_chain.calls.iterrows():
                                k = (exp_key, round(float(r["strike"]), 1))
                                yf_call_oi[k] = _safe_oi(r["openInterest"])
                            for _, r in yf_chain.puts.iterrows():
                                k = (exp_key, round(float(r["strike"]), 1))
                                yf_put_oi[k] = _safe_oi(r["openInterest"])
                            ok_exps += 1
                        except Exception as ee:
                            print(f"    [{idx_sym} {exp} OI 실패] {ee}")

                    idx_oi_cover = (ok_exps / len(valid_exps)) if valid_exps else 0.0
                    print(f"    [{idx_sym} yf OI] {ok_exps}/{len(valid_exps)}만기 "
                          f"({idx_oi_cover*100:.0f}%) call={len(yf_call_oi)} put={len(yf_put_oi)}")
                except Exception as e:
                    print(f"    [{idx_sym} yf OI 실패] {e}")

                # ✅ [v3.2] 90% 미만 완주면 '부분 수집'이므로 신뢰 불가 → GEX 산출 포기
                yf_oi_available = bool(yf_call_oi or yf_put_oi) and idx_oi_cover >= 0.90
                if (yf_call_oi or yf_put_oi) and not yf_oi_available:
                    print(f"    [{idx_sym}] OI 부분수집({idx_oi_cover*100:.0f}%) → GEX 산출 생략")

                try:
                    req     = OptionChainRequest(underlying_symbol=idx_sym)
                    chain   = opt_client.get_option_chain(req)
                    options = list(chain.values())

                    filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
                    idx_dte_band = "30-45"
                    if not filtered:
                        # ✅ [v3.2] 폴백 발동 시 대상 계약 수가 몇 배로 뛰므로 반드시 기록
                        filtered = [opt for opt in options if 7 <= days_to_expiry(opt.symbol) <= 60]
                        idx_dte_band = "7-60(fallback)"

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
                    # ✅ [v3.2] 레벨이 '몇 개 계약을 합쳤는가'에 좌우되므로
                    #    추적 컬럼 없이는 사후에 오염된 날을 걸러낼 수 없음
                    row[f"{idx_col}_gex_matched"]  = matched
                    row[f"{idx_col}_gex_band"]     = idx_dte_band
                    row[f"{idx_col}_gex_oi_src"]   = "yf+alpaca" if yf_oi_available else "alpaca_only"
                    row[f"{idx_col}_gex_oi_cover"] = round(idx_oi_cover, 3)
                    # 계약당 평균 — 만기 사이클에 덜 흔들리는 정규화 값
                    row[f"{idx_col}_gex_per_contract"] = (
                        round((call_gex_total - put_gex_total) / matched, 4) if matched else None)

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
    "date", "symbol", "calc_ver",   # ✅ [v3.1] 계산 방식 버전 (과거 행은 공란)
    "price_source", "dte_range", "oi_exp_dte",   # ✅ oi_exp_dte 추가
    "open", "gap_pct",                         # ✅ [v3.2] gap_pct 신규 (open은 기존 컬럼이나 미기록이었음)
    "high", "low", "close", "volume", "vwap", "vwap_diff",
    "cur_price",
    "avg_iv", "atm_call_iv", "atm_put_iv", "skew", "iv_hv_diff",
    "iv_30d", "iv_45d", "iv_60d", "iv_term_slope",
    "hv10", "hv20", "hv60",
    "avg_delta", "avg_gamma", "avg_theta", "avg_vega", "avg_rho",
    "gex", "gex_call", "gex_put", "dex",
    "pcr_oi", "pcr_vol", "call_oi", "put_oi",
    "call_vol", "put_vol",   # ✅ 추가
    "call_prem", "put_prem", "pcr_prem",       # ✅ [v3.2] 월물 콜/풋 프리미엄·금액기준 PCR
    "otm_call_share",                          # ✅ [추가 3] OTM 비중
    "otm_near_share", "itm_call_share", "oi_exp",   # ✅ [조언 2·3·4] 근접OTM·ITM 비중·월물 만기
    "strike_conc",                              # ✅ 월물 행사가 집중도(HHI)
    "top_call_strike", "top_call_bid", "top_call_ask", "top_call_last", "top_call_volume",  # ✅ 복원(월물)
    "call_oi_st", "put_oi_st", "call_vol_st", "put_vol_st", "pcr_vol_st", "exp_st",  # ✅ [추가 1] put_oi_st
    "call_prem_st", "put_prem_st", "pcr_prem_st",  # ✅ [v3.2] 근월물 콜/풋 프리미엄·금액기준 PCR
    "dte_st",                                  # ✅ [v3.2] 근월물 잔존일수(만기 이질성 추적)
    "otm_call_share_st",                       # ✅ [추가 3] 근월물 OTM 비중
    "otm_near_share_st", "itm_call_share_st",  # ✅ [조언 2·3] 근월물 근접OTM·ITM 비중
    "strike_conc_st",                          # ✅ 근월물 행사가 집중도(HHI)
    "iv_rank",                                  # ✅ [추가 4] IV 백분위(자기 과거 대비, 표본 부족 시 None)
    "short_pct_float", "market_cap", "sector", "days_to_exdiv",  # ✅ [추가 5·6]
    "top_call_strike_st", "top_call_bid_st", "top_call_ask_st", "top_call_last_st", "top_call_volume_st",  # ✅ 근월물
    "call_buy_ratio_st", "last_trade_buy_ratio_st", "call_buy_cnt_st", "call_sell_cnt_st",  # ✅ 매수주도 근사
    "call_checked_cnt_st", "call_no_quote_cnt_st", "last_trade_buy_notional_st",
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
    # ✅ [v3.1 핵심수정] 원자적 저장: 쓰는 도중 프로세스가 죽어도
    #    (Actions 타임아웃/OOM 등) 반기치 축적 CSV가 손상되지 않도록
    #    임시파일에 완전히 쓴 뒤 os.replace로 교체
    tmp_path = file_path + ".tmp"
    df_new.to_csv(tmp_path, index=False)
    os.replace(tmp_path, file_path)
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
# ✅ [업그레이드] 콜 급등 '신호의 질' 채점 기준
SURGE_PREM_MIN = 1_000_000   # 콜 프리미엄 거래대금 $1M 이상 = 돈이 실린 급증
SURGE_OTM_MIN  = 0.40        # ✅ [조언 3] '근접 OTM(+0~15%)' 비중 40% 이상 = 방향성 베팅
                             #    (딥OTM 로또콜은 제외하고 계산하므로 기준을 50→40%로 조정)
SURGE_PCR_MAX  = 0.70        # 풋/콜 거래량 비율 0.7 이하 = 콜 단독(풋 동반 아님)
SURGE_ITM_MIN  = 0.50        # ✅ [조언 2] ITM 비중 50% 이상 = 주식 대용 대량 매수 의심
EXDIV_NOISE_DAYS = 3         # ✅ 배당락 D-3 이내의 ITM 콜 대량은 배당 캡처 노이즈로 간주

def format_top_call(strike, bid, ask, last, volume, exp=None) -> str:
    """✅ 알림용 최다 거래 콜 행사가 표시 문자열.
    yfinance bid/ask는 장마감 후 0으로 지워질 수 있어 그 경우 마지막 체결가로 대체."""
    if strike is None:
        return "주력행사가=N/A"
    s = f"주력행사가 ${strike:g}"
    if exp:
        s += f"({exp})"
    if volume:
        s += f" 거래량={volume:,}"
    if bid is not None and ask is not None:
        s += f" bid={bid:.2f}/ask={ask:.2f}"
    elif last is not None:
        s += f" 체결가={last:.2f} (bid/ask 마감소멸)"
    else:
        s += " bid/ask=N/A"
    return s


def detect_call_surge(results: list, vol_oi_threshold: float = 2.0, min_call_vol: int = 500) -> list:
    """
    call_vol / call_oi >= threshold 이고 call_vol >= min_call_vol 인 종목 탐지.
    ✅ [v3.1 문서화] OPRA는 OI를 다음 날 아침(ET)에 갱신하므로, 장마감 후 저녁
    수집 시 call_oi는 '전일 기준 OI'임. 즉 이 비율은 "당일 거래량 ÷ 전일 OI"이며,
    unusual activity 탐지의 업계 표준 정의와 동일. OI 급증 탐지(prev_date)의
    해석도 하루씩 밀려 있음을 백테스트 시 감안할 것.
    ✅ [업그레이드] 1차 필터 통과 후 신규 지표로 '신호의 질'을 채점해 선별:
      💰 프리미엄: 콜 거래대금 >= SURGE_PREM_MIN → 로또콜 아닌 '돈이 실린' 급증
      🎯 OTM집중: OTM 콜 비중 >= SURGE_OTM_MIN → 헤지가 아닌 방향성 베팅
      📉 콜단독: 풋 거래 동반이 적음(PCR<=기준) → 변동성 베팅/헤지 아님
    3개 중 2개 이상이면 ⭐핵심 후보. 점수 → 비율 순으로 정렬.
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

        # ✅ 신호의 질 채점 (신규 지표 + 동생분 조언 반영)
        call_prem  = row.get("call_prem")
        otm_near   = row.get("otm_near_share")     # ✅ [조언 3] +0~15% 근접 OTM만
        itm_share  = row.get("itm_call_share")     # ✅ [조언 2]
        d_exdiv    = row.get("days_to_exdiv")
        flags = []
        if call_prem is not None and call_prem >= SURGE_PREM_MIN:
            flags.append("💰돈실림")
        if otm_near is not None and otm_near >= SURGE_OTM_MIN:
            flags.append("🎯근접OTM")
        if pcr_vol is not None and pcr_vol <= SURGE_PCR_MAX:
            flags.append("📉콜단독")
        score = len(flags)

        # ✅ [조언 2] ITM 대량 = 주식 대용의 강한 확신 매수 의심 (독립 배지)
        #    단, 배당락 D-3 이내면 배당 캡처 차익거래 노이즈일 가능성이 높아 제외
        itm_flag = None
        if itm_share is not None and itm_share >= SURGE_ITM_MIN:
            if d_exdiv is not None and d_exdiv <= EXDIV_NOISE_DAYS:
                itm_flag = f"🏦ITM대량(배당락D-{d_exdiv} 노이즈의심)"
            else:
                itm_flag = "🏦ITM대량"

        surges.append({
            "symbol":    row["symbol"],
            "call_vol":  int(call_vol),
            "call_oi":   int(call_oi),
            "ratio":     round(ratio, 2),
            "pcr_vol":   pcr_vol,
            "score":     score,
            "flags":     flags,
            "itm_flag":  itm_flag,
            "call_prem": call_prem,
            "otm_near":  otm_near,
            "itm_share": itm_share,
            "oi_exp":    row.get("oi_exp"),        # ✅ [조언 4] 만기일 표시
            "iv_rank":   row.get("iv_rank"),
            "short_pct": row.get("short_pct_float"),
            "days_to_earn": row.get("days_to_earn"),
            # ✅ 최다 거래 콜 행사가 정보 (bid/ask 표시 복원)
            "top_strike": row.get("top_call_strike"),
            "top_bid":    row.get("top_call_bid"),
            "top_ask":    row.get("top_call_ask"),
            "top_last":   row.get("top_call_last"),
            "top_volume": row.get("top_call_volume"),
        })

    # ✅ 신호의 질(점수) 우선, 같은 점수면 거래량/OI 비율 순
    surges.sort(key=lambda x: (x["score"], x["ratio"]), reverse=True)
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
        buy_ratio_notional = row.get("last_trade_buy_ratio_st")
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
            # ✅ [수정] 근월물 주력행사가 bid/ask도 알림에 표시
            "top_strike":   row.get("top_call_strike_st"),
            "top_bid":      row.get("top_call_bid_st"),
            "top_ask":      row.get("top_call_ask_st"),
            "top_last":     row.get("top_call_last_st"),
            "top_volume":   row.get("top_call_volume_st"),
            "exp_st":       row.get("exp_st"),
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
    """지금까지 축적된 iv_data CSV 전체 로드 (반기 파일 경계 자동 처리, 오늘 제외)
    ✅ iv_data_*_H*.csv 패턴으로 H1/H2, 연도가 바뀌어 파일이 몇 개로 나뉘어도
       전부 읽어 이어붙이므로 IV Rank·상대거래량 계산이 파일 경계에서 끊기지 않음"""
    need_cols = {"date", "symbol", "call_vol_st", "call_oi_st", "exp_st", "avg_iv",
                 "dte_range"}   # ✅ [v3.1] IV Rank의 만기 혼합 방지용
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


# ====================================================
# ✅ [추가 4] IV Rank (IV 백분위)
#    오늘의 avg_iv가 그 종목 자신의 과거 분포에서 몇 %ile인지.
#    avg_iv=0.45가 "높은지 낮은지"는 종목마다 다르므로 자기 과거와 비교.
#    표본 IV_RANK_MIN_DAYS(40거래일≈2개월) 미만이면 None → 데이터가
#    쌓이면 자동으로 켜짐. 최대 252거래일(1년) 윈도우 사용.
# ====================================================
IV_RANK_MIN_DAYS = 40
IV_RANK_WINDOW   = 252

def add_iv_rank(results: list, hist_df):
    filled = 0
    if hist_df is None or hist_df.empty or "avg_iv" not in hist_df.columns:
        for row in results:
            row["iv_rank"] = None
        return 0
    STD_DTE_BANDS = ("30-45", "25-50")   # ✅ [v3.1] 표준 만기 밴드
    grouped = {sym: g for sym, g in hist_df.groupby("symbol")}
    for row in results:
        row["iv_rank"] = None
        cur_iv = row.get("avg_iv")
        h = grouped.get(row["symbol"])
        if cur_iv is None or h is None:
            continue
        # ✅ [v3.1 수정] auto:Nd(최근접 만기 폴백)로 계산된 avg_iv는 근월물이라
        #    구조적으로 값이 달라 백분위를 왜곡 → 표준 밴드 행만 표본으로 사용.
        #    오늘 값 자체가 auto 폴백이면 비교 대상이 이질적이므로 IV Rank 생략(None).
        if str(row.get("dte_range", "")).startswith("auto"):
            continue
        h_std = h
        if "dte_range" in h.columns:
            m = h["dte_range"].isin(STD_DTE_BANDS)
            if m.any():
                h_std = h[m]
        past = h_std.sort_values("date")["avg_iv"].dropna().tail(IV_RANK_WINDOW)
        if len(past) >= IV_RANK_MIN_DAYS:
            row["iv_rank"] = round(float((past < cur_iv).mean() * 100), 1)
            filled += 1
    return filled


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
                        # ✅ [수정] 주력행사가 bid/ask 알림 표시용
                        "top_strike": row.get("top_call_strike_st"),
                        "top_bid":    row.get("top_call_bid_st"),
                        "top_ask":    row.get("top_call_ask_st"),
                        "top_last":   row.get("top_call_last_st"),
                        "top_volume": row.get("top_call_volume_st"),
                        "exp_st":     row.get("exp_st"),
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
                        # ✅ [수정] 주력행사가 bid/ask 알림 표시용
                        "top_strike": row.get("top_call_strike_st"),
                        "top_bid":    row.get("top_call_bid_st"),
                        "top_ask":    row.get("top_call_ask_st"),
                        "top_last":   row.get("top_call_last_st"),
                        "top_volume": row.get("top_call_volume_st"),
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

# ====================================================
# ✅ [v3.1 수정] 일시적 사유 실패 종목 1회 자동 재시도
#    기존엔 failed_symbols.txt 저장만 하고 끝 → RATELIMIT/네트워크성 실패는
#    잠시 뒤 다시 시도하면 대부분 성공하므로 성공률을 공짜로 올릴 수 있음.
#    (AUTH/DTE/NO_IV/EMPTY 등 결정적 사유는 재시도해도 같은 결과라 제외)
# ====================================================
RETRYABLE_REASONS = {"ALPACA_RATELIMIT", "UNKNOWN", "PRICE_FAIL"}
_retry_targets = [d["symbol"] for d in failed_details if d["reason"] in RETRYABLE_REASONS]
if _retry_targets and auth_consecutive_fails < 3:   # 인증 오류로 중단된 경우는 재시도 무의미
    print(f"\n🔁 일시 오류 {len(_retry_targets)}개 종목 재시도 (30초 대기 후)...")
    time.sleep(30)
    _recovered = set()
    for symbol in _retry_targets:
        print(f"  [재시도] {symbol}")
        row = collect_data(symbol)
        if row and "_fail_reason" not in row:
            results.append(row)
            _recovered.add(symbol)
            if row.get("price_source") == "alpaca":
                yf_fallback_count += 1
        time.sleep(0.5)
    if _recovered:
        failed         = [s for s in failed if s not in _recovered]
        failed_details = [d for d in failed_details if d["symbol"] not in _recovered]
        print(f"  ✅ 재시도 성공: {len(_recovered)}개 → 실패 {len(failed)}개로 감소")

save_info_cache()   # ✅ [v3.1] .info 7일 캐시 저장
save_earn_cache()   # ✅ [v3.1] 어닝 날짜 7일 캐시 저장

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
            f"샘플: {esc(' / '.join(dte_samples))}"   # ✅ [v3.1] HTML 이스케이프
        )

    rate_fail_count = reason_counter.get("ALPACA_RATELIMIT", 0)
    if rate_fail_count >= 10:
        send_telegram(
            f"🚨 <b>Alpaca API 장애 의심</b> — {today}\n"
            f"Rate Limit / 장애 실패: {rate_fail_count}개\n"
            f"→ status.alpaca.markets 확인 권장"
        )

if results:
    # ✅ [추가 4] 저장 전에 축적 히스토리를 로드해 IV Rank 부여
    #    (분할된 모든 iv_data_*_H*.csv를 읽으므로 파일 경계 무관)
    hist_df = load_history_csv()
    iv_rank_cnt = add_iv_rank(results, hist_df)
    if iv_rank_cnt:
        print(f"  📐 IV Rank 계산 완료: {iv_rank_cnt}개 종목 (표본 {IV_RANK_MIN_DAYS}일 이상)")
    else:
        print(f"  📐 IV Rank: 표본 부족(종목당 {IV_RANK_MIN_DAYS}거래일 필요) → 데이터가 쌓이면 자동 활성화")
    save_csv(results, IV_COL_ORDER, "iv_data")
else:
    hist_df = None

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
        f"📊 <b>IV 데이터 수집 완료</b> [{SCRIPT_VERSION}]\n"
        f"📅 날짜: {today}\n"
        f"✅ 성공: {success_count}개 종목\n"
        f"❌ 실패: {fail_count}개 종목\n"
        f"🔄 Alpaca 폴백: {yf_fallback_count}개 종목\n"
        f"⏱ 소요시간: {elapsed//60}분 {elapsed%60}초\n"
        f"📈 수집항목: IV/HV/Skew/Greeks/GEX/PCR/MaxPain/RSI/Beta/MA/ATR/어닝"
    )
    if stale_volume_risk:
        msg += f"\n⚠️ 기준일 다음날 수집됨 → 옵션 거래량은 참고용"
    if ALERT_SUPPRESS:
        msg += f"\n♻️ 같은 날 재수집 → 급등 알림은 생략됨(중복 방지)"
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
# ✅ [v3.1] 지연 수집분의 급등 알림은 오탐 가능성이 높은데 기존엔
#    완료 메시지에만 경고가 붙고 정작 급등 알림에는 표시가 없었음 → 헤더에 명시
surge_warn = ""
if rolled_back or stale_volume_risk:
    surge_warn = "\n⚠️ 지연 수집분 — 옵션 거래량 신뢰도 낮음(참고용으로만)"

if ALERT_SUPPRESS:
    print("  ♻️ 재수집 모드 → 급등 알림 4종 생략 (중복 방지)")

if results and not ALERT_SUPPRESS:
    call_surges = detect_call_surge(results)
    if call_surges:
        core  = [s for s in call_surges if s["score"] >= 2]   # ⭐ 핵심 후보 (3개 기준 중 2개+)
        other = [s for s in call_surges if s["score"] < 2]

        def _surge_line(s, star=False):
            pcr_str = f"PCR={s['pcr_vol']:.2f}" if s["pcr_vol"] else "PCR=N/A"
            prem_str = (f"대금=${s['call_prem']/1e6:.1f}M" if s["call_prem"] is not None
                        else "대금=N/A")
            otm_str = (f"근접OTM={s['otm_near']*100:.0f}%" if s["otm_near"] is not None
                       else "근접OTM=N/A")
            ivr_str = f"IVR={s['iv_rank']:.0f}" if s["iv_rank"] is not None else "IVR=수집중"
            exp_str = f"만기={s['oi_exp']}" if s.get("oi_exp") else "만기=N/A"   # ✅ [조언 4]
            extra = []
            if s.get("itm_flag"):
                extra.append(s["itm_flag"])                        # ✅ [조언 2] ITM대량 배지
            if s["short_pct"] is not None and s["short_pct"] >= 0.10:
                extra.append(f"⚡공매도{s['short_pct']*100:.0f}%")   # 스퀴즈 후보 참고
            if s["days_to_earn"] is not None and 0 <= s["days_to_earn"] <= 7:
                extra.append(f"📅어닝D-{s['days_to_earn']}")          # 어닝 노이즈 주의 표시
            flag_str = " ".join(s["flags"] + extra)
            top_str = format_top_call(s.get("top_strike"), s.get("top_bid"),
                                      s.get("top_ask"), s.get("top_last"),
                                      s.get("top_volume"))
            head = "⭐ " if star else "• "
            return (
                f"{head}<b>{s['symbol']}</b>  "
                f"콜거래량={s['call_vol']:,}  OI={s['call_oi']:,}  "
                f"비율={s['ratio']}x  {pcr_str}  {exp_str}\n"
                f"  └ {prem_str}  {otm_str}  {ivr_str}  {flag_str}\n"
                f"  └ {top_str}"
            )

        lines = [f"🚀 <b>콜 거래량 급등 감지</b> — {today}{surge_warn}",
                 f"(⭐=핵심 후보: 💰대금 $1M+ / 🎯근접OTM(+0~15%) 40%+ / 📉콜단독 중 2개 이상)\n"]
        if core:
            lines.append(f"<b>⭐ 핵심 후보 {len(core)}개</b>")
            for s in core[:10]:
                lines.append(_surge_line(s, star=True))
            lines.append("")
        if other:
            lines.append(f"참고 후보 {len(other)}개")
            for s in other[:8]:
                lines.append(_surge_line(s))
            if len(other) > 8:
                lines.append(f"... 외 {len(other)-8}개")
        send_telegram("\n".join(lines))
        print(f"  📡 콜 급등 감지 전송 완료 (핵심 {len(core)} / 참고 {len(other)})")
    else:
        print("  ℹ️ 콜 거래량 급등 종목 없음")

    # ✅ 단기 콜 "매수주도" 급등 (인텔 사례처럼 뉴스 전 콜 매수 우위 패턴 겨냥)
    buyside_surges = detect_buyside_call_surge(results)
    if buyside_surges:
        lines = [f"🎯 <b>단기 콜 매수주도 급등 감지</b> — {today}{surge_warn}",
                 f"(근월물 최근접 만기 · bid/ask 근사 · 참고용 지표)\n"]
        for s in buyside_surges[:15]:
            pcr_str    = f"PCR={s['pcr_vol_st']:.2f}" if s["pcr_vol_st"] else "PCR=N/A"
            bidask_str = (f"평균bid={s['avg_bid']:.2f}/ask={s['avg_ask']:.2f}"
                          f"(스프레드{s['avg_spread']}%)"
                          if s["avg_bid"] is not None and s["avg_ask"] is not None
                          else "평균bid/ask=N/A(Alpaca quote 부재)")
            notional_str = (f"금액기준={s['buy_ratio_notional']*100:.0f}%"
                             if s["buy_ratio_notional"] is not None else "금액기준=N/A")
            top_str = format_top_call(s.get("top_strike"), s.get("top_bid"),
                                      s.get("top_ask"), s.get("top_last"),
                                      s.get("top_volume"), exp=s.get("exp_st"))
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"매수비율={s['buy_ratio']*100:.0f}% ({notional_str}) "
                f"(매수{s['buy_cnt']}/매도{s['sell_cnt']})  "
                f"콜거래량={s['call_vol_st']:,}  OI={s['call_oi_st']:,}  "
                f"비율={s['ratio']}x  {pcr_str}\n"
                f"  └ {top_str}\n"       # ✅ [수정] 근월물 주력행사가 bid/ask 표시
                f"  └ {bidask_str}"
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
    #    (hist_df는 저장 전 IV Rank 계산 때 이미 로드됨 → 재사용)
    # ====================================================
    rel_surges, oi_surges = detect_unusual_activity(results, hist_df)

    if rel_surges:
        lines = [f"📈 <b>평소 대비 콜 거래량 급등</b> — {today}{surge_warn}",
                 f"(자기 자신의 최근 {REL_VOL_LOOKBACK}일 평균과 비교)\n"]
        for s in rel_surges[:15]:
            pcr_str = f"PCR={s['pcr']:.2f}" if s["pcr"] is not None else "PCR=N/A"
            top_str = format_top_call(s.get("top_strike"), s.get("top_bid"),
                                      s.get("top_ask"), s.get("top_last"),
                                      s.get("top_volume"), exp=s.get("exp_st"))
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"오늘={s['call_vol']:,}  평소={s['avg_vol']:,}  "
                f"<b>{s['ratio']}배</b>  {pcr_str} (표본 {s['days']}일)\n"
                f"  └ {top_str}"   # ✅ [수정] 주력행사가 bid/ask 표시
            )
        if len(rel_surges) > 15:
            lines.append(f"... 외 {len(rel_surges)-15}개")
        send_telegram("\n".join(lines))
        print(f"  📡 상대 거래량 급등 {len(rel_surges)}개 전송 완료")
    else:
        print("  ℹ️ 상대 거래량 급등 종목 없음 (또는 과거 표본 부족)")

    if oi_surges:
        lines = [f"🧲 <b>근월물 콜 OI 급증 감지</b> — {today}{surge_warn}",
                 f"(전 거래일 대비, 동일 만기 기준 — 전일 세션의 신규 포지션 개설 확증)\n"]
        for s in oi_surges[:15]:
            top_str = format_top_call(s.get("top_strike"), s.get("top_bid"),
                                      s.get("top_ask"), s.get("top_last"),
                                      s.get("top_volume"))
            lines.append(
                f"• <b>{s['symbol']}</b>  "
                f"OI {s['prev_oi']:,} → {s['oi']:,} (<b>{s['ratio']}배</b>)  "
                f"만기={s['exp']} (기준일 {s['prev_date']})\n"
                f"  └ {top_str}"   # ✅ [수정] 주력행사가 bid/ask 표시
            )
        if len(oi_surges) > 15:
            lines.append(f"... 외 {len(oi_surges)-15}개")
        send_telegram("\n".join(lines))
        print(f"  📡 콜 OI 급증 {len(oi_surges)}개 전송 완료")
    else:
        print("  ℹ️ 콜 OI 급증 종목 없음 (또는 동일 만기 과거 표본 없음)")

print("=== IV Data Collector DONE ===")
