# Contributing

This project targets Python 3.10+ and uses the standard library for its core functionality.

When changing expiry or reminder behavior, add a test for the date or stage boundary that changes. Keep the date calculations in `rules.py` and database changes in `store.py`.

Database changes need a migration in `db._MIGRATIONS` and a test that opens an older database. Do not silently change the meaning of existing stored fields.

Use `textContent` when placing untrusted values into the dashboard. If a change introduces `innerHTML`, review the input and rendering paths before merging.

Run the tests from the repository root:

```bash
python -m unittest discover -s . -t . -v
```

In a pull request, explain what behavior changed, why it changed and which tests were run. Note any limitations you could not verify.
