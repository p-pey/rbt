# Architecture (Phase 2)

## 1. MT5 integration: Architecture A vs B

| Concern | A: `MetaTrader5` Python package | B: MQL5 EA + local IPC (HTTP/socket) |
|---|---|---|
| Moving parts | Python + running terminal | Python server + EA + terminal + WebRequest/socket permissions |
| Reliability | Official, maintained; IPC handled by package | Custom protocol, EA must be attached to a chart, can be removed/disabled by user |
| Data access | `copy_rates_from_pos`, `symbol_info_tick`, history for backtest | Only what EA pushes; backtest history needs separate export |
| Failure handling | `initialize()` / `last_error()` / `terminal_info().connected` | Must invent heartbeats, framing, reconnects on both sides |
| Platform | Windows only; terminal must be running | Terminal still required; MT4 possible |
| MT4 future | Not supported (no official package) | Natural fit |

**Decision: A for v1.** Fewer failure modes, official support, history access for backtesting. It still needs the terminal running and logged in; we do not assume otherwise. MT4 later = a new `MarketDataProvider` implementation backed by an EA bridge; nothing else changes.

## 2. Principles
- Strategy engine is pure Python, no MT5/Qt/network imports. Fully typed (`mypy --strict`).
- One engine for live and backtest: `engine.on_candle_closed(candle) -> list[Signal]`. Engine only ever sees candles up to the one that just closed, so lookahead is structurally impossible.
- Dependency injection via constructor args and `typing.Protocol` interfaces; composition root in `app/bootstrap.py`.
- GUI holds no business logic; it binds to a `MonitorService` through Qt signals.
- Core uses stdlib dataclasses (frozen, slots) + `Decimal`-free floats with point-size rounding at the edge. numpy only in backtest stats.

## 3. Layout
```
src/xauassist/
  domain/        candle.py signal.py enums.py (Direction, Scenario, Pattern, MaMethod)
  config/        models.py (typed dataclasses) loader.py (tomllib) secrets.py (keyring/secrets.toml)
  indicators/    ma.py (EMA/SMA, incremental) atr.py
  patterns/      pin_bar.py engulfing.py (pure functions on Candle)
  trend/         trend.py htf.py
  strategy/      zone.py scenario_a.py scenario_b.py engine.py
  validation/    validator.py (symbol/timeframe/spread/data-complete gates)
  signals/       manager.py dedup_store.py (SQLite)  cooldown.py
  data/          provider.py (Protocol) mt5_provider.py csv_provider.py gap_checker.py
  notify/        dispatcher.py telegram.py sound.py (Notifier Protocol)
  app/           monitor_service.py bootstrap.py logging_setup.py
  gui/           main_window.py settings_dialog.py history_model.py
  backtest/      runner.py execution.py stats.py splits.py report.py
tests/           mirrors src; fixtures/ with XAUUSD M5 CSV samples
config/          config.example.toml  secrets.example.toml
assets/sounds/   buy.wav sell.wav
packaging/       xauassist.spec build_windows.md
docs/
```

## 4. Key interfaces
```python
class MarketDataProvider(Protocol):
    def connect(self) -> ConnectionStatus: ...
    def disconnect(self) -> None: ...
    def get_closed_candles(
        self, symbol: str, tf: Timeframe, count: int
    ) -> list[Candle]: ...  # excludes forming bar
    def get_spread_points(self, symbol: str) -> float: ...
    def resolve_symbol(self, pattern: str) -> str | None: ...


class Notifier(Protocol):
    def notify(self, signal: Signal) -> None: ...  # must not block caller


class DedupStore(Protocol):
    def seen(self, key: SignalKey) -> bool: ...
    def mark(self, key: SignalKey) -> None: ...
```
`Candle`: time_open (UTC), O/H/L/C, tick_volume, spread_points, is_closed (engine rejects `False`).

## 5. Runtime flow (live)
1. `MonitorService` thread polls every `poll_seconds`; detects a new closed bar by comparing last closed `time_open`.
2. Fetch last `N` closed candles; `gap_checker` verifies continuity (gaps spanning known market closure/weekend OK; intra-session gaps -> refetch, then skip evaluation + log + reset Scenario B pending state).
3. Engine evaluates the new bar -> candidate signals.
4. `validator` (spread, symbol, completeness) -> `SignalManager` (dedup + cooldown, persisted) -> `NotificationDispatcher`.
5. Dispatcher fans out to Telegram (queue + worker thread, exponential backoff retries, idempotent via dedup key) and Sound (daemon thread). Neither blocks the loop.
6. Provider errors: bounded reconnect with backoff; status surfaced to GUI (Connected / Reconnecting / Terminal missing / Market closed).

