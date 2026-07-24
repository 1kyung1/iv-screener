"""
validate_iv_data.py — 수집된 CSV의 '연속성'을 검증한다.
=========================================================
iv_collector.py의 check_data_quality()는 오늘 수집분만 보므로
"어제와 이어지는가"를 판정할 수 없다. 이 모듈이 그 역할을 맡는다.

검증 규칙 (전부 전일 대비)
  R1 오늘 데이터 존재     — 행이 아예 없거나 종목 수가 급감
  R2 결측률 급변          — 컬럼별 null 비율이 급등/급락
  R3 레벨 급변            — 컬럼별 중앙값이 배수로 점프
  R4 값 범위 위반         — 음수 실적일수, 음수 프리미엄, 비정상 IV 등
  R5 만기 기준 변경       — 근월물/OI 만기의 잔존일수 분포가 바뀜
  R6 계산 버전 혼재       — calc_ver가 최근 구간에 2개 이상

사용법
  # 수집기에서 import (권장)
  from validate_iv_data import check_history_drift
  alerts = check_history_drift("iv_data", today)

  # 단독 실행
  python validate_iv_data.py              # 최신일 점검 + 텔레그램
  python validate_iv_data.py --all        # 전 기간 소급 점검 (알림 없음)
  python validate_iv_data.py --csv path.csv --date 2026-07-13
  python validate_iv_data.py --all --quiet   # 요약만
"""

import os
import sys
import glob
import argparse

import numpy as np
import pandas as pd

# ==========================================================
# 임계값 — 실제 15일 데이터로 튜닝한 값
# ==========================================================
ROW_DROP_RATE = 0.20        # R1: 종목 수가 전일 대비 20% 이상 감소
NULL_JUMP_PP = 0.15         # R2: null 비율이 15%p 이상 변동
NULL_MIN_BASE = 0.02        # R2: 원래 null이 거의 없던 컬럼만 (오탐 억제)
LEVEL_RATIO = 3.0           # R3: 중앙값이 3배 이상 또는 1/3 이하
LEVEL_MIN_ABS = 1e-9        # R3: 0 근처 컬럼 제외
DTE_SHIFT_DAYS = 3          # R5: 최빈 잔존일수가 3일 이상 이동

# R2/R3 감시 대상 — 분석에 실제로 쓰는 컬럼만 본다.
WATCH_NULL = [
    "avg_iv", "skew", "pcr_oi", "pcr_vol", "max_pain", "call_oi", "put_oi",
    "call_prem", "call_prem_st", "put_prem", "put_prem_st",
    "iv_hv_diff", "iv_term_slope", "gex", "dex", "close",
]
WATCH_LEVEL = [
    "call_prem", "call_prem_st", "put_prem", "put_prem_st",
    "call_oi", "put_oi", "call_vol", "put_vol", "gex", "dex", "avg_iv",
]

# R4: (컬럼, 조건함수, 설명)
RANGE_RULES = [
    ("days_to_earn", lambda s: s < 0,            "음수(과거 실적일)"),
    ("call_prem",    lambda s: s < 0,            "음수"),
    ("call_prem_st", lambda s: s < 0,            "음수"),
    ("put_prem",     lambda s: s < 0,            "음수"),
    ("put_prem_st",  lambda s: s < 0,            "음수"),
    ("avg_iv",       lambda s: s <= 0,           "0 이하"),
    ("pcr_oi",       lambda s: s < 0,            "음수"),
    ("pcr_vol",      lambda s: s < 0,            "음수"),
    ("skew",         lambda s: s.abs() > 2.0,    "|skew|>2 (비정상)"),
    ("close",        lambda s: s <= 0,           "0 이하"),
]
RANGE_ALERT_RATE = 0.01     # R4: 해당 위반이 1% 이상일 때만 알림
RANGE_WORSEN_PP  = 0.02     # R4: 지속 결함은 2%p 이상 악화될 때만 재알림


# ==========================================================
def _load(csv_path=None, base_name="iv_data"):
    """분할된 iv_data_*_H*.csv를 모두 읽어 하나로 합친다."""
    if csv_path:
        paths = [csv_path]
    else:
        paths = sorted(glob.glob(f"{base_name}_*_H*.csv"))
    if not paths:
        return None
    frames = []
    for p in paths:
        try:
            frames.append(pd.read_csv(p))
        except Exception as e:
            print(f"  [읽기 실패] {p}: {e}")
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    if "date" not in df.columns:
        return None
    return df.sort_values("date").reset_index(drop=True)


