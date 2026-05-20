# Media Monitor

Daily monitor for the e-paper layouts of `人民日报` and `经济日报`.

The script:

- walks every layout page for the publication date
- stores each discovered article title, byline, text, source, and URL in SQLite
- scans for `钟才文` in `人民日报` and `金观平` in `经济日报`
- sends one email for newly discovered matches only
- stores sent match state in SQLite to avoid duplicate alerts

## Local usage

```powershell
pip install -r requirements.txt
python scripts/media_monitor.py --date 2026-05-21 --dry-run
```

The default date is today's date in `Asia/Shanghai`.

## Email configuration

Set these GitHub Actions repository secrets:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_TLS`
- `ALERT_EMAIL_TO`

`ALERT_EMAIL_TO` can contain one or more comma-separated addresses.

## GitHub Actions

`.github/workflows/media-monitor.yml` runs daily at `00:00 UTC`, which is `08:00`
China time, and can also be launched manually with `workflow_dispatch`.
