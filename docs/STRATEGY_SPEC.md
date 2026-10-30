# Strategy Specification (Phase 1 output)

Status legend: **[C]** confirmed by user in the original brief. **[A]** accepted by user via "accept all defaults" (proposal approved).
**[?]** assumption made when accepting defaults — user never gave an explicit answer; **re-confirm before Phase 3**.

## Inputs
- Symbol: XAUUSD (regex `^XAUUSD[\.\w]*$`, covers `XAUUSD.a`, `XAUUSDm`) [C]
- Timeframe: M5 (configurable, warning emitted if not M5) [A]
- MA: period 20, applied price Close, EMA default, SMA selectable [C]
- EMA: alpha = 2/(N+1), seeded as MT5 does; warm-up >= 5N bars before any signal [A]

## Evaluation rule (no repaint, no lookahead) [C]
Let `i` = last CLOSED candle. Only candles with index <= i are visible to the engine. The forming candle is always dropped.
Live and backtest use the same engine, fed one closed candle at a time.

## Candle math [A]
- body = |C-O|; range = H-L; upper = H-max(O,C); lower = min(O,C)-L
- range == 0 -> candle rejected
- body_eff = max(body, min_body_frac * range)  (safe zero-body handling)

## Pin bar [A]  (bearish; bullish mirrors)
- upper >= pin_wick_body_ratio (2.0) * body_eff
- lower <= pin_opposite_wick_max_frac (0.30) * range
- range >= min_range
- close bearish (C<O) OR close in lowest pin_close_zone_frac (0.33) of range

## Engulfing [A]  (bearish; bullish mirrors)
- prev bullish, current bearish
- O[i] >= C[i-1] and C[i] <= O[i-1]  (non-strict: M5 gold opens ~= prior close)

## Trend [A]
- Bearish: MA[i] < MA[i-k], k = slope_lookback (configurable), optional min slope. Bullish mirrors.
- Optional HTF (M15) filter: same rule on closed M15 bars only (bar close time <= M5 signal time). Default OFF.

## Zone [A]
- [MA-d, MA+d]; d in points | percent | ATR multiple. Value not specified by user -> placeholder default `0.30 * ATR(14)`, **[?] user to supply real value**.

## Scenario A — trend + MA20 retest [A]
Bearish: touch (H[i] >= MA[i]-d, on confirmation candle or the one before), C[i] <= MA[i] (+tolerance),
previous n=3 closes below MA, trend filter passes, bearish pin/engulfing.

## Scenario B — break + pullback + rejection [A]/[?]
Bearish: trend filter passes on bar before breakout; breakout bar b: C[b] > MA[b] + buffer;
within P=10 bars price pulls back into zone from above; bearish confirmation.
- [?] Direction as literally written: bearish B = SELL on bearish candle after failed upside break (trend resumption), bullish B = BUY on bullish candle after failed downside break. Treated as intended.
- [?] Bearish B confirmation candle must close at or below MA (price re-enters bearish side). Bullish mirrors (close at or above MA).
- One signal per breakout; setup expires after P bars.
- A and B are mutually exclusive (A: no close beyond MA in context; B: at least one).

## Signal identity / dedup [A]
Key = (symbol, direction, scenario, confirmation_bar_open_time); persisted (SQLite) across restarts. Per-direction cooldown N bars.
One signal per confirmation candle per direction; if pin bar and engulfing both match, both listed in one signal.

## Validation gates [C]
symbol ok, timeframe ok, candle closed, MA interaction valid, trend ok, not duplicate, spread <= max, data complete (no gap inside session).
Live spread = current tick; backtest spread = bar's recorded spread. Max spread value: **[?] not provided — placeholder, user to supply.**

## Backtest conventions
- [A] Entry = NEXT bar open, spread applied (SELL at bid, BUY at ask; MT5 candles are bid).
- [?] Win/loss convention: SL beyond confirmation candle extreme + buffer, TP = R x risk. **R and buffer not provided — placeholders (R=1.0, buffer=0), user to supply.** SL and TP in same bar -> SL assumed first. MFE/MAE always reported, independent of SL/TP.
- Alerts do NOT include SL/TP by default (proposal, off).
- [A] Chronological split 60/20/20 (exploration / validation / final test); final test touched once.

