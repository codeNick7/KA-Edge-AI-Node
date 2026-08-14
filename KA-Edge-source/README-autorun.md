# KA-Edge Auto-Run After Reboot

The system must resume data collection automatically after a power failure or reboot,
continuing from where it left off (timestamps remain accurate because `received_at`
uses the NTP-synced system clock, not `millis()`).

## Primary method — App Lab "Always On"

This is the recommended approach. No extra configuration files are needed.

1. Open the **App Lab** web UI.
2. Navigate to your project (`test-sensors` or equivalent).
3. In project settings, enable **"Always On"** (or "Auto-start on boot").
4. App Lab will automatically restart the Python relay (`main.py`) whenever
   the board powers on, reboots, or the app crashes.

The Arduino sketch flashed to the board already runs continuously — it is not
affected by App Lab restarts.

## Fallback — crontab (if App Lab "Always On" is unavailable)

SSH into the Linux side of the board and add a crontab entry:

```bash
crontab -e
```

Add this line (adjust the path to match your App Lab project directory):

```
@reboot sleep 30 && cd /home/arduino/ArduinoApps/test-sensors && arduino-applab run >> /home/arduino/logs/ka-edge.log 2>&1 &
```

- `sleep 30` — waits for network to initialise before NTP sync.
- `>> /home/arduino/logs/ka-edge.log` — appends startup logs for debugging.
- Create the log directory first: `mkdir -p /home/arduino/logs`

## What happens on restart

| Component | Behaviour |
|-----------|-----------|
| Arduino sketch | Resumes immediately (always runs from flash) |
| Python relay (`main.py`) | Starts, queries NTP, logs session start to `session_log.jsonl`, **appends** to existing JSONL files |
| Dashboard | On next page open, loads full history from first-ever reading |
| Timestamps | Accurate because `received_at` uses NTP-synced system clock |

## Verifying restart behaviour

After a reboot, check `data/session_log.jsonl`:
```json
{"event": "session_start", "started_at": "2026-08-14T09:00:00+00:00", "ntp_synced": true, "ntp_offset_s": 0.012}
{"event": "session_start", "started_at": "2026-08-14T12:30:00+00:00", "ntp_synced": true, "ntp_offset_s": -0.003}
```
Each line is one startup. Gaps in the `pot_a_readings.jsonl` timestamps will
show exactly when power was lost, and data resumes from where it left off.
