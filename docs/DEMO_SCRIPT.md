# Interview demo walkthrough (5 minutes)

1. **Business problem:** Tickets from email, chat, and phone need explainable assignments and auditable SLAs.
2. **Schema:** Open `docs/ER_DIAGRAM.md` and `sql/schema.sql`. Explain that agent skills/languages are many-to-many bridge tables and ticket lifecycle history is append-only.
3. **Reproduce data:** Run a small generator command and show the fixed seed, manifest counts, SQLite file, and separate `ground_truth/` labels.
4. **Routing:** Explain why the router never assigns an unskilled agent, how it ranks available capacity, why critical tickets require exact language, and what happens when no agent is eligible.
5. **SLA:** Show a deadline crossing the end of a workday and explain how weekends/holidays/time zones are skipped instead of adding elapsed minutes.
6. **Anomalies:** Run validation and inspect all five expected anomaly records and detection outcomes; confirm unrelated unexpected errors are zero.
7. **Metrics/UI:** Run `src.analytics` and open the Streamlit dashboard. Explain that each KPI comes from the same generated SQLite data.
8. **Tradeoffs:** Call out sequential single-process simulation, synthetic calendars, challenge fixture agents, and the lack of production authentication.

## Suggested small demo commands

```powershell
py -m src.generator --tickets 100 --agents 10 --messages 250 --event-days 7 --output-dir data/small
py -m src.validate --data-dir data/small
py -m src.analytics --data-dir data/small
$env:YOTTAFLEX_DATA_DIR = "data/small"
py -m streamlit run dashboard/app.py
```
