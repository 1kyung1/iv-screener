import os
import requests
import pandas as pd
from datetime import datetime, date
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
import time

print("=== IV Data Collector START ===")

# ====================================================
# ✅ 주말/공휴일 체크 — 장 닫힌 날은 그냥 종료
# ====================================================
today_date = date.today()
today = today_date.strftime("%Y-%m-%d")
weekday = today_date.weekday()  # 0=월 ~ 6=일

if weekday >= 5:  # 토(5), 일(6)
    print(f"📅 오늘은 주말({['월','화','수','목','금','토','일'][weekday]})이라 스킵합니다.")
    exit(0)

# 미국 주요 공휴일 (고정일 기준, 매년 업데이트 필요)
US_HOLIDAYS = [
    "2026-01-01",  # 신정
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-07-03",  # Independence Day (관찰일)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-11-27",  # Black Friday (반일)
    "2026-12-25",  # 크리스마스
]

if today in US_HOLIDAYS:
    print(f"🎉 오늘은 미국 공휴일이라 스킵합니다.")
    exit(0)

# ====================================================
# ✅ 텔레그램 알림 함수
# ====================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ 텔레그램 설정 없음, 알림 스킵")
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
        return [
            "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG",
            "LLY","AVGO","JPM","TSLA","UNH","V","XOM","MA",
            "JNJ","PG","HD","COST","WMT","NFLX","AMD","CRM",
            "ABBV","CVX","MRK","KO","PEP","TMO","ACN",
        ]

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
# ✅ 단일 종목 IV 수집
# ====================================================
def collect_iv(symbol: str) -> dict | None:
    try:
        alpaca_symbol = symbol.replace("-", ".")
        req = OptionChainRequest(underlying_symbol=alpaca_symbol)
        chain = client.get_option_chain(req)
        options = list(chain.values())
        if not options:
            return None

        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
        if not filtered:
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]
        if not filtered:
            return None

        call_ivs, put_ivs = [], []
        for opt in filtered:
            iv = getattr(opt, "implied_volatility", None)
            if not iv:
                continue
            delta = getattr(opt, "delta", None)
            if delta is not None and not (0.4 <= abs(float(delta)) <= 0.6):
                continue
            opt_type = opt.symbol[-9]
            if opt_type == "C":
                call_ivs.append(float(iv))
            elif opt_type == "P":
                put_ivs.append(float(iv))

        all_ivs = call_ivs + put_ivs
        if not all_ivs:
            all_ivs = [float(opt.implied_volatility)
                       for opt in filtered
                       if getattr(opt, "implied_volatility", None)]
        if not all_ivs:
            return None

        avg_call = round(sum(call_ivs)/len(call_ivs), 4) if call_ivs else None
        avg_put  = round(sum(put_ivs) /len(put_ivs),  4) if put_ivs  else None
        avg_iv   = round(sum(all_ivs) /len(all_ivs),  4)
        skew     = round(avg_put - avg_call, 4) if (avg_call and avg_put) else None

        return {
            "date": today,
            "symbol": symbol,
            "dte_range": "30-45",
            "avg_iv": avg_iv,
            "atm_call_iv": avg_call,
            "atm_put_iv": avg_put,
            "skew": skew,
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
    row = collect_iv(symbol)
    if row:
        results.append(row)
        print(f"  ✅ avg_iv={row['avg_iv']}, skew={row['skew']}, n={row['sample_count']}")
    else:
        failed.append(symbol)
    time.sleep(0.3)

elapsed = round(time.time() - start_time)

# ====================================================
# ✅ CSV 저장
# ====================================================
file_path = "iv_data.csv"
col_order = ["date", "symbol", "dte_range", "avg_iv",
             "atm_call_iv", "atm_put_iv", "skew", "sample_count"]

if results:
    df_new = pd.DataFrame(results)[col_order]
    if os.path.exists(file_path):
        df_existing = pd.read_csv(file_path)
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
# ✅ 텔레그램 알림 전송
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
    )
    if fail_count > 0:
        msg += f"⚠️ 실패 종목: {', '.join(failed[:10])}"
        if fail_count > 10:
            msg += f" 외 {fail_count-10}개"
else:
    msg = (
        f"❌ <b>IV 데이터 수집 실패</b>\n"
        f"📅 날짜: {today}\n"
        f"수집된 데이터가 없습니다."
    )

send_telegram(msg)
print("=== IV Data Collector DONE ===")