## 6. Backtest
`runner` replays CSV/MT5 history through the same engine, `execution` applies next-open entry + bar spread + SL/TP convention (SL-first on ties), `stats` computes the required metrics (totals, win rate, MFE/MAE, max consecutive losses, by scenario, by pattern), `splits` enforces 60/20/20 with a guard that the final-test split is evaluated once (recorded in a lock file). Reports state no profitability claim, only measured results with sample sizes.

## 7. Config
`config/config.toml` (strategy, filters, spread, sound, telegram enabled flag) and separate secrets. Every parameter in STRATEGY_SPEC is a key; loader validates types/ranges and fails with clear messages.

## 8. Tooling
Python 3.11+ (dev box has 3.13), PySide6, MetaTrader5 (Windows-only extra), httpx, keyring, pytest, mypy --strict, ruff, PyInstaller (Windows build). Sound: `winsound` on Windows with `QSoundEffect`/`simpleaudio` fallback.

## 9. Testing seams
Fake provider (scripted candles, failures, disconnects), fake HTTP transport for Telegram failures, injectable clock. Realistic XAUUSD M5 fixture CSVs.

## 10. Known limitations
- MT5 provider and PyInstaller build cannot be verified on this Linux machine.
- Broker history depth limits backtest span.

## Phase 5 addendum: data layer (as built)

```
MarketDataProvider (Protocol)  <-- Mt5Provider (official MetaTrader5 package, injected Mt5Api)
                               <-- ReplayProvider (CSV / tests / demo)
          ^
      DataFeed  (reconnect+backoff, symbol resolve, exactly-once delivery, gap handling, market-closed)
          ^
   caller: monitor (Phase 6/7) -> StrategyEngine
```
- `DataFeed.bootstrap()` returns warm-up history (`is_history=True`): the caller feeds it to the engine and DISCARDS any signals produced from it.
- `DataFeed.poll()` never blocks and never raises provider errors; it returns a `FeedStatus` (OK, NO_NEW_DATA, MARKET_CLOSED, DATA_GAP, DATA_ERROR, DISCONNECTED, TERMINAL_MISSING, SYMBOL_MISSING, RECONNECTING).
- Missing candles: wait `backfill_retries` polls for the terminal to back-fill, then deliver with `data_complete=False` (caller resets pending setups and passes `data_complete=False` to the validator so no signal is built on a hole).
- **Server time:** candle times are converted to UTC with the CONFIGURED `mt5.server_utc_offset_hours`. It is never auto-adjusted at runtime (that would shift the timeline and stall the feed); instead `Mt5Provider.offset_warning()` reports a *confirmed* mismatch (two distinct fresh ticks >= 30 s apart), e.g. after a DST change.
- Gap classification (no broker calendar available): daily break = <= `daily_break_max_minutes` inside `daily_break_window_*`; weekend = gap spanning Saturday 12:00 <= `weekend_max_hours`; everything else is unexpected.

Known limitations (data layer)
- The last candle before a market closure is only delivered after the market reopens (MT5 keeps it as "forming" until the next tick). The monitor must drop signals whose candle is stale (planned for Phase 6).
- Holiday closures longer than the daily break are reported as unexpected gaps; candles after them are flagged incomplete until enough clean history accumulates (re-bootstrap via `reset()`).
- A real hole inside the configured daily-break window is indistinguishable from the break.
- The MT5 provider is verified only against a fake `MetaTrader5` module. `scripts/mt5_smoke_test.py` is provided to verify the live path on Windows.

## Phase 6 addendum: notifications and the monitor (as built)

