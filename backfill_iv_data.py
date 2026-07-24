"""
iv_data CSV 소급 보정 스크립트
================================
iv_collector.py v3.1 데이터의 '복구 가능한' 결함만 수정한다.

수정 항목
  1) dte_st        — exp_st - date 로 역산해 신규 컬럼 추가 (만기 이질성 추적용)
  2) days_to_earn  — get_earnings_date()의 과거 실적일 버그로 생긴 음수 보정
  3) iv_rank       — CSV에 이미 있는 avg_iv 이력으로 백분위 재계산

복구 불가 (표시만)
  - call_prem / call_prem_st : 행사가별 bid/ask 미저장 → lastPrice 오차 잔존
  - gex 계열                 : 과거 옵션체인 재조회 불가
  - put_prem                 : 신규 컬럼, 과거 행은 NULL

사용법
  python backfill_iv_data.py iv_data_2026_H2.csv            # 실적일 보정 포함(네트워크 필요)
  python backfill_iv_data.py iv_data_2026_H2.csv --no-earn  # 네트워크 없이 1,3번만
원본은 <파일명>.bak 으로 백업한 뒤 원자적으로 교체한다.
"""

import os
import sys
import glob
import shutil

import numpy as np
import pandas as pd

IV_RANK_MIN_DAYS = 40      # iv_collector.py와 동일
IV_RANK_WINDOW = 252
STD_DTE_BANDS = ("30-45", "25-50")


# ----------------------------------------------------------
# 1) dte_st — 근월물 잔존일수 역산
# ----------------------------------------------------------
def add_dte_st(df):
    if "exp_st" not in df.columns:
        print("  [dte_st] exp_st 컬럼 없음 → 건너뜀")
        return df, 0
    exp = pd.to_datetime(df["exp_st"], errors="coerce")
    dte = (exp - pd.to_datetime(df["date"])).dt.days
    df["dte_st"] = dte
    n = int(dte.notna().sum())
    if n:
        vc = dte.value_counts().sort_index()
        print(f"  [dte_st] {n}행 산출 | 분포: "
              + ", ".join(f"{int(k)}d={v}" for k, v in vc.items()))
    return df, n


# ----------------------------------------------------------
# 2) days_to_earn — 음수(과거 실적일) 보정
# ----------------------------------------------------------
def fetch_earnings_calendar(symbols):
    """종목별 실제 실적일 목록을 yfinance에서 수집. {symbol: [date, ...]}"""
    import yfinance as yf

    cal = {}
    for i, sym in enumerate(symbols, 1):
        try:
            t = yf.Ticker(sym.replace("-", "."))
            dates = []
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                dates += [d.date() for d in ed.index.tz_localize(None)]
            c = t.calendar
            if isinstance(c, dict) and c.get("Earnings Date"):
                v = c["Earnings Date"]
                dates += [pd.Timestamp(x).date() for x in (v if isinstance(v, list) else [v])]
            if dates:
                cal[sym] = sorted(set(dates))
        except Exception as e:
            print(f"    [실적일 실패] {sym}: {e}")
        if i % 50 == 0:
            print(f"    ...{i}/{len(symbols)} 종목")
    return cal


def fix_days_to_earn(df):
    """음수가 하나라도 있는 종목만 재조회해 호출량을 줄인다."""
    if "days_to_earn" not in df.columns:
        print("  [days_to_earn] 컬럼 없음 → 건너뜀")
        return df, 0

    bad = df["days_to_earn"] < 0
    targets = sorted(df.loc[bad, "symbol"].dropna().unique())
    print(f"  [days_to_earn] 음수 {int(bad.sum())}행 / {len(targets)}종목 → 실적일 재조회")
    if not targets:
        return df, 0

    cal = fetch_earnings_calendar(targets)
    if not cal:
        print("  [days_to_earn] 실적일 수집 실패 → 원본 유지")
        return df, 0

    dates = pd.to_datetime(df["date"]).dt.date
    fixed = 0
    for sym, earn_dates in cal.items():
        arr = np.array(earn_dates)
        m = (df["symbol"] == sym) & bad
        for idx in df.index[m]:
            d0 = dates.loc[idx]
            future = arr[arr >= d0]
            # 다음 실적일이 확인되면 재계산, 없으면 NULL (음수보다 안전)
            df.at[idx, "days_to_earn"] = (future[0] - d0).days if len(future) else np.nan
            fixed += 1
    remain = int((df["days_to_earn"] < 0).sum())
    print(f"  [days_to_earn] {fixed}행 보정 완료 | 잔여 음수 {remain}행")
    return df, fixed


