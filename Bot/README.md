# MIS2 SSV Auto Bot

Automates [mis2.ssv.uz](https://mis2.ssv.uz): filter by birth date, open order, **Запросить бюджет**.

## Precise schedule

Budget button clicks happen at **exact seconds**:

| Time | Action |
|------|--------|
| **09:00:00** | Click Запросить бюджет (patient 1) |
| **09:00:03** | Click Запросить бюджет (patient 2, +3 sec) |
| **09:30:00** | Next slot |
| **10:00:00** | Every 30 minutes… |

**Prep starts 55 seconds before** each slot (e.g. 08:59:05 for 09:00:00) so steps 1–4 finish before the exact click.

## Config (`schedule` section)

```json
"schedule": {
  "start_time": "09:00:00",
  "interval_minutes": 30,
  "prep_seconds": 55,
  "patient_stagger_seconds": 3
}
```

| Key | Meaning |
|-----|---------|
| `start_time` | First daily slot (HH:MM:SS) |
| `interval_minutes` | 30 = 09:00, 09:30, 10:00… |
| `prep_seconds` | Start job this many seconds before click (default 55) |
| `patient_stagger_seconds` | Gap between patients (3 sec) |

## Run

```powershell
cd "c:\Users\Zarmed IT\Desktop\Bot"
python bot.py
```

Logs show exact click times, e.g. `Step 5: clicked Запросить бюджет at 09:00:00.012`.

If steps 1–4 take longer than `prep_seconds`, increase `prep_seconds` in config.
