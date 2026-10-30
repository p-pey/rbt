"""Typed configuration models. Defaults are documented in docs/STRATEGY_SPEC.md.

Values marked PLACEHOLDER were not supplied by the user and must be confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xauassist.domain.enums import MaMethod, SameCandleRule, Timeframe, ZoneMode


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _minutes_of_day(text: str, where: str) -> int:
    try:
        hours, minutes = text.split(":")
        value = int(hours) * 60 + int(minutes)
    except ValueError as exc:
        raise ConfigError(f"{where}: expected HH:MM, got {text!r}") from exc
    _require(0 <= value < 24 * 60 and 0 <= int(minutes) < 60, f"{where}: invalid time {text!r}")
    return value


@dataclass(frozen=True, slots=True)
class MaConfig:
    period: int = 20
    method: MaMethod = MaMethod.EMA
    warmup_multiplier: int = 5  # no signals until period * multiplier bars have been seen

    def __post_init__(self) -> None:
        _require(self.period >= 2, "ma.period must be >= 2")
        _require(self.warmup_multiplier >= 1, "ma.warmup_multiplier must be >= 1")

    @property
    def label(self) -> str:
        return f"{self.method.value}{self.period}"


@dataclass(frozen=True, slots=True)
class PinBarConfig:
    wick_body_ratio: float = 2.0  # dominant wick >= ratio * body
    opposite_wick_max_frac: float = 0.30  # opposite wick <= frac * range
    close_zone_frac: float = 0.33  # "clear rejection": close within this fraction of range extreme
    min_body_frac: float = 0.05  # body floor (fraction of range) for zero-body safety
    min_range: float = 0.0  # PLACEHOLDER: minimum candle range in price units

    def __post_init__(self) -> None:
        _require(self.wick_body_ratio > 0, "pin_bar.wick_body_ratio must be > 0")
        _require(0 <= self.opposite_wick_max_frac <= 1, "pin_bar.opposite_wick_max_frac in [0,1]")
        _require(0 <= self.close_zone_frac <= 1, "pin_bar.close_zone_frac in [0,1]")
        _require(0 < self.min_body_frac <= 1, "pin_bar.min_body_frac in (0,1]")
        _require(self.min_range >= 0, "pin_bar.min_range must be >= 0")


@dataclass(frozen=True, slots=True)
class EngulfingConfig:
    strict: bool = False  # True: open/close must strictly exceed the previous body
    min_body: float = 0.0  # minimum engulfing body in price units

    def __post_init__(self) -> None:
        _require(self.min_body >= 0, "engulfing.min_body must be >= 0")


@dataclass(frozen=True, slots=True)
class TrendConfig:
    slope_filter_enabled: bool = True
    slope_lookback: int = 5
    min_slope: float = 0.0  # minimum |MA change| per bar, price units
    htf_enabled: bool = False
    htf_timeframe: Timeframe = Timeframe.M15
    htf_slope_lookback: int = 3

    def __post_init__(self) -> None:
        _require(self.slope_lookback >= 1, "trend.slope_lookback must be >= 1")
        _require(self.min_slope >= 0, "trend.min_slope must be >= 0")
        _require(self.htf_slope_lookback >= 1, "trend.htf_slope_lookback must be >= 1")


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    mode: ZoneMode = ZoneMode.ATR
    value: float = 0.30  # PLACEHOLDER: 0.30 * ATR(14) until the user supplies a value
    atr_period: int = 14

    def __post_init__(self) -> None:
        _require(self.value > 0, "zone.value must be > 0")
        _require(self.atr_period >= 1, "zone.atr_period must be >= 1")


@dataclass(frozen=True, slots=True)
class ScenarioAConfig:
    enabled: bool = True
    prior_bars_below: int = (
        3  # closes before the confirmation candle that must be on the trend side
    )
    close_tolerance: float = 0.0  # confirmation close may exceed MA by this much (price units)
    touch_lookback: int = 1  # zone touch may occur on the confirmation candle or this many before

    def __post_init__(self) -> None:
        _require(self.prior_bars_below >= 1, "scenario_a.prior_bars_below must be >= 1")
        _require(self.close_tolerance >= 0, "scenario_a.close_tolerance must be >= 0")
        _require(self.touch_lookback >= 0, "scenario_a.touch_lookback must be >= 0")


@dataclass(frozen=True, slots=True)
class ScenarioBConfig:
    enabled: bool = True
    breakout_buffer: float = 0.0  # close must exceed MA by this much to count as a break
    max_pullback_bars: int = 10
    require_close_reenter: bool = True  # confirmation must close back on the original trend side
    close_tolerance: float = 0.0
    touch_lookback: int = 1

    def __post_init__(self) -> None:
        _require(self.breakout_buffer >= 0, "scenario_b.breakout_buffer must be >= 0")
        _require(self.max_pullback_bars >= 1, "scenario_b.max_pullback_bars must be >= 1")
        _require(self.close_tolerance >= 0, "scenario_b.close_tolerance must be >= 0")
        _require(self.touch_lookback >= 0, "scenario_b.touch_lookback must be >= 0")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    timeframe: Timeframe = Timeframe.M5
    ma: MaConfig = field(default_factory=MaConfig)
    pin_bar: PinBarConfig = field(default_factory=PinBarConfig)
    engulfing: EngulfingConfig = field(default_factory=EngulfingConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    zone: ZoneConfig = field(default_factory=ZoneConfig)
    scenario_a: ScenarioAConfig = field(default_factory=ScenarioAConfig)
    scenario_b: ScenarioBConfig = field(default_factory=ScenarioBConfig)

    def __post_init__(self) -> None:
        if self.trend.htf_enabled:
            _require(
                self.trend.htf_timeframe.minutes > self.timeframe.minutes
                and self.trend.htf_timeframe.minutes % self.timeframe.minutes == 0,
                "trend.htf_timeframe must be a larger multiple of strategy.timeframe",
            )


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    symbol_pattern: str = r"XAUUSD[\.\w]*"  # matches XAUUSD, XAUUSD.a, XAUUSDm
    expected_timeframe: Timeframe = Timeframe.M5
    max_spread_points: float = 50.0  # PLACEHOLDER: maximum spread in broker points

    def __post_init__(self) -> None:
        _require(self.max_spread_points >= 0, "validation.max_spread_points must be >= 0")


@dataclass(frozen=True, slots=True)
class SignalsConfig:
    cooldown_bars: int = 3  # PLACEHOLDER: min bars between signals in the same direction (0 = off)

    def __post_init__(self) -> None:
        _require(self.cooldown_bars >= 0, "signals.cooldown_bars must be >= 0")


@dataclass(frozen=True, slots=True)
class Mt5Config:
    terminal_path: str = ""  # optional path to terminal64.exe; empty = use the running terminal
    # Broker server time minus UTC, e.g. 2.0 (winter) / 3.0 (summer) for typical NY-close brokers.
    # Fixed on purpose: a changing offset would shift the candle timeline. A mismatch with the
    # server's tick clock is reported as a warning (Mt5Provider.offset_warning) - update it on DST.
    server_utc_offset_hours: float = 0.0


@dataclass(frozen=True, slots=True)
class FeedConfig:
    poll_seconds: float = 2.0
    history_bars: int = 500  # candles fetched at start-up / when backfilling
    min_history_bars: int = 120  # minimum continuous history wanted for indicator warm-up
    reconnect_initial_seconds: float = 2.0
    reconnect_max_seconds: float = 60.0
    backfill_retries: int = 3  # polls to wait for missing candles to appear before giving up
    daily_break_max_minutes: int = 90  # longest missing stretch accepted as the daily break ...
    daily_break_window_start: str = "21:00"  # ... if it lies inside this window (candle-time clock,
    daily_break_window_end: str = "01:00"  # HH:MM; may cross midnight). Broker/DST dependent.
    weekend_max_hours: int = 80  # a gap across Saturday up to this long is a weekend closure
    stale_tick_seconds: float = 180.0  # no tick for this long (and no new candle) = market closed

    def __post_init__(self) -> None:
        _require(self.poll_seconds > 0, "feed.poll_seconds must be > 0")
        _require(self.history_bars >= self.min_history_bars >= 1, "feed history bar limits invalid")
        _require(
            0 < self.reconnect_initial_seconds <= self.reconnect_max_seconds,
            "feed reconnect seconds must satisfy 0 < initial <= max",
        )
        _require(self.backfill_retries >= 0, "feed.backfill_retries must be >= 0")
        _require(self.daily_break_max_minutes >= 0, "feed.daily_break_max_minutes must be >= 0")
        for name in ("daily_break_window_start", "daily_break_window_end"):
            _minutes_of_day(getattr(self, name), f"feed.{name}")
        _require(self.weekend_max_hours >= 0, "feed.weekend_max_hours must be >= 0")
        _require(self.stale_tick_seconds > 0, "feed.stale_tick_seconds must be > 0")

    @property
    def daily_break_window_minutes(self) -> tuple[int, int]:
        """(start, end) as minutes since midnight."""
        return (
            _minutes_of_day(self.daily_break_window_start, "feed.daily_break_window_start"),
            _minutes_of_day(self.daily_break_window_end, "feed.daily_break_window_end"),
        )


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Behaviour only. The bot token and chat id live in the secret store, never here."""

    enabled: bool = False
    timeout_seconds: float = 10.0
    max_attempts: int = 5
    retry_initial_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    queue_size: int = 100

    def __post_init__(self) -> None:
        _require(self.timeout_seconds > 0, "telegram.timeout_seconds must be > 0")
        _require(self.max_attempts >= 1, "telegram.max_attempts must be >= 1")
        _require(
            0 < self.retry_initial_seconds <= self.retry_max_seconds,
            "telegram retry seconds must satisfy 0 < initial <= max",
        )
        _require(self.queue_size >= 1, "telegram.queue_size must be >= 1")


