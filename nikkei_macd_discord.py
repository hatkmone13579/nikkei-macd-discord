#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日経225 MACD(8,17,9) クロス検知 → Discord 通知  ★v2 (Pro Edition)
================================================================================
■ 検知エンジン
  - 終値ベースの MACD(8,17,9) 当日クロスを検出（休場日スキップ／重複通知防止）
  - シグナル品質スコア(0-100点) と S/A/B/C グレードを自動判定
      +30 週足MACDの方向と一致（マルチタイムフレーム順行）
      +20 0ラインに対する位置が有利（GC:0上 / DC:0下）
      +20 200日移動平均とトレンド整合
      +20 出来高確認（当日 >= 25日平均 × 1.2倍）
      +10 ヒストグラムが加速方向
      -15 直近10営業日に他のクロスあり（往来＝ダマシ多発ゾーン）
  - 市場全体の地合い（MACD強気比率＝ブレッドス）と日経平均自体の状態も把握

■ UI（Discord）
  - マーケット概況カード（日経平均・地合いゲージ・本日の検知数）
  - 銘柄ごとにグレードバッジ・確認タグ（週足/0ライン/出来高/200日線）付き
  - スコア上位シグナルは チャート画像（株価+MACD）を自動生成して添付
  - 多数検知日は自動分割送信（Discord上限で切れない）

■ 使いやすさ
  - 設定は環境変数で上書き可能（コード編集不要）
  - TEST_MODE=1 でセットアップ疎通テスト（株価取得なしで即確認）
  - FORCE_RUN=1 で休場日/データ未反映チェックを無視して強制実行

【免責】スコアはルールベースの参考指標であり、将来の値動きや勝率を保証する
        ものではありません。売買の推奨ではなく、投資判断は自己責任で。
【注意】データは Yahoo Finance（非公式）。稀に欠損・遅延・調整ズレがあります。
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta, date

import requests
import pandas as pd
import yfinance as yf

try:
    import jpholiday
    HAS_JPHOLIDAY = True
except Exception:
    HAS_JPHOLIDAY = False

# ============================================================
# 設定（環境変数で上書き可能）
# ============================================================
JST = timezone(timedelta(hours=9))


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
ADMIN_WEBHOOK_URL = os.environ.get("ADMIN_WEBHOOK_URL", DISCORD_WEBHOOK_URL)

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 8, 17, 9
HISTORY_PERIOD = "2y"            # 週足MACD・200日線の計算に必要な長さ

# ---- 出来高フィルター --------------------------------------------------------
VOLUME_FILTER_ENABLED = _env_bool("VOLUME_FILTER_ENABLED", True)
VOLUME_CONFIRM_RATIO = _env_float("VOLUME_CONFIRM_RATIO", 1.2)
VOLUME_MA_WINDOW = 25
MIN_TURNOVER_JPY = _env_float("MIN_TURNOVER_JPY", 100_000_000)
# ---- 通知の絞り込み ----------------------------------------------------------
MIN_GRADE = os.environ.get("MIN_GRADE", "C").upper()   # S/A/B/C（C=全通知）
MIN_HIST_ABS = _env_float("MIN_HIST_ABS", 0.0)
# ---- チャート ----------------------------------------------------------------
CHART_TOP_N = _env_int("CHART_TOP_N", 3)               # 上位N件にチャート添付(0で無効)
CHART_BARS = 120                                       # 表示する日足本数
# ---- 実行制御 ----------------------------------------------------------------
REQUIRE_TODAY = _env_bool("REQUIRE_TODAY", True)
TEST_MODE = _env_bool("TEST_MODE", False)
FORCE_RUN = _env_bool("FORCE_RUN", False)
# ---- 表示 --------------------------------------------------------------------
STOCKS_PER_EMBED = _env_int("STOCKS_PER_EMBED", 6)
EMBEDS_PER_MESSAGE = 4
# ---- 運用 --------------------------------------------------------------------
REBALANCE_WARN_DAYS = 30        # 日経225定期見直し(4月/10月第1営業日)の事前通知
BATCH_SIZE = 50
RETRY = 4
SLEEP_BETWEEN_BATCH = 1.5
STATE_FILE = os.environ.get("STATE_FILE", "macd_state.json")

