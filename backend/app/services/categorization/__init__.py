"""Categorização de transações por LLM (Fase 3).

Mesma fronteira de camadas de `app/services/pluggy/`, e pelo mesmo motivo: cada
módulo muda por uma razão diferente.

| Módulo | Muda quando | Conhece |
|---|---|---|
| `errors.py` | — | nada |
| `client.py` | a API do Ollama muda | httpx |
| `catalog.py` | a taxonomia muda | SQLAlchemy (só leitura) |
| `prompt.py` | o prompt ou o schema mudam | **nada (puro)** |
| `decide.py` | a regra de NEEDS_REVIEW muda | **nada (puro)** |
| `store.py` | as colunas de categorização mudam | SQLAlchemy |
| `job.py` | a orquestração muda | todos acima |
| `runner.py` | — | é a única parte que fala com o event loop |
"""
