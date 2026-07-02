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

                all_resistance = collect_side(resistance_levels, tier_b_resistance, True)
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