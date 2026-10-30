# XAUUSD Smart Trading Assistant - Windows installation and use

The assistant watches **XAUUSD on M5**, applies your MA20 strategy and sends **alerts only**
(Telegram + a sound). It never places, modifies or closes a trade.

## 1. Requirements

| Needed | Notes |
|---|---|
| Windows 10 / 11, 64-bit | |
| **MetaTrader 5 terminal**, installed from your broker | It must be **running and logged in** while the assistant works. The assistant reads prices through the official MetaTrader 5 Python interface, which talks to the running terminal. |
| `XAUUSD` visible in MT5 **Market Watch** | Brokers name it differently (`XAUUSD.a`, `XAUUSDm` ...): that is fine, the assistant finds it. |
| A Telegram bot (optional) | See section 4. |

Without MetaTrader 5 the app still starts; it shows **"MetaTrader 5 not found"** in red and keeps retrying.

## 2. Install

1. Unzip `XAUAssistant-windows.zip` to a folder you own, e.g. `C:\Users\<you>\XAUAssistant\`
   (installing under `C:\Program Files` also works: settings are then kept in `%APPDATA%\XAUAssistant`).
2. Double-click `XAUAssistant.exe`. No terminal window opens.
   - **Windows SmartScreen** may say "Windows protected your PC" because the file is not code-signed.
     Click **More info -> Run anyway** (only for a build you made yourself or got from someone you trust).
   - Some antivirus products flag new unsigned executables. The one-folder build is flagged less often
     than a single-file build; add an exception for the folder if needed.
3. On first launch the app creates, next to the program: `config.toml` (documented settings),
   `secrets.example.toml`, `logs\` and `data\`.

To check an installation at any time: run `XAUAssistant.exe --selfcheck`, then read `logs\selfcheck.txt`
(`RESULT: PASS` means all bundled files and libraries work).

## 3. First configuration (do this before trusting any alert)

Open **Settings...** and set, at minimum:

- **MetaTrader 5 -> Server utc offset hours**: your broker's server time minus UTC (often `2` in winter,
  `3` in summer). Change it after daylight-saving changes. The app shows a yellow warning if it detects a mismatch.
- **Validation -> Max spread points**: signals are rejected when the spread is higher. (The default `50` is a placeholder.)
- **Strategy -> Zone**: how close to the MA counts as a "touch". (The default `0.30 x ATR` is a placeholder.)
- **Signals -> Cooldown bars** and **Display -> Timezone** (`local`, `UTC` or e.g. `Europe/Berlin`).

Saving changes to the strategy restarts monitoring (history is re-read; it never raises alerts).

## 4. Telegram alerts

1. In Telegram, talk to **@BotFather**, send `/newbot`, follow the steps, and copy the **bot token**.
2. Open your new bot and send it any message (a bot cannot message you first).
3. Find your **chat id**: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and look for
   `"chat":{"id": ...}` (for a group, add the bot to the group; the id is negative).
4. In the app: **Settings -> Telegram**, paste token and chat id, **Save credentials**, tick **Enabled**,
   then **Test connection + send test message**.

The token is stored in the **Windows Credential Manager** (or, if that is unavailable, in `secrets.toml`
next to the program). It is never written to `config.toml` or to the logs. Never share `secrets.toml`.

## 5. Daily use

1. Start MetaTrader 5 and log in. 2. Start the assistant, press **Start monitoring**.
3. Closing the window **keeps it running in the system tray**; use the tray icon to show it or quit.
   Add a shortcut to `shell:startup` if you want it to start with Windows (`XAUAssistant.exe --autostart`).

The window shows connection state, symbol, timeframe, the live EMA20, the last signal and the signal history
(tick *"Also show rejected / dropped..."* to see why a setup was not alerted). A candle's signal is evaluated only
after it has **closed**; there is no repainting.

Status meanings: **Connected** (receiving candles) - **Market closed** (no ticks; normal on weekends) -
**Waiting for missing candles** (terminal is back-filling history) - **Disconnected / MetaTrader 5 not found**
(start and log in to the terminal; the app reconnects by itself).

## 6. Files

| File | Purpose |
|---|---|
| `config.toml` | All strategy and app settings (edit by hand or via Settings) |
| `secrets.toml` | Only if the Credential Manager is unavailable: Telegram token/chat id |
| `data\signals.db` | Remembers sent signals so a restart never re-sends them |
| `logs\xauassist.log` | Main log (rotating). Attach it when reporting a problem; secrets are redacted |
| `logs\selfcheck.txt` | Result of the last `--selfcheck` |

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| "MetaTrader 5 not found" | Install MT5 from your broker, start it, log in. |
| "XAUUSD symbol not found" | MT5 -> Market Watch -> right-click -> Symbols -> enable your gold symbol. |
| "Disconnected" | MT5 lost its broker connection; check its status bar (bottom-right). |
| No alerts but signals in history show **Stale** | `mt5.server_utc_offset_hours` is wrong (candle times are off by hours). |
| Telegram test fails with 401 | Wrong token. 400 "chat not found": send your bot a message first / wrong chat id. |
| No sound | Check Windows volume; **Test** buttons in the Sound panel; files must be `.wav`. |
| App will not start | Run `XAUAssistant.exe --selfcheck`, read `logs\selfcheck.txt` and `logs\xauassist.log`. |
| Two copies | Only one instance can run (to avoid duplicate alerts). |

## 8. Building from source (developers)

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[gui,mt5,secrets,build,dev]"
python packaging\build.py --test
```
Output: `dist\XAUAssistant\XAUAssistant.exe` (+ `dist\XAUAssistant-windows.zip`). `--onefile` builds a single exe.
`packaging\build_windows.bat` does all of this in one click. The build **fails** if the packaged app's own
`--selfcheck` fails. A GitHub Actions workflow (`.github/workflows/build-windows.yml`) builds and self-checks
on a clean Windows runner.

## 9. Backtesting

`python -m xauassist.backtest --csv XAUUSD_M5.csv` (from a source checkout) evaluates the strategy on history.
See `docs/ARCHITECTURE.md` (Phase 8) for the out-of-sample rules. Backtest results are historical evidence, not a promise of profit.
