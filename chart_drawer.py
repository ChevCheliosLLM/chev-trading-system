"""
title: Live Chart
author: Kev
requirements: requests, pandas, mplfinance, matplotlib, numpy
"""

import requests
import pandas as pd
import numpy as np
import mplfinance as mpf
import io
import base64
import matplotlib.transforms as transforms


class Tools:
    def __init__(self):
        pass

    def _fetch_candles(self, symbol: str, timeframe: str, limit: int = 300):
        clean_symbol = symbol.upper().replace("BINANCE:", "").replace(":", "")
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": clean_symbol, "interval": timeframe, "limit": limit}
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        df = pd.DataFrame(
            data,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "qav",
                "trades",
                "tb_base",
                "tb_quote",
                "ignore",
            ],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df.set_index("open_time", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def _add_indicators(self, df):
        df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        df["VWAP"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        df["RSI"] = 100 - (100 / (1 + rs))
        return df

    def _candles_ago_to_ts(self, df, candles_ago):
        idx = len(df) - 1 - int(candles_ago)
        idx = max(0, min(len(df) - 1, idx))
        return df.index[idx]

    def _get_timed_touches(self, df, window=3, threshold_pct=0.3, confirm_window=8):
        highs = df["high"].values
        lows = df["low"].values
        n = len(df)
        touches = []
        for i in range(window, n - window):
            if highs[i] == max(highs[i - window : i + window + 1]):
                future_lows = lows[i + 1 : min(i + 1 + confirm_window, n)]
                if (
                    len(future_lows) > 0
                    and (highs[i] - future_lows.min()) / highs[i] * 100 >= threshold_pct
                ):
                    touches.append((df.index[i], highs[i], "resistance"))
            if lows[i] == min(lows[i - window : i + window + 1]):
                future_highs = highs[i + 1 : min(i + 1 + confirm_window, n)]
                if (
                    len(future_highs) > 0
                    and (future_highs.max() - lows[i]) / lows[i] * 100 >= threshold_pct
                ):
                    touches.append((df.index[i], lows[i], "support"))
        return touches

    def _find_last_impulse(self, touches):
        """
        Finds the most recent genuine swing-to-swing move: takes the very last
        confirmed swing point, then walks backward to find the most recent
        swing point of the OPPOSITE kind - those two together are the real
        start and end of the current move, not just a blind min/max.
        """
        if len(touches) < 2:
            return None
        touches_sorted = sorted(touches, key=lambda t: t[0])
        last_ts, last_price, last_kind = touches_sorted[-1]
        for ts, price, kind in reversed(touches_sorted[:-1]):
            if kind != last_kind:
                return (ts, price, last_ts, last_price)
        return None

    def _build_validated_levels(
        self, touches_by_tf, kind, tolerance_pct=0.6, episode_gap_hours=12
    ):
        min_touches = {"15m": 5, "30m": 3, "1h": 2, "4h": 2}
        all_prices = [p for tf in touches_by_tf for (_, p) in touches_by_tf[tf]]
        if not all_prices:
            return []
        sorted_prices = sorted(all_prices)
        zones, current = [], [sorted_prices[0]]
        for p in sorted_prices[1:]:
            if abs(p - current[-1]) / current[-1] * 100 <= tolerance_pct:
                current.append(p)
            else:
                zones.append(current)
                current = [p]
        zones.append(current)
        zone_centers = [sum(z) / len(z) for z in zones]

        results = []
        for center in zone_centers:
            zone_lo = center * (1 - tolerance_pct / 100)
            zone_hi = center * (1 + tolerance_pct / 100)
            combined = []
            for tf, touches in touches_by_tf.items():
                for ts, p in touches:
                    if zone_lo <= p <= zone_hi:
                        combined.append((ts, tf))
            if not combined:
                continue
            combined.sort(key=lambda x: x[0])
            episodes, current_episode = [], [combined[0]]
            for item in combined[1:]:
                gap = (item[0] - current_episode[-1][0]).total_seconds() / 3600
                if gap > episode_gap_hours:
                    episodes.append(current_episode)
                    current_episode = [item]
                else:
                    current_episode.append(item)
            episodes.append(current_episode)

            validated_count = 0
            for ep in episodes:
                counts = {"15m": 0, "30m": 0, "1h": 0, "4h": 0}
                for ts, tf in ep:
                    counts[tf] += 1
                if all(counts[tf] >= min_touches[tf] for tf in min_touches):
                    validated_count += 1
            if validated_count >= 1:
                results.append(
                    {
                        "price": round(center, 4),
                        "instances": validated_count,
                        "kind": kind,
                    }
                )
        return sorted(results, key=lambda r: r["instances"], reverse=True)

    def _build_single_tf_levels(self, touches, min_touches=3, tolerance_pct=0.5):
        prices = [p for _, p in touches]
        if not prices:
            return []
        prices_sorted = sorted(prices)
        clusters, current = [], [prices_sorted[0]]
        for p in prices_sorted[1:]:
            if abs(p - current[-1]) / current[-1] * 100 <= tolerance_pct:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)
        results = []
        for c in clusters:
            if len(c) >= min_touches:
                results.append({"price": round(sum(c) / len(c), 4), "touches": len(c)})
        return results

    def _calculate_volume_profile(self, df, bin_count=75):
        """
        Real Volume Profile: slices the price range into bins, distributes each
        candle's volume across the bins its range touches, finds POC (highest-volume
        bin), then greedily expands outward - always adding whichever neighboring
        bin (above or below) has more volume - until 70% of total volume is captured.
        VAH/VAL come from that expansion, not a fixed formula.
        """
        price_min = df["low"].min()
        price_max = df["high"].max()
        if price_max == price_min:
            return None

        bin_width = (price_max - price_min) / bin_count
        bin_volumes = [0.0] * bin_count

        for _, row in df.iterrows():
            low, high, vol = row["low"], row["high"], row["volume"]
            if high == low:
                idx = min(int((low - price_min) / bin_width), bin_count - 1)
                bin_volumes[idx] += vol
                continue
            start_idx = max(0, min(int((low - price_min) / bin_width), bin_count - 1))
            end_idx = max(0, min(int((high - price_min) / bin_width), bin_count - 1))
            span = end_idx - start_idx + 1
            vol_per_bin = vol / span
            for i in range(start_idx, end_idx + 1):
                bin_volumes[i] += vol_per_bin

        poc_idx = max(range(bin_count), key=lambda i: bin_volumes[i])
        poc_price = price_min + (poc_idx + 0.5) * bin_width
        total_volume = sum(bin_volumes)
        target = total_volume * 0.70

        running = bin_volumes[poc_idx]
        low_idx, high_idx = poc_idx, poc_idx
        while running < target and (low_idx > 0 or high_idx < bin_count - 1):
            next_low = low_idx - 1 if low_idx > 0 else None
            next_high = high_idx + 1 if high_idx < bin_count - 1 else None
            vol_low = bin_volumes[next_low] if next_low is not None else -1
            vol_high = bin_volumes[next_high] if next_high is not None else -1
            if vol_low >= vol_high:
                low_idx = next_low
                running += bin_volumes[low_idx]
            else:
                high_idx = next_high
                running += bin_volumes[high_idx]

        val_price = price_min + low_idx * bin_width
        vah_price = price_min + (high_idx + 1) * bin_width

        return {
            "poc": round(poc_price, 4),
            "val": round(val_price, 4),
            "vah": round(vah_price, 4),
            "total_volume": round(total_volume, 2),
        }

    def _calculate_atr(self, df, period=14):
        """
        Standard True Range based ATR: True Range is the largest of
        (high-low), (high-prev_close), (low-prev_close), then averaged
        over the period using a rolling mean.
        """
        prev_close = df["close"].shift(1)
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - prev_close).abs()
        tr3 = (df["low"] - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(period).mean()
        return atr

    def _pick_nearest_level(
        self, current_price, tier_a, tier_b, primary_timeframe, near_threshold_pct=5.0
    ):
        nearest_a = (
            min(tier_a, key=lambda z: abs(z["price"] - current_price))
            if tier_a
            else None
        )
        nearest_b = (
            min(tier_b, key=lambda z: abs(z["price"] - current_price))
            if tier_b
            else None
        )
        if (
            nearest_a
            and abs(nearest_a["price"] - current_price) / current_price * 100
            <= near_threshold_pct
        ):
            return {
                "price": nearest_a["price"],
                "label": f"{nearest_a['instances']}x validated (multi-TF confirmed)",
            }
        if nearest_b:
            return {
                "price": nearest_b["price"],
                "label": f"{nearest_b['touches']}x touches ({primary_timeframe} only - lower confidence)",
            }
        if nearest_a:
            return {
                "price": nearest_a["price"],
                "label": f"{nearest_a['instances']}x validated (multi-TF, but distant from price)",
            }
        return None

    def _add_right_tag(self, ax, price, tag_text, box_color, linestyle="--"):
        ax.axhline(
            price, color=box_color, linestyle=linestyle, linewidth=0.8, alpha=0.6
        )
        if tag_text:
            ax.text(
                0.02,
                price,
                f"{tag_text}",
                transform=ax.get_yaxis_transform(),
                color="#9598a1",
                fontsize=8,
                va="center",
                ha="left",
            )
        ax.text(
            1.001,
            price,
            f" {price:.2f} ",
            transform=ax.get_yaxis_transform(),
            color="white",
            fontsize=9,
            va="center",
            ha="left",
            bbox=dict(facecolor=box_color, edgecolor="none", pad=2),
        )

    def _render_chart(
        self,
        df,
        symbol,
        timeframe,
        hlines=None,
        hline_colors=None,
        hline_tags=None,
        alines=None,
        aline_colors=None,
        zones=None,
        golden_zone=None,
        points=None,
        vp_levels=None,
        sr_zones=None,
    ):
        mc = mpf.make_marketcolors(
            up="#089981",
            down="#f23645",
            edge="inherit",
            wick="inherit",
            volume={"up": "#089981", "down": "#f23645"},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            base_mpf_style="nightclouds",
            facecolor="#131722",
            edgecolor="#2a2e39",
            figcolor="#131722",
            gridcolor="#1e222d",
            gridstyle="-",
            rc={
                "axes.labelcolor": "#d1d4dc",
                "xtick.color": "#787b86",
                "ytick.color": "#787b86",
            },
        )
        addplots = [
            mpf.make_addplot(df["EMA20"], color="orange", width=1),
            mpf.make_addplot(df["VWAP"], color="cyan", width=1),
        ]
        kwargs = dict(
            type="candle",
            style=style,
            addplot=addplots,
            volume=True,
            returnfig=True,
            figsize=(12, 7),
        )
        if alines:
            kwargs["alines"] = dict(
                alines=alines,
                colors=aline_colors or ["white"] * len(alines),
                linewidths=[2] * len(alines),
            )
        fig, axes = mpf.plot(df, **kwargs)
        n_candles = len(df)
        max_labels = 30
        tick_step = max(1, n_candles // max_labels)
        tick_positions = list(range(0, n_candles, tick_step))
        tick_labels = [df.index[i].strftime("%H:%M") for i in tick_positions]

        price_ax = axes[0]
        price_ax.yaxis.tick_right()
        price_ax.yaxis.set_label_position("right")

        volume_ax = None
        for ax in fig.axes:
            label = ax.get_ylabel()
            if label and "volume" in label.lower():
                volume_ax = ax
        if volume_ax:
            volume_ax.set_yticklabels([])
            volume_ax.set_ylabel("")
            volume_ax.tick_params(axis="y", length=0)

        clean_symbol = symbol.upper().replace("BINANCE:", "").replace(":", "")
        price_ax.text(
            0.5,
            0.5,
            f"{clean_symbol}  {timeframe.upper()}",
            transform=price_ax.transAxes,
            fontsize=42,
            color="white",
            alpha=0.06,
            ha="center",
            va="center",
            zorder=0,
            weight="bold",
        )
        price_ax.text(
            0.01,
            0.97,
            "EMA 20",
            transform=price_ax.transAxes,
            color="orange",
            fontsize=9,
            va="top",
        )
        price_ax.text(
            0.01,
            0.92,
            "VWAP",
            transform=price_ax.transAxes,
            color="cyan",
            fontsize=9,
            va="top",
        )

        if zones:
            for price_top, price_bottom, ts_left, ts_right, label, color in zones:
                price_ax.axhspan(
                    price_bottom,
                    price_top,
                    xmin=0,
                    xmax=1,
                    color=color,
                    alpha=0.10,
                    zorder=0,
                )
                if label:
                    price_ax.text(
                        0.97,
                        price_top,
                        label,
                        transform=price_ax.get_yaxis_transform(),
                        color="#d1d4dc",
                        fontsize=9,
                        va="bottom",
                        ha="right",
                        style="italic",
                    )

        if golden_zone:
            glow, ghigh = golden_zone
            if ghigh > glow:
                price_ax.axhspan(
                    glow, ghigh, xmin=0, xmax=1, color="#d4af37", alpha=0.20, zorder=0
                )
                mid = (glow + ghigh) / 2
                label_trans = transforms.blended_transform_factory(
                    price_ax.transAxes, price_ax.transData
                )
                price_ax.text(
                    0.5,
                    mid,
                    "GOLDEN POCKET",
                    transform=label_trans,
                    color="#d4af37",
                    alpha=0.2,
                    fontsize=16,
                    weight="bold",
                    ha="center",
                    va="center",
                    zorder=1,
                )

        if sr_zones:
            for price, tolerance_pct, label, color in sr_zones:
                band_high = price * (1 + tolerance_pct / 100)
                band_low = price * (1 - tolerance_pct / 100)
                price_ax.axhspan(
                    band_low,
                    band_high,
                    xmin=0,
                    xmax=1,
                    color=color,
                    alpha=0.15,
                    zorder=1,
                )
                self._add_right_tag(price_ax, price, label, color, linestyle="-")

        if vp_levels:
            for price, label, color in vp_levels:
                price_ax.axhline(
                    price, color=color, linestyle="-", linewidth=1, alpha=0.20, zorder=2
                )
                price_ax.text(
                    0.01,
                    price,
                    label,
                    transform=price_ax.get_yaxis_transform(),
                    color=color,
                    fontsize=7,
                    alpha=0.25,
                    va="bottom",
                    ha="left",
                    zorder=3,
                )

        if points:
            for ts, price, dot_color in points:
                try:
                    x_pos = df.index.get_loc(ts)
                except KeyError:
                    continue
                price_ax.plot(
                    x_pos,
                    price,
                    marker="o",
                    markersize=8,
                    markerfacecolor="white",
                    markeredgecolor=dot_color,
                    markeredgewidth=1.5,
                    zorder=6,
                )

        if hlines:
            tags = hline_tags or [""] * len(hlines)
            colors = hline_colors or ["yellow"] * len(hlines)
            for price, tag, color in zip(hlines, tags, colors):
                self._add_right_tag(price_ax, price, tag, color)

        last_price = df["close"].iloc[-1]
        last_color = (
            "#089981" if df["close"].iloc[-1] >= df["open"].iloc[-1] else "#f23645"
        )
        self._add_right_tag(price_ax, last_price, "", last_color)
        price_ax.set_xticks(tick_positions)
        price_ax.set_xticklabels(tick_labels, rotation=45, ha="right")
        fig.subplots_adjust(left=0.02, right=0.90, top=0.96, bottom=0.08)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            facecolor=fig.get_facecolor(),
            bbox_inches="tight",
            pad_inches=0.15,
        )
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    async def get_live_chart(
        self,
        symbol: str,
        timeframe: str,
        zoom_candles: int = 300,
        __event_emitter__=None,
    ) -> str:
        """
        Fetches REAL live candle data directly from Binance and renders a TradingView-styled chart with EMA20, VWAP, and RSI calculated mathematically.
        :param symbol: e.g. SOLUSDT, BTCUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param zoom_candles: How many candles to display - default 300, lower zooms in, higher zooms out
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=zoom_candles)
            df = self._add_indicators(df)
            img_b64 = self._render_chart(df, symbol, timeframe)
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )
            latest = df.iloc[-1]
            recent = df.tail(15)[["open", "high", "low", "close"]].round(2)
            recent_str = "\n".join(
                [
                    f"{i} candles ago ({idx.strftime('%H:%M')}): O={row.open} H={row.high} L={row.low} C={row.close}"
                    for i, (idx, row) in enumerate(recent[::-1].iterrows())
                ]
            )
            return (
                f"Live chart shown to Kev for {symbol} {timeframe}.\n"
                f"Current price: {latest['close']:.2f} | EMA20: {latest['EMA20']:.2f} | VWAP: {latest['VWAP']:.2f} | RSI: {latest['RSI']:.1f}\n"
                f"Recent candles (use the 'candles ago' number to reference a point for drawing):\n{recent_str}"
            )
        except Exception as e:
            return f"Error fetching live chart: {e}"

    async def get_volume_profile(
        self,
        symbol: str,
        timeframe: str,
        anchor_candles_ago: int = None,
        zoom_candles: int = 300,
        __event_emitter__=None,
    ) -> str:
        """
        Calculates a real Volume Profile (POC, VAH, VAL) from the most recent significant
        swing point to now, using the actual volume-at-price method - not a guess. Use this
        when Kev asks about Volume Profile, POC, value area, or wants to see where real trading
        activity has concentrated on a pair.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param anchor_candles_ago: Optional - manually set how many candles back to start measuring from. If not given, auto-detects the most recent significant swing point as the starting point.
        :param zoom_candles: How many candles to display - default 300
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=700)

            if anchor_candles_ago is not None:
                anchor_ts = self._candles_ago_to_ts(df, anchor_candles_ago)
                anchor_idx = df.index.get_loc(anchor_ts)
            else:
                touches = self._get_timed_touches(df)
                if touches:
                    last_ts = touches[-1][0]
                    anchor_idx = df.index.get_loc(last_ts)
                else:
                    anchor_idx = 0

            profile_df = df.iloc[anchor_idx:]
            vp = self._calculate_volume_profile(profile_df)
            if vp is None:
                return f"Couldn't calculate Volume Profile for {symbol} {timeframe} - not enough price movement in range."

            chart_df = self._add_indicators(
                self._fetch_candles(symbol, timeframe, limit=zoom_candles)
            )
            vp_levels = [
                (vp["poc"], "POC", "#f23645"),
                (vp["vah"], "VAH", "white"),
                (vp["val"], "VAL", "white"),
            ]
            img_b64 = self._render_chart(
                chart_df, symbol, timeframe, vp_levels=vp_levels
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )

            return (
                f"Volume Profile for {symbol} {timeframe} (measured from the most recent significant swing point):\n"
                f"POC (most traded price, strongest agreement): {vp['poc']}\n"
                f"VAH (upper edge of accepted value): {vp['vah']}\n"
                f"VAL (lower edge of accepted value): {vp['val']}\n"
                f"Total volume measured: {vp['total_volume']}\n"
                f"Price trading above VAH can be considered relatively expensive, below VAL relatively cheap. "
                f"Acceptance outside either edge can signal a shift in market value. Shown on chart."
            )
        except Exception as e:
            return f"Error calculating Volume Profile: {e}"

    async def get_atr_stop_suggestion(
        self,
        symbol: str,
        timeframe: str,
        entry_price: float,
        direction: str,
        multiplier: float = 2.0,
    ) -> str:
        """
        Calculates real 14-period ATR (Average True Range) from live candle data and suggests
        a volatility-based stop-loss distance, instead of an arbitrary or guessed stop. Also
        compares current ATR to its own recent average so Kev/Chev knows if today is unusually
        quiet or unusually volatile relative to normal - this is a reality check on stop
        placement and expected price movement, not an entry signal.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param entry_price: The real entry price being considered
        :param direction: "long" or "short"
        :param multiplier: How many multiples of ATR to use for the stop distance - default 2.0 (industry common range is 1.5-3.0, wider for more volatile assets)
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=100)
            atr_series = self._calculate_atr(df, period=14)
            current_atr = atr_series.iloc[-1]
            if pd.isna(current_atr):
                return f"Not enough candle history yet to calculate a reliable ATR for {symbol} {timeframe}."

            recent_avg_atr = atr_series.tail(20).mean()
            ratio = current_atr / recent_avg_atr if recent_avg_atr else 1.0

            if ratio < 0.7:
                volatility_note = "quieter than usual right now - expect smaller moves than normal, don't expect a huge range today"
            elif ratio > 1.3:
                volatility_note = "more volatile than usual right now - bigger moves are in play, a tight stop may get clipped by normal noise"
            else:
                volatility_note = "roughly normal volatility for this pair right now"

            stop_distance = round(current_atr * multiplier, 4)
            if direction.lower() == "long":
                suggested_stop = round(entry_price - stop_distance, 4)
            else:
                suggested_stop = round(entry_price + stop_distance, 4)

            return (
                f"ATR-based stop suggestion for {symbol} {timeframe}:\n"
                f"Current 14-period ATR: {round(current_atr, 4)}\n"
                f"Recent average ATR (last 20 periods): {round(recent_avg_atr, 4)}\n"
                f"Volatility context: {volatility_note}\n"
                f"Using {multiplier}x ATR = {stop_distance} stop distance\n"
                f"Entry: {entry_price} ({direction}) -> Suggested stop-loss: {suggested_stop}\n"
                f"This is a volatility reality check, not an entry signal - still use real S/R/structure to judge if this stop placement makes sense."
            )
        except Exception as e:
            return f"Error calculating ATR stop suggestion: {e}"

    async def mark_price_level(
        self,
        symbol: str,
        timeframe: str,
        target_price: float,
        tag: str,
        color: str = "#2962ff",
        __event_emitter__=None,
    ) -> str:
        """
        Draws an exact horizontal line at a real price with a TradingView-style right-axis tag label.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param target_price: The exact price to mark
        :param tag: Short tag shown next to the price, e.g. "1H S/R", "1W", "Resistance"
        :param color: hex color or name, e.g. "#2962ff" (blue), "purple", "yellow", "red", "green"
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=300)
            df = self._add_indicators(df)
            img_b64 = self._render_chart(
                df,
                symbol,
                timeframe,
                hlines=[target_price],
                hline_tags=[tag],
                hline_colors=[color],
            )
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )
            return f"Marked '{tag}' at exactly {target_price} and showed it to Kev."
        except Exception as e:
            return f"Error marking level: {e}"

    async def draw_trend_line(
        self,
        symbol: str,
        timeframe: str,
        price_1: float,
        candles_ago_1: int,
        price_2: float,
        candles_ago_2: int,
        label: str,
        color: str = "yellow",
        __event_emitter__=None,
    ) -> str:
        """
        Draws a precise diagonal trend line connecting two real points.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param price_1: Price at the first point
        :param candles_ago_1: How many candles back the first point is
        :param price_2: Price at the second point
        :param candles_ago_2: How many candles back the second point is
        :param label: Short label
        :param color: yellow, red, green, or white
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=300)
            df2 = self._add_indicators(df)
            ts1 = self._candles_ago_to_ts(df, candles_ago_1)
            ts2 = self._candles_ago_to_ts(df, candles_ago_2)
            line = [(ts1, price_1), (ts2, price_2)]
            img_b64 = self._render_chart(
                df2, symbol, timeframe, alines=[line], aline_colors=[color]
            )
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )
            return f"Drew trend line '{label}' connecting two real points and showed it to Kev."
        except Exception as e:
            return f"Error drawing trend line: {e}"

    async def draw_zone_box(
        self,
        symbol: str,
        timeframe: str,
        price_top: float,
        price_bottom: float,
        candles_ago_left: int,
        candles_ago_right: int,
        label: str,
        color: str = "#787b86",
        __event_emitter__=None,
    ) -> str:
        """
        Draws a shaded supply/demand zone box with a label, TradingView style.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param price_top: Upper price boundary
        :param price_bottom: Lower price boundary
        :param candles_ago_left: How many candles back the LEFT edge is
        :param candles_ago_right: How many candles back the RIGHT edge is
        :param label: e.g. "SUPPLY" or "DEMAND"
        :param color: hex or name, e.g. "#787b86" (grey), "red", "green"
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=300)
            df2 = self._add_indicators(df)
            ts_left = self._candles_ago_to_ts(df, candles_ago_left)
            ts_right = self._candles_ago_to_ts(df, candles_ago_right)
            zones = [(price_top, price_bottom, ts_left, ts_right, label, color)]
            img_b64 = self._render_chart(df2, symbol, timeframe, zones=zones)
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )
            return f"Drew zone box '{label}' and showed it to Kev."
        except Exception as e:
            return f"Error drawing zone box: {e}"

    async def get_support_resistance(
        self,
        symbol: str,
        timeframe: str = "1h",
        zoom_candles: int = 300,
        highlight_resistance: list = None,
        highlight_support: list = None,
        __event_emitter__=None,
    ) -> str:
        """
        Detects ALL validated support/resistance levels currently visible in the zoomed
        price range - not just the single nearest one. Uses Kev's validated-instance
        model: Tier A requires minimum touch density on ALL of 15m(>=5), 30m(>=3),
        1h(>=2), 4h(>=2) simultaneously; falls back to Tier B (single-timeframe) if no
        Tier A level is visible. Returns the FULL list of qualifying levels so Chev can
        use judgment - by default draws the 2 nearest per side, but if a different
        level feels more significant given other confluences (Fib, Volume Profile,
        structure), call this again with highlight_resistance/highlight_support set to
        the exact prices (from the returned list) to draw that selection instead.
        :param symbol: e.g. SOLUSDT
        :param timeframe: Primary timeframe for the chart, default 1h
        :param zoom_candles: How many candles to display - default 300
        :param highlight_resistance: optional list of exact resistance prices to draw instead of the default nearest 2
        :param highlight_support: optional list of exact support prices to draw instead of the default nearest 2
        """
        try:
            timeframes = ["15m", "30m", "1h", "4h"]
            resistance_touches_by_tf, support_touches_by_tf = {}, {}
            primary_df = None
            for tf in timeframes:
                df = self._fetch_candles(symbol, tf, limit=700)
                if tf == timeframe:
                    primary_df = df
                touches = self._get_timed_touches(df)
                resistance_touches_by_tf[tf] = [
                    (ts, p) for ts, p, k in touches if k == "resistance"
                ]
                support_touches_by_tf[tf] = [
                    (ts, p) for ts, p, k in touches if k == "support"
                ]

            if primary_df is None:
                primary_df = self._fetch_candles(symbol, timeframe, limit=700)

            current_price = primary_df["close"].iloc[-1]
            resistance_levels = self._build_validated_levels(
                resistance_touches_by_tf, "resistance"
            )
            support_levels = self._build_validated_levels(
                support_touches_by_tf, "support"
            )

            chart_df = self._add_indicators(
                self._fetch_candles(symbol, timeframe, limit=zoom_candles)
            )
            display_low = chart_df["low"].min()
            display_high = chart_df["high"].max()

            tier_b_resistance = self._build_single_tf_levels(
                resistance_touches_by_tf[timeframe]
            )
            tier_b_support = self._build_single_tf_levels(
                support_touches_by_tf[timeframe]
            )

            def collect_side(tier_a_levels, tier_b_levels, is_resistance):
                visible_a = [
                    z
                    for z in tier_a_levels
                    if display_low <= z["price"] <= display_high
                    and (
                        z["price"] > current_price
                        if is_resistance
                        else z["price"] < current_price
                    )
                ]
                if visible_a:
                    visible_a.sort(key=lambda z: abs(z["price"] - current_price))
                    return [
                        {
                            "price": z["price"],
                            "label": f"{z['instances']}t validated (multi-TF)",
                        }
                        for z in visible_a
                    ]
                visible_b = [
                    z
                    for z in tier_b_levels
                    if display_low <= z["price"] <= display_high
                    and (
                        z["price"] > current_price
                        if is_resistance
                        else z["price"] < current_price
                    )
                ]
                visible_b.sort(key=lambda z: abs(z["price"] - current_price))
                return [
                    {
                        "price": z["price"],
                        "label": f"{z['touches']}t ({timeframe} only)",
                    }
                    for z in visible_b
                ]

            all_resistance = collect_side(resistance_levels, tier_b_resistance, True)
            all_support = collect_side(support_levels, tier_b_support, False)

            chosen_resistance = None
            if highlight_resistance:
                chosen_resistance = [
                    z
                    for z in all_resistance
                    if any(abs(z["price"] - hp) < 1e-6 for hp in highlight_resistance)
                ]
            if not chosen_resistance:
                chosen_resistance = all_resistance[:2]

            chosen_support = None
            if highlight_support:
                chosen_support = [
                    z
                    for z in all_support
                    if any(abs(z["price"] - hp) < 1e-6 for hp in highlight_support)
                ]
            if not chosen_support:
                chosen_support = all_support[:2]

            sr_zones = []
            for z in chosen_resistance:
                sr_zones.append((z["price"], 0.6, f"R: {z['label']}", "#f23645"))
            for z in chosen_support:
                sr_zones.append((z["price"], 0.6, f"S: {z['label']}", "#089981"))

            img_b64 = self._render_chart(chart_df, symbol, timeframe, sr_zones=sr_zones)
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )

            resistance_list_str = (
                "\n".join(
                    [f"  {z['price']:.4f} - {z['label']}" for z in all_resistance]
                )
                or "  none found"
            )
            support_list_str = (
                "\n".join([f"  {z['price']:.4f} - {z['label']}" for z in all_support])
                or "  none found"
            )

            return (
                f"Support/Resistance for {symbol} {timeframe} (current price {current_price:.4f}):\n\n"
                f"ALL resistance levels visible in this zoom range:\n{resistance_list_str}\n\n"
                f"ALL support levels visible in this zoom range:\n{support_list_str}\n\n"
                f"Drew the {len(chosen_resistance)} nearest resistance and {len(chosen_support)} nearest support by default. "
                f"If a different level looks more significant given other confluences, call this again with "
                f"highlight_resistance/highlight_support set to the exact prices you want drawn instead."
            )
        except Exception as e:
            return f"Error detecting support/resistance: {e}"

    async def get_fibonacci_levels(
        self, symbol: str, timeframe: str, lookback: int = 150, __event_emitter__=None
    ) -> str:
        """
        Calculates real Fibonacci retracement levels anchored to the most recent genuine
        swing-to-swing move (not just the highest/lowest point in a flat window), with the
        golden pocket (61.8%-65%) highlighted as a distinct gold zone and the two real swing
        points marked.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        :param lookback: How many recent candles to search within for the swing move
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=max(lookback, 150) + 50)
            touches = self._get_timed_touches(df.tail(max(lookback, 150) + 50))
            impulse = self._find_last_impulse(touches)

            if impulse:
                ts1, price1, ts2, price2 = impulse
                if price1 < price2:
                    idx_low_ts, swing_low = ts1, price1
                    idx_high_ts, swing_high = ts2, price2
                else:
                    idx_high_ts, swing_high = ts1, price1
                    idx_low_ts, swing_low = ts2, price2
            else:
                window = df.tail(lookback)
                idx_high_ts = window["high"].idxmax()
                idx_low_ts = window["low"].idxmin()
                swing_high = window.loc[idx_high_ts, "high"]
                swing_low = window.loc[idx_low_ts, "low"]

            diff = swing_high - swing_low
            fib_ratios = {
                "50%": 0.5,
                "61.8% (golden pocket)": 0.618,
                "65%": 0.65,
                "78.6%": 0.786,
            }

            if idx_low_ts < idx_high_ts:
                levels = {
                    name: round(swing_high - diff * ratio, 4)
                    for name, ratio in fib_ratios.items()
                }
            else:
                levels = {
                    name: round(swing_low + diff * ratio, 4)
                    for name, ratio in fib_ratios.items()
                }

            golden_zone = (
                min(levels["61.8% (golden pocket)"], levels["65%"]),
                max(levels["61.8% (golden pocket)"], levels["65%"]),
            )
            df2 = self._add_indicators(df.tail(max(lookback, 150)))
            points = [
                (idx_high_ts, swing_high, "#d4af37"),
                (idx_low_ts, swing_low, "#d4af37"),
            ]
            img_b64 = self._render_chart(
                df2,
                symbol,
                timeframe,
                golden_zone=golden_zone,
                points=points,
            )
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )
            level_lines = "\n".join(
                [f"{name}: {price}" for name, price in levels.items()]
            )
            direction = (
                "upward (retracing down from the high)"
                if idx_low_ts < idx_high_ts
                else "downward (retracing up from the low)"
            )
            return f"Fibonacci anchored to the most recent real swing move ({direction}) - swing high {swing_high}, swing low {swing_low}:\n{level_lines}\nGolden pocket highlighted with real swing points marked. Shown on chart."
        except Exception as e:
            return f"Error calculating Fibonacci: {e}"

    async def show_confluences(
        self,
        symbol: str,
        timeframe: str = "1h",
        include: list = None,
        zoom_candles: int = 300,
        __event_emitter__=None,
    ) -> str:
        """
        Draws ONE chart combining specific confluences together (e.g. support/resistance AND Fibonacci at once), so Kev can see exactly what Chev found interesting in one view. EMA20 and VWAP are always shown by default.
        :param symbol: e.g. SOLUSDT
        :param timeframe: Primary timeframe, default 1h
        :param include: List of confluence types to draw, choose from "support_resistance", "fibonacci". e.g. ["support_resistance", "fibonacci"]
        :param zoom_candles: How many candles to display - default 300
        """
        try:
            if include is None:
                include = ["support_resistance", "fibonacci"]

            chart_df = self._add_indicators(
                self._fetch_candles(symbol, timeframe, limit=zoom_candles)
            )
            display_low = chart_df["low"].min()
            display_high = chart_df["high"].max()

            golden_zone = None
            points = []
            sr_zones = []
            summary_lines = [
                f"Combined chart for {symbol} {timeframe} showing: {', '.join(include)}"
            ]

            if "support_resistance" in include:
                timeframes = ["15m", "30m", "1h", "4h"]
                resistance_touches_by_tf, support_touches_by_tf = {}, {}
                primary_df = None
                for tf in timeframes:
                    df = self._fetch_candles(symbol, tf, limit=700)
                    if tf == timeframe:
                        primary_df = df
                    touches = self._get_timed_touches(df)
                    resistance_touches_by_tf[tf] = [
                        (ts, p) for ts, p, k in touches if k == "resistance"
                    ]
                    support_touches_by_tf[tf] = [
                        (ts, p) for ts, p, k in touches if k == "support"
                    ]
                if primary_df is None:
                    primary_df = self._fetch_candles(symbol, timeframe, limit=700)
                current_price = primary_df["close"].iloc[-1]

                resistance_levels = self._build_validated_levels(
                    resistance_touches_by_tf, "resistance"
                )
                support_levels = self._build_validated_levels(
                    support_touches_by_tf, "support"
                )
                tier_b_resistance = self._build_single_tf_levels(
                    resistance_touches_by_tf[timeframe]
                )
                tier_b_support = self._build_single_tf_levels(
                    support_touches_by_tf[timeframe]
                )

                def collect_side(tier_a_levels, tier_b_levels, is_resistance):
                    visible_a = [
                        z
                        for z in tier_a_levels
                        if display_low <= z["price"] <= display_high
                        and (
                            z["price"] > current_price
                            if is_resistance
                            else z["price"] < current_price
                        )
                    ]
                    if visible_a:
                        visible_a.sort(key=lambda z: abs(z["price"] - current_price))
                        return [
                            {
                                "price": z["price"],
                                "label": f"{z['instances']}t validated (multi-TF)",
                            }
                            for z in visible_a
                        ]
                    visible_b = [
                        z
                        for z in tier_b_levels
                        if display_low <= z["price"] <= display_high
                        and (
                            z["price"] > current_price
                            if is_resistance
                            else z["price"] < current_price
                        )
                    ]
                    visible_b.sort(key=lambda z: abs(z["price"] - current_price))
                    return [
                        {
                            "price": z["price"],
                            "label": f"{z['touches']}t ({timeframe} only)",
                        }
                        for z in visible_b
                    ]

                all_resistance = collect_side(
                    resistance_levels, tier_b_resistance, True
                )
                all_support = collect_side(support_levels, tier_b_support, False)

                for z in all_resistance[:2]:
                    sr_zones.append((z["price"], 0.6, f"R: {z['label']}", "#f23645"))
                    summary_lines.append(f"Resistance: {z['price']:.4f} - {z['label']}")
                for z in all_support[:2]:
                    sr_zones.append((z["price"], 0.6, f"S: {z['label']}", "#089981"))
                    summary_lines.append(f"Support: {z['price']:.4f} - {z['label']}")

            if "fibonacci" in include:
                fib_lookback = min(zoom_candles, 300)
                fib_df = self._fetch_candles(
                    symbol, timeframe, limit=max(fib_lookback, 150) + 50
                )
                touches = self._get_timed_touches(fib_df)
                impulse = self._find_last_impulse(touches)

                if impulse:
                    ts1, price1, ts2, price2 = impulse
                    if price1 < price2:
                        idx_low_ts, swing_low = ts1, price1
                        idx_high_ts, swing_high = ts2, price2
                    else:
                        idx_high_ts, swing_high = ts1, price1
                        idx_low_ts, swing_low = ts2, price2
                else:
                    window = fib_df.tail(fib_lookback)
                    idx_high_ts = window["high"].idxmax()
                    idx_low_ts = window["low"].idxmin()
                    swing_high = window.loc[idx_high_ts, "high"]
                    swing_low = window.loc[idx_low_ts, "low"]

                diff = swing_high - swing_low
                fib_ratios = {
                    "50%": 0.5,
                    "61.8% (golden pocket)": 0.618,
                    "65%": 0.65,
                    "78.6%": 0.786,
                }
                if idx_low_ts < idx_high_ts:
                    levels = {
                        name: round(swing_high - diff * ratio, 4)
                        for name, ratio in fib_ratios.items()
                    }
                else:
                    levels = {
                        name: round(swing_low + diff * ratio, 4)
                        for name, ratio in fib_ratios.items()
                    }

                golden_zone = (
                    min(levels["61.8% (golden pocket)"], levels["65%"]),
                    max(levels["61.8% (golden pocket)"], levels["65%"]),
                )
                points.extend(
                    [
                        (idx_high_ts, swing_high, "#d4af37"),
                        (idx_low_ts, swing_low, "#d4af37"),
                    ]
                )
                summary_lines.append(
                    f"Fibonacci anchored to the most recent real swing move (swing high {swing_high}, swing low {swing_low}), golden pocket highlighted with real swing points marked."
                )

            img_b64 = self._render_chart(
                chart_df,
                symbol,
                timeframe,
                golden_zone=golden_zone,
                points=points,
                sr_zones=sr_zones,
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )

            summary_lines.append("Shown on chart.")
            return "\n".join(summary_lines)
        except Exception as e:
            return f"Error showing combined confluences: {e}"

    async def detect_candlestick_patterns(
        self, symbol: str, timeframe: str, __event_emitter__=None
    ) -> str:
        """
        Detects common candlestick patterns in recent candles using real price math.
        :param symbol: e.g. SOLUSDT
        :param timeframe: One of 5m, 15m, 30m, 1h, 4h, 1d, 1w
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=20)
            found = []
            for i in range(2, len(df)):
                o, h, l, c = (
                    df["open"].iloc[i],
                    df["high"].iloc[i],
                    df["low"].iloc[i],
                    df["close"].iloc[i],
                )
                po, pc = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
                body = abs(c - o)
                full_range = h - l if h != l else 0.0001
                upper_shadow = h - max(o, c)
                lower_shadow = min(o, c) - l
                idx_label = df.index[i].strftime("%H:%M")
                if body / full_range < 0.1:
                    found.append(f"{idx_label}: Doji (indecision)")
                elif lower_shadow > body * 2 and upper_shadow < body * 0.5:
                    found.append(
                        f"{idx_label}: {'Hammer (bullish)' if c > o else 'Hanging Man (bearish)'}"
                    )
                elif upper_shadow > body * 2 and lower_shadow < body * 0.5:
                    found.append(
                        f"{idx_label}: {'Inverted Hammer' if c > o else 'Shooting Star (bearish)'}"
                    )
                if pc < po and c > o and c > po and o < pc:
                    found.append(f"{idx_label}: Bullish Engulfing")
                elif pc > po and c < o and c < po and o > pc:
                    found.append(f"{idx_label}: Bearish Engulfing")
            if not found:
                return "No clear standard candlestick patterns detected in the most recent candles."
            return "Detected patterns (most recent candles):\n" + "\n".join(found[-6:])
        except Exception as e:
            return f"Error detecting patterns: {e}"

    async def get_stacked_fibonacci(
        self,
        symbol: str,
        primary_timeframe: str = "1h",
        __event_emitter__=None,
    ) -> str:
        """
        Calculates Fibonacci golden pockets (61.8%-65% zone) on THREE timeframes
        simultaneously — 15m, 1h, and 4h — and draws them all on one chart.
        When two or more golden pockets land at the same price level, that is a
        stacked confluence: significantly stronger than any single-timeframe Fib alone.
        Overlap zones are flagged explicitly in the text output.
        :param symbol: e.g. SOLUSDT
        :param primary_timeframe: The timeframe used for the background chart (default 1h)
        """
        try:
            TFS = [
                ("15m", "#5dade2", 0.18),
                ("1h",  "#d4af37", 0.20),
                ("4h",  "#e67e22", 0.18),
            ]
            zones_list = []
            gp_ranges = []
            summary = [f"Stacked Fibonacci for {symbol}:"]
            primary_df = None

            for tf, color, _ in TFS:
                df = self._fetch_candles(symbol, tf, limit=300)
                if tf == primary_timeframe:
                    primary_df = self._add_indicators(df.copy())

                touches = self._get_timed_touches(df)
                impulse = self._find_last_impulse(touches)

                if impulse:
                    ts1, p1, ts2, p2 = impulse
                    if p1 < p2:
                        swing_low, swing_high = p1, p2
                        idx_low_ts, idx_high_ts = ts1, ts2
                    else:
                        swing_high, swing_low = p1, p2
                        idx_high_ts, idx_low_ts = ts1, ts2
                else:
                    window = df.tail(150)
                    swing_high = window["high"].max()
                    swing_low  = window["low"].min()
                    idx_high_ts = window["high"].idxmax()
                    idx_low_ts  = window["low"].idxmin()

                diff = swing_high - swing_low
                if diff == 0:
                    summary.append(f"  {tf}: no price range found, skipping")
                    continue

                if idx_low_ts < idx_high_ts:
                    gp_618 = swing_high - diff * 0.618
                    gp_65  = swing_high - diff * 0.65
                else:
                    gp_618 = swing_low + diff * 0.618
                    gp_65  = swing_low + diff * 0.65

                gp_lo = min(gp_618, gp_65)
                gp_hi = max(gp_618, gp_65)

                zones_list.append((gp_hi, gp_lo, df.index[0], df.index[-1], f"{tf} GP", color))
                gp_ranges.append((tf, gp_lo, gp_hi))
                summary.append(
                    f"  {tf}: golden pocket {gp_lo:.5f} — {gp_hi:.5f}  "
                    f"(swing {swing_low:.5f} → {swing_high:.5f})"
                )

            if primary_df is None:
                primary_df = self._add_indicators(
                    self._fetch_candles(symbol, primary_timeframe, limit=300)
                )

            overlaps = []
            for i in range(len(gp_ranges)):
                for j in range(i + 1, len(gp_ranges)):
                    tf_i, lo_i, hi_i = gp_ranges[i]
                    tf_j, lo_j, hi_j = gp_ranges[j]
                    o_lo = max(lo_i, lo_j)
                    o_hi = min(hi_i, hi_j)
                    if o_hi > o_lo:
                        overlaps.append(
                            f"  ⚡ STACKED: {tf_i} + {tf_j} overlap @ {o_lo:.5f} – {o_hi:.5f}"
                        )

            if overlaps:
                summary.append("\nConfluence overlaps (treat these as high-priority zones):")
                summary.extend(overlaps)
            else:
                summary.append(
                    "\nNo direct overlaps — golden pockets are at distinct price levels on each TF."
                )

            img_b64 = self._render_chart(
                primary_df, symbol, primary_timeframe, zones=zones_list
            )
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )
            summary.append(
                "\nChart: blue = 15m GP, gold = 1h GP, orange = 4h GP. "
                "Overlapping areas are the strongest Fibonacci confluences."
            )
            return "\n".join(summary)
        except Exception as e:
            return f"Error calculating stacked Fibonacci: {e}"

    async def detect_rsi_divergence(
        self,
        symbol: str,
        timeframe: str = "1h",
        lookback: int = 100,
        __event_emitter__=None,
    ) -> str:
        """
        Detects RSI divergence using the same logic as Dexter's internal scanner.
        Checks the two most recent swing highs (bearish divergence) and two most recent
        swing lows (bullish divergence) and compares price direction against RSI direction.

        Types detected:
          Regular Bearish  — price makes HH but RSI makes LH (momentum fading at top)
          Hidden Bearish   — price makes LH but RSI makes HH (trend continuation down)
          Regular Bullish  — price makes LL but RSI makes HL (momentum turning at bottom)
          Hidden Bullish   — price makes HL but RSI makes LL (trend continuation up)

        Divergence lines are drawn on both the price panel and the RSI panel.
        :param symbol: e.g. SOLUSDT
        :param timeframe: e.g. 1h
        :param lookback: How many recent candles to analyse
        """
        try:
            import matplotlib.pyplot as plt
            import math

            BG    = "#131722"
            GRID  = "#2a2e39"
            DIM   = "#787b86"
            UP    = "#089981"
            DOWN  = "#f23645"

            df = self._add_indicators(self._fetch_candles(symbol, timeframe, limit=lookback + 50))
            df = df.tail(lookback).copy()

            highs = df["high"].values
            lows  = df["low"].values
            n     = len(df)
            window, threshold_pct, confirm_w = 3, 0.3, 8

            swing_highs, swing_lows = [], []
            for i in range(window, n - window):
                if highs[i] == max(highs[i - window: i + window + 1]):
                    fl = lows[i + 1: min(i + 1 + confirm_w, n)]
                    if len(fl) and (highs[i] - fl.min()) / highs[i] * 100 >= threshold_pct:
                        swing_highs.append((i, highs[i]))
                if lows[i] == min(lows[i - window: i + window + 1]):
                    fh = highs[i + 1: min(i + 1 + confirm_w, n)]
                    if len(fh) and (fh.max() - lows[i]) / lows[i] * 100 >= threshold_pct:
                        swing_lows.append((i, lows[i]))

            def safe_rsi(i):
                if 0 <= i < len(df):
                    v = float(df["RSI"].iloc[i])
                    return None if math.isnan(v) else v
                return None

            divs = []
            if len(swing_highs) >= 2:
                i1, p1 = swing_highs[-2]
                i2, p2 = swing_highs[-1]
                r1, r2 = safe_rsi(i1), safe_rsi(i2)
                if r1 and r2:
                    if p2 > p1 and r2 < r1:
                        divs.append({"type": "Regular Bearish", "bias": "bear",
                                     "i1": i1, "p1": p1, "i2": i2, "p2": p2, "r1": r1, "r2": r2})
                    elif p2 < p1 and r2 > r1:
                        divs.append({"type": "Hidden Bearish", "bias": "bear",
                                     "i1": i1, "p1": p1, "i2": i2, "p2": p2, "r1": r1, "r2": r2})
            if len(swing_lows) >= 2:
                i1, p1 = swing_lows[-2]
                i2, p2 = swing_lows[-1]
                r1, r2 = safe_rsi(i1), safe_rsi(i2)
                if r1 and r2:
                    if p2 < p1 and r2 > r1:
                        divs.append({"type": "Regular Bullish", "bias": "bull",
                                     "i1": i1, "p1": p1, "i2": i2, "p2": p2, "r1": r1, "r2": r2})
                    elif p2 > p1 and r2 < r1:
                        divs.append({"type": "Hidden Bullish", "bias": "bull",
                                     "i1": i1, "p1": p1, "i2": i2, "p2": p2, "r1": r1, "r2": r2})

            fig, (ax, ax_rsi) = plt.subplots(
                2, 1, figsize=(12, 8),
                gridspec_kw={"height_ratios": [3, 1]},
                facecolor=BG,
            )
            for a in (ax, ax_rsi):
                a.set_facecolor(BG)
                a.spines[:].set_color(GRID)
                a.grid(color=GRID, linewidth=0.4)
                a.tick_params(colors=DIM, labelsize=7)

            for i, (_, row) in enumerate(df.iterrows()):
                color = UP if row["close"] >= row["open"] else DOWN
                ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.7)
                ax.bar(i, abs(row["close"] - row["open"]),
                       bottom=min(row["open"], row["close"]),
                       color=color, width=0.6, linewidth=0)

            xs = list(range(len(df)))
            ax.plot(xs, df["EMA20"].values, color="orange", linewidth=0.9, label="EMA20")
            ax.plot(xs, df["VWAP"].values,  color="cyan",   linewidth=0.9, label="VWAP")

            for i, p in swing_highs:
                ax.plot(i, p, "v", color=DOWN,  markersize=4, alpha=0.55)
            for i, p in swing_lows:
                ax.plot(i, p, "^", color=UP,    markersize=4, alpha=0.55)

            p_range = df["high"].max() - df["low"].min()
            for div in divs:
                color = DOWN if div["bias"] == "bear" else UP
                ax.annotate("", xy=(div["i2"], div["p2"]), xytext=(div["i1"], div["p1"]),
                            arrowprops=dict(arrowstyle="-", color=color, lw=1.5, linestyle="dashed"))
                ax.text((div["i1"] + div["i2"]) // 2,
                        max(div["p1"], div["p2"]) + p_range * 0.025,
                        div["type"], color=color, fontsize=7.5, ha="center", fontweight="bold")
                ax_rsi.annotate("", xy=(div["i2"], div["r2"]), xytext=(div["i1"], div["r1"]),
                               arrowprops=dict(arrowstyle="-", color=color, lw=1.5, linestyle="dashed"))

            ax_rsi.plot(xs, df["RSI"].values, color="#9b59b6", linewidth=1.0)
            ax_rsi.axhline(70, color=DOWN, linewidth=0.6, linestyle="--")
            ax_rsi.axhline(30, color=UP,   linewidth=0.6, linestyle="--")
            ax_rsi.axhline(50, color=GRID, linewidth=0.4, linestyle="--")
            ax_rsi.set_ylim(0, 100)
            ax_rsi.set_ylabel("RSI(14)", color=DIM, fontsize=7)

            step   = max(1, len(df) // 8)
            idxs   = list(range(0, len(df), step))
            labels = [df.index[i].strftime("%b %d %H:%M") for i in idxs]
            for a in (ax, ax_rsi):
                a.set_xticks(idxs)
                a.set_xticklabels(labels, rotation=25, fontsize=6.5, color=DIM)

            div_summary = ", ".join(d["type"] for d in divs) if divs else "None detected"
            ax.set_title(f"{symbol} · {timeframe} · RSI Divergence — {div_summary}",
                         color="#d1d4dc", fontsize=10)
            ax.legend(fontsize=7, labelcolor=DIM, facecolor="#1e222d", edgecolor=GRID)
            plt.tight_layout(pad=0.5)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
            plt.close(fig)
            img_b64 = base64.b64encode(buf.getvalue()).decode()

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )

            if not divs:
                return f"RSI Divergence scan for {symbol} {timeframe}: No divergence detected in the last {lookback} candles. Chart shown for reference."
            result_lines = [f"RSI Divergence found on {symbol} {timeframe}:"]
            for d in divs:
                result_lines.append(
                    f"  {d['type']}: price swing {d['p1']:.5f} → {d['p2']:.5f}, "
                    f"RSI {d['r1']:.1f} → {d['r2']:.1f}. "
                    f"{'Bearish signal — momentum fading.' if d['bias'] == 'bear' else 'Bullish signal — momentum building.'}"
                )
            result_lines.append("Divergence lines drawn on both price and RSI panels.")
            return "\n".join(result_lines)
        except Exception as e:
            return f"Error detecting RSI divergence: {e}"

    async def detect_chart_patterns(
        self,
        symbol: str,
        timeframe: str = "1h",
        lookback: int = 100,
        __event_emitter__=None,
    ) -> str:
        """
        Detects chart patterns using actual high/low pivots, convergence validation
        (triangles/wedges: gap must shrink ≥15%), and volume confirmation for reversal
        patterns (H&S, double tops/bottoms).

        Patterns: Symmetrical/Ascending/Descending Triangle, Rising/Falling Wedge,
                  Rectangle/Range, Head & Shoulders, Inverse H&S, Double Top, Double Bottom.
        """
        try:
            import matplotlib.pyplot as plt
            import matplotlib.gridspec as gridspec

            BG   = "#131722"
            GRID = "#2a2e39"
            DIM  = "#787b86"
            UP   = "#089981"
            DOWN = "#f23645"
            GOLD = "#d4af37"
            ORNG = "#f0b429"

            df_raw = self._add_indicators(self._fetch_candles(symbol, timeframe, limit=lookback + 30))
            df = df_raw.tail(lookback).copy().reset_index(drop=True)
            n = len(df)

            highs   = df["high"].values.astype(float)
            lows    = df["low"].values.astype(float)
            closes  = df["close"].values.astype(float)
            opens   = df["open"].values.astype(float)
            volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(n)
            avg_vol = float(volumes.mean()) if volumes.mean() > 0 else 1.0

            # ── Pivot detection on actual high/low prices ─────────────────────
            window, confirm_w = 3, 6
            swing_highs, swing_lows = [], []
            for i in range(window, n - window):
                win_h = highs[max(0, i - window): i + window + 1]
                win_l = lows[max(0, i - window):  i + window + 1]
                if highs[i] >= win_h.max():
                    fl = lows[i + 1: min(i + 1 + confirm_w, n)]
                    if len(fl) and (highs[i] - fl.min()) / highs[i] >= 0.003:
                        swing_highs.append(i)
                if lows[i] <= win_l.min():
                    fh = highs[i + 1: min(i + 1 + confirm_w, n)]
                    if len(fh) and (fh.max() - lows[i]) / lows[i] >= 0.003:
                        swing_lows.append(i)

            # ── Build figure (price + volume strip) ───────────────────────────
            fig = plt.figure(figsize=(13, 8), facecolor=BG)
            gs  = gridspec.GridSpec(2, 1, height_ratios=[4, 1], hspace=0.04)
            ax  = fig.add_subplot(gs[0])
            axv = fig.add_subplot(gs[1], sharex=ax)

            for a in (ax, axv):
                a.set_facecolor(BG)
                a.spines[:].set_color(GRID)
                a.grid(color=GRID, linewidth=0.4)
                a.tick_params(colors=DIM, labelsize=7)

            xs = list(range(n))
            for i in range(n):
                col = UP if closes[i] >= opens[i] else DOWN
                ax.plot([i, i], [lows[i], highs[i]], color=col, linewidth=0.7)
                ax.bar(i, abs(closes[i] - opens[i]),
                       bottom=min(opens[i], closes[i]),
                       color=col, width=0.6, linewidth=0)
                axv.bar(i, volumes[i], color=col, width=0.8, linewidth=0, alpha=0.6)

            if "EMA20" in df.columns:
                ax.plot(xs, df["EMA20"].values, color="orange", linewidth=0.9, label="EMA20", alpha=0.8)
            if "VWAP" in df.columns:
                ax.plot(xs, df["VWAP"].values, color="cyan", linewidth=0.9, label="VWAP", alpha=0.8)

            axv.set_ylabel("Vol", color=DIM, fontsize=6)
            plt.setp(ax.get_xticklabels(), visible=False)

            if len(swing_highs) < 2 or len(swing_lows) < 2:
                ax.set_title(f"{symbol} · {timeframe} · Not enough swing points",
                             color="#d1d4dc", fontsize=10)
                plt.tight_layout(pad=0.5)
                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
                plt.close(fig)
                img_b64 = base64.b64encode(buf.getvalue()).decode()
                if __event_emitter__:
                    await __event_emitter__({"type": "files", "data": {"files": [{"type": "image", "url": f"data:image/png;base64,{img_b64}"}]}})
                return f"Not enough confirmed swing points on {symbol} {timeframe}. Try a longer lookback or different timeframe."

            # Mark swing pivots
            for i in swing_highs:
                ax.plot(i, highs[i], "v", color=DOWN, markersize=5, alpha=0.75)
            for i in swing_lows:
                ax.plot(i, lows[i], "^", color=UP, markersize=5, alpha=0.75)

            # ── Trendline fitting ─────────────────────────────────────────────
            k = min(5, max(2, len(swing_highs)))
            hi_idx = swing_highs[-k:]
            lo_idx = swing_lows[-min(k, len(swing_lows)):]

            def fit_tl(indices, values):
                x = np.array(indices, dtype=float)
                y = values[list(indices)]
                ref_x = x[0]
                xn = x - ref_x
                s, b = np.polyfit(xn, y, 1)
                y_pred = s * xn + b
                ss_res = np.sum((y - y_pred) ** 2)
                ss_tot = np.sum((y - y.mean()) ** 2)
                r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-10 else 1.0
                return float(s), float(ref_x), float(b), float(r2)

            hi_slope, hi_ref, hi_int, hi_r2 = fit_tl(hi_idx, highs)
            lo_slope, lo_ref, lo_int, lo_r2 = fit_tl(lo_idx, lows)

            price     = float(closes[-1])
            flat_thr  = price * 0.0004

            def sd(s):
                return "rising" if s > flat_thr else ("falling" if s < -flat_thr else "flat")

            hd, ld = sd(hi_slope), sd(lo_slope)

            def tl_val(slope, ref, intercept, x):
                return slope * (x - ref) + intercept

            x_first = float(min(hi_idx[0], lo_idx[0]))
            x_last  = float(n - 1)
            x_plot  = np.linspace(x_first, x_last, 200)

            upper_line = np.array([tl_val(hi_slope, hi_ref, hi_int, x) for x in x_plot])
            lower_line = np.array([tl_val(lo_slope, lo_ref, lo_int, x) for x in x_plot])

            upper_now  = tl_val(hi_slope, hi_ref, hi_int, x_last)
            lower_now  = tl_val(lo_slope, lo_ref, lo_int, x_last)
            breakout_pct = 0.012
            breakout_up  = price > upper_now * (1 + breakout_pct)
            breakout_dn  = price < lower_now * (1 - breakout_pct)

            # ── Convergence validation (≥15% gap shrink required) ─────────────
            gap_start = tl_val(hi_slope, hi_ref, hi_int, x_first) - tl_val(lo_slope, lo_ref, lo_int, x_first)
            gap_end   = upper_now - lower_now
            is_converging = gap_start > 0 and gap_end > 0 and gap_end < gap_start * 0.85
            conv_pct  = max(0.0, 1.0 - gap_end / gap_start) if gap_start > 0 else 0.0

            # ── Volume helpers ─────────────────────────────────────────────────
            def vol_at(idx, r=2):
                return float(volumes[max(0, idx - r): min(n, idx + r + 1)].mean())

            def vol_declining(left_idx, right_idx):
                return vol_at(right_idx) < vol_at(left_idx) * 0.88

            def vol_spike():
                return float(volumes[-3:].mean()) > avg_vol * 1.3

            # ── Pattern classification ─────────────────────────────────────────
            results = []
            min_r2 = 0.50

            if hi_r2 >= min_r2 and lo_r2 >= min_r2 and is_converging:
                conf_base = (hi_r2 + lo_r2) / 2 * (0.85 + 0.15 * conv_pct)
                if hd == "falling" and ld == "rising":
                    sig  = "BUY" if breakout_up else ("SELL" if breakout_dn else "NEUTRAL")
                    bias = "bullish" if breakout_up else ("bearish" if breakout_dn else "neutral")
                    results.append({"name": "Symmetrical Triangle", "bias": bias, "signal": sig,
                                    "confidence": conf_base, "breakout": breakout_up or breakout_dn,
                                    "color": GOLD, "vol_confirmed": vol_spike() if (breakout_up or breakout_dn) else None})
                elif hd == "flat" and ld == "rising":
                    sig = "BUY" if breakout_up else "NEUTRAL"
                    results.append({"name": "Ascending Triangle", "bias": "bullish", "signal": sig,
                                    "confidence": conf_base, "breakout": breakout_up,
                                    "color": UP, "vol_confirmed": vol_spike() if breakout_up else None})
                elif hd == "falling" and ld == "flat":
                    sig = "SELL" if breakout_dn else "NEUTRAL"
                    results.append({"name": "Descending Triangle", "bias": "bearish", "signal": sig,
                                    "confidence": conf_base, "breakout": breakout_dn,
                                    "color": DOWN, "vol_confirmed": vol_spike() if breakout_dn else None})
                elif hd == "rising" and ld == "rising":
                    sig = "SELL" if breakout_dn else "NEUTRAL"
                    results.append({"name": "Rising Wedge", "bias": "bearish", "signal": sig,
                                    "confidence": conf_base, "breakout": breakout_dn,
                                    "color": ORNG, "vol_confirmed": vol_spike() if breakout_dn else None})
                elif hd == "falling" and ld == "falling":
                    sig = "BUY" if breakout_up else "NEUTRAL"
                    results.append({"name": "Falling Wedge", "bias": "bullish", "signal": sig,
                                    "confidence": conf_base, "breakout": breakout_up,
                                    "color": UP, "vol_confirmed": vol_spike() if breakout_up else None})

            if hd == "flat" and ld == "flat" and hi_r2 >= min_r2 and lo_r2 >= min_r2:
                sig  = "BUY" if breakout_up else ("SELL" if breakout_dn else "NEUTRAL")
                bias = "bullish" if breakout_up else ("bearish" if breakout_dn else "neutral")
                results.append({"name": "Rectangle / Range", "bias": bias, "signal": sig,
                                "confidence": (hi_r2 + lo_r2) / 2, "breakout": breakout_up or breakout_dn,
                                "color": DIM, "vol_confirmed": vol_spike() if (breakout_up or breakout_dn) else None})

            # H&S (volume confirmation required)
            if len(swing_highs) >= 3:
                h = swing_highs[-3:]
                p = highs[h]
                sym = abs(p[0] - p[2]) / p[1] if p[1] > 0 else 1.0
                if p[1] > p[0] and p[1] > p[2] and sym < 0.08:
                    vol_ok = vol_declining(h[0], h[2])
                    conf = 0.70 if (breakout_dn and vol_ok) else (0.55 if breakout_dn else (0.45 if vol_ok else 0.35))
                    sig  = "SELL" if breakout_dn else "NEUTRAL"
                    results.append({"name": "Head & Shoulders", "bias": "bearish", "signal": sig,
                                    "confidence": conf, "breakout": breakout_dn,
                                    "color": DOWN, "vol_confirmed": vol_ok,
                                    "hs_idx": h, "hs_prices": p})

            if len(swing_lows) >= 3:
                l = swing_lows[-3:]
                p = lows[l]
                sym = abs(p[0] - p[2]) / abs(p[1]) if p[1] != 0 else 1.0
                if p[1] < p[0] and p[1] < p[2] and sym < 0.08:
                    vol_ok = vol_declining(l[0], l[2])
                    conf = 0.70 if (breakout_up and vol_ok) else (0.55 if breakout_up else (0.45 if vol_ok else 0.35))
                    sig  = "BUY" if breakout_up else "NEUTRAL"
                    results.append({"name": "Inverse H&S", "bias": "bullish", "signal": sig,
                                    "confidence": conf, "breakout": breakout_up,
                                    "color": UP, "vol_confirmed": vol_ok,
                                    "hs_idx": l, "hs_prices": p})

            # Double Top/Bottom (volume confirmation required)
            tol, min_sep = 0.03, 5
            if len(swing_highs) >= 2:
                t1, t2 = swing_highs[-2], swing_highs[-1]
                p1, p2 = highs[t1], highs[t2]
                if abs(p1 - p2) / p1 < tol and (t2 - t1) >= min_sep:
                    vol_ok = vol_declining(t1, t2)
                    conf = 0.68 if (breakout_dn and vol_ok) else (0.50 if breakout_dn else (0.38 if vol_ok else 0.28))
                    sig  = "SELL" if breakout_dn else "NEUTRAL"
                    results.append({"name": "Double Top", "bias": "bearish", "signal": sig,
                                    "confidence": conf, "breakout": breakout_dn,
                                    "color": DOWN, "vol_confirmed": vol_ok,
                                    "dt_idx": [t1, t2], "dt_prices": [p1, p2]})

            if len(swing_lows) >= 2:
                b1, b2 = swing_lows[-2], swing_lows[-1]
                p1, p2 = lows[b1], lows[b2]
                if abs(p1 - p2) / p1 < tol and (b2 - b1) >= min_sep:
                    vol_ok = vol_declining(b1, b2)
                    conf = 0.68 if (breakout_up and vol_ok) else (0.50 if breakout_up else (0.38 if vol_ok else 0.28))
                    sig  = "BUY" if breakout_up else "NEUTRAL"
                    results.append({"name": "Double Bottom", "bias": "bullish", "signal": sig,
                                    "confidence": conf, "breakout": breakout_up,
                                    "color": UP, "vol_confirmed": vol_ok,
                                    "dt_idx": [b1, b2], "dt_prices": [p1, p2]})

            results.sort(key=lambda p: (
                int(bool(p["breakout"] and p.get("vol_confirmed"))),
                int(bool(p["breakout"])),
                p["confidence"],
            ), reverse=True)

            # ── Draw trendlines + annotations ─────────────────────────────────
            ax.plot(x_plot, upper_line, color=DOWN, linewidth=1.5, linestyle="--", label="Resistance TL", alpha=0.85)
            ax.plot(x_plot, lower_line, color=UP,   linewidth=1.5, linestyle="--", label="Support TL",    alpha=0.85)

            if results:
                top = results[0]
                ax.fill_between(x_plot, lower_line, upper_line, alpha=0.06, color=top["color"])

                # H&S markers
                if "hs_idx" in top:
                    for j, idx in enumerate(top["hs_idx"]):
                        marker = "v" if top["bias"] == "bearish" else "^"
                        col    = DOWN if top["bias"] == "bearish" else UP
                        ax.plot(idx, top["hs_prices"][j], marker, color=col, markersize=10, alpha=0.9)
                        label = ["LS", "Head", "RS"][j]
                        ax.annotate(label, (idx, top["hs_prices"][j]), textcoords="offset points",
                                    xytext=(0, 8 if top["bias"] == "bearish" else -12),
                                    color=col, fontsize=7, ha="center")

                # Double Top/Bottom markers
                if "dt_idx" in top:
                    col = DOWN if top["bias"] == "bearish" else UP
                    marker = "v" if top["bias"] == "bearish" else "^"
                    for j, (idx, pr) in enumerate(zip(top["dt_idx"], top["dt_prices"])):
                        ax.plot(idx, pr, marker, color=col, markersize=10, alpha=0.9)
                        ax.annotate(f"T{j+1}" if top["bias"] == "bearish" else f"B{j+1}",
                                    (idx, pr), textcoords="offset points",
                                    xytext=(0, 8 if top["bias"] == "bearish" else -12),
                                    color=col, fontsize=7, ha="center")

                # Convergence annotation
                if is_converging:
                    ax.annotate(f"Conv {conv_pct*100:.0f}%", xy=(x_last, (upper_now + lower_now) / 2),
                                fontsize=7, color=top["color"], alpha=0.8,
                                bbox=dict(boxstyle="round,pad=0.2", fc=BG, ec=top["color"], alpha=0.5))

                # Breakout annotation
                if top["breakout"]:
                    bx, by = x_last, price
                    blabel = f"BREAKOUT {'↑' if breakout_up else '↓'}"
                    if top.get("vol_confirmed"):
                        blabel += " + VOL ✓"
                    ax.annotate(blabel, xy=(bx, by),
                                fontsize=8, color=top["color"], fontweight="bold",
                                bbox=dict(boxstyle="round,pad=0.3", fc=BG, ec=top["color"], alpha=0.7))

                pat_label  = top["name"]
                pat_color  = top["color"]
                conf_label = f"  conf={top['confidence']:.2f}  signal={top['signal']}"
                if top.get("vol_confirmed") is True:
                    conf_label += "  vol✓"
                elif top.get("vol_confirmed") is False:
                    conf_label += "  vol✗"
            else:
                pat_label = f"No clean pattern (res:{hd} sup:{ld})"
                pat_color = DIM
                conf_label = f"  hi_r²={hi_r2:.2f}  lo_r²={lo_r2:.2f}"

            step = max(1, n // 8)
            idxs = list(range(0, n, step))
            axv.set_xticks(idxs)
            axv.set_xticklabels([df.index[i].strftime("%b %d %H:%M") if hasattr(df.index[i], "strftime") else str(i)
                                  for i in idxs], rotation=25, fontsize=6.5, color=DIM)

            ax.set_title(f"{symbol} · {timeframe} · {pat_label}{conf_label}", color=pat_color, fontsize=9)
            ax.legend(fontsize=7, labelcolor=DIM, facecolor="#1e222d", edgecolor=GRID, loc="upper left")
            plt.tight_layout(pad=0.5)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
            plt.close(fig)
            img_b64 = base64.b64encode(buf.getvalue()).decode()

            if __event_emitter__:
                await __event_emitter__({"type": "files", "data": {"files": [{"type": "image", "url": f"data:image/png;base64,{img_b64}"}]}})

            _EXPECTED = {
                "Symmetrical Triangle": (
                    "Apex approach — indecision builds; one side will give way",
                    "Decisive close through either trendline on expanding volume"),
                "Ascending Triangle": (
                    "Retest of flat resistance — volume should decline into test",
                    "Impulsive close above flat resistance with volume >=1.5x avg"),
                "Descending Triangle": (
                    "Retest of flat support — volume should decline into test",
                    "Impulsive close below flat support with volume >=1.5x avg"),
                "Rising Wedge": (
                    "Continued compression; possible final thrust high (throw-over)",
                    "Close below lower wedge boundary on bearish momentum shift"),
                "Falling Wedge": (
                    "Continued compression; possible final thrust low (spring)",
                    "Close above upper wedge boundary on bullish momentum shift"),
                "Rectangle / Range": (
                    "Test of either boundary — direction of breakout is the trade",
                    "Close outside balance zone on volume >=1.3x avg"),
                "Head & Shoulders": (
                    "Neckline retest after RS formation completes",
                    "Break + close below neckline with volume >=1.5x avg and no immediate recovery"),
                "Inverse H&S": (
                    "Neckline retest after right shoulder completes",
                    "Break + close above neckline with volume >=1.5x avg and no immediate pullback"),
                "Double Top": (
                    "Pullback between the two peaks — neckline is the trigger",
                    "Close below neckline (valley between tops) with declining volume at T2"),
                "Double Bottom": (
                    "Bounce between the two lows — neckline is the trigger",
                    "Close above neckline (peak between bottoms) with declining volume at B2"),
            }

            if not results:
                return (f"No clean chart pattern found on {symbol} {timeframe}. "
                        f"Trendline slopes: resistance={hd}, support={ld}. "
                        f"R2 scores: hi={hi_r2:.2f}, lo={lo_r2:.2f}. Try a different timeframe.")

            lines = [f"Pattern detected on {symbol} {timeframe}:"]
            for idx_r, r in enumerate(results[:3]):
                vol_txt = ""
                if r.get("vol_confirmed") is True:
                    vol_txt = " · volume confirms"
                elif r.get("vol_confirmed") is False:
                    vol_txt = " · volume not confirmed (watch for false breakout)"
                bo_txt   = " · BREAKOUT ACTIVE" if r["breakout"] else ""
                conv_txt = f" · convergence {conv_pct*100:.0f}%" if is_converging and r["name"] not in ("Head & Shoulders", "Inverse H&S", "Double Top", "Double Bottom") else ""
                lines.append(f"  [{idx_r + 1}] {r['name']} — {r['bias'].upper()} bias | signal: {r['signal']} | confidence: {r['confidence']:.2f}{bo_txt}{vol_txt}{conv_txt}")

                ev = _EXPECTED.get(r["name"])
                if ev:
                    lines.append(f"      NEXT   : {ev[0]}")
                    lines.append(f"      TRIGGER: {ev[1]}")

            lines.append(f"Trendline quality: resistance R2={hi_r2:.2f}, support R2={lo_r2:.2f}.")
            if is_converging:
                lines.append(f"Lines are converging ({conv_pct*100:.0f}% gap reduction) — pattern is well-formed.")
            return "\n".join(lines)

        except Exception as e:
            return f"Error detecting chart patterns: {e}"

    async def get_multitime_structure(
        self,
        symbol: str,
        primary_timeframe: str = "1h",
        zoom_candles: int = 150,
        __event_emitter__=None,
    ) -> str:
        """
        Multi-timeframe market structure analysis across 15m, 1h, 4h, and 1d.
        For each timeframe: classifies trend as UPTREND (HH+HL), DOWNTREND (LH+LL), or RANGING.
        S/R levels are scored by AGE-WEIGHT — older levels that still hold get a higher score
        than fresh recent touches. A 15m level with 400 candles of history can outrank a
        brand-new 4h touch. Score formula: sqrt(candles_since_formed).
        Top 3 resistance and top 3 support drawn on the primary chart, shaded by strength.
        Trend table shown in top-left corner of chart.
        :param symbol: e.g. SOLUSDT
        :param primary_timeframe: Chart display timeframe (analysis always covers all 4 TFs)
        :param zoom_candles: Candles to display on the primary chart
        """
        try:
            import matplotlib.pyplot as plt
            import math

            BG    = "#131722"
            GRID  = "#2a2e39"
            DIM   = "#787b86"
            BRIGHT= "#d1d4dc"
            UP    = "#089981"
            DOWN  = "#f23645"

            ANALYSIS_TFS   = ["15m", "1h", "4h", "1d"]
            CANDLES_PER_TF = {"15m": 500, "1h": 300, "4h": 200, "1d": 150}
            window, threshold_pct, confirm_w = 3, 0.3, 8

            tf_results       = {}
            all_weighted_res = []
            all_weighted_sup = []

            for tf in ANALYSIS_TFS:
                try:
                    df_tf = self._fetch_candles(symbol, tf, limit=CANDLES_PER_TF[tf])
                    n     = len(df_tf)
                    if n < 10:
                        continue
                    highs = df_tf["high"].values
                    lows  = df_tf["low"].values

                    sh, sl = [], []
                    for i in range(window, n - window):
                        if highs[i] == max(highs[i - window: i + window + 1]):
                            fl = lows[i + 1: min(i + 1 + confirm_w, n)]
                            if len(fl) and (highs[i] - fl.min()) / highs[i] * 100 >= threshold_pct:
                                sh.append((i, highs[i]))
                        if lows[i] == min(lows[i - window: i + window + 1]):
                            fh = highs[i + 1: min(i + 1 + confirm_w, n)]
                            if len(fh) and (fh.max() - lows[i]) / lows[i] * 100 >= threshold_pct:
                                sl.append((i, lows[i]))

                    if len(sh) >= 2 and len(sl) >= 2:
                        h_seq = [p for _, p in sh[-3:]]
                        l_seq = [p for _, p in sl[-3:]]
                        hh = all(h_seq[k] > h_seq[k - 1] for k in range(1, len(h_seq)))
                        hl = all(l_seq[k] > l_seq[k - 1] for k in range(1, len(l_seq)))
                        lh = all(h_seq[k] < h_seq[k - 1] for k in range(1, len(h_seq)))
                        ll = all(l_seq[k] < l_seq[k - 1] for k in range(1, len(l_seq)))
                        if hh and hl:
                            trend, tc = "UPTREND  ▲", UP
                        elif lh and ll:
                            trend, tc = "DOWNTREND ▼", DOWN
                        else:
                            trend, tc = "RANGING  ◆", "#d4af37"
                    else:
                        trend, tc = "N/A", DIM

                    for i, p in sh:
                        weight = math.sqrt(max(n - i, 1))
                        all_weighted_res.append({"price": p, "weight": weight, "tf": tf})
                    for i, p in sl:
                        weight = math.sqrt(max(n - i, 1))
                        all_weighted_sup.append({"price": p, "weight": weight, "tf": tf})

                    tf_results[tf] = {"trend": trend, "tc": tc}
                except Exception:
                    tf_results[tf] = {"trend": "ERR", "tc": DIM}

            def cluster_weighted(levels, current, above, tol=0.8, top_n=3):
                side = [l for l in levels if (l["price"] > current) == above]
                if not side:
                    return []
                sorted_l = sorted(side, key=lambda x: x["price"])
                groups, cur = [], [sorted_l[0]]
                for l in sorted_l[1:]:
                    if abs(l["price"] - cur[-1]["price"]) / cur[-1]["price"] * 100 <= tol:
                        cur.append(l)
                    else:
                        groups.append(cur)
                        cur = [l]
                groups.append(cur)
                result = []
                for g in groups:
                    total_w = sum(x["weight"] for x in g)
                    avg_p   = sum(x["price"] * x["weight"] for x in g) / total_w
                    tfs     = sorted(set(x["tf"] for x in g))
                    result.append({"price": avg_p, "score": total_w, "tfs": tfs})
                result.sort(key=lambda x: x["score"], reverse=True)
                return result[:top_n]

            df_primary = self._add_indicators(
                self._fetch_candles(symbol, primary_timeframe, limit=zoom_candles)
            )
            current = float(df_primary["close"].iloc[-1])
            p_range = df_primary["high"].max() - df_primary["low"].min()

            top_res = cluster_weighted(all_weighted_res, current, above=True)
            top_sup = cluster_weighted(all_weighted_sup, current, above=False)

            fig, ax = plt.subplots(figsize=(12, 7), facecolor=BG)
            ax.set_facecolor(BG)
            ax.spines[:].set_color(GRID)
            ax.grid(color=GRID, linewidth=0.4)
            ax.tick_params(colors=DIM, labelsize=7)

            for i, (_, row) in enumerate(df_primary.iterrows()):
                color = UP if row["close"] >= row["open"] else DOWN
                ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.7)
                ax.bar(i, abs(row["close"] - row["open"]),
                       bottom=min(row["open"], row["close"]),
                       color=color, width=0.6, linewidth=0)

            xs = list(range(len(df_primary)))
            ax.plot(xs, df_primary["EMA20"].values, color="orange", linewidth=0.9, label="EMA20")
            ax.plot(xs, df_primary["VWAP"].values,  color="cyan",   linewidth=0.9, label="VWAP")

            # HH / HL / LH / LL labels at swing points (primary chart only, last 5 of each)
            _ph = df_primary["high"].values
            _pl = df_primary["low"].values
            _pn = len(df_primary)
            _psh, _psl = [], []
            for _i in range(window, _pn - window):
                if _ph[_i] == max(_ph[_i - window: _i + window + 1]):
                    _fl = _pl[_i + 1: min(_i + 1 + confirm_w, _pn)]
                    if len(_fl) and (_ph[_i] - _fl.min()) / _ph[_i] * 100 >= threshold_pct:
                        _psh.append((_i, _ph[_i]))
                if _pl[_i] == min(_pl[_i - window: _i + window + 1]):
                    _fh = _ph[_i + 1: min(_i + 1 + confirm_w, _pn)]
                    if len(_fh) and (_fh.max() - _pl[_i]) / _pl[_i] * 100 >= threshold_pct:
                        _psl.append((_i, _pl[_i]))
            for _pts, _mk, _lu, _ld, _yo in [
                (_psh[-5:], "v", "HH", "LH",  0.014),
                (_psl[-5:], "^", "HL", "LL", -0.021),
            ]:
                for _k in range(1, len(_pts)):
                    _xi, _pi = _pts[_k]; _, _pip = _pts[_k - 1]
                    _up  = _pi > _pip
                    _lbl = _lu if _up else _ld
                    _c   = UP  if _up else DOWN
                    _mc  = DOWN if _mk == "v" else UP
                    ax.plot(_xi, _pi, _mk, color=_mc, markersize=4, alpha=0.55)
                    ax.text(_xi, _pi + p_range * _yo, _lbl, color=_c,
                            fontsize=6, ha="center", fontweight="bold", zorder=5)

            zone_h   = p_range * 0.005
            max_sc   = max([z["score"] for z in top_res + top_sup], default=1)
            for z in top_res:
                alpha = 0.12 + 0.22 * (z["score"] / max_sc)
                ax.axhspan(z["price"] - zone_h, z["price"] + zone_h, color=DOWN, alpha=alpha)
                ax.axhline(z["price"], color=DOWN, linewidth=0.8, linestyle="--", alpha=0.85)
                tag = "/".join(z["tfs"]) + f"  ⚡{z['score']:.0f}"
                ax.text(len(df_primary) + 0.5, z["price"], tag, color=DOWN, fontsize=6, va="center")
            for z in top_sup:
                alpha = 0.12 + 0.22 * (z["score"] / max_sc)
                ax.axhspan(z["price"] - zone_h, z["price"] + zone_h, color=UP, alpha=alpha)
                ax.axhline(z["price"], color=UP,   linewidth=0.8, linestyle="--", alpha=0.85)
                tag = "/".join(z["tfs"]) + f"  ⚡{z['score']:.0f}"
                ax.text(len(df_primary) + 0.5, z["price"], tag, color=UP, fontsize=6, va="center")

            lines = ["TF     Trend", "─" * 24]
            for tf in ANALYSIS_TFS:
                info = tf_results.get(tf, {})
                lines.append(f"{tf:>4}   {info.get('trend', 'N/A')}")
            ax.text(0.01, 0.99, "\n".join(lines),
                    transform=ax.transAxes, color=BRIGHT,
                    fontsize=6.5, va="top", ha="left", fontfamily="monospace",
                    bbox=dict(facecolor="#1e222d", edgecolor=GRID, alpha=0.88, pad=4))

            step   = max(1, len(df_primary) // 8)
            idxs   = list(range(0, len(df_primary), step))
            ax.set_xticks(idxs)
            ax.set_xticklabels([df_primary.index[i].strftime("%b %d %H:%M") for i in idxs],
                               rotation=25, fontsize=6.5, color=DIM)
            ax.set_title(f"{symbol} · {primary_timeframe} · Multi-TF Structure (age-weighted S/R)",
                         color=BRIGHT, fontsize=10)
            ax.legend(fontsize=7, labelcolor=DIM, facecolor="#1e222d", edgecolor=GRID)
            plt.tight_layout(pad=0.5)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
            plt.close(fig)
            img_b64 = base64.b64encode(buf.getvalue()).decode()

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "image",
                                    "url": f"data:image/png;base64,{img_b64}",
                                }
                            ]
                        },
                    }
                )

            summary = [f"Multi-TF Structure for {symbol}:"]
            for tf in ANALYSIS_TFS:
                info = tf_results.get(tf, {})
                summary.append(f"  {tf}: {info.get('trend', 'N/A')}")
            summary.append("\nAge-weighted S/R (higher ⚡score = older and more tested level):")
            for z in top_res:
                summary.append(f"  Resistance {z['price']:.5f}  score {z['score']:.0f}  TFs: {'/'.join(z['tfs'])}")
            for z in top_sup:
                summary.append(f"  Support    {z['price']:.5f}  score {z['score']:.0f}  TFs: {'/'.join(z['tfs'])}")
            summary.append("Trend table and S/R levels shown on chart.")
            return "\n".join(summary)
        except Exception as e:
            return f"Error calculating multi-timeframe structure: {e}"

    async def get_trade_performance(self, __event_emitter__=None) -> str:
        """
        Pulls the complete closed trade history from the Google Sheet and computes:
          - Overall win rate, total PnL, average win/loss, profit factor
          - Win rate and average PnL broken down by confluence tag (SR, FB, GP, etc.)
          - Win rate by trade type (scalp / day / swing)
          - Win rate by direction (LONG / SHORT)
          - Best and worst individual trades
        Use this whenever Kev asks about his performance, win rate, or which confluences work.
        ALWAYS call this tool — never estimate or guess historical results.
        """
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            CREDS_FILE = r"C:\ChevTools\google_credentials.json"
            SHEET_ID   = "1V1b2aU3SJu_R7VjFKGp9J6uFwucGSamhRWyq6jgCbFs"
            SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]

            creds  = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
            client = gspread.authorize(creds)
            ws     = client.open_by_key(SHEET_ID).worksheet("Trade Log")
            rows   = ws.get_all_values()[1:]
        except ImportError:
            return "ERROR: gspread or google-auth not installed in Open WebUI's Python environment."
        except Exception as e:
            return f"ERROR connecting to Google Sheet: {e}"

        if not rows:
            return "No trades found in the sheet yet."

        closed = []
        for row in rows:
            if len(row) < 14:
                continue
            pair, direction, _, _, _, _, _, _, _, tags, _, _, status = row[:13]
            result_dollar = row[13] if len(row) > 13 else ""
            trade_type    = row[15] if len(row) > 15 else "day"
            outcome       = status.strip().upper()
            if outcome not in ("WIN", "LOSS"):
                continue
            try:
                pnl = float(result_dollar) if result_dollar else 0.0
            except ValueError:
                pnl = 0.0
            closed.append({
                "pair":       pair.strip(),
                "direction":  direction.strip().upper(),
                "tags":       [t.strip().upper() for t in tags.split(",") if t.strip()],
                "trade_type": trade_type.strip().lower() or "day",
                "outcome":    outcome,
                "pnl":        pnl,
            })

        if not closed:
            return "No closed trades (WIN/LOSS) in the sheet yet — keep trading, the data will come."

        total  = len(closed)
        wins   = [t for t in closed if t["outcome"] == "WIN"]
        losses = [t for t in closed if t["outcome"] == "LOSS"]
        win_rate   = len(wins) / total * 100
        total_pnl  = sum(t["pnl"] for t in closed)
        avg_win    = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
        avg_loss   = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        gross_wins = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        pf = round(gross_wins / gross_loss, 2) if gross_loss > 0 else float("inf")

        lines = [
            "=" * 52,
            "  CHEV CHELIOS — TRADE PERFORMANCE REVIEW",
            "=" * 52,
            f"  Total closed trades : {total}",
            f"  Win rate            : {win_rate:.1f}%",
            f"  Total PnL           : ${total_pnl:+.2f}",
            f"  Average win         : ${avg_win:+.2f}",
            f"  Average loss        : ${avg_loss:+.2f}",
            f"  Profit factor       : {pf}",
            "",
        ]

        tag_stats = {}
        for t in closed:
            for tag in t["tags"]:
                if tag not in tag_stats:
                    tag_stats[tag] = {"w": 0, "l": 0, "pnl": 0.0}
                if t["outcome"] == "WIN":
                    tag_stats[tag]["w"] += 1
                else:
                    tag_stats[tag]["l"] += 1
                tag_stats[tag]["pnl"] += t["pnl"]

        lines.append("  WIN RATE BY CONFLUENCE TAG")
        lines.append("  " + "─" * 48)
        lines.append(f"  {'Tag':<8} {'Trades':>6} {'Win%':>7} {'PnL':>10}")
        lines.append("  " + "─" * 48)
        for tag, s in sorted(tag_stats.items(), key=lambda x: -(x[1]["w"] / max(x[1]["w"] + x[1]["l"], 1))):
            cnt    = s["w"] + s["l"]
            wr     = s["w"] / cnt * 100 if cnt > 0 else 0
            marker = " ★" if wr >= 60 else (" ⚠" if wr <= 40 else "")
            lines.append(f"  {tag:<8} {cnt:>6}   {wr:>5.1f}%  ${s['pnl']:>+8.2f}{marker}")
        lines.append("")

        type_stats = {}
        for t in closed:
            tt = t["trade_type"]
            if tt not in type_stats:
                type_stats[tt] = {"w": 0, "l": 0, "pnl": 0.0}
            if t["outcome"] == "WIN":
                type_stats[tt]["w"] += 1
            else:
                type_stats[tt]["l"] += 1
            type_stats[tt]["pnl"] += t["pnl"]

        lines.append("  WIN RATE BY TRADE TYPE")
        lines.append("  " + "─" * 48)
        for tt, s in sorted(type_stats.items()):
            cnt = s["w"] + s["l"]
            wr  = s["w"] / cnt * 100 if cnt > 0 else 0
            lines.append(f"  {tt:<10} {cnt:>4} trades   {wr:.1f}%   ${s['pnl']:+.2f}")
        lines.append("")

        dir_stats = {}
        for t in closed:
            d = t["direction"]
            if d not in dir_stats:
                dir_stats[d] = {"w": 0, "l": 0, "pnl": 0.0}
            if t["outcome"] == "WIN":
                dir_stats[d]["w"] += 1
            else:
                dir_stats[d]["l"] += 1
            dir_stats[d]["pnl"] += t["pnl"]

        lines.append("  WIN RATE BY DIRECTION")
        lines.append("  " + "─" * 48)
        for d, s in sorted(dir_stats.items()):
            cnt = s["w"] + s["l"]
            wr  = s["w"] / cnt * 100 if cnt > 0 else 0
            lines.append(f"  {d:<6}  {cnt:>4} trades   {wr:.1f}%   ${s['pnl']:+.2f}")
        lines.append("")

        best  = max(closed, key=lambda t: t["pnl"])
        worst = min(closed, key=lambda t: t["pnl"])
        lines += [
            "  BEST TRADE",
            f"  {best['pair']} {best['direction']}  ${best['pnl']:+.2f}  tags: {','.join(best['tags']) or 'none'}",
            "",
            "  WORST TRADE",
            f"  {worst['pair']} {worst['direction']}  ${worst['pnl']:+.2f}  tags: {','.join(worst['tags']) or 'none'}",
            "",
        ]

        lines.append("  RECENT CLOSED TRADES (row # for annotate_trade)")
        lines.append("  " + "─" * 48)
        recent_closed = []
        for i, row in enumerate(rows, start=2):
            if len(row) < 14:
                continue
            pair, direction, _, _, _, _, _, _, _, tags, _, _, status = row[:13]
            result_dollar = row[13] if len(row) > 13 else ""
            if status.strip().upper() in ("WIN", "LOSS"):
                try:
                    pnl = float(result_dollar) if result_dollar else 0.0
                except ValueError:
                    pnl = 0.0
                recent_closed.append((i, pair.strip(), direction.strip().upper(), status.strip().upper(), pnl))
        for row_i, pair_r, dir_r, out_r, pnl_r in recent_closed[-10:]:
            lines.append(f"  row {row_i:>3}  {pair_r:<12} {dir_r:<5} {out_r:<4}  ${pnl_r:+.2f}")
        lines.append("=" * 52)
        return "\n".join(lines)

    async def get_market_survey(
        self,
        symbol: str,
        timeframe: str = "1h",
        __event_emitter__=None,
    ) -> str:
        """
        Full market structure survey — runs Swing, Leg, State, Geometry, Auction, and
        Hypothesis analysis inline. Returns rich text with no chart image.
        Call this FIRST before any other tool to orient your structural analysis.
        Dexter never predicts. Dexter measures. Chev interprets.

        :param symbol: e.g. BTCUSDT, ETHUSDT
        :param timeframe: Candle timeframe (15m, 1h, 4h, 1d)
        """
        try:
            df = self._fetch_candles(symbol, timeframe, limit=500)
            n = len(df)
            current_price = float(df["close"].iloc[-1])

            highs   = df["high"].values.astype(float)
            lows    = df["low"].values.astype(float)
            closes  = df["close"].values.astype(float)
            opens   = df["open"].values.astype(float)
            volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(n)
            avg_vol = float(volumes.mean()) if volumes.mean() > 0 else 1.0

            # ATR (EMA-smoothed)
            prev_c = np.roll(closes, 1); prev_c[0] = closes[0]
            tr_vals = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_c), np.abs(lows - prev_c)))
            atr_vals = np.zeros(n)
            atr_vals[0] = tr_vals[0]
            alpha = 1.0 / 14
            for k in range(1, n):
                atr_vals[k] = alpha * tr_vals[k] + (1 - alpha) * atr_vals[k - 1]
            atr_now = float(atr_vals[-1]) if atr_vals[-1] > 0 else 1e-6

            # ── Swing detection ───────────────────────────────────────────────
            window, confirm_w = 3, 8
            raw_swings = []
            for i in range(window, n - confirm_w):
                h_i, l_i = highs[i], lows[i]
                if all(h_i >= highs[i - j] for j in range(1, window + 1)) and \
                   all(h_i >= highs[i + j] for j in range(1, confirm_w + 1)):
                    raw_swings.append((i, h_i, "H"))
                if all(l_i <= lows[i - j] for j in range(1, window + 1)) and \
                   all(l_i <= lows[i + j] for j in range(1, confirm_w + 1)):
                    raw_swings.append((i, l_i, "L"))
            raw_swings.sort(key=lambda x: x[0])

            # Deduplicate consecutive same-kind swings
            swings = []
            for s in raw_swings:
                if not swings:
                    swings.append(s); continue
                if s[2] == swings[-1][2]:
                    if s[2] == "H" and s[1] >= swings[-1][1]:
                        swings[-1] = s
                    elif s[2] == "L" and s[1] <= swings[-1][1]:
                        swings[-1] = s
                else:
                    swings.append(s)

            # ── Leg engine ────────────────────────────────────────────────────
            legs = []
            for i in range(len(swings) - 1):
                idx0, p0, k0 = swings[i]
                idx1, p1, k1 = swings[i + 1]
                direction = "UP" if p1 > p0 else "DOWN"
                dist_pct = abs(p1 - p0) / max(p0, 1e-10) * 100.0
                atr_at   = float(atr_vals[min(idx0, n - 1)])
                dist_atr = abs(p1 - p0) / max(atr_at, 1e-10)
                bar_count = max(1, idx1 - idx0)

                seg_vols   = volumes[idx0: idx1 + 1]
                vol_exp    = float(seg_vols.mean()) / max(avg_vol, 1e-10)
                seg_bodies = np.abs(closes[idx0: idx1 + 1] - opens[idx0: idx1 + 1])
                seg_ranges = np.maximum(highs[idx0: idx1 + 1] - lows[idx0: idx1 + 1], 1e-10)
                body_ratio = float(np.mean(seg_bodies / seg_ranges))
                speed_norm = min(1.0, (dist_pct / bar_count) / 0.4)
                energy     = min(1.0, speed_norm * 0.4 + body_ratio * 0.4 + min(1.0, vol_exp / 1.5) * 0.2)

                if   dist_atr >= 2.0 and energy >= 0.55: character = "IMPULSIVE"
                elif dist_atr <= 1.0 or  energy <= 0.35: character = "CORRECTIVE"
                else:                                     character = "NEUTRAL"

                dist_norm    = min(1.0, dist_atr / 8.0)
                anchor_score = min(100.0, (dist_norm * 0.5 + energy * 0.35 + 0.15) * 100.0) \
                               if character == "IMPULSIVE" else min(100.0, energy * dist_atr * 5.0)

                legs.append({
                    "idx0": idx0, "p0": p0, "idx1": idx1, "p1": p1,
                    "direction": direction, "dist_atr": round(dist_atr, 2),
                    "dist_pct": round(dist_pct, 3), "energy": round(energy, 3),
                    "character": character, "anchor_score": round(anchor_score, 1),
                    "bar_count": bar_count, "vol_exp": round(vol_exp, 3),
                })

            # ── Asset Profile (percentile distributions from all historical legs) ──
            # Build once from all available legs — downstream uses this to annotate
            # measurements with "how unusual is this for THIS asset?" context.
            def _pct_rank(value, sorted_vals):
                if len(sorted_vals) < 3: return 50
                idx = int(np.searchsorted(sorted_vals, value, side="right"))
                return max(0, min(100, round(idx / len(sorted_vals) * 100)))

            _leg_atrs_sorted   = sorted(l["dist_atr"] for l in legs)
            _leg_energy_sorted = sorted(l["energy"]   for l in legs)
            _vol_exp_sorted    = sorted(l["vol_exp"]  for l in legs)

            # Balance width distribution: corrective leg pairs proxy historical auctions
            _bal_widths: list = []
            for _i in range(1, len(legs)):
                if (legs[_i]["character"] in ("CORRECTIVE", "NEUTRAL") and
                        legs[_i - 1]["character"] in ("CORRECTIVE", "NEUTRAL")):
                    _hi = max(legs[_i]["p0"], legs[_i]["p1"],
                              legs[_i - 1]["p0"], legs[_i - 1]["p1"])
                    _lo = min(legs[_i]["p0"], legs[_i]["p1"],
                              legs[_i - 1]["p0"], legs[_i - 1]["p1"])
                    _atr_at = float(atr_vals[min(legs[_i - 1]["idx0"], n - 1)])
                    if _atr_at > 0 and _hi > _lo:
                        _bal_widths.append((_hi - _lo) / _atr_at)
            _bal_width_sorted = sorted(_bal_widths)

            # Participation distribution: rolling windows of 6 legs across full history
            _part_samples: list = []
            for _i in range(6, len(legs)):
                _rec = legs[_i - 6: _i]
                _part_samples.append(float(np.mean([l["energy"] for l in _rec])) * 100.0)
            _part_sorted = sorted(_part_samples)

            n_legs_profile = len(legs)

            # Annotate each leg dict with percentile rank (readable by hypothesis engine)
            for l in legs:
                l["dist_atr_pct"] = _pct_rank(l["dist_atr"], _leg_atrs_sorted)
                l["energy_pct"]   = _pct_rank(l["energy"],   _leg_energy_sorted)
                l["vol_exp_pct"]  = _pct_rank(l["vol_exp"],  _vol_exp_sorted)

            # ── State engine ──────────────────────────────────────────────────
            lookback = 6
            recent_legs = legs[-lookback:] if legs else []
            leg_comp = round(float(np.mean([l["energy"] for l in recent_legs])) * 100.0, 1) if recent_legs else 0.0

            atr_comp = vol_comp = 50.0
            if n >= 20:
                ra, ea = float(atr_vals[-5:].mean()), float(atr_vals[-20:-5].mean())
                atr_comp = round(min(100.0, max(0.0, (ra / max(ea, 1e-10) - 0.5) * 100.0)), 1)
                rv, ev   = float(volumes[-5:].mean()), float(volumes[-20:-5].mean())
                vol_comp = round(min(100.0, max(0.0, (rv / max(ev, 1e-10) - 0.5) * 100.0)), 1)

            participation = round(leg_comp * 0.40 + atr_comp * 0.25 + vol_comp * 0.20 + 50.0 * 0.15, 1) if recent_legs else 0.0

            w_sum = w_total = 0.0
            for leg in recent_legs:
                sign = 1.0 if leg["direction"] == "UP" else -1.0
                w    = leg["dist_atr"] * leg["energy"]
                w_sum += sign * w; w_total += w
            direction_score = round(max(-100.0, min(100.0, w_sum / max(w_total, 1e-10) * 100.0)), 1) if recent_legs else 0.0

            if n >= 20:
                ra, ea = float(atr_vals[-5:].mean()), float(atr_vals[-20:-5].mean())
                atr_trend = "EXPANDING" if ra > ea * 1.15 else ("CONTRACTING" if ra < ea * 0.85 else "FLAT")
                rv, ev    = float(volumes[-5:].mean()), float(volumes[-20:-5].mean())
                vol_trend = "RISING" if rv > ev * 1.2 else ("FALLING" if rv < ev * 0.8 else "FLAT")
            else:
                atr_trend = vol_trend = "FLAT"

            if   atr_trend == "CONTRACTING" and participation < 50: phase = "COMPRESSION"
            elif atr_trend == "EXPANDING"   and participation > 65: phase = "EXPANSION"
            else:                                                    phase = "UNKNOWN"

            def _lbl(leg):
                c = "IMP" if leg["character"] == "IMPULSIVE" else ("COR" if leg["character"] == "CORRECTIVE" else "NEU")
                return f"{c}_{leg['direction']}"
            leg_seq = " -> ".join(_lbl(l) for l in recent_legs[-5:]) if recent_legs else "(no legs)"

            # ── Geometry engine ───────────────────────────────────────────────
            hi_pts = [(s[0], s[1]) for s in swings if s[2] == "H"]
            lo_pts = [(s[0], s[1]) for s in swings if s[2] == "L"]

            def _fit(pts):
                if len(pts) < 2: return None
                xs = np.array([p[0] for p in pts], dtype=float)
                ys = np.array([p[1] for p in pts], dtype=float)
                xm, ym  = xs.mean(), ys.mean()
                ssxy = float(np.sum((xs - xm) * (ys - ym)))
                ssxx = float(np.sum((xs - xm) ** 2))
                if ssxx < 1e-10: return {"slope_raw": 0.0, "slope_norm": 0.0, "intercept": ym, "r2": 1.0, "tc": len(pts)}
                sr = ssxy / ssxx
                ic = ym - sr * xm
                yp = sr * xs + ic
                ssr = float(np.sum((ys - yp) ** 2))
                sst = float(np.sum((ys - ym) ** 2))
                r2  = max(0.0, 1.0 - ssr / max(sst, 1e-10))
                return {"slope_raw": sr, "slope_norm": sr / max(atr_now, 1e-10), "intercept": ic, "r2": round(r2, 3), "tc": len(pts)}

            geo_ok = len(hi_pts) >= 2 and len(lo_pts) >= 2
            upper = _fit(hi_pts[-5:]) if geo_ok else None
            lower = _fit(lo_pts[-5:]) if geo_ok else None

            upper_now = lower_now = 0.0
            compression = parallelism = 0.0
            is_converging = is_diverging = is_parallel = False
            breakout_up = breakout_dn = False
            structure_axis = "UNKNOWN"
            measurement_quality = 0.0

            if geo_ok and upper and lower:
                upper_now = upper["slope_raw"] * (n - 1) + upper["intercept"]
                lower_now = lower["slope_raw"] * (n - 1) + lower["intercept"]
                sb = min(swings[-min(10, len(swings))][0], max(0, n - 50))
                ul = upper["slope_raw"] * sb + upper["intercept"]
                ll = lower["slope_raw"] * sb + lower["intercept"]
                gap_l = max(1e-10, ul - ll)
                gap_r = max(1e-10, upper_now - lower_now)
                compression = round(max(0.0, min(1.0, 1.0 - gap_r / gap_l)), 3)

                FLAT = 0.05
                u_sn, l_sn = upper["slope_norm"], lower["slope_norm"]
                u_up, u_dn = u_sn > FLAT, u_sn < -FLAT
                l_up, l_dn = l_sn > FLAT, l_sn < -FLAT
                is_converging = u_dn and l_up
                is_diverging  = u_up and l_dn
                sa = abs(u_sn) + abs(l_sn)
                parallelism   = round(max(0.0, min(1.0, 1.0 - abs(u_sn - l_sn) / max(sa, 1e-10))), 3)
                is_parallel   = parallelism > 0.85 and not is_converging and not is_diverging

                breakout_up = current_price > upper_now * 1.002
                breakout_dn = current_price < lower_now * 0.998
                measurement_quality = round((upper["r2"] + lower["r2"]) / 2, 3)

                if is_converging:
                    structure_axis = "CONTRACTING"
                elif is_diverging:
                    structure_axis = "EXPANDING"
                elif is_parallel and u_up and l_up:
                    structure_axis = "ASCENDING"
                elif is_parallel and u_dn and l_dn:
                    structure_axis = "DESCENDING"
                elif (u_up or u_dn) != (l_up or l_dn):
                    structure_axis = "ASYMMETRIC"
                else:
                    structure_axis = "HORIZONTAL"

            # ── Auction engine ────────────────────────────────────────────────
            bal_legs = []
            for leg in reversed(legs[-8:]):
                if leg["character"] in ("CORRECTIVE", "NEUTRAL") or leg["energy"] < 0.5:
                    bal_legs.insert(0, leg)
                else:
                    break

            if len(bal_legs) >= 2:
                bal_high    = max(max(l["p0"], l["p1"]) for l in bal_legs)
                bal_low     = min(min(l["p0"], l["p1"]) for l in bal_legs)
                anchor_bar  = bal_legs[0]["idx0"]
                created_why = f"Balance ({len(bal_legs)} corrective legs)"
            elif legs:
                last = legs[-1]
                bal_high    = max(last["p0"], last["p1"])
                bal_low     = min(last["p0"], last["p1"])
                anchor_bar  = last["idx0"]
                created_why = "No balance — using last leg range"
            else:
                bal_high   = float(df["high"].iloc[-20:].max())
                bal_low    = float(df["low"].iloc[-20:].min())
                anchor_bar = max(0, n - 20)
                created_why = "No legs — using recent range"

            bal_width_atr = (bal_high - bal_low) / max(atr_now, 1e-10)
            anchor_price  = (bal_high + bal_low) / 2.0
            auction_age   = n - 1 - anchor_bar
            maturity      = round(min(1.0, auction_age / 30.0), 2)

            # Impulse anchor for Fib
            fib_high = fib_low = None
            anchor_leg = None
            for leg in reversed(legs):
                if leg["idx1"] <= anchor_bar and leg["character"] == "IMPULSIVE":
                    anchor_leg = leg
                    fib_high   = max(leg["p0"], leg["p1"])
                    fib_low    = min(leg["p0"], leg["p1"])
                    break

            # VP within auction window
            poc = vah = val = None
            seg = df.iloc[max(0, anchor_bar):]
            if len(seg) >= 5:
                seg_h = float(seg["high"].max()); seg_l = float(seg["low"].min())
                if seg_h > seg_l:
                    N = 24; bsz = (seg_h - seg_l) / N; bins = np.zeros(N)
                    for _, row in seg.iterrows():
                        bh, bl, bv = float(row["high"]), float(row["low"]), float(row.get("volume", 0))
                        for b in range(N):
                            blo = seg_l + b * bsz; bhi = blo + bsz
                            if bh >= blo and bl <= bhi:
                                bins[b] += bv / max(1, int((bh - bl) / bsz + 1))
                    pb = int(np.argmax(bins))
                    poc = round(seg_l + (pb + 0.5) * bsz, 8)
                    tot = bins.sum(); tgt = tot * 0.70; vb = hb = pb; acc = bins[pb]; lo_b, hi_b = pb - 1, pb + 1
                    while acc < tgt:
                        lv = bins[lo_b] if lo_b >= 0 else -1.0
                        hv = bins[hi_b] if hi_b < N else -1.0
                        if lv <= 0 and hv <= 0: break
                        if lv >= hv: acc += lv; vb = lo_b; lo_b -= 1
                        else:        acc += hv; hb = hi_b; hi_b += 1
                    vah = round(seg_l + (hb + 1) * bsz, 8)
                    val = round(seg_l + vb * bsz, 8)

            if   current_price > bal_high * 1.005 or current_price < bal_low * 0.995: auction_state = "INACTIVE"
            elif bal_width_atr < 0.8:                                                  auction_state = "BALANCING"
            else:                                                                       auction_state = "ACTIVE"

            # ── Hypothesis engine ─────────────────────────────────────────────
            hypotheses = []
            if geo_ok and upper and lower and measurement_quality >= 0.3:
                FLAT = 0.05
                u_sn, l_sn = upper["slope_norm"], lower["slope_norm"]
                u_up, u_dn = u_sn > FLAT, u_sn < -FLAT
                l_up, l_dn = l_sn > FLAT, l_sn < -FLAT
                u_fl = not u_up and not u_dn
                l_fl = not l_up and not l_dn

                candidates = []  # (name, bias, because, against, missing, next_event, entry_trigger)
                if is_converging:
                    if u_fl and l_up:
                        candidates.append(("Ascending Triangle", "LONG",
                            ["Flat resistance", "Rising support", "Converging"],
                            (["Market direction bearish"] if direction_score < -30 else []),
                            ([] if breakout_up else ["Breakout above flat resistance"]),
                            "Retest of flat resistance — volume should decline into test",
                            "Impulsive close above flat resistance with volume >=1.5x avg"))
                    elif u_dn and l_fl:
                        candidates.append(("Descending Triangle", "SHORT",
                            ["Falling resistance", "Flat support", "Converging"],
                            (["Market direction bullish"] if direction_score > 30 else []),
                            ([] if breakout_dn else ["Breakout below flat support"]),
                            "Retest of flat support — volume should decline into test",
                            "Impulsive close below flat support with volume >=1.5x avg"))
                    elif u_dn and l_up:
                        bias = "LONG" if breakout_up else ("SHORT" if breakout_dn else "NEUTRAL")
                        candidates.append(("Symmetrical Triangle", bias,
                            ["Upper falling", "Lower rising", f"Convergence {compression:.0%}"],
                            [],
                            ([] if (breakout_up or breakout_dn) else ["Decisive breakout through either side"]),
                            "Apex approach — indecision builds; one side will give way",
                            "Decisive close through either trendline on expanding volume"))
                    elif u_up and l_up:
                        candidates.append(("Rising Wedge", "SHORT",
                            ["Both lines rising", "Converging"],
                            (["Direction bullish — fights bearish thesis"] if direction_score > 30 else []),
                            ([] if breakout_dn else ["Break below lower boundary"]),
                            "Continued compression; possible final thrust high (throw-over)",
                            "Close below lower wedge boundary on bearish momentum shift"))
                    elif u_dn and l_dn:
                        candidates.append(("Falling Wedge", "LONG",
                            ["Both lines falling", "Converging"],
                            (["Direction bearish — fights bullish thesis"] if direction_score < -30 else []),
                            ([] if breakout_up else ["Break above upper boundary"]),
                            "Continued compression; possible final thrust low (spring)",
                            "Close above upper wedge boundary on bullish momentum shift"))
                    if compression > 0.65:
                        candidates.append(("Compression", "NEUTRAL",
                            [f"High compression ({compression:.0%})", "Converging"],
                            [],
                            ["Directional resolution — break either way"],
                            "Apex approach — maximum indecision before resolution",
                            "First impulsive close outside compression zone — direction defines bias"))

                if is_parallel:
                    if u_up and l_up:
                        candidates.append(("Bull Channel", "LONG",
                            ["Both lines rising", "Parallel (uptrend)"],
                            (["Direction turned bearish"] if direction_score < -30 else []),
                            (["Price at lower channel for pullback entry"] if abs(current_price - upper_now) < abs(current_price - lower_now) else []),
                            "Pullback to lower channel boundary",
                            "Bullish reversal candle at lower channel with rising volume"))
                    elif u_dn and l_dn:
                        candidates.append(("Bear Channel", "SHORT",
                            ["Both lines falling", "Parallel (downtrend)"],
                            (["Direction turned bullish"] if direction_score > 30 else []),
                            (["Price at upper channel for pullback entry"] if abs(current_price - lower_now) < abs(current_price - upper_now) else []),
                            "Pullback to upper channel boundary",
                            "Bearish reversal candle at upper channel with rising volume"))
                    elif u_fl and l_fl:
                        bias = "LONG" if breakout_up else ("SHORT" if breakout_dn else "NEUTRAL")
                        candidates.append(("Rectangle", bias,
                            ["Flat resistance", "Flat support", "Parallel (range)"],
                            [],
                            ([] if (breakout_up or breakout_dn) else ["Breakout outside range"]),
                            "Test of either boundary — direction of breakout is the trade",
                            "Close outside balance zone on volume >=1.3x avg"))

                for name, bias, because, against, missing, nxt, trigger in candidates:
                    if   breakout_up and bias == "LONG":  status = "CONFIRMED"
                    elif breakout_dn and bias == "SHORT": status = "CONFIRMED"
                    elif breakout_up and bias == "SHORT": status = "FAILED"; against.append("Price broke UP — thesis invalidated")
                    elif breakout_dn and bias == "LONG":  status = "FAILED"; against.append("Price broke DOWN — thesis invalidated")
                    else:                                  status = "FORMING"

                    req_score = len(because) / max(len(because) + len(missing), 1)
                    conf      = round(max(0.0, min(1.0, req_score * 0.65 + measurement_quality * 0.20 + 0.15)), 3)
                    if status == "FAILED": conf *= 0.3

                    invalidation = lower_now if bias == "LONG" else upper_now if bias == "SHORT" \
                                   else (lower_now if current_price > (upper_now + lower_now) / 2 else upper_now)
                    dist_pct = abs(current_price - invalidation) / max(abs(current_price), 1e-10) * 100.0
                    urgency  = round(max(0.0, min(1.0, 1.0 - dist_pct / 2.5)), 3)

                    if conf >= 0.35:
                        hypotheses.append({
                            "name": name, "bias": bias, "confidence": conf,
                            "status": status, "because": because, "against": against,
                            "missing": missing, "urgency": urgency,
                            "invalidation": round(invalidation, 8),
                            "dist_pct": round(dist_pct, 2),
                            "expected_next_event": nxt,
                            "expected_entry_trigger": trigger,
                        })
                hypotheses.sort(key=lambda h: -h["confidence"])

            # ── Opportunity engine ────────────────────────────────────────────
            opportunity = None
            if hypotheses and auction_state != "FAILED":
                top_h = hypotheses[0]
                if top_h["confidence"] >= 0.35 and top_h["bias"] != "NEUTRAL":
                    bias = top_h["bias"]
                    if vah and val:
                        entry_lo, entry_hi = (min(val, bal_low * 1.003), val * 1.005) if bias == "LONG" \
                                             else (vah * 0.995, max(vah, bal_high * 0.997))
                    else:
                        entry_lo, entry_hi = (bal_low, bal_low * 1.005) if bias == "LONG" \
                                             else (bal_high * 0.995, bal_high)
                    inv = top_h["invalidation"]
                    if top_h["status"] == "CONFIRMED" and fib_high and fib_low:
                        impulse_r = fib_high - fib_low
                        target    = (entry_lo + impulse_r) if bias == "LONG" else (entry_hi - impulse_r)
                        rprofile  = "BREAKOUT_RETRACE"
                    elif abs(current_price - bal_high) < abs(current_price - bal_low):
                        target   = bal_low if bias == "SHORT" else bal_high
                        rprofile = "AT_BALANCE_EXTREME" if bias == "SHORT" else "MID_BALANCE"
                    else:
                        target   = bal_high if bias == "LONG" else bal_low
                        rprofile = "AT_BALANCE_EXTREME" if bias == "LONG" else "MID_BALANCE"

                    risk_d   = abs((entry_lo if bias == "LONG" else entry_hi) - inv)
                    reward_d = abs(target - (entry_lo if bias == "LONG" else entry_hi))
                    srr      = round(reward_d / max(risk_d, 1e-10), 2)

                    quality = "A+" if top_h["confidence"] >= 0.70 and srr >= 2.0 else \
                              "A"  if top_h["confidence"] >= 0.55 and srr >= 1.5 else \
                              "B"  if top_h["confidence"] >= 0.40 and srr >= 1.0 else "C"

                    opportunity = {
                        "name": top_h["name"], "bias": bias, "quality": quality,
                        "entry_zone": (round(entry_lo, 8), round(entry_hi, 8)),
                        "invalidation": round(inv, 8), "target": round(target, 8),
                        "srr": srr, "profile": rprofile,
                        "urgency": top_h["urgency"],
                        "trigger": top_h["expected_entry_trigger"],
                    }

            # ── Format output ─────────────────────────────────────────────────
            dir_lbl  = "BULLISH" if direction_score > 30 else ("BEARISH" if direction_score < -30 else "NEUTRAL")
            part_lbl = "HIGH" if participation > 65 else ("LOW" if participation < 35 else "MODERATE")

            # Asset context header — only show when we have enough history for reliable percentiles
            _asset_ctx_lines: list = []
            if n_legs_profile >= 8:
                _imp_p50 = _leg_atrs_sorted[len(_leg_atrs_sorted) // 2] if _leg_atrs_sorted else 0.0
                _bal_p50 = _bal_width_sorted[len(_bal_width_sorted) // 2] if _bal_width_sorted else 0.0
                _asset_ctx_lines = [
                    f"ASSET CONTEXT [{symbol} {timeframe} — {n_legs_profile} legs from {n} bars]:",
                    f"  Typical impulse: {_imp_p50:.1f} ATR (p50) | "
                    f"Typical balance width: {_bal_p50:.1f} ATR (p50)",
                    f"  Percentile ranks below are specific to {symbol} — 90th = unusual for this asset.",
                    "",
                ]

            _part_pct = _pct_rank(participation, _part_sorted)
            _part_pct_str = f" | {_part_pct}th pct" if n_legs_profile >= 8 else ""

            out = [
                f"MARKET SURVEY — {symbol} | {timeframe} | Price: {current_price:.6f}",
                f"Swings: {len(swings)} | Legs: {len(legs)} | ATR: {atr_now:.6f}",
                "",
                *_asset_ctx_lines,
                "MARKET STATE:",
                f"  Participation : {participation:.0f}/100 [{part_lbl}{_part_pct_str}]"
                f" [legs={leg_comp:.0f} atr={atr_comp:.0f} vol={vol_comp:.0f}]",
                f"  Direction     : {direction_score:+.0f}/100 [{dir_lbl}]",
                f"  Phase         : {phase} | ATR {atr_trend} | Volume {vol_trend}",
                f"  Leg sequence  : {leg_seq}",
                "",
            ]

            if geo_ok and upper and lower:
                geo_shape = ("CONVERGING" if is_converging else "PARALLEL" if is_parallel else
                             "DIVERGING" if is_diverging else "OPEN")
                out += [
                    "GEOMETRY:",
                    f"  Shape      : {geo_shape} | Axis: {structure_axis} | Compression: {compression:.0%}",
                    f"  Upper line : slope_norm={upper['slope_norm']:+.5f} R2={upper['r2']:.2f} ({upper['tc']} pts)",
                    f"  Lower line : slope_norm={lower['slope_norm']:+.5f} R2={lower['r2']:.2f} ({lower['tc']} pts)",
                ]
                if breakout_up: out.append("  BREAKOUT UP — price above upper trendline")
                if breakout_dn: out.append("  BREAKOUT DOWN — price below lower trendline")
                out += [f"  Measurement quality: {measurement_quality:.0%}", ""]

            _bal_pct_str = ""
            if n_legs_profile >= 8 and _bal_width_sorted:
                _bal_pct = _pct_rank(bal_width_atr, _bal_width_sorted)
                _bal_pct_str = f" | {_bal_pct}th pct width for {symbol}"
            out += [
                f"ACTIVE AUCTION [{auction_state}] | Maturity: {maturity:.0%}:",
                f"  Balance zone : {bal_low:.6f} – {bal_high:.6f} ({bal_width_atr:.1f} ATR wide{_bal_pct_str})",
                f"  Anchor price : {anchor_price:.6f} | Age: {auction_age} bars",
            ]
            if poc:
                out.append(f"  VP           : POC={poc:.6f} | VAH={vah:.6f} | VAL={val:.6f}")
            if fib_high and fib_low:
                out.append(f"  Fib anchors  : {fib_low:.6f} – {fib_high:.6f}")
            out += [f"  Created      : {created_why}", ""]

            if hypotheses:
                out.append(f"HYPOTHESES — {len(hypotheses)} identified:")
                for i, h in enumerate(hypotheses[:3], 1):
                    out.append(f"  [{i}] {h['name']} | bias={h['bias']} | confidence={h['confidence']:.0%} | {h['status']}")
                    out.append(f"       Invalidation: {h['invalidation']:.6f} ({h['dist_pct']:.1f}% away) | urgency={h['urgency']:.0%}")
                    if h["because"]:  out.append(f"       FOR    : {' | '.join(h['because'][:3])}")
                    if h["against"]:  out.append(f"       AGAINST: {' | '.join(h['against'][:2])}")
                    if h["missing"]:  out.append(f"       MISSING: {' | '.join(h['missing'][:2])} (not yet, not wrong)")
                    if h["expected_next_event"]:    out.append(f"       NEXT   : {h['expected_next_event']}")
                    if h["expected_entry_trigger"]: out.append(f"       TRIGGER: {h['expected_entry_trigger']}")
                    out.append("")
            else:
                out += ["HYPOTHESES: No pattern cleared the 35% confidence threshold (geometry noisy or insufficient).", ""]

            if opportunity:
                o = opportunity
                out += [
                    f"OPPORTUNITY [{o['quality']}] — {o['name']} | {o['bias']}:",
                    f"  Entry zone    : {o['entry_zone'][0]:.6f} – {o['entry_zone'][1]:.6f}",
                    f"  Invalidation  : {o['invalidation']:.6f} | Target: {o['target']:.6f}",
                    f"  Structural R:R: {o['srr']:.1f}R | Profile: {o['profile']}",
                    f"  Urgency: {o['urgency']:.0%}",
                    f"  Entry trigger : {o['trigger']}",
                ]
            else:
                out.append("OPPORTUNITY: No actionable setup at current threshold.")

            return "\n".join(out)

        except Exception as e:
            return f"Market survey failed for {symbol} {timeframe}: {e}"

    async def annotate_trade(
        self,
        row_number: int,
        note: str,
    ) -> str:
        """
        Writes a post-trade reflection note to column S of a specific row in the Trade Log sheet.
        Use this after reviewing a closed trade to record what you got right, what you missed,
        or what you'd do differently next time. Call get_trade_performance first to find the row number.
        ALWAYS write honest notes — this is your own learning record, not PR copy.
        :param row_number: Row number from the Trade Log (shown in get_trade_performance output)
        :param note: Your honest reflection on this trade (1-3 sentences max)
        """
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            CREDS_FILE = r"C:\ChevTools\google_credentials.json"
            SHEET_ID   = "1V1b2aU3SJu_R7VjFKGp9J6uFwucGSamhRWyq6jgCbFs"
            SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]

            creds  = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
            client = gspread.authorize(creds)
            ws     = client.open_by_key(SHEET_ID).worksheet("Trade Log")
            ws.update_cell(row_number, 19, note)
            return f"Note saved to Trade Log row {row_number}: \"{note}\""
        except ImportError:
            return "ERROR: gspread or google-auth not installed in Open WebUI's Python environment."
        except Exception as e:
            return f"ERROR writing note to sheet: {e}"
