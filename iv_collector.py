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

for idx_sym in ["SPY", "QQQ", "IWM"]:
    print(f"\n{'='*50}")
    print(f"🔍 {idx_sym} GEX 디버깅")
    print(f"{'='*50}")

    # ── Alpaca 체인 확인 ──────────────────────────────
    try:
        req     = OptionChainRequest(underlying_symbol=idx_sym)
        chain   = client.get_option_chain(req)
        options = list(chain.values())
        print(f"  Alpaca 전체 옵션 수: {len(options)}")

        if options:
            sample = options[0]
            print(f"  샘플 심볼: {sample.symbol}")
            print(f"  샘플 DTE: {days_to_expiry(sample.symbol)}")
            print(f"  greeks 있음: {getattr(sample, 'greeks', None) is not None}")
            print(f"  open_interest: {getattr(sample, 'open_interest', 'N/A')}")
            print(f"  implied_volatility: {getattr(sample, 'implied_volatility', 'N/A')}")

            # DTE 분포 확인
            dte_list = [days_to_expiry(o.symbol) for o in options]
            dte_set  = sorted(set(dte_list))
            print(f"  DTE 종류: {dte_set[:10]} ...")  # 앞 10개만

            # 필터 결과
            f3045 = [o for o in options if 30 <= days_to_expiry(o.symbol) <= 45]
            f2550 = [o for o in options if 25 <= days_to_expiry(o.symbol) <= 50]
            print(f"  30~45 DTE 필터: {len(f3045)}개")
            print(f"  25~50 DTE 필터: {len(f2550)}개")

            filtered = f3045 if f3045 else f2550
            if filtered:
                has_greeks = sum(1 for o in filtered if getattr(o, "greeks", None) is not None)
                has_oi     = sum(1 for o in filtered if (getattr(o, "open_interest", None) or 0) > 0)
                has_gamma  = sum(1 for o in filtered
                                 if getattr(o, "greeks", None) is not None
                                 and getattr(o.greeks, "gamma", None) is not None)
                print(f"  필터된 옵션 중 greeks 있는 것: {has_greeks}개")
                print(f"  필터된 옵션 중 open_interest > 0: {has_oi}개")
                print(f"  필터된 옵션 중 gamma 있는 것: {has_gamma}개")

                # 샘플 5개 상세 출력
                print(f"\n  [샘플 5개 상세]")
                for o in filtered[:5]:
                    g    = getattr(o, "greeks", None)
                    oi   = getattr(o, "open_interest", None)
                    gamma = getattr(g, "gamma", None) if g else None
                    print(f"    {o.symbol} | DTE={days_to_expiry(o.symbol)} "
                          f"| OI={oi} | gamma={gamma} | greeks={'O' if g else 'X'}")
            else:
                print("  ⚠️ DTE 필터 통과 옵션 없음!")

    except Exception as e:
        print(f"  ❌ Alpaca 실패: {e}")

    # ── yfinance 폴백 확인 ────────────────────────────
    print(f"\n  [yfinance 폴백 확인]")
    try:
        ticker     = yf.Ticker(idx_sym)
        exps       = ticker.options
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

        if target_exp:
            yf_chain = ticker.option_chain(target_exp)
            has_gamma_col = "gamma" in yf_chain.calls.columns
            print(f"  target_exp: {target_exp}")
            print(f"  gamma 컬럼 있음: {has_gamma_col}")
            if has_gamma_col:
                valid_gamma = yf_chain.calls["gamma"].notna().sum()
                valid_oi    = yf_chain.calls["openInterest"].notna().sum()
                print(f"  calls gamma 유효값: {valid_gamma}개 / OI 유효값: {valid_oi}개")
                idx_price = ticker.history(period="1d")["Close"].iloc[-1]
                gex_call  = float(
                    (yf_chain.calls["gamma"].fillna(0)
                     * yf_chain.calls["openInterest"].fillna(0)
                     * idx_price ** 2 * 0.01).sum()
                )
                gex_put   = float(
                    (yf_chain.puts["gamma"].fillna(0)
                     * yf_chain.puts["openInterest"].fillna(0)
                     * idx_price ** 2 * 0.01).sum()
                )
                print(f"  ✅ yfinance GEX 계산 결과: call={round(gex_call,2)}, "
                      f"put={round(gex_put,2)}, net={round(gex_call-gex_put,2)}")
            else:
                print("  ⚠️ gamma 컬럼 없음 → yfinance 폴백도 불가")
        else:
            print("  ⚠️ 적합한 만기일 없음")
    except Exception as e:
        print(f"  ❌ yfinance 실패: {e}")