def _fmt(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "-"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:.2f}B"
    if a >= 1e6:
        return f"{v/1e6:.2f}M"
    if a >= 1e3:
        return f"{v:,.0f}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")


# ==========================================================
# 규칙별 검사
# ==========================================================
def _r1_presence(cur, prev, d_cur):
    out = []
    if len(cur) == 0:
        return [f"🚨 <b>{d_cur} 데이터 없음</b>\n   수집이 실패했거나 저장되지 않음"]
    if prev is None or len(prev) == 0:
        return out
    drop = 1 - len(cur) / len(prev)
    if drop >= ROW_DROP_RATE:
        out.append(
            f"🚨 <b>수집 종목 급감</b>\n"
            f"   {len(prev)}종목 → {len(cur)}종목 ({drop*100:.0f}% 감소)"
        )
    return out


def _r2_null(cur, prev):
    out = []
    for col in WATCH_NULL:
        if col not in cur.columns or col not in prev.columns:
            continue
        a, b = prev[col].isna().mean(), cur[col].isna().mean()
        if abs(b - a) < NULL_JUMP_PP:
            continue
        if min(a, b) > 1 - NULL_MIN_BASE:      # 양쪽 다 거의 전부 결측이면 무시
            continue
        arrow = "급등" if b > a else "급감"
        n_aff = int(abs(b - a) * len(cur))
        out.append(
            f"⚠️ <b>{col} 결측률 {arrow}</b>\n"
            f"   {a*100:.0f}% → {b*100:.0f}% ({(b-a)*100:+.0f}%p, 약 {n_aff}종목)"
        )
    return out


def _r3_level(cur, prev):
    out = []
    for col in WATCH_LEVEL:
        if col not in cur.columns or col not in prev.columns:
            continue
        a = pd.to_numeric(prev[col], errors="coerce").dropna()
        b = pd.to_numeric(cur[col], errors="coerce").dropna()
        if len(a) < 30 or len(b) < 30:
            continue
        ma, mb = a.median(), b.median()
        if abs(ma) < LEVEL_MIN_ABS or abs(mb) < LEVEL_MIN_ABS:
            continue
        if ma * mb < 0:                         # 부호가 뒤집힘
            out.append(f"⚠️ <b>{col} 부호 반전</b>\n   중앙값 {_fmt(ma)} → {_fmt(mb)}")
            continue
        r = abs(mb / ma)
        if r >= LEVEL_RATIO or r <= 1 / LEVEL_RATIO:
            out.append(
                f"⚠️ <b>{col} 레벨 급변</b>\n"
                f"   중앙값 {_fmt(ma)} → {_fmt(mb)} ({r:.1f}배)"
            )
    return out


def _r4_range(cur, prev):
    """이미 알고 있는 결함이 매일 울리면 알림 피로가 생긴다.
    신규 발생이거나 비율이 악화됐을 때만 알린다."""
    out = []
    n = len(cur)
    for col, cond, desc in RANGE_RULES:
        if col not in cur.columns:
            continue
        s = pd.to_numeric(cur[col], errors="coerce")
        bad = cond(s).fillna(False)
        rate = bad.sum() / n if n else 0
        if rate < RANGE_ALERT_RATE:
            continue

        prev_rate = 0.0
        if prev is not None and col in prev.columns and len(prev):
            ps = pd.to_numeric(prev[col], errors="coerce")
            prev_rate = cond(ps).fillna(False).sum() / len(prev)

        is_new = prev_rate < RANGE_ALERT_RATE
        is_worse = rate - prev_rate >= RANGE_WORSEN_PP
        if not (is_new or is_worse):
            continue                       # 지속 중인 기존 결함 → 침묵

        tag = "신규 발생" if is_new else f"악화 {prev_rate*100:.0f}%→{rate*100:.0f}%"
        syms = ""
        if "symbol" in cur.columns:
            ex = cur.loc[bad, "symbol"].head(5).tolist()
            syms = f"\n   예: {', '.join(map(str, ex))}"
        out.append(
            f"⚠️ <b>{col} 값 이상 ({tag})</b>\n"
            f"   {desc} {int(bad.sum())}건 ({rate*100:.0f}%){syms}"
        )
    return out


def _dte(df, exp_col):
    if exp_col not in df.columns or "date" not in df.columns:
        return None
    e = pd.to_datetime(df[exp_col], errors="coerce")
    d = pd.to_datetime(df["date"], errors="coerce")
    return (e - d).dt.days.dropna()


