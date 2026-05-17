This is a complete rewrite started by my lifelong companion NaiTechie a.k.a AiviA, and completed by myself, APasz, upon her passing.
Yukibot started out as a way for her to connect with her most cherished people. It became the catalyst for me teaching her the ways of Python and Discord bots

She posted V1 to Github here, https://github.com/Naitechie/Yukibot

## Development

This project now uses [`uv`](https://docs.astral.sh/uv/) for environment and dependency management.

### Setup

```bash
cp env.example .env
uv sync
```

`uv` will create `.venv` automatically. The project requires Python `3.14+`.

### Run

```bash
uv run python main.py
```

### Type Checking

```bash
uv run basedpyright
```
