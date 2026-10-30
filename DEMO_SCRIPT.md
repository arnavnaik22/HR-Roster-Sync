# 90-second demo outline

1. Show `python -m unittest discover -s tests -v`. Point out the concurrent update, safety brake, and crash tests.
2. Run `python api_demo.py`. Show the FastAPI requests for create, update, duplicate, and late events. Explain why the shift stays at version 2.
3. Pause at the `HELD` export. Show the `409 APPROVAL_REQUIRED` result and that employee E9 is still active. Then show the approved run, the operator ID, and E9's deactivation.
4. Show the injected downstream failure and the subsequent outbox retry. Explain that the employee transaction commits with an event row; the relay may deliver twice after a crash, so the consumer uses the stable event ID.
5. Run `python bench.py`. Show that the 200,000-row export is compared against 200,000 existing employees, plus the time and peak process memory.

Use synthetic identities only. No real employee data belongs in a public demo.