## Time handling [A]
Signal time = candle CLOSE time, shown in local tz with broker offset noted.

## Secrets [A]
Telegram token/chat id: Windows Credential Manager (`keyring`) with git-ignored `secrets.toml` fallback. Never in source or `config.toml`.

## Implementation clarifications (Phase 3) — please review, these refine [A] items
1. **Trend bar:** trend (price on trend side of MA *and*, if enabled, MA slope) is evaluated on the bar BEFORE the confirmation candle (Scenario A) or BEFORE the breakout candle (Scenario B), because the retest/confirmation candle itself touches the MA.
2. **Zone mode "points" is implemented as `PRICE`** (half-width in price units, e.g. 0.50 = $0.50) to avoid broker point-size assumptions. `PERCENT` and `ATR` unchanged.
3. **A vs B precedence:** while a Scenario B breakout is pending (within `max_pullback_bars`), Scenario A is not evaluated for that direction. This makes A and B mutually exclusive. After the window expires, A may fire again.
4. **Breakout:** the first close beyond MA (+buffer) on a bar whose predecessor satisfied the trend. A new breakout while one is pending restarts the pullback window. One signal per breakout; if the HTF filter blocks a B confirmation, the setup stays pending until it expires.
5. **Pullback touch (B):** only candles AFTER the breakout candle count (confirmation candle, or the one before it if `touch_lookback=1`). Bearish B: `low <= MA + d`. Bullish B: `high >= MA - d`.
6. **HTF filter** is applied at signal time. M15 bars are aggregated from closed M5 candles and become visible only when their last M5 candle closes (no unfinished HTF bar can be seen). Rule: close vs HTF MA + HTF MA slope direction over `htf_slope_lookback` bars. Default OFF.
7. **Engine contract:** closed candles only (`IncompleteCandleError` otherwise), strictly increasing time (re-delivery of the last candle is ignored; an older candle raises `OutOfOrderCandleError`). No signals during warm-up (`period × warmup_multiplier` bars, min `atr_period+1`).
8. **Placeholders still needing real values:** `zone.value` (0.30×ATR), `validation.max_spread_points` (50), `signals.cooldown_bars` (3), `pin_bar.min_range` (0), backtest R-multiple/buffer (Phase 8).

## Backtest conventions as implemented (Phase 8) — supersedes the earlier [?] placeholder note
All are configurable under `[backtest]` and printed in every report. **Still unconfirmed by the user:** r_multiple, sl_buffer, max_hold_bars, assumed_spread_points, slippage_points (reports warn while they are at their placeholder values).
- Entry: next candle's open; SELL fills at bid, BUY at ask (= bid + spread). Skipped if a market gap separates signal and entry (the alert would be stale).
- Stop: beyond the confirmation candle's extreme (+ `sl_buffer`). Target: `r_multiple` x risk.
- SELL exits are tested against bid + spread (closing a short buys at the ask); BUY exits against the bid.
- One candle reaching both stop and target: `same_candle = STOP_FIRST` (pessimistic, default) or `TARGET_FIRST` (optimistic) to bracket the answer. On random-walk data with zero costs and tight stops this rule alone moves mean R by roughly -0.05 / +0.05.
- Stops that gap past their level fill at the worse open; targets fill at their level. Slippage is adverse on entry and stop fills.
- No exit within `max_hold_bars`: close at the last candle's close (TIME). MFE/MAE use the same horizon with executable prices, independent of stop/target.
- Win = pnl > 0 after costs. Trades are scored independently (overlap allowed): a signal-quality measure, not a portfolio simulation.
- Live rules reused unchanged: same engine, validator (spread gate uses the candle's recorded spread), de-duplication/cooldown, and the data-gap rule (setups cleared, signals suppressed for `monitor.post_gap_suppress_bars`).
