import os
import time
import json
import logging
import asyncio
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TWELVE_DATA_API_KEY = "ca1acbf0cedb4488b130c59252891c5e"
TELEGRAM_BOT_TOKEN = "8662325806:AAEj9zw70aEEJX52tlaUkutu7jQkZ3gbNdE"
TELEGRAM_CHAT_ID = "1126071150"
SYMBOL = "XAU/USD"
TIMEFRAMES = {"4h": "4h", "1h": "1h", "15min": "15min"}
OUTPUT_SIZE = 300
RISK_PER_TRADE = 0.01
MIN_RR = 2.0
MAX_DAILY_TRADES = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TwelveDataFeed:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.twelvedata.com"

    def fetch_ohlcv(self, symbol, interval, outputsize=300):
        url = f"{self.base_url}/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": self.api_key,
            "order": "asc",
            "format": "JSON"
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "values" not in data:
                logger.error(f"No values in response: {data}")
                return pd.DataFrame()
            df = pd.DataFrame(data["values"])
            df["datetime"] = pd.to_datetime(df["datetime"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna()
            df = df.sort_values("datetime").reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"Fetch error: {e}")
            return pd.DataFrame()

class IndicatorEngine:
    def __init__(self):
        pass

    def ema(self, series, period):
        return series.ewm(span=period, adjust=False).mean()

    def sma(self, series, period):
        return series.rolling(window=period).mean()

    def rsi(self, series, period=14):
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1.0/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0/period, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    def macd(self, series, fast=12, slow=26, signal=9):
        ema_fast = self.ema(series, fast)
        ema_slow = self.ema(series, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self.ema(macd_line, signal)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def atr(self, df, period=14):
        high_low = df["high"] - df["low"]
        high_close = np.abs(df["high"] - df["close"].shift())
        low_close = np.abs(df["low"] - df["close"].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def bollinger_bands(self, series, period=20, std_dev=2):
        sma = self.sma(series, period)
        std = series.rolling(window=period).std()
        upper = sma + (std * std_dev)
        lower = sma - (std * std_dev)
        return upper, sma, lower

    def stochastic(self, df, k_period=14, d_period=3):
        lowest_low = df["low"].rolling(window=k_period).min()
        highest_high = df["high"].rolling(window=k_period).max()
        k = 100.0 * ((df["close"] - lowest_low) / (highest_high - lowest_low))
        d = self.sma(k, d_period)
        return k, d

    def adx(self, df, period=14):
        plus_dm = df["high"].diff()
        minus_dm = (-df["low"]).diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        tr = pd.concat([
            df["high"] - df["low"],
            np.abs(df["high"] - df["close"].shift()),
            np.abs(df["low"] - df["close"].shift())
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100.0 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
        minus_di = 100.0 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.ewm(span=period, adjust=False).mean()
        return adx, plus_di, minus_di

    def vwap(self, df):
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
        return vwap

    def apply_all(self, df):
        if len(df) < 50:
            return df
        df["ema9"] = self.ema(df["close"], 9)
        df["ema20"] = self.ema(df["close"], 20)
        df["ema50"] = self.ema(df["close"], 50)
        df["ema200"] = self.ema(df["close"], 200)
        df["rsi"] = self.rsi(df["close"], 14)
        df["macd_line"], df["macd_signal"], df["macd_hist"] = self.macd(df["close"], 12, 26, 9)
        df["atr"] = self.atr(df, 14)
        df["bb_upper"], df["bb_mid"], df["bb_lower"] = self.bollinger_bands(df["close"], 20, 2)
        df["stoch_k"], df["stoch_d"] = self.stochastic(df, 14, 3)
        df["adx"], df["plus_di"], df["minus_di"] = self.adx(df, 14)
        df["vwap"] = self.vwap(df)
        return df

class MarketStructureEngine:
    def __init__(self):
        self.swing_lookback = 5

    def detect_swings(self, df):
        highs = df["high"].values
        lows = df["low"].values
        swing_highs = []
        swing_lows = []
        for i in range(self.swing_lookback, len(df) - self.swing_lookback):
            if all(highs[i] > highs[i - j] for j in range(1, self.swing_lookback + 1)) and                all(highs[i] > highs[i + j] for j in range(1, self.swing_lookback + 1)):
                swing_highs.append((i, highs[i]))
            if all(lows[i] < lows[i - j] for j in range(1, self.swing_lookback + 1)) and                all(lows[i] < lows[i + j] for j in range(1, self.swing_lookback + 1)):
                swing_lows.append((i, lows[i]))
        return swing_highs, swing_lows

    def detect_bos(self, df, swing_highs, swing_lows):
        bos_events = []
        closes = df["close"].values
        if len(swing_highs) >= 2:
            for i in range(1, len(swing_highs)):
                prev_idx, prev_high = swing_highs[i - 1]
                curr_idx, curr_high = swing_highs[i]
                if closes[curr_idx] > prev_high:
                    bos_events.append((curr_idx, "bullish_bos", prev_high))
        if len(swing_lows) >= 2:
            for i in range(1, len(swing_lows)):
                prev_idx, prev_low = swing_lows[i - 1]
                curr_idx, curr_low = swing_lows[i]
                if closes[curr_idx] < prev_low:
                    bos_events.append((curr_idx, "bearish_bos", prev_low))
        return bos_events

    def detect_choch(self, df, swing_highs, swing_lows):
        choch_events = []
        closes = df["close"].values
        if len(swing_lows) >= 2:
            for i in range(1, len(swing_lows)):
                prev_idx, prev_low = swing_lows[i - 1]
                curr_idx, curr_low = swing_lows[i]
                if closes[curr_idx] > prev_low:
                    choch_events.append((curr_idx, "bullish_choch", prev_low))
        if len(swing_highs) >= 2:
            for i in range(1, len(swing_highs)):
                prev_idx, prev_high = swing_highs[i - 1]
                curr_idx, curr_high = swing_highs[i]
                if closes[curr_idx] < prev_high:
                    choch_events.append((curr_idx, "bearish_choch", prev_high))
        return choch_events

    def classify_trend(self, df, swing_highs, swing_lows):
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "range"
        last_highs = [h for _, h in swing_highs[-3:]]
        last_lows = [l for _, l in swing_lows[-3:]]
        if len(last_highs) >= 2 and len(last_lows) >= 2:
            higher_highs = all(last_highs[i] > last_highs[i - 1] for i in range(1, len(last_highs)))
            higher_lows = all(last_lows[i] > last_lows[i - 1] for i in range(1, len(last_lows)))
            lower_highs = all(last_highs[i] < last_highs[i - 1] for i in range(1, len(last_highs)))
            lower_lows = all(last_lows[i] < last_lows[i - 1] for i in range(1, len(last_lows)))
            if higher_highs and higher_lows:
                return "bullish"
            if lower_highs and lower_lows:
                return "bearish"
        return "range"

class LiquidityEngine:
    def __init__(self):
        self.cluster_threshold = 0.001

    def detect_equal_levels(self, df, lookback=20):
        highs = df["high"].tail(lookback).values
        lows = df["low"].tail(lookback).values
        eq_highs = []
        eq_lows = []
        for i in range(len(highs)):
            for j in range(i + 1, len(highs)):
                if abs(highs[i] - highs[j]) / highs[i] < self.cluster_threshold:
                    eq_highs.append((highs[i] + highs[j]) / 2)
        for i in range(len(lows)):
            for j in range(i + 1, len(lows)):
                if abs(lows[i] - lows[j]) / lows[i] < self.cluster_threshold:
                    eq_lows.append((lows[i] + lows[j]) / 2)
        return list(set(eq_highs)), list(set(eq_lows))

    def detect_sweeps(self, df, eq_highs, eq_lows):
        last_high = df["high"].iloc[-1]
        last_low = df["low"].iloc[-1]
        swept_highs = [h for h in eq_highs if last_high >= h]
        swept_lows = [l for l in eq_lows if last_low <= l]
        return swept_highs, swept_lows

    def cluster_zones(self, df, levels, zone_size=0.002):
        if not levels:
            return []
        levels = sorted(levels)
        zones = []
        current_zone = [levels[0]]
        for i in range(1, len(levels)):
            if abs(levels[i] - current_zone[-1]) / current_zone[-1] < zone_size:
                current_zone.append(levels[i])
            else:
                zones.append(sum(current_zone) / len(current_zone))
                current_zone = [levels[i]]
        zones.append(sum(current_zone) / len(current_zone))
        return zones

    def get_liquidity_status(self, df):
        eq_highs, eq_lows = self.detect_equal_levels(df)
        swept_highs, swept_lows = self.detect_sweeps(df, eq_highs, eq_lows)
        total_levels = len(eq_highs) + len(eq_lows)
        swept_levels = len(swept_highs) + len(swept_lows)
        if total_levels == 0:
            return "stable"
        ratio = swept_levels / total_levels
        if ratio > 0.6:
            return "high"
        if ratio < 0.2:
            return "low"
        return "stable"

class SessionEngine:
    def __init__(self):
        self.sessions = {
            "asian": (0, 8),
            "london": (8, 16),
            "ny": (13, 21)
        }
        self.kill_zones = {
            "london_open": (8, 10),
            "ny_open": (13, 15)
        }

    def get_current_session(self):
        now = datetime.now(timezone.utc)
        hour = now.hour
        for name, (start, end) in self.sessions.items():
            if start <= hour < end:
                return name
        return "asian"

    def is_kill_zone(self):
        now = datetime.now(timezone.utc)
        hour = now.hour
        for name, (start, end) in self.kill_zones.items():
            if start <= hour < end:
                return True, name
        return False, ""

    def is_valid_trading_time(self):
        session = self.get_current_session()
        return session in ["london", "ny"]

class VolumeEngine:
    def __init__(self):
        self.spike_threshold = 2.0

    def analyze_volume(self, df):
        if len(df) < 20:
            return False, 0.0
        vol = df["volume"].values
        avg_vol = np.mean(vol[-20:])
        current_vol = vol[-1]
        if avg_vol == 0:
            return False, 0.0
        ratio = current_vol / avg_vol
        is_spike = ratio > self.spike_threshold
        return is_spike, ratio

    def displacement_strength(self, df):
        if len(df) < 2:
            return 0.0
        last_candle = df.iloc[-1]
        body = abs(last_candle["close"] - last_candle["open"])
        range_val = last_candle["high"] - last_candle["low"]
        if range_val == 0:
            return 0.0
        return body / range_val

    def confirm_momentum(self, df):
        is_spike, ratio = self.analyze_volume(df)
        strength = self.displacement_strength(df)
        return is_spike and strength > 0.6, ratio, strength

class TrendConfirmationEngine:
    def __init__(self, indicators):
        self.indicators = indicators

    def confirm_trend(self, df):
        if len(df) < 50 or "ema50" not in df.columns:
            return "neutral", 0.0
        last = df.iloc[-1]
        close = last["close"]
        ema9 = last["ema9"]
        ema20 = last["ema20"]
        ema50 = last["ema50"]
        ema200 = last["ema200"]
        rsi = last["rsi"]
        macd_line = last["macd_line"]
        macd_signal = last["macd_signal"]
        macd_hist = last["macd_hist"]
        adx = last["adx"]
        plus_di = last["plus_di"]
        minus_di = last["minus_di"]
        stoch_k = last["stoch_k"]
        stoch_d = last["stoch_d"]
        bb_upper = last["bb_upper"]
        bb_lower = last["bb_lower"]
        vwap = last["vwap"]
        atr = last["atr"]
        score = 0.0
        max_score = 0.0
        if close > ema50 and ema50 > ema200:
            score += 2.0
        elif close < ema50 and ema50 < ema200:
            score -= 2.0
        max_score += 2.0
        if ema9 > ema20:
            score += 1.0
        else:
            score -= 1.0
        max_score += 1.0
        if rsi > 50 and rsi < 80:
            score += 1.0
        elif rsi < 50 and rsi > 20:
            score -= 1.0
        max_score += 1.0
        if macd_line > macd_signal and macd_hist > 0:
            score += 1.5
        elif macd_line < macd_signal and macd_hist < 0:
            score -= 1.5
        max_score += 1.5
        if adx > 25:
            if plus_di > minus_di:
                score += 1.0
            else:
                score -= 1.0
        max_score += 1.0
        if close > vwap:
            score += 0.5
        else:
            score -= 0.5
        max_score += 0.5
        if close > bb_mid:
            score += 0.5
        else:
            score -= 0.5
        max_score += 0.5
        if stoch_k > stoch_d and stoch_k < 80:
            score += 0.5
        elif stoch_k < stoch_d and stoch_k > 20:
            score -= 0.5
        max_score += 0.5
        confidence = abs(score) / max_score if max_score > 0 else 0.0
        if score >= 2.0:
            return "bullish", confidence
        if score <= -2.0:
            return "bearish", confidence
        return "neutral", confidence

    def is_trending(self, df):
        if len(df) < 50 or "adx" not in df.columns:
            return False
        return df["adx"].iloc[-1] > 25

    def is_volatile(self, df):
        if len(df) < 20 or "atr" not in df.columns:
            return False
        atr = df["atr"].iloc[-1]
        close = df["close"].iloc[-1]
        return (atr / close) > 0.001

    def is_ranging(self, df):
        if len(df) < 50 or "adx" not in df.columns:
            return True
        return df["adx"].iloc[-1] < 20

class EntryEngine:
    def __init__(self, structure, liquidity, volume, session, trend_conf):
        self.structure = structure
        self.liquidity = liquidity
        self.volume = volume
        self.session = session
        self.trend_conf = trend_conf

    def find_fvg(self, df):
        fvgs = []
        for i in range(2, len(df)):
            c1 = df.iloc[i - 2]
            c2 = df.iloc[i - 1]
            c3 = df.iloc[i]
            if c2["low"] > c1["high"]:
                fvgs.append((i, "bullish", c1["high"], c2["low"]))
            if c2["high"] < c1["low"]:
                fvgs.append((i, "bearish", c2["high"], c1["low"]))
        return fvgs

    def find_order_blocks(self, df):
        obs = []
        for i in range(1, len(df) - 1):
            prev = df.iloc[i - 1]
            curr = df.iloc[i]
            next_c = df.iloc[i + 1]
            if curr["close"] > curr["open"] and next_c["low"] > curr["low"] and next_c["high"] > curr["high"]:
                obs.append((i, "bullish", curr["low"], curr["high"]))
            if curr["close"] < curr["open"] and next_c["high"] < curr["high"] and next_c["low"] < curr["low"]:
                obs.append((i, "bearish", curr["low"], curr["high"]))
        return obs

    def check_entry_conditions(self, htf_df, ltf_df, bias):
        if not self.session.is_valid_trading_time():
            return None
        if len(htf_df) < 50 or len(ltf_df) < 50:
            return None
        trend_bias, trend_confidence = self.trend_conf.confirm_trend(htf_df)
        if trend_bias == "neutral" or trend_confidence < 0.5:
            return None
        if trend_bias != bias:
            return None
        swing_highs, swing_lows = self.structure.detect_swings(htf_df)
        structure_trend = self.structure.classify_trend(htf_df, swing_highs, swing_lows)
        if structure_trend != bias and structure_trend != "range":
            return None
        eq_highs, eq_lows = self.liquidity.detect_equal_levels(ltf_df)
        swept_highs, swept_lows = self.liquidity.detect_sweeps(ltf_df, eq_highs, eq_lows)
        momentum_ok, vol_ratio, strength = self.volume.confirm_momentum(ltf_df)
        if not momentum_ok:
            return None
        last_close = ltf_df["close"].iloc[-1]
        if bias == "bullish" and swept_lows:
            fvgs = self.find_fvg(ltf_df)
            obs = self.find_order_blocks(ltf_df)
            for fvg in fvgs:
                if fvg[1] == "bullish" and fvg[2] <= last_close <= fvg[3]:
                    return {
                        "direction": "BUY",
                        "entry": last_close,
                        "sweep_level": min(swept_lows),
                        "setup": "sweep+FVG",
                        "vol_ratio": vol_ratio,
                        "strength": strength,
                        "trend_confidence": trend_confidence
                    }
            for ob in obs:
                if ob[1] == "bullish" and ob[2] <= last_close <= ob[3]:
                    return {
                        "direction": "BUY",
                        "entry": last_close,
                        "sweep_level": min(swept_lows),
                        "setup": "sweep+OB",
                        "vol_ratio": vol_ratio,
                        "strength": strength,
                        "trend_confidence": trend_confidence
                    }
        if bias == "bearish" and swept_highs:
            fvgs = self.find_fvg(ltf_df)
            obs = self.find_order_blocks(ltf_df)
            for fvg in fvgs:
                if fvg[1] == "bearish" and fvg[2] <= last_close <= fvg[3]:
                    return {
                        "direction": "SELL",
                        "entry": last_close,
                        "sweep_level": max(swept_highs),
                        "setup": "sweep+FVG",
                        "vol_ratio": vol_ratio,
                        "strength": strength,
                        "trend_confidence": trend_confidence
                    }
            for ob in obs:
                if ob[1] == "bearish" and ob[2] <= last_close <= ob[3]:
                    return {
                        "direction": "SELL",
                        "entry": last_close,
                        "sweep_level": max(swept_highs),
                        "setup": "sweep+OB",
                        "vol_ratio": vol_ratio,
                        "strength": strength,
                        "trend_confidence": trend_confidence
                    }
        return None

class RiskEngine:
    def __init__(self):
        self.risk_percent = RISK_PER_TRADE
        self.min_rr = MIN_RR

    def calculate_sl_tp(self, entry, direction, structure_level, liquidity_targets, atr_value):
        if direction == "BUY":
            sl = entry - (atr_value * 1.5)
            if sl >= entry:
                sl = structure_level * 0.998
            tp_candidates = [t for t in liquidity_targets if t > entry]
            if not tp_candidates:
                tp = entry + (abs(entry - sl) * self.min_rr)
            else:
                tp = min(tp_candidates)
        else:
            sl = entry + (atr_value * 1.5)
            if sl <= entry:
                sl = structure_level * 1.002
            tp_candidates = [t for t in liquidity_targets if t < entry]
            if not tp_candidates:
                tp = entry - (abs(entry - sl) * self.min_rr)
            else:
                tp = max(tp_candidates)
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return None, None
        rr = reward / risk
        if rr < self.min_rr:
            return None, None
        return sl, tp

    def calculate_position_size(self, account_balance, entry, sl):
        risk_amount = account_balance * self.risk_percent
        points_risk = abs(entry - sl)
        if points_risk == 0:
            return 0
        lot_size = risk_amount / points_risk
        return round(lot_size, 2)

class SignalValidator:
    def __init__(self, structure, liquidity, volume, session, trend_conf):
        self.structure = structure
        self.liquidity = liquidity
        self.volume = volume
        self.session = session
        self.trend_conf = trend_conf

    def validate(self, htf_df, ltf_df, entry_data, bias):
        if not self.session.is_valid_trading_time():
            return False, "invalid_session"
        if len(htf_df) < 50 or len(ltf_df) < 50:
            return False, "insufficient_data"
        trend_bias, trend_confidence = self.trend_conf.confirm_trend(htf_df)
        if trend_bias == "neutral" or trend_confidence < 0.5:
            return False, "weak_trend"
        if trend_bias != bias:
            return False, "trend_mismatch"
        swing_highs, swing_lows = self.structure.detect_swings(htf_df)
        structure_trend = self.structure.classify_trend(htf_df, swing_highs, swing_lows)
        if structure_trend != bias and structure_trend != "range":
            return False, "structure_mismatch"
        eq_highs, eq_lows = self.liquidity.detect_equal_levels(ltf_df)
        swept_highs, swept_lows = self.liquidity.detect_sweeps(ltf_df, eq_highs, eq_lows)
        if entry_data["direction"] == "BUY" and not swept_lows:
            return False, "no_sweep"
        if entry_data["direction"] == "SELL" and not swept_highs:
            return False, "no_sweep"
        momentum_ok, _, _ = self.volume.confirm_momentum(ltf_df)
        if not momentum_ok:
            return False, "no_momentum"
        if self.trend_conf.is_ranging(htf_df):
            return False, "ranging_market"
        return True, "valid"

class TelegramBot:
    def __init__(self, token, chat_id, data_feed, structure, liquidity, volume, session, entry, risk, validator, indicators, trend_conf):
        self.token = token
        self.chat_id = chat_id
        self.data_feed = data_feed
        self.structure = structure
        self.liquidity = liquidity
        self.volume = volume
        self.session = session
        self.entry = entry
        self.risk = risk
        self.validator = validator
        self.indicators = indicators
        self.trend_conf = trend_conf
        self.daily_trades = 0
        self.last_trade_date = datetime.now(timezone.utc).date()
        self.active_signals = []
        self.trade_history = []
        self.running = False
        self.app = None

    async def send_message(self, text):
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            await asyncio.to_thread(requests.post, url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("XAUUSD Quantum Sniper AI Bot started. Use /status, /liquidity, /signal, /indicators.")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = self.session.get_current_session()
        is_kz, kz_name = self.session.is_kill_zone()
        status_text = f"Session: {session}\nKill Zone: {is_kz} ({kz_name})\nDaily Trades: {self.daily_trades}/{MAX_DAILY_TRADES}"
        await update.message.reply_text(status_text)

    async def liquidity_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        df = self.data_feed.fetch_ohlcv(SYMBOL, TIMEFRAMES["15min"], 100)
        if df.empty:
            await update.message.reply_text("Data unavailable. Try again.")
            return
        status = self.liquidity.get_liquidity_status(df)
        eq_highs, eq_lows = self.liquidity.detect_equal_levels(df)
        swept_highs, swept_lows = self.liquidity.detect_sweeps(df, eq_highs, eq_lows)
        msg = f"Liquidity Status: {status.upper()}\n"
        msg += f"Equal Highs: {len(eq_highs)}\n"
        msg += f"Equal Lows: {len(eq_lows)}\n"
        msg += f"Swept Highs: {len(swept_highs)}\n"
        msg += f"Swept Lows: {len(swept_lows)}"
        await update.message.reply_text(msg)

    async def indicators_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        df = self.data_feed.fetch_ohlcv(SYMBOL, TIMEFRAMES["4h"], OUTPUT_SIZE)
        if df.empty or len(df) < 50:
            await update.message.reply_text("Data unavailable. Try again.")
            return
        df = self.indicators.apply_all(df)
        last = df.iloc[-1]
        trend_bias, confidence = self.trend_conf.confirm_trend(df)
        is_trending = self.trend_conf.is_trending(df)
        is_ranging = self.trend_conf.is_ranging(df)
        is_volatile = self.trend_conf.is_volatile(df)
        msg = f"*XAUUSD INDICATORS*\n\n"
        msg += f"Trend Bias: {trend_bias.upper()}\n"
        msg += f"Confidence: {confidence:.1%}\n"
        msg += f"Trending: {is_trending}\n"
        msg += f"Ranging: {is_ranging}\n"
        msg += f"Volatile: {is_volatile}\n\n"
        msg += f"EMA9: {last['ema9']:.2f}\n"
        msg += f"EMA20: {last['ema20']:.2f}\n"
        msg += f"EMA50: {last['ema50']:.2f}\n"
        msg += f"EMA200: {last['ema200']:.2f}\n"
        msg += f"RSI: {last['rsi']:.1f}\n"
        msg += f"MACD: {last['macd_line']:.4f}\n"
        msg += f"MACD Signal: {last['macd_signal']:.4f}\n"
        msg += f"MACD Hist: {last['macd_hist']:.4f}\n"
        msg += f"ADX: {last['adx']:.1f}\n"
        msg += f"+DI: {last['plus_di']:.1f}\n"
        msg += f"-DI: {last['minus_di']:.1f}\n"
        msg += f"ATR: {last['atr']:.2f}\n"
        msg += f"VWAP: {last['vwap']:.2f}\n"
        msg += f"Stoch K: {last['stoch_k']:.1f}\n"
        msg += f"Stoch D: {last['stoch_d']:.1f}\n"
        msg += f"BB Upper: {last['bb_upper']:.2f}\n"
        msg += f"BB Lower: {last['bb_lower']:.2f}"
        await update.message.reply_text(msg)

    async def signal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        signal = await self.generate_signal()
        if signal:
            await update.message.reply_text(signal)
        else:
            await update.message.reply_text("No valid signal at this time.")

    async def generate_signal(self):
        htf_df = self.data_feed.fetch_ohlcv(SYMBOL, TIMEFRAMES["4h"], OUTPUT_SIZE)
        ltf_df = self.data_feed.fetch_ohlcv(SYMBOL, TIMEFRAMES["15min"], OUTPUT_SIZE)
        if htf_df.empty or ltf_df.empty:
            return None
        if len(htf_df) < 50 or len(ltf_df) < 50:
            return None
        htf_df = self.indicators.apply_all(htf_df)
        ltf_df = self.indicators.apply_all(ltf_df)
        trend_bias, trend_confidence = self.trend_conf.confirm_trend(htf_df)
        if trend_bias == "neutral" or trend_confidence < 0.5:
            return None
        bias = trend_bias
        entry_data = self.entry.check_entry_conditions(htf_df, ltf_df, bias)
        if not entry_data:
            return None
        valid, reason = self.validator.validate(htf_df, ltf_df, entry_data, bias)
        if not valid:
            return None
        eq_highs, eq_lows = self.liquidity.detect_equal_levels(ltf_df)
        all_levels = eq_highs + eq_lows
        zones = self.liquidity.cluster_zones(ltf_df, all_levels)
        atr_value = htf_df["atr"].iloc[-1] if "atr" in htf_df.columns else 5.0
        sl, tp = self.risk.calculate_sl_tp(
            entry_data["entry"],
            entry_data["direction"],
            entry_data["sweep_level"],
            zones,
            atr_value
        )
        if sl is None or tp is None:
            return None
        today = datetime.now(timezone.utc).date()
        if today != self.last_trade_date:
            self.daily_trades = 0
            self.last_trade_date = today
        if self.daily_trades >= MAX_DAILY_TRADES:
            return None
        self.daily_trades += 1
        session = self.session.get_current_session()
        is_kz, kz_name = self.session.is_kill_zone()
        signal_text = f"*XAUUSD SIGNAL*\n\n"
        signal_text += f"Direction: {entry_data['direction']}\n"
        signal_text += f"Entry: {entry_data['entry']:.2f}\n"
        signal_text += f"Stop Loss: {sl:.2f}\n"
        signal_text += f"Take Profit: {tp:.2f}\n"
        signal_text += f"Session: {session}"
        if is_kz:
            signal_text += f" (Kill Zone: {kz_name})"
        signal_text += f"\nBias: {bias.upper()}\n"
        signal_text += f"Setup: {entry_data['setup']}\n"
        signal_text += f"Volume Ratio: {entry_data['vol_ratio']:.2f}\n"
        signal_text += f"Strength: {entry_data['strength']:.2f}\n"
        signal_text += f"Trend Confidence: {entry_data['trend_confidence']:.1%}\n"
        signal_text += f"Confidence: HIGH"
        self.active_signals.append({
            "time": datetime.now(timezone.utc),
            "direction": entry_data["direction"],
            "entry": entry_data["entry"],
            "sl": sl,
            "tp": tp,
            "bias": bias,
            "setup": entry_data["setup"]
        })
        return signal_text

    async def monitor_loop(self):
        while self.running:
            try:
                signal = await self.generate_signal()
                if signal:
                    await self.send_message(signal)
                await asyncio.sleep(300)
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(60)

    def setup_handlers(self):
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("liquidity", self.liquidity_command))
        self.app.add_handler(CommandHandler("indicators", self.indicators_command))
        self.app.add_handler(CommandHandler("signal", self.signal_command))

    async def run(self):
        if not self.token:
            logger.error("Telegram token missing")
            return
        self.setup_handlers()
        self.running = True
        monitor_task = asyncio.create_task(self.monitor_loop())
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot running")
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            monitor_task.cancel()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

class PerformanceTracker:
    def __init__(self):
        self.trades = []

    def log_trade(self, signal, outcome):
        self.trades.append({
            "time": signal["time"],
            "direction": signal["direction"],
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp": signal["tp"],
            "outcome": outcome
        })

    def get_stats(self):
        if not self.trades:
            return {}
        wins = sum(1 for t in self.trades if t["outcome"] == "win")
        total = len(self.trades)
        win_rate = wins / total if total > 0 else 0
        rr_values = []
        for t in self.trades:
            if t["outcome"] == "win":
                rr = abs(t["tp"] - t["entry"]) / abs(t["entry"] - t["sl"])
                rr_values.append(rr)
        avg_rr = np.mean(rr_values) if rr_values else 0
        return {
            "total_trades": total,
            "win_rate": win_rate,
            "avg_rr": avg_rr
        }

class AdaptiveLayer:
    def __init__(self, tracker):
        self.tracker = tracker
        self.session_performance = {}
        self.setup_performance = {}

    def adapt(self):
        stats = self.tracker.get_stats()
        if stats.get("total_trades", 0) < 10:
            return
        for trade in self.tracker.trades:
            session = trade.get("session", "unknown")
            setup = trade.get("setup", "unknown")
            outcome = trade["outcome"]
            if session not in self.session_performance:
                self.session_performance[session] = {"wins": 0, "total": 0}
            self.session_performance[session]["total"] += 1
            if outcome == "win":
                self.session_performance[session]["wins"] += 1
            if setup not in self.setup_performance:
                self.setup_performance[setup] = {"wins": 0, "total": 0}
            self.setup_performance[setup]["total"] += 1
            if outcome == "win":
                self.setup_performance[setup]["wins"] += 1

async def main():
    data_feed = TwelveDataFeed(TWELVE_DATA_API_KEY)
    structure = MarketStructureEngine()
    liquidity = LiquidityEngine()
    volume = VolumeEngine()
    session = SessionEngine()
    indicators = IndicatorEngine()
    trend_conf = TrendConfirmationEngine(indicators)
    entry = EntryEngine(structure, liquidity, volume, session, trend_conf)
    risk = RiskEngine()
    validator = SignalValidator(structure, liquidity, volume, session, trend_conf)
    bot = TelegramBot(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        data_feed,
        structure,
        liquidity,
        volume,
        session,
        entry,
        risk,
        validator,
        indicators,
        trend_conf
    )
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
