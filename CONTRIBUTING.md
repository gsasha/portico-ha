# Contributing

Portico is alpha software; changes are welcome as pull requests.

1. Branch from `main`; one topic per branch.
2. Make the change. Keep documentation in step: user-facing behaviour belongs in [`docs/`](docs/README.md) (a guide or a reference page), not in the README.
3. Run the tests (offline, in a throwaway container; see [running the tests](docs/development/running-tests.md)):

   ```
   scripts/test_offline.sh custom_components/portico
   scripts/test_offline.sh --node        # the card and panel tests
   ```

   There is no CI service yet: quote the run in the pull request description. Some tests read the documentation under `docs/`; a moved or removed sentence can fail them.
4. Changes to the wire contract (`contract/`) are changes for the phone app too: the files marked "app" in [contract fixtures](docs/reference/contract-fixtures.md) are vendored by it.
5. Open a pull request against `main` and describe what changed and how you checked it.

## Reporting a security problem

Please report vulnerabilities privately through GitHub's private vulnerability reporting (the repository's Security tab, then Report a vulnerability), not in a public issue. Include what you saw, which version, and how to reproduce it. Portico opens doors and gates, so anything that lets someone ring, answer or open without being enrolled is worth reporting.