# ----------------------------------------------------------
# 3) iv_rank — avg_iv 이력으로 백분위 재계산
# ----------------------------------------------------------
def rebuild_iv_rank(df, hist_df=None):
    """iv_collector.py의 add_iv_rank()와 동일 규칙을 소급 적용.
    각 행 시점까지의 과거 데이터만 사용하므로 lookahead 없음.

    ⚠️ CSV가 iv_data_2026_H1 / H2로 분할돼 있으면 파일 하나만 봐서는
    이력이 끊긴다. hist_df에 전 파일을 합쳐 넘기면 경계를 넘어 계산한다.
    (iv_collector.py add_iv_rank()가 glob으로 전 파일을 읽는 것과 동일한 규칙)
    """
    if "avg_iv" not in df.columns:
        print("  [iv_rank] avg_iv 컬럼 없음 → 건너뜀")
        return df, 0

    src = df if hist_df is None else hist_df
    src = src.sort_values(["symbol", "date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["iv_rank"] = np.nan
    is_auto = df.get("dte_range", pd.Series("", index=df.index)).astype(str).str.startswith("auto")
    is_std = df.get("dte_range", pd.Series("", index=df.index)).isin(STD_DTE_BANDS)

    # 종목별 이력: (date, avg_iv) — 표준 DTE밴드 행만
    hist_map = {}
    src_std = src[src.get("dte_range", pd.Series("", index=src.index)).isin(STD_DTE_BANDS)]
    for sym, g in src_std.groupby("symbol", sort=False):
        h = g[["date", "avg_iv"]].dropna()
        if len(h) >= IV_RANK_MIN_DAYS:
            hist_map[sym] = h

    filled = 0
    for sym, g in df.groupby("symbol", sort=False):
        h = hist_map.get(sym)
        if h is None:
            continue
        for idx in g.index:
            if is_auto.loc[idx] or pd.isna(df.at[idx, "avg_iv"]):
                continue
            d0 = df.at[idx, "date"]
            past = h.loc[h["date"] < d0, "avg_iv"].tail(IV_RANK_WINDOW)
            if len(past) >= IV_RANK_MIN_DAYS:
                df.at[idx, "iv_rank"] = round(float((past < df.at[idx, "avg_iv"]).mean() * 100), 1)
                filled += 1

    if filled:
        print(f"  [iv_rank] {filled}행 산출")
    else:
        n_days = src["date"].nunique()
        print(f"  [iv_rank] 표본 부족 (이력 {n_days}일 < 필요 {IV_RANK_MIN_DAYS}일) → 전량 NULL 유지")
    return df, filled


# ----------------------------------------------------------
def _targets(argv):
    """인자로 받은 파일들, 없으면 iv_data_*_H*.csv 전부."""
    files = [a for a in argv[1:] if not a.startswith("-")]
    if not files:
        files = sorted(glob.glob("iv_data_*_H*.csv"))
    ok = []
    for f in files:
        if not os.path.exists(f):
            print(f"⚠️ 파일 없음, 건너뜀: {f}")
            continue
        if os.path.basename(f).startswith("market_data"):
            # market_data는 symbol/avg_iv/exp_st가 없어 보정 대상이 아니다.
            print(f"⚠️ market_data는 보정 대상이 아님, 건너뜀: {f}")
            continue
        ok.append(f)
    return ok


def _combined_history(files):
    """iv_rank용 이력 — 분할된 전 파일을 합친다 (H1/H2 경계 무시)."""
    frames = []
    for f in sorted(glob.glob("iv_data_*_H*.csv")) or files:
        try:
            frames.append(pd.read_csv(f, usecols=lambda c: c in
                          ("date", "symbol", "avg_iv", "dte_range")))
        except Exception as e:
            print(f"  [이력 읽기 실패] {f}: {e}")
    if not frames:
        return None
    h = pd.concat(frames, ignore_index=True)
    return h.sort_values(["symbol", "date"])


def process_one(path, do_earn, hist_df):
    df = pd.read_csv(path)
    orig_cols = list(df.columns)
    print(f"\n▶ {path} ({len(df)}행 × {len(orig_cols)}컬럼)")

    df, _ = add_dte_st(df)
    if do_earn:
        df, _ = fix_days_to_earn(df)
    else:
        print("  [days_to_earn] --no-earn → 건너뜀")
    df, _ = rebuild_iv_rank(df, hist_df)

    # 컬럼 순서 유지 + 신규 컬럼은 뒤에 추가
    new_cols = [c for c in df.columns if c not in orig_cols]
    df = df[orig_cols + new_cols]
    df = df.sort_values(["date", "symbol"]).reset_index(drop=True)

    shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)
    print(f"  저장 완료: {len(df)}행 × {len(df.columns)}컬럼"
          + (f" | 신규 컬럼 {new_cols}" if new_cols else ""))


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    do_earn = "--no-earn" not in sys.argv
    files = _targets(sys.argv)
    if not files:
        print("보정할 iv_data 파일이 없습니다.")
        sys.exit(1)
    print(f"대상 파일 {len(files)}개: {', '.join(files)}")
    hist = _combined_history(files)
    if hist is not None:
        print(f"iv_rank 이력: {hist['date'].nunique()}일 / {hist['symbol'].nunique()}종목 (전 파일 합산)")
    for f in files:
        process_one(f, do_earn, hist)
    print("\n완료.")


if __name__ == "__main__":
    main()
