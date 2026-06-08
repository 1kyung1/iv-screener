import os
import pandas as pd
from datetime import datetime, date
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
import time

print("=== IV Data Collector START ===")

# ✅ API 키 (GitHub Secrets)
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

today = datetime.now().strftime("%Y-%m-%d")
today_date = date.today()

# ====================================================
# ✅ S&P 500 전체 종목 리스트 (Wikipedia에서 동적 로드)
# ====================================================
def get_sp500_symbols():
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = tables[0]
        symbols = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"✅ S&P 500 종목 수: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"⚠️ Wikipedia 로드 실패, 기본 리스트 사용: {e}")
        # 기본 fallback 리스트 (주요 종목)
        return [
            "AAPL", "MSFT", "NVDA", "AMZN", "META",
            "GOOGL", "GOOG", "BRK-B", "LLY", "AVGO",
            "JPM", "TSLA", "UNH", "V", "XOM",
            "MA", "JNJ", "PG", "HD", "COST",
        ]

# ====================================================
# ✅ 만기일 → 오늘 기준 남은 일수 계산
# ====================================================
def days_to_expiry(expiry_str: str) -> int:
    """옵션 심볼에서 만기일 추출 후 남은 일수 반환"""
    try:
        # Alpaca 옵션 심볼 형식: AAPL240119C00150000
        # 6자리 날짜: YYMMDD
        exp_str = expiry_str[-15:-9]  # 심볼 파싱
        exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        return (exp_date - today_date).days
    except Exception:
        return -1

# ====================================================
# ✅ 단일 종목 IV 데이터 수집
# 반환 컬럼:
#   date, symbol, dte_range,
#   atm_call_iv, atm_put_iv, avg_iv,
#   skew (put_iv - call_iv),
#   sample_count
# ====================================================
def collect_iv(symbol: str) -> dict | None:
    try:
        req = OptionChainRequest(underlying_symbol=symbol)
        chain = client.get_option_chain(req)
        options = list(chain.values())

        if not options:
            return None

        # ── 1) 30~45일 만기 필터 ──────────────────────────
        filtered = []
        for opt in options:
            # 옵션 심볼에서 만기일 파싱
            dte = days_to_expiry(opt.symbol)
            if 30 <= dte <= 45:
                filtered.append(opt)

        if not filtered:
            # fallback: 25~50일로 범위 확장
            for opt in options:
                dte = days_to_expiry(opt.symbol)
                if 25 <= dte <= 50:
                    filtered.append(opt)

        if not filtered:
            print(f"  ⚠️ {symbol}: 30~45일 만기 없음 (전체 {len(options)}개)")
            return None

        # ── 2) ATM 기준 필터 (delta 0.4~0.6 근처) ─────────
        # Alpaca가 delta를 제공하는 경우 사용, 없으면 IV 평균만
        call_ivs = []
        put_ivs = []

        for opt in filtered:
            iv = getattr(opt, "implied_volatility", None)
            delta = getattr(opt, "delta", None)
            if not iv:
                continue

            # delta 있으면 ATM 필터 (0.4~0.6)
            if delta is not None:
                abs_delta = abs(float(delta))
                if abs_delta < 0.4 or abs_delta > 0.6:
                    continue

            # 콜/풋 분류 (심볼 형식: ...C... or ...P...)
            option_type = opt.symbol[-9]  # C or P
            if option_type == "C":
                call_ivs.append(float(iv))
            elif option_type == "P":
                put_ivs.append(float(iv))

        # ATM 필터 후 데이터 없으면 전체 평균
        if not call_ivs and not put_ivs:
            all_ivs = [
                float(opt.implied_volatility)
                for opt in filtered
                if getattr(opt, "implied_volatility", None)
            ]
            if not all_ivs:
                return None
            avg_iv = round(sum(all_ivs) / len(all_ivs), 4)
            return {
                "date": today,
                "symbol": symbol,
                "dte_range": "30-45",
                "atm_call_iv": None,
                "atm_put_iv": None,
                "avg_iv": avg_iv,
                "skew": None,          # put_iv - call_iv (양수 = put skew)
                "sample_count": len(all_ivs),
            }

        # ── 3) 평균 계산 ──────────────────────────────────
        avg_call = round(sum(call_ivs) / len(call_ivs), 4) if call_ivs else None
        avg_put  = round(sum(put_ivs)  / len(put_ivs),  4) if put_ivs  else None

        all_ivs = call_ivs + put_ivs
        avg_iv  = round(sum(all_ivs) / len(all_ivs), 4)

        skew = None
        if avg_call and avg_put:
            skew = round(avg_put - avg_call, 4)  # 양수 → put premium (정상 시장)

        return {
            "date": today,
            "symbol": symbol,
            "dte_range": "30-45",
            "atm_call_iv": avg_call,
            "atm_put_iv": avg_put,
            "avg_iv": avg_iv,
            "skew": skew,
            "sample_count": len(all_ivs),
        }

    except Exception as e:
        print(f"  ❌ {symbol} 에러: {e}")
        return None


# ====================================================
# ✅ 메인 수집 루프
# ====================================================
symbols = get_sp500_symbols()
results = []
failed = []

for i, symbol in enumerate(symbols):
    print(f"[{i+1}/{len(symbols)}] {symbol} 수집 중...")
    row = collect_iv(symbol)
    if row:
        results.append(row)
        print(f"  ✅ avg_iv={row['avg_iv']}, skew={row['skew']}, n={row['sample_count']}")
    else:
        failed.append(symbol)

    # API rate limit 방지 (초당 요청 제한)
    time.sleep(0.3)

# ====================================================
# ✅ CSV 저장 (날짜별 누적 append)
# ====================================================
file_path = "iv_data.csv"

if results:
    df_new = pd.DataFrame(results)

    # 컬럼 순서 정렬
    col_order = ["date", "symbol", "dte_range", "avg_iv",
                 "atm_call_iv", "atm_put_iv", "skew", "sample_count"]
    df_new = df_new[col_order]

    if os.path.exists(file_path):
        # 오늘 날짜 중복 방지: 같은 날 데이터가 있으면 덮어쓰기
        df_existing = pd.read_csv(file_path)
        df_existing = df_existing[df_existing["date"] != today]  # 오늘 데이터 제거
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined.to_csv(file_path, index=False)
        print(f"✅ 업데이트 저장 완료: {len(df_new)}개 종목")
    else:
        df_new.to_csv(file_path, index=False)
        print(f"✅ 신규 저장 완료: {len(df_new)}개 종목")
else:
    print("❌ 수집된 데이터 없음")

# ====================================================
# ✅ 실패 종목 로그
# ====================================================
if failed:
    print(f"\n⚠️ 수집 실패 종목 ({len(failed)}개): {', '.join(failed)}")
    with open("failed_symbols.txt", "w") as f:
        f.write("\n".join(failed))

print("=== IV Data Collector DONE ===")
