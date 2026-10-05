# Kudos portal

Internal employee portal feature for giving a colleague a short note of appreciation. The behaviour is the approved blueprint in `SPECIFICATION.md`.

## Run

Python 3.11 or newer. No third-party packages.

```text
python app.py
```

Open http://127.0.0.1:8000

| Person | Email | Role |
| --- | --- | --- |
| Alex Chen | alex.chen@datacom.example | Employee |
| Sam Patel | sam.patel@datacom.example | Employee |
| Jordan Lee | jordan.lee@datacom.example | Employee |
| Riley Morgan | riley.morgan@datacom.example | Administrator |

The demo password for every account is `kudos-demo`.

`KUDOS_PORT` changes the port. `KUDOS_DB` changes the SQLite file. Deleting `kudos.db` reseeds the demo.

## Tests

```text
python -m unittest tests.test_kudos
```