```
DataFeed.poll() -> FeedUpdate -> SignalPipeline.handle()
   history? -> fresh engine, warm-up only, alerts discarded
   live     -> engine -> stale guard -> SignalValidator -> SignalManager (dedup/cooldown) -> Dispatcher
                                                                                       |-> SoundNotifier   (daemon thread)
                                                                                       |-> TelegramNotifier (queue + worker thread, retry)
MonitorService: background loop around poll+handle, snapshots/signals to listeners (GUI in Phase 7)
```
- **Telegram:** stdlib `urllib` transport (no extra dependency to package). 2xx+`ok` = sent; 429 and 5xx and network errors are retried with exponential backoff (and `retry_after` honoured); 400/401/403/404 are permanent (no retry, status ERROR so the UI can say "fix your token/chat id"). Delivery is at-least-once: a request that times out after Telegram accepted it can arrive twice (the Bot API has no idempotency key).
- **Credentials:** never in source/config. `SecretStore`: OS keyring (Windows Credential Manager) -> `secrets.toml` fallback (0600, git-ignored). `TelegramSecrets.__repr__` hides the token; log output is redacted at the formatter level (tokens, `bot<id>:<hash>` URLs, registered secrets, tracebacks). A test scans `src/` and `config/` for token-shaped strings.
- **Sound:** BUY/SELL files from `[sound]`, relative paths resolve against the app folder; missing file -> system beep; playback always off-thread; `enabled` and files changeable at runtime (`configure`).
- **Pipeline safety rules:** history never alerts; engine rebuilt on every history delivery; after a data hole pending Scenario B setups are cleared and signals suppressed for `monitor.post_gap_suppress_bars` candles; signals older than `monitor.max_signal_age_seconds` are dropped as STALE (e.g. delivered late after an outage) and are NOT recorded as sent. Every outcome (delivered / rejected / duplicate / cooldown / stale) is kept in the in-memory history with reasons for the UI.
- Limitation: a wrong `mt5.server_utc_offset_hours` that makes candle times *older* than reality would make every signal look stale; this shows up as STALE records with reasons and as the offset warning, not as silence.

## Phase 7 addendum: desktop GUI (as built)

```
MainWindow / SettingsDialog (PySide6, rendering only)
   |  calls                         ^ Qt signals (MonitorBridge: monitor thread -> GUI thread)
   v                                |
AppController (Qt-free) ---------- MonitorService -> SignalPipeline -> ...
   | owns: config, secrets, notifiers (outlive start/stop), signal-record history, dedup db
view_models.py (status text/levels)   forms.py (settings fields generated from the config dataclasses)
```
- **No business logic in widgets.** Status wording/colours live in `gui/view_models.py`; the Settings form is generated from `AppConfig` by `gui/forms.py` and validated by the config layer (`build_config`), so a new config field appears in the UI automatically and invalid values are rejected with the config layer's own message. Both are tested without Qt.
- **Apply semantics:** Telegram / sound / display changes apply instantly; strategy, validation, feed, MT5 and monitor changes restart monitoring (history is re-read and never alerts). Config is saved atomically to `config.toml`; credentials go to the OS keyring (fallback `secrets.toml`) and are never shown again in the UI.
- **Background operation:** closing the window minimises to the system tray (monitoring continues; tray menu = show / start / stop / quit; tray balloon on each delivered signal). A `QLockFile` prevents a second instance (which would duplicate every alert).
- **Run:** `python -m xauassist [--autostart] [--no-tray] [--config PATH] [--data-dir DIR]`.
  **Replay mode** (no MetaTrader needed, any OS): `python -m xauassist --replay candles.csv --replay-start 300 --replay-interval 1` runs the entire real stack (warm-up, detection, validation, dedup, sound, Telegram, GUI) on a simulated clock.
- Dependency: `PySide6-Essentials` (extra `gui`). Tests run headless on Qt's `offscreen` platform.

Known limitations (GUI)
- Clicking Stop waits for the monitor thread to finish its current poll (up to ~5 s if MT5 is hanging) and blocks the window meanwhile.
- Settings changes that restart monitoring are not applied while a candle is closing in that second; the restart re-reads history, so a signal on exactly that candle would be treated as history (documented rare edge).
- The tray icon and Windows audio could not be exercised on this Linux machine (tray unavailable offscreen; `winsound` Windows-only).
- Timestamps in the table/panel are in `display.timezone`; the "Last closed candle" field shows that candle's *open* time.

## Phase 8 addendum: backtesting (as built)

```
CSV / MT5 export -> load_candles_csv -> run_development()  (exploration + validation ONLY)
                                     -> run_final_test()   (test segment, once per dataset)
run_backtest(): live StrategyEngine -> SignalValidator -> SignalManager -> simulate_trade() per signal
stats.py (Wilson / mean-R CIs, MFE/MAE, streaks, drawdown, by scenario / pattern / direction)
report.py (report.md, summary.json, trades.csv)      sweep.py (grid search that cannot see the test set)
```
Run: `python -m xauassist.backtest --csv XAUUSD_M5.csv [--config config.toml] [--r 1.5] [--sl-buffer 0.5] [--max-hold 48] [--spread 25] [--same-candle TARGET_FIRST]`; add `--final-test` once, when the configuration is frozen. `--synthetic N` generates random-walk data to exercise the machinery (never evidence about the strategy).