@dataclass(frozen=True, slots=True)
class SoundConfig:
    enabled: bool = True
    buy_sound: str = "assets/sounds/buy.wav"  # relative paths resolve against the app folder
    sell_sound: str = "assets/sounds/sell.wav"


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    timezone: str = "local"  # "local", "UTC" or an IANA name such as "Europe/Berlin"


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    # A signal whose candle closed longer ago than this is dropped (e.g. delivered after an outage).
    max_signal_age_seconds: float = 600.0
    # After a data hole, suppress signals for this many candles (indicators span the hole).
    post_gap_suppress_bars: int = 20
    history_size: int = 500  # signal records kept in memory for the UI

    def __post_init__(self) -> None:
        _require(self.max_signal_age_seconds > 0, "monitor.max_signal_age_seconds must be > 0")
        _require(self.post_gap_suppress_bars >= 0, "monitor.post_gap_suppress_bars must be >= 0")
        _require(self.history_size >= 1, "monitor.history_size must be >= 1")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Execution and scoring assumptions. Every value marked PLACEHOLDER is an unconfirmed guess."""

    point_size: float = 0.01  # price units per broker point (XAUUSD with 2 digits)
    assumed_spread_points: float = 20.0  # PLACEHOLDER: used when a candle has no recorded spread
    slippage_points: float = 0.0  # PLACEHOLDER: adverse slippage on entry and stop-loss fills
    sl_buffer: float = 0.0  # PLACEHOLDER: stop distance beyond the confirmation candle extreme
    r_multiple: float = 1.0  # PLACEHOLDER: take-profit = r_multiple x risk
    max_hold_bars: int = 48  # PLACEHOLDER: time exit / MFE-MAE horizon (48 x M5 = 4 hours)
    min_risk: float = 0.0  # signals with a smaller stop distance (price units) are skipped
    same_candle: SameCandleRule = SameCandleRule.STOP_FIRST  # stop AND target inside one candle
    exploration_fraction: float = 0.6  # chronological split: tune/explore here
    validation_fraction: float = 0.2  # ... confirm here; the remainder is the untouched test set

    def __post_init__(self) -> None:
        _require(self.point_size > 0, "backtest.point_size must be > 0")
        _require(self.assumed_spread_points >= 0, "backtest.assumed_spread_points must be >= 0")
        _require(self.slippage_points >= 0, "backtest.slippage_points must be >= 0")
        _require(self.sl_buffer >= 0, "backtest.sl_buffer must be >= 0")
        _require(self.r_multiple > 0, "backtest.r_multiple must be > 0")
        _require(self.max_hold_bars >= 1, "backtest.max_hold_bars must be >= 1")
        _require(self.min_risk >= 0, "backtest.min_risk must be >= 0")
        _require(
            0 < self.exploration_fraction
            and 0 < self.validation_fraction
            and self.exploration_fraction + self.validation_fraction < 1,
            "backtest fractions must be > 0 and leave room for a test set (sum < 1)",
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    signals: SignalsConfig = field(default_factory=SignalsConfig)
    mt5: Mt5Config = field(default_factory=Mt5Config)
    feed: FeedConfig = field(default_factory=FeedConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