GRADE_ORDER = {"S": 3, "A": 2, "B": 1, "C": 0}
GRADE_EMOJI = {"S": "🏆", "A": "🥇", "B": "🥈", "C": "🥉"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nikkei_macd")

# ============================================================
# 日経225 構成銘柄（code: 名称）— サンプル。最新の全225銘柄は
# nikkei225.csv (列: code,name) を置けば自動で差し替わります。
# ============================================================
NIKKEI225_SAMPLE = {
    "1332": "ニッスイ", "1925": "大和ハウス工業", "1928": "積水ハウス",
    "2502": "アサヒグループHD", "2503": "キリンHD", "2802": "味の素",
    "2914": "日本たばこ産業", "3382": "セブン&アイHD", "4063": "信越化学工業",
    "4452": "花王", "4502": "武田薬品工業", "4503": "アステラス製薬",
    "4519": "中外製薬", "4523": "エーザイ", "4543": "テルモ",
    "4568": "第一三共", "4661": "オリエンタルランド", "4901": "富士フイルムHD",
    "5108": "ブリヂストン", "6098": "リクルートHD", "6273": "SMC",
    "6301": "コマツ", "6367": "ダイキン工業", "6501": "日立製作所",
    "6594": "ニデック", "6645": "オムロン", "6701": "NEC",
    "6702": "富士通", "6752": "パナソニックHD", "6758": "ソニーグループ",
    "6861": "キーエンス", "6902": "デンソー", "6920": "レーザーテック",
    "6954": "ファナック", "6981": "村田製作所", "7011": "三菱重工業",
    "7203": "トヨタ自動車", "7267": "ホンダ", "7269": "スズキ",
    "7741": "HOYA", "7751": "キヤノン", "7974": "任天堂",
    "8001": "伊藤忠商事", "8002": "丸紅", "8031": "三井物産",
    "8035": "東京エレクトロン", "8053": "住友商事", "8058": "三菱商事",
    "8306": "三菱UFJ FG", "8316": "三井住友FG", "8411": "みずほFG",
    "8591": "オリックス", "8766": "東京海上HD", "9020": "JR東日本",
    "9022": "JR東海", "9432": "NTT", "9433": "KDDI",
    "9434": "ソフトバンク", "9983": "ファーストリテイリング",
    "9984": "ソフトバンクグループ",
}


def load_constituents() -> dict:
    path = os.environ.get("CONSTITUENTS_CSV", "nikkei225.csv")
    if os.path.exists(path):
        # 文字コードはExcel保存等でまちまちなので順に試す（UTF-8/BOM/Shift-JIS等）
        df = None
        for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
            try:
                df = pd.read_csv(path, dtype={"code": str}, encoding=enc)
                log.info("CSV読込成功 (encoding=%s)", enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if df is None:
            df = pd.read_csv(path, dtype={"code": str},
                             encoding="utf-8", encoding_errors="replace")
            log.warning("文字コード不明のため強制読込しました")
        # 列名のゆらぎ・前後空白を吸収
        df.columns = [str(c).strip().lower() for c in df.columns]
        m = dict(zip(df["code"].astype(str).str.strip().str.zfill(4),
                     df["name"].astype(str).str.strip()))
        log.info("構成銘柄を %s から読込: %d 件", path, len(m))
        return m
    log.warning("構成銘柄CSVが無いためサンプル %d 件で実行", len(NIKKEI225_SAMPLE))
    return dict(NIKKEI225_SAMPLE)


# ============================================================
# 営業日・定期見直し
# ============================================================
def is_trading_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    dd = d.date() if isinstance(d, datetime) else d
    if HAS_JPHOLIDAY and jpholiday.is_holiday(dd):
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    return True


def first_business_day(y: int, m: int) -> date:
    d = date(y, m, 1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def next_rebalance_date(today: date) -> date:
    cands = [first_business_day(today.year, 4),
             first_business_day(today.year, 10),
             first_business_day(today.year + 1, 4)]
    return min(c for c in cands if c >= today)


# ============================================================
# テクニカル計算
# ============================================================
def compute_macd(close: pd.Series):
    ema_f = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_s = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_f - ema_s
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return macd, signal, macd - signal


def detect_cross(macd: pd.Series, signal: pd.Series):
    if len(macd) < 2:
        return None
    prev = macd.iloc[-2] - signal.iloc[-2]
    cur = macd.iloc[-1] - signal.iloc[-1]
    if prev <= 0 and cur > 0:
        return "GOLDEN"
    if prev >= 0 and cur < 0:
        return "DEAD"
    return None


def weekly_macd_bull(close_daily: pd.Series):
    """週足MACDが強気(MACD>Signal)か。データ不足は None。"""
    try:
        w = close_daily.resample("W-FRI").last().dropna()
        if len(w) < MACD_SLOW + MACD_SIGNAL:
            return None
        mw, sw, _ = compute_macd(w)
        return bool(mw.iloc[-1] > sw.iloc[-1])
    except Exception:
        return None


def count_recent_flips(macd: pd.Series, signal: pd.Series, lookback: int = 10) -> int:
    """直近lookback本での MACD-Signal の符号反転回数（当日クロス含む）。
    差分がほぼ0の浮動小数ノイズは無視する。"""
    tail_m = macd.tail(lookback + 1)
    tail_s = signal.tail(lookback + 1)
    d = (tail_m - tail_s).values
    scale = max(float(tail_m.abs().max()), float(tail_s.abs().max()), 1e-9)
    eps = scale * 1e-6
    flips, prev_sign = 0, 0
    for v in d:
        s = 1 if v > eps else (-1 if v < -eps else 0)
        if s == 0:
            continue
        if prev_sign != 0 and s != prev_sign:
            flips += 1
        prev_sign = s
    return flips


def volume_confirmed(volume: pd.Series) -> tuple:
    """(確認OKか, 当日/平均の倍率) を返す。データ不足は (True, None)。"""
    if len(volume) < VOLUME_MA_WINDOW + 1:
        return True, None
    avg = volume.iloc[-(VOLUME_MA_WINDOW + 1):-1].mean()
    if avg <= 0:
        return True, None
    ratio = float(volume.iloc[-1] / avg)
    return ratio >= VOLUME_CONFIRM_RATIO, ratio


def score_signal(cross: str, macd, signal, hist, close, volume) -> dict:
    """シグナル品質スコア(0-100)・グレード・確認タグを算出。"""
    score, tags = 0, []

    # ① 週足トレンド一致 (+30)
    wb = weekly_macd_bull(close)
    if wb is not None:
        aligned = (wb and cross == "GOLDEN") or ((not wb) and cross == "DEAD")
        if aligned:
            score += 30
            tags.append("週足順行")
        else:
            tags.append("週足逆行⚠️")

    # ② 0ライン位置 (+20)
    m = float(macd.iloc[-1])
    if (cross == "GOLDEN" and m > 0) or (cross == "DEAD" and m < 0):
        score += 20
        tags.append("0ライン" + ("上" if m > 0 else "下"))
    else:
        tags.append("0ライン" + ("上" if m > 0 else "下") + "(逆)")

    # ③ 200日線との整合 (+20)
    if len(close) >= 200:
        sma200 = float(close.rolling(200).mean().iloc[-1])
        above = float(close.iloc[-1]) > sma200
        if (cross == "GOLDEN" and above) or (cross == "DEAD" and not above):
            score += 20
            tags.append("200日線" + ("上" if above else "下"))
        else:
            tags.append("200日線" + ("上" if above else "下") + "(逆)")

    # ④ 出来高確認 (+20)
    vol_ok, ratio = volume_confirmed(volume)
    if vol_ok and ratio is not None:
        score += 20
        tags.append(f"出来高{ratio:.1f}倍◎")
    elif ratio is not None:
        tags.append(f"出来高{ratio:.1f}倍")

    # ⑤ ヒストグラム加速 (+10)
    if len(hist) >= 2:
        if (cross == "GOLDEN" and hist.iloc[-1] > hist.iloc[-2]) or \
           (cross == "DEAD" and hist.iloc[-1] < hist.iloc[-2]):
            score += 10

    # ⑥ 往来（ダマシ）ペナルティ (-15)
    if count_recent_flips(macd, signal, 10) >= 2:
        score -= 15
        tags.append("往来注意⚠️")

    score = max(0, min(100, score))
    grade = "S" if score >= 80 else "A" if score >= 60 else "B" if score >= 40 else "C"
    return {"score": score, "grade": grade, "tags": tags,
            "vol_ok": vol_ok, "vol_ratio": ratio}


# ============================================================
# データ取得
# ============================================================
def fetch_prices(tickers: list) -> dict:
    result = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        for attempt in range(1, RETRY + 1):
            try:
                data = yf.download(
                    tickers=batch, period=HISTORY_PERIOD, interval="1d",
                    auto_adjust=True, group_by="ticker",
                    threads=True, progress=False,
                )
                for t in batch:
                    try:
                        df = data[t] if len(batch) > 1 else data
                        df = df.dropna(subset=["Close"])
                        if len(df) >= MACD_SLOW + MACD_SIGNAL + 1:
                            result[t] = df
                    except Exception:
                        pass
                break
            except Exception as e:
                log.warning("バッチ取得失敗(%d/%d): %s", attempt, RETRY, e)
                time.sleep(2 * attempt)
        time.sleep(SLEEP_BETWEEN_BATCH)
    return result


def fetch_index():
    """日経平均(^N225)の概況。失敗時は None。"""
    try:
        df = yf.download("^N225", period="6mo", interval="1d",
                         auto_adjust=True, progress=False)
        df = df.dropna(subset=["Close"]) if "Close" in df else df.dropna()
        close = df["Close"].squeeze()
        if len(close) < MACD_SLOW + MACD_SIGNAL:
            return None
        m, s, _ = compute_macd(close)
        return {"close": float(close.iloc[-1]),
                "chg": float((close.iloc[-1] / close.iloc[-2] - 1) * 100),
                "bull": bool(m.iloc[-1] > s.iloc[-1])}
    except Exception as e:
        log.warning("指数取得失敗: %s", e)
        return None


# ============================================================
# 状態（重複通知防止）
# ============================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s: dict):
    try:
        json.dump(s, open(STATE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("状態保存失敗: %s", e)


# ============================================================
# チャート生成（上位シグナル用）
# ============================================================
def _setup_chart_font():
    try:
        import matplotlib
        import matplotlib.font_manager as fm
        names = {f.name for f in fm.fontManager.ttflist}
        for cand in ("Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic"):
            if cand in names:
                matplotlib.rcParams["font.family"] = cand
                return True
        return False
    except Exception:
        return False


def make_chart(code: str, name: str, df: pd.DataFrame, cross: str,
               grade: str, score: int, out_dir: str = "charts"):
    """株価+200日線 と MACD の2段チャートPNGを生成。失敗時 None。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        jp = _setup_chart_font()
        os.makedirs(out_dir, exist_ok=True)

        close_full = df["Close"]
        sma200 = close_full.rolling(200).mean()
        macd, signal, hist = compute_macd(close_full)

        sl = slice(-CHART_BARS, None)
        x = close_full.index[sl]
        up = cross == "GOLDEN"
        accent = "#2ecc71" if up else "#e74c3c"

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(8, 5.2), sharex=True,
            gridspec_kw={"height_ratios": [3, 2]})
        fig.patch.set_facecolor("#1e2124")
        for ax in (ax1, ax2):
            ax.set_facecolor("#1e2124")
            ax.tick_params(colors="#cccccc", labelsize=8)
            for sp in ax.spines.values():
                sp.set_color("#555555")
            ax.grid(True, color="#333333", linewidth=0.5)

        ax1.plot(x, close_full[sl], color="#4da3ff", lw=1.4, label="終値" if jp else "Close")
        ax1.plot(x, sma200[sl], color="#f1c40f", lw=1.0, ls="--",
                 label="200日線" if jp else "SMA200")
        ax1.scatter([x[-1]], [close_full.iloc[-1]], color=accent, zorder=5, s=45,
                    marker="^" if up else "v")
        ax1.legend(loc="upper left", fontsize=8, facecolor="#1e2124",
                   labelcolor="#cccccc", edgecolor="#555555")

        colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in hist[sl]]
        ax2.bar(x, hist[sl], color=colors, width=1.0, alpha=0.55)
        ax2.plot(x, macd[sl], color="#4da3ff", lw=1.2, label="MACD")
        ax2.plot(x, signal[sl], color="#ff9f43", lw=1.2, label="Signal")
        ax2.axhline(0, color="#888888", lw=0.8)
        ax2.scatter([x[-1]], [macd.iloc[-1]], color=accent, zorder=5, s=45)
        ax2.legend(loc="upper left", fontsize=8, facecolor="#1e2124",
                   labelcolor="#cccccc", edgecolor="#555555")

        label = "ゴールデンクロス" if up else "デッドクロス"
        title_name = f"{code} {name}" if jp else code
        fig.suptitle(f"{title_name}  {label if jp else cross}  "
                     f"[{grade}] {score}点" if jp else
                     f"{title_name}  {cross}  [{grade}] {score}",
                     color="#ffffff", fontsize=11)
        fig.tight_layout(rect=[0, 0, 1, 0.95])

        path = os.path.join(out_dir, f"{code}.png")
        fig.savefig(path, dpi=110, facecolor=fig.get_facecolor())
        plt.close(fig)
        return path
    except Exception as e:
        log.warning("チャート生成失敗 %s: %s", code, e)
        return None


# ============================================================
# Discord 送信
# ============================================================
def post_discord(webhook: str, content=None, embeds=None, filepaths=None):
    if not webhook:
        log.error("Discord Webhook 未設定")
        return
    payload = {}
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]

    for attempt in range(1, RETRY + 1):
        handles = []
        try:
            if filepaths:
                files = {}
                for i, p in enumerate(filepaths[:10]):
                    fh = open(p, "rb")
                    handles.append(fh)
                    files[f"files[{i}]"] = (os.path.basename(p), fh, "image/png")
                r = requests.post(webhook,
                                  data={"payload_json": json.dumps(payload)},
                                  files=files, timeout=30)
            else:
                r = requests.post(webhook, json=payload, timeout=20)
            if r.status_code in (200, 204):
                return
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            log.warning("Discord %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Discord送信失敗(%d/%d): %s", attempt, RETRY, e)
            time.sleep(2 * attempt)
        finally:
            for fh in handles:
                fh.close()


def send_in_batches(webhook: str, content: str, embeds: list):
    if not embeds:
        post_discord(webhook, content=content)
        return
    first = True
    for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        post_discord(webhook, content=(content if first else None),
                     embeds=embeds[i:i + EMBEDS_PER_MESSAGE])
        first = False
        time.sleep(1.0)


# ============================================================
# 通知の組み立て
# ============================================================
def breadth_bar(pct: float) -> str:
    filled = round(pct / 10)
    return "▰" * filled + "▱" * (10 - filled)


def build_overview_embed(date_str, index_info, bull_pct, n_gc, n_dc):
    if bull_pct is None:
        regime, color = "—", 0x95a5a6
        gauge = "データ不足"
    elif bull_pct >= 65:
        regime, color = "強気（リスクオン）🔥 GCの信頼度が上がる地合い", 0x2ecc71
        gauge = f"{breadth_bar(bull_pct)} {bull_pct:.0f}% 強気"
    elif bull_pct <= 35:
        regime, color = "弱気（リスクオフ）🧊 DCに追随しやすい地合い", 0xe74c3c
        gauge = f"{breadth_bar(bull_pct)} {bull_pct:.0f}% 強気"
    else:
        regime, color = "中立 ⚖️ ダマシが増えやすい地合い", 0xf1c40f
        gauge = f"{breadth_bar(bull_pct)} {bull_pct:.0f}% 強気"

    fields = []
    if index_info:
        trend = "MACD強気 📈" if index_info["bull"] else "MACD弱気 📉"
        fields.append({"name": "日経平均",
                       "value": f"{index_info['close']:,.0f}円"
                                f"（{index_info['chg']:+.2f}%）｜{trend}",
                       "inline": False})
    fields.append({"name": "市場の地合い（MACD強気銘柄の比率）",
                   "value": f"{gauge}\n{regime}", "inline": False})
    fields.append({"name": "本日の検知",
                   "value": f"🟢 ゴールデンクロス **{n_gc}** 銘柄　　"
                            f"🔴 デッドクロス **{n_dc}** 銘柄",
                   "inline": False})
    return {"title": f"📊 マーケット概況 — {date_str}", "color": color,
            "fields": fields,
            "footer": {"text": "スコアは参考指標です。投資判断は自己責任で。"}}


def build_signal_embeds(golden, dead, date_str):
    def stock_field(it):
        ge = GRADE_EMOJI[it["grade"]]
        tags = "・".join(it["tags"]) if it["tags"] else "—"
        return {
            "name": f"{ge} [{it['grade']}] {it['score']}点｜{it['code']} {it['name']}",
            "value": (f"終値 **{it['close']:,.1f}**（{it['chg']:+.2f}%）｜"
                      f"Hist {it['hist']:+.2f}\n"
                      f"{tags}\n"
                      f"[チャートを見る](https://finance.yahoo.co.jp/quote/{it['code']}.T)"),
            "inline": False,
        }

    embeds = []
    for label, color, items in (("🟢 ゴールデンクロス", 0x2ecc71, golden),
                                ("🔴 デッドクロス", 0xe74c3c, dead)):
        if not items:
            continue
        chunks = [items[i:i + STOCKS_PER_EMBED]
                  for i in range(0, len(items), STOCKS_PER_EMBED)]
        for idx, part in enumerate(chunks):
            title = f"{label} {len(items)}銘柄"
            if len(chunks) > 1:
                title += f" [{idx + 1}/{len(chunks)}]"
            embeds.append({"title": title, "color": color,
                           "fields": [stock_field(it) for it in part]})
    if not embeds:
        embeds.append({"title": f"本日のMACDクロス該当なし（{date_str}）",
                       "color": 0x95a5a6,
                       "description": "条件に合致するクロスはありませんでした。"})
    return embeds


def send_top_charts(signals: list, prices: dict, date_str: str):
    if CHART_TOP_N <= 0 or not signals:
        return
    top = sorted(signals, key=lambda x: (x["score"], abs(x["hist"])),
                 reverse=True)[:min(CHART_TOP_N, 4)]
    embeds, paths = [], []
    for it in top:
        df = prices.get(f"{it['code']}.T")
        if df is None:
            continue
        p = make_chart(it["code"], it["name"], df, it["cross"],
                       it["grade"], it["score"])
        if not p:
            continue
        paths.append(p)
        cross_jp = "ゴールデンクロス" if it["cross"] == "GOLDEN" else "デッドクロス"
        embeds.append({
            "title": f"{GRADE_EMOJI[it['grade']]} {it['code']} {it['name']} — {cross_jp}",
            "color": 0x2ecc71 if it["cross"] == "GOLDEN" else 0xe74c3c,
            "image": {"url": f"attachment://{os.path.basename(p)}"},
        })
    if paths:
        post_discord(DISCORD_WEBHOOK_URL,
                     content=f"📈 **注目シグナル チャート**（スコア上位 / {date_str}）",
                     embeds=embeds, filepaths=paths)


# ============================================================
# 運用リマインダー / テストモード
# ============================================================
def send_operational_reminders(today: date, state: dict):
    nxt = next_rebalance_date(today)
    days = (nxt - today).days
    if 0 <= days <= REBALANCE_WARN_DAYS:
        key = f"rebalance:{nxt.isoformat()}"
        reminders = state.setdefault("_reminders", {})
        if key not in reminders:
            post_discord(
                DISCORD_WEBHOOK_URL,
                content=(f"🗓 **日経225 定期見直し予告**\n{nxt:%Y-%m-%d}"
                         f"（4月/10月の第1営業日, あと{days}日）に構成銘柄が"
                         f"入れ替わります。新しいリストで nikkei225.csv を"
                         f"更新してください。"))
            reminders[key] = today.isoformat()


def run_test_mode():
    cons = load_constituents()
    font_ok = _setup_chart_font()
    lines = [
        "🧪 **セットアップテスト OK** — Webhook疎通に成功しました",
        f"・監視銘柄: **{len(cons)}** 件"
        + ("（⚠️サンプル。nikkei225.csvで全225銘柄に拡張可）" if len(cons) < 200 else ""),
        f"・祝日判定(jpholiday): {'✅' if HAS_JPHOLIDAY else '⚠️ 未導入（土日のみ判定）'}",
        f"・チャート日本語フォント: {'✅' if font_ok else '⚠️ 無し（コード表記で出力）'}",
        f"・出来高フィルター: {'ON' if VOLUME_FILTER_ENABLED else 'OFF'} "
        f"(x{VOLUME_CONFIRM_RATIO}) ／ 最低グレード: {MIN_GRADE} ／ "
        f"チャート添付: 上位{CHART_TOP_N}件",
        "本番は平日19:00(JST)に自動実行されます。",
    ]
    post_discord(DISCORD_WEBHOOK_URL, content="\n".join(lines))
    log.info("TEST_MODE 完了")


# ============================================================
# メイン
# ============================================================
def main():
    now = datetime.now(JST)
    today = now.date()
    log.info("=== 実行開始 %s (v2) ===", now.isoformat())

    if TEST_MODE:
        run_test_mode()
        return

    if not is_trading_day(now) and not FORCE_RUN:
        log.info("本日は休場日。スキップします。")
        return

    state = load_state()
    send_operational_reminders(today, state)

    constituents = load_constituents()
    tickers = [f"{code}.T" for code in constituents]
    prices = fetch_prices(tickers)
    log.info("価格取得: %d / %d 銘柄", len(prices), len(tickers))

    failed = len(tickers) - len(prices)
    if failed > len(tickers) * 0.3:
        post_discord(ADMIN_WEBHOOK_URL,
                     content=f"⚠️ MACD監視: 価格取得失敗が多発 ({failed}/{len(tickers)})")

    golden, dead = [], []
    latest_seen = None
    bull_count, breadth_total = 0, 0

    for code, name in constituents.items():
        df = prices.get(f"{code}.T")
        if df is None or df.empty:
            continue
        last_date = df.index[-1].date()
        latest_seen = last_date if latest_seen is None else max(latest_seen, last_date)

        close, volume = df["Close"], df.get("Volume", pd.Series(dtype=float))
        macd, signal, hist_line = compute_macd(close)

        # 地合い（ブレッドス）集計
        breadth_total += 1
        if macd.iloc[-1] > signal.iloc[-1]:
            bull_count += 1

        cross = detect_cross(macd, signal)
        if cross is None:
            continue
        if MIN_HIST_ABS > 0 and abs(hist_line.iloc[-1]) < MIN_HIST_ABS:
            continue

        q = score_signal(cross, macd, signal, hist_line, close, volume)

        # ハードフィルター（出来高 / 売買代金 / 最低グレード）
        if VOLUME_FILTER_ENABLED and not q["vol_ok"]:
            continue
        if MIN_TURNOVER_JPY > 0 and len(volume) and \
           float(volume.iloc[-1]) * float(close.iloc[-1]) < MIN_TURNOVER_JPY:
            continue
        if GRADE_ORDER.get(q["grade"], 0) < GRADE_ORDER.get(MIN_GRADE, 0):
            continue

        key = f"{code}:{cross}:{last_date}"
        if state.get(code, {}).get("last_key") == key:
            continue
        state[code] = {"last_key": key}

        chg = (close.iloc[-1] / close.iloc[-2] - 1) * 100
        rec = {"code": code, "name": name, "cross": cross,
               "close": float(close.iloc[-1]), "chg": float(chg),
               "macd": float(macd.iloc[-1]), "signal": float(signal.iloc[-1]),
               "hist": float(hist_line.iloc[-1]),
               "score": q["score"], "grade": q["grade"], "tags": q["tags"]}
        (golden if cross == "GOLDEN" else dead).append(rec)

    if REQUIRE_TODAY and not FORCE_RUN and latest_seen is not None \
            and latest_seen != today:
        msg = (f"⚠️ 当日({today})の株価がまだ反映されていません"
               f"（最新={latest_seen}）。Actions→Run workflow で "
               f"force=true にして再実行できます。")
        log.warning(msg)
        post_discord(ADMIN_WEBHOOK_URL, content=msg)
        save_state(state)
        return

    golden.sort(key=lambda x: (x["score"], abs(x["hist"])), reverse=True)
    dead.sort(key=lambda x: (x["score"], abs(x["hist"])), reverse=True)
    log.info("ゴールデン %d / デッド %d", len(golden), len(dead))

    bull_pct = (bull_count / breadth_total * 100) if breadth_total else None
    index_info = fetch_index()
    date_str = (latest_seen or today).strftime("%Y-%m-%d (%a)")

    embeds = [build_overview_embed(date_str, index_info, bull_pct,
                                   len(golden), len(dead))]
    embeds += build_signal_embeds(golden, dead, date_str)
    send_in_batches(DISCORD_WEBHOOK_URL,
                    f"📊 **日経225 MACD(8,17,9) デイリーレポート** — {date_str}",
                    embeds)

    send_top_charts(golden + dead, prices, date_str)
    save_state(state)
    log.info("=== 完了 ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("致命的エラー: %s", e)
        if ADMIN_WEBHOOK_URL:
            post_discord(ADMIN_WEBHOOK_URL, content=f"❌ MACD監視が異常終了: {e}")
        sys.exit(1)
