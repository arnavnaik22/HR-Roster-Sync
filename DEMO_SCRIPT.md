# 90-second demo outline

1. Show `python -m unittest discover -s tests -v`. Point out the concurrent update, safety brake, and crash tests.
2. Run `python demo.py`. Explain why version 6 is ignored after version 7, and why the truncated export is held without deactivating anyone.
3. Run `python bench.py`. Show the 200,000-row staging and diff time plus peak process memory.
4. Show the API flow from `README.md`: create an office, submit an event, upload `examples/snapshot.csv`, inspect the run, and apply it.
5. Explain the outbox boundary: the employee transaction commits with an event row; the relay may deliver twice after a crash, so the consumer uses the stable event ID.

Use synthetic identities only. No real employee data belongs in a public demo.
