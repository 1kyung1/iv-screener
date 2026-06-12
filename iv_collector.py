import os
import pandas as pd
from datetime import date, datetime
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
import yfinance as yf

today_date = date.today()
API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client     = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

def days_to_expiry(symbol: str) -> int:
    try:
        exp_str  = symbol[-15:-9]
        exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        return (exp_date - today_date).days
    except Exception:
        return -1

for idx_sym in ["SPY", "QQQ"]:
    print(f"\n{'='*50}")
    print(f"🔍 {idx_sym} strike 매칭 디버깅")
    print(f"{'='*50}")

    # ── yfinance OI 맵 ────────────────────────────────
    yf_call_oi = {}
    yf_put_oi  = {}
    target_exp = None
    try:
        ticker = yf.Ticker(idx_sym)
        for exp in ticker.options:
            dte = (pd.Timestamp(exp).date() - today_date).days
            if 30 <= dte <= 45:
                target_exp = exp
                break
        if not target_exp:
            for exp in ticker.options:
                dte = (pd.Timestamp(exp).date() - today_date).days
                if 25 <= dte <= 50:
                    target_exp = exp
                    break

        if target_exp:
            chain = ticker.option_chain(target_exp)
            for _, r in chain.calls.iterrows():
                yf_call_oi[round(float(r["strike"]), 1)] = float(r["openInterest"] or 0)
            for _, r in chain.puts.iterrows():
                yf_put_oi[round(float(r["strike"]), 1)]  = float(r["openInterest"] or 0)

            print(f"  yfinance exp={target_exp}")
            print(f"  yfinance call strikes 샘플: {sorted(yf_call_oi.keys())[:5]}")
            print(f"  yfinance put  strikes 샘플: {sorted(yf_put_oi.keys())[:5]}")
    except Exception as e:
        print(f"  yfinance 실패: {e}")

    # ── Alpaca gamma 맵 ───────────────────────────────
    try:
        req     = OptionChainRequest(underlying_symbol=idx_sym)
        chain   = client.get_option_chain(req)
        options = list(chain.values())

        filtered = [opt for opt in options if 30 <= days_to_expiry(opt.symbol) <= 45]
        if not filtered:
            filtered = [opt for opt in options if 25 <= days_to_expiry(opt.symbol) <= 50]

        # greeks 있는 것만
        with_greeks = [opt for opt in filtered
                       if getattr(opt, "greeks", None) is not None
                       and getattr(opt.greeks, "gamma", None) is not None]

        print(f"\n  Alpaca filtered={len(filtered)} / greeks+gamma 있는 것={len(with_greeks)}")

        # strike 파싱 샘플
        alpaca_strikes = []
        for opt in with_greeks[:10]:
            try:
                strike = round(int(opt.symbol[-8:]) / 1000, 1)
                alpaca_strikes.append(strike)
            except Exception:
                pass
        print(f"  Alpaca strike 샘플 (심볼 끝 8자리/1000): {alpaca_strikes}")

        # 실제 심볼 샘플 출력해서 파싱 검증
        print(f"\n  [심볼 파싱 검증 - 샘플 5개]")
        for opt in with_greeks[:5]:
            raw_strike_str = opt.symbol[-8:]
            parsed = round(int(raw_strike_str) / 1000, 1)
            opt_type = opt.symbol[-9]
            oi_from_yf = yf_call_oi.get(parsed, "없음") if opt_type == "C" else yf_put_oi.get(parsed, "없음")
            print(f"    심볼={opt.symbol} | 끝8자리={raw_strike_str} | "
                  f"파싱strike={parsed} | type={opt_type} | yf_oi={oi_from_yf}")

        # 매칭 성공 수
        matched = 0
        for opt in with_greeks:
            opt_type = opt.symbol[-9]
            try:
                strike = round(int(opt.symbol[-8:]) / 1000, 1)
            except Exception:
                continue
            oi = yf_call_oi.get(strike, 0) if opt_type == "C" else yf_put_oi.get(strike, 0)
            if oi > 0:
                matched += 1

        print(f"\n  ✅ 최종 매칭 성공: {matched} / {len(with_greeks)}개")

    except Exception as e:
        print(f"  Alpaca 실패: {e}")
