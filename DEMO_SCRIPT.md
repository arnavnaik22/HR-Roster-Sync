# A simple live walkthrough

Open `http://localhost:8000/` after `docker compose up --build`. The sample workspace creates a fresh tenant with synthetic employees, so every run starts clean.

1. **Start with the person affected.** A short HR file can remove rides for people who are still employed. Click **Open sample workspace** and point to the ten active employees.
2. **Check the file.** Click **Check sample file**. The six-row export is held because four active employees are missing. Show that they remain active while the file is held.
3. **Make the decision visible.** Enter a specific approval reason and click **Approve and apply**. The result shows six active and four inactive employees; the activity panel records who approved the run and why.
4. **Show event ordering.** Click **Try an out-of-order update**. A version 11 shift change applies; a later-arriving version 10 change is stale. The newest shift remains visible.
5. **If asked about reliability, open the code or terminal.** `python api_demo.py` exercises duplicate events and outbox retry. `python -m unittest discover -s tests -v` runs the 14 tests. `python bench.py` measures the 200,000-row stage and diff.

The [illustrated walkthrough](assets/demo.mp4) and [quick tour](assets/quick-tour.mp4) are useful if a live demo is impractical. They illustrate this flow with synthetic records and results checked against local runs; they are not a screen recording. The actual rules run in the web workspace and API.

Keep the explanation direct: **the file pauses before anyone loses a ride; a human can review the exact missing people; the approval has a recorded reason.**
