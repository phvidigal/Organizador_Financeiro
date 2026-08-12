"""Schemas Pydantic da API.

Separados dos modelos SQLAlchemy de propósito: o que a API expõe e o que o banco
guarda mudam por motivos diferentes. `raw_payload`, por exemplo, nunca sai daqui.
"""
