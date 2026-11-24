# Repository layout

Where things live in this repository.

The file lists of the integration and of the door package are in [`custom_components/portico/README.md`](../../custom_components/portico/README.md) and [`door/README.md`](../../custom_components/portico/door/README.md).

## Top level

```
custom_components/portico/          the HA integration (domain portico): UI, phones, push, content, websocket commands
custom_components/portico/door/     the door side: hub, transport client, call state, door entities, the three drivers,
                                    the lazy wheel install (wheel.py); entry_type "door"
custom_components/portico/tests/    the Python tests (door side and the rest)
custom_components/portico/www/      the card, the panel and its Node tests
contract/                           wire contracts and fixtures shared with the phone app ([contract fixtures](../reference/contract-fixtures.md))
transport-rs/                       the Rust transport: the source of the pinned wheel (0.7.x)
packaging/rust/                     builds the Rust wheels, the tag scheme, release dry runs
scripts/                            test_offline.sh, build_test_venv.sh, migrate_unify.py (the one-time offline migration
                                    of the live door entry from the former portico_intercom, stdlib only)
docs/                               the documentation tree ([index](../README.md)); docs/history/ holds dated records of one-off
                                    operations on the maintainer's own installation
hacs.json                           HACS repository metadata (see publishing)
```

## Also present

| Path | What it is |
| :--- | :--- |
| `packaging/standalone/` | a standalone-transport container ([README](../../packaging/standalone/README.md)) |
| `contract/` | wire contracts and fixtures, see [contract fixtures](../reference/contract-fixtures.md) |
