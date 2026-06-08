import os
import pandas as pd
from datetime import datetime, date
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
import time

print("=== IV Data Collector START ===")

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

today = datetime.now().strftime("%Y-%m-%d")
today_date = date.today()

# ====================================================
# ✅ S&P 500 리스트 — yfinance 방식 (lxml 불필요)
# ====================================================
def get_sp500_symbols():
    try:
        import yfinance as yf
        sp500 = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            flavor="html5lib"  # lxml 대신 html5lib 사용
        )[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"✅ S&P 500 종목 수: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"⚠️ Wikipedia 로드 실패: {e}")
        # ── fallback: requests + html5lib 직접 파싱 ──
        try:
            import requests
            resp = requests.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            sp500 = pd.read_html(resp.text, flavor="html5lib")[0]
            symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
            print(f"✅ fallback 성공: {len(symbols)}개")
            return symbols
        except Exception as e2:
            print(f"❌ 전체 실패, 기본 리스트 사용: {e2}")
            return [
                "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG",
                "LLY","AVGO","JPM","TSLA","UNH","V","XOM","MA",
                "JNJ","PG","HD","COST","WMT","NFLX","AMD","CRM",
                "ABBV","CVX","MRK","KO","PEP","TMO","ACN",
            ]

# ====================================================
# ✅ 만기일 파싱 (Alpaca 심볼: AAPL240119C00150000)
# ====================================================
def days_to_expiry(symbol: str) -> int:
    try:
        # 뒤에서 15번째 ~ 9번째 자리: YYMMDD
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
        # BRK-B 같은 심볼은 Alpaca에서 점(.)으로 쓰는 경우가 있음
        alpaca_symbol = symbol.replace("-", ".")

        req = OptionChainRequest(underlying_symbol=alpaca_symbol)
        chain = client.get_option_chain(req)
        options = list(chain.values())

        if not options:
            return None

        # ── 30~45일 만기 필터 ──────────────────────────
        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]

        # fallback: 25~50일
        if not filtered:
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]

        if not filtered:
            print(f"  ⚠️ {symbol}: 30~45일 만기 없음")
            return None

        # ── ATM 콜/풋 분리 ────────────────────────────
        call_ivs, put_ivs = [], []
        for opt in filtered:
            iv = getattr(opt, "implied_volatility", None)
            if not iv:
                continue
            delta = getattr(opt, "delta", None)
            if delta is not None and not (0.4 <= abs(float(delta)) <= 0.6):
                continue
            opt_type = opt.symbol[-9]  # C or P
            if opt_type == "C":
                call_ivs.append(float(iv))
            elif opt_type == "P":
                put_ivs.append(float(iv))

        # ATM 없으면 전체 평균
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
            "symbol": symbol,          # 원래 심볼 저장 (BRK-B)
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

for i, symbol in enumerate(symbols):
    print(f"[{i+1}/{len(symbols)}] {symbol} 수집 중...")
    row = collect_iv(symbol)
    if row:
        results.append(row)
        print(f"  ✅ avg_iv={row['avg_iv']}, skew={row['skew']}, n={row['sample_count']}")
    else:
        failed.append(symbol)
    time.sleep(0.3)

# ====================================================
# ✅ CSV 저장 (오늘 날짜 중복 방지)
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
else:
    print("❌ 수집된 데이터 없음")

if failed:
    print(f"\n⚠️ 실패 종목 ({len(failed)}개): {', '.join(failed)}")
    with open("failed_symbols.txt", "w") as f:
        f.write("\n".join(failed))

print("=== IV Data Collector DONE ===")
