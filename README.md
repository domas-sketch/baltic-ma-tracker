# Baltic MA Tracker

Tracks new human medicine authorisations in Lithuania, Latvia and Estonia.

- **Dashboard:** `https://<your-github-username>.github.io/<repo-name>/` (updated daily)
- **Email digest:** every Monday morning, covering the previous 7 days
- **Alert email:** sent if a register can't be downloaded or looks incomplete

## Sources
| Country | Source | Date used |
|---|---|---|
| Lithuania | VVKT VAPRIS export (CSV) | Registracijos data |
| Latvia | ZVA HumanProducts.xml | date_of_authorisation |
| Estonia | Ravimiamet "detailne CSV v2" | Müügiloa kehtivuse algus |

The dashboard also shows **First seen**, the day the tracker first found a product in the register. That date catches late entries. Products already in a register when tracking started show "before tracking".

## Manual run
Go to **Actions → Baltic MA tracker → Run workflow**. Tick "Send the weekly email now" to get the digest immediately.

## Files
- `tracker/tracker.py` downloads, compares, builds `docs/data.json` and sends the email
- `docs/index.html` is the dashboard
- `state/` holds what has been seen before (managed automatically)
- `.github/workflows/tracker.yml` is the schedule