Out-of-sample protections (all covered by tests, each verified by breaking it on purpose):
- Chronological 60/20/20 split. Development runs receive only the first 80% of the candles (a guarded sequence in the tests raises if anything reads further), and the result is identical whatever the test data contains.
- Trades whose outcome window crosses a segment boundary are purged, so no segment's score uses prices from the next one. `trades.csv` contains exactly the scored trades.
- `FinalTestLock`: the final test of a dataset can be evaluated once (a lock file records the first evaluation, keyed by a hash of the data). A repeat is refused; `--allow-test-reuse` runs it but stamps the report "NOT out-of-sample". A failed run does not burn the test set.
- Sweeps report the number of configurations tried and the selection-bias warning, and only ever see development data.
- Reports never claim profitability: n < 30 -> "insufficient sample"; mean-R 95% interval including zero -> "no evidence of an edge"; subgroup tables are labelled exploratory.

Simulator validation (random-walk data, which has no edge by construction):
- zero costs + wide stops (ties rare): pooled mean R ~ 0 (within 4 standard errors, regression-tested) - no hidden bias in fills or exits;
- with realistic spread the result is consistently about -0.28 R and flagged as losing - costs show up, edge is not invented.

Known limitations (backtest)
- No real XAUUSD data has been run yet; every number above comes from synthetic data or hand-built fixtures.
- Candle-level model: intra-candle order is unknown, hence the explicit same-candle rule; fills at stop/target levels ignore intra-candle liquidity. Spread is constant within a candle.
- Overlapping trades are scored independently (no capital or position-sizing model).
- Tiny-range candles (e.g. `pin_bar.min_range` = 0) give tiny stops, where spread dominates the R-result; consider `pin_bar.min_range` / `backtest.min_risk` once you see real data.
- Gap classification uses the same heuristics as live (broker calendar unavailable); holidays register as unexpected gaps.

## Phase 9 addendum: Windows packaging (as built)

```
packaging/xauassist.spec   PyInstaller spec (one-folder default, XAU_ONEFILE=1 for a single exe)
packaging/entry.py         entry script          packaging/build.py   build + self-check + zip
packaging/build_windows.bat one-click build       .github/workflows/build-windows.yml  CI build on windows-latest
assets/icon.ico            multi-size icon (scripts/make_icon.py)      docs/INSTALL_WINDOWS.md  user guide
```
- **Bundled:** sounds, icon, `config.example.toml`, `secrets.example.toml`, all of `xauassist` (incl. lazily imported Qt/backtest code), `tzdata` (Windows has no tz database), `keyring` backends, MetaTrader5 + numpy. Windowed (no console), UPX off (UPX-packed exes are flagged by antivirus far more often).
- **Paths when frozen:** resources come from the bundle (`_MEIPASS`); settings/logs/signal db live next to the exe ("portable") and fall back to `%APPDATA%\XAUAssistant` when that folder is read-only (e.g. Program Files). Tested by simulating a read-only folder.
- **First run:** creates `config.toml` (copied from the documented example), `logs/`, `data/`, `secrets.example.toml`; never overwrites user files; never creates credentials.
- **Windowed-exe hazards handled:** `sys.stderr is None` (no console handler), uncaught exceptions in the main thread / worker threads / Qt slots are logged and shown in a dialog with the log path, missing MetaTrader 5 is a status not a crash.
- **`--selfcheck`:** the packaged app verifies its own bundled sounds/config, writable folder + sqlite, timezone DB, secret store, strategy engine, Qt, TLS, keyring, MetaTrader5 import, audio. Writes `logs/selfcheck.txt`; non-zero exit on any essential failure. `build.py` runs it and refuses to produce an archive if it fails.

Verification done here (Linux): the same spec was built as a Linux bundle; the FROZEN binary passed `--selfcheck`, and - copied to an unrelated folder with no source tree and `PYTHONPATH` unset - ran the full GUI app in replay mode: first-run files created, history warmed up, SELL signal detected and logged. The bundle contains no tests, databases, credentials or token-like strings.

Not verified (needs Windows): the actual `.exe` build, `winsound`, the Credential Manager backend, the system tray, MetaTrader5 import in the frozen app, and the CI workflow itself. The first CI run is the real test; the self-check step is designed to make any missing-module problem fail loudly there.
Possible size optimisations (untested on Windows, so not applied): exclude Qt translations and Qt's software-OpenGL DLL.
