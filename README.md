### Rarebooks Control

RareBooks billing control site: the hosted site registry, payments and pushes

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app rarebooks_control
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/rarebooks_control
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff (lint, import order and format, version 0.14.10)

### License

MIT
