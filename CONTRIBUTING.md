# Contributing

BlueChat is an educational open-source project. Contributions should include
focused tests, type hints, and documentation for user-visible behavior. Keep
operating-system Bluetooth details behind `BluetoothBackend` and never introduce
custom cryptography.

## Development

```sh
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
pytest
ruff check .
```

Open an issue before proposing a large protocol or security change. Include the
threat model and compatibility impact in the proposal.