def _r5_expiry(cur, prev):
    out = []
    for exp_col, label in [("exp_st", "근월물"), ("oi_exp", "OI 만기")]:
        a, b = _dte(prev, exp_col), _dte(cur, exp_col)
        if a is None or b is None or len(a) < 30 or len(b) < 30:
            continue
        ma, mb = a.mode(), b.mode()
        if ma.empty or mb.empty:
            continue
        ma, mb = int(ma.iloc[0]), int(mb.iloc[0])
        # 하루 경과분(-1)은 정상이므로 제외
        shift = abs((mb - ma) + 1)
        if shift >= DTE_SHIFT_DAYS:
            out.append(
                f"⚠️ <b>{label} 만기 교체</b>\n"
                f"   최빈 잔존일수 {ma}d → {mb}d\n"
                f"   → 프리미엄/OI 시계열 단절, 전일 대비 비교 불가"
            )
    return out


def _r6_calcver(df, d_cur, window=5):
    if "calc_ver" not in df.columns:
        return []
    dates = sorted(df["date"].unique())
    recent = [d for d in dates if d <= d_cur][-window:]
    vers = df[df["date"].isin(recent)]["calc_ver"].dropna().unique()
    if len(vers) >= 2:
        return [
            f"ℹ️ <b>계산 버전 혼재</b>\n"
            f"   최근 {len(recent)}일에 {', '.join(map(str, vers))} 공존\n"
            f"   → 해당 구간은 버전별로 분리해 분석할 것"
        ]
    return []


# ==========================================================
# 공개 API
# ==========================================================
def check_history_drift(base_name="iv_data", target_date=None, csv_path=None):
    """target_date(기본: 최신일)를 직전 수집일과 비교해 경고 목록을 반환."""
    df = _load(csv_path, base_name)
    if df is None:
        return ["🚨 <b>CSV를 찾을 수 없음</b>\n   검증을 수행하지 못함"]

    dates = sorted(df["date"].unique())
    if not dates:
        return ["🚨 <b>CSV에 데이터가 없음</b>"]

    d_cur = str(target_date) if target_date else dates[-1]
    if d_cur not in dates:
        return [f"🚨 <b>{d_cur} 데이터 없음</b>\n   수집 실패 또는 저장 누락 (최신: {dates[-1]})"]

    idx = dates.index(d_cur)
    cur = df[df["date"] == d_cur]
    prev = df[df["date"] == dates[idx - 1]] if idx > 0 else None

    alerts = _r1_presence(cur, prev, d_cur)
    if prev is not None and len(prev):
        alerts += _r2_null(cur, prev)
        alerts += _r3_level(cur, prev)
        alerts += _r5_expiry(cur, prev)
    alerts += _r4_range(cur, prev)
    alerts += _r6_calcver(df, d_cur)
    return alerts


def send_telegram(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("  [텔레그램] 환경변수 없음 → 전송 생략")
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"  [텔레그램 실패] {e}")


# ==========================================================
def main():
    ap = argparse.ArgumentParser(description="수집 CSV 연속성 검증")
    ap.add_argument("--csv", default=None, help="CSV 경로 (기본: iv_data_*_H*.csv 자동 탐색)")
    ap.add_argument("--base", default="iv_data", help="파일 접두어")
    ap.add_argument("--date", default=None, help="점검 기준일 YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="전 기간 소급 점검 (텔레그램 미전송)")
    ap.add_argument("--quiet", action="store_true", help="요약만 출력")
    a = ap.parse_args()

    if a.all:
        df = _load(a.csv, a.base)
        if df is None:
            print("CSV 없음")
            sys.exit(1)
        dates = sorted(df["date"].unique())
        print(f"소급 점검: {len(dates)}일 ({dates[0]} ~ {dates[-1]})\n")
        total = 0
        for d in dates:
            al = check_history_drift(a.base, d, a.csv)
            if not al:
                if not a.quiet:
                    print(f"  {d}  ✅ 이상 없음")
                continue
            total += len(al)
            print(f"  {d}  🔔 경고 {len(al)}건")
            if not a.quiet:
                for x in al:
                    for i, line in enumerate(x.replace("<b>", "").replace("</b>", "").split("\n")):
                        print(("      " if i == 0 else "      ") + line.strip())
                print()
        print(f"\n총 경고 {total}건 / {len(dates)}일")
        return

    alerts = check_history_drift(a.base, a.date, a.csv)
    if not alerts:
        print("✅ 데이터 연속성 이상 없음")
        return
    body = "\n\n".join(alerts)
    print(body.replace("<b>", "").replace("</b>", ""))
    send_telegram(f"🔎 <b>데이터 연속성 점검</b>\n\n{body}")


if __name__ == "__main__":
    main()
