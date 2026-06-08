import os
import pandas as pd
from datetime import datetime

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

# ✅ API 키 (GitHub Secrets에서 가져옴)
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

# ✅ 수집할 종목 (일단 3개만 테스트)
symbols = ["AAPL", "MSFT", "NVDA"]

today = datetime.now().strftime("%Y-%m-%d")

results = []

try:
    for symbol in symbols:
        print(f"{symbol} 수집 시작")

        req = OptionChainRequest(
            underlying_symbol=symbol
        )

        chain = client.get_option_chain(req)

        options = list(chain.values())

        # ✅ IV 추출
        iv_list = [
            opt.greeks.implied_volatility
            for opt in options
            if opt.greeks and opt.greeks.implied_volatility
        ]

        if iv_list:
            avg_iv = sum(iv_list) / len(iv_list)

            results.append({
                "date": today,
                "symbol": symbol,
                "iv": round(avg_iv, 4)
            })

            print(f"{symbol} IV:", round(avg_iv, 4))
        else:
            print(f"{symbol} IV 없음")

    # ✅ CSV 저장
    if results:
        df = pd.DataFrame(results)

        file_path = "iv_data.csv"

        if os.path.exists(file_path):
            df.to_csv(file_path, mode='a', header=False, index=False)
        else:
            df.to_csv(file_path, index=False)

        print("✅ 저장 완료")

except Exception as e:
    print("❌ 에러 발생:", str(e))
    raise
