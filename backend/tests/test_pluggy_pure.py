"""Auxiliares do cliente da Pluggy que não fazem I/O.

Módulo separado de `test_pluggy_client.py` porque aquele carrega o marcador de
asyncio no topo, e um teste síncrono ali dentro vira warning — mesma razão de
`test_ingestion_pure.py` existir.
"""

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from app.services.pluggy.client import (
    _extract_after,
    _format_created_at_from,
    _jwt_expiry,
    _loads,
)


def test_extract_after_handles_both_shapes() -> None:
    """A Pluggy pode devolver `next` como URL absoluta ou como query string solta."""
    assert _extract_after("https://api.pluggy.ai/v2/transactions?accountId=x&after=abc") == "abc"
    assert _extract_after("?accountId=x&after=abc") == "abc"
    assert _extract_after("https://api.pluggy.ai/v2/transactions?accountId=x") is None


def test_created_at_from_is_converted_to_utc() -> None:
    """O parâmetro é especificado em UTC com o sufixo Z. Mandar horário local
    deslocaria a janela do sync incremental em três horas."""
    brasilia = timezone(timedelta(hours=-3))
    assert (
        _format_created_at_from(datetime(2026, 8, 1, 0, 30, 0, tzinfo=brasilia))
        == "2026-08-01T03:30:00.000Z"
    )
    assert (
        _format_created_at_from(datetime(2026, 8, 1, 3, 30, 0, tzinfo=UTC))
        == "2026-08-01T03:30:00.000Z"
    )


def test_json_parse_never_produces_float() -> None:
    """A invariante "dinheiro nunca é float" começa aqui, no parse da resposta.

    `json.loads` padrão devolveria 12.339999999999999857891452848 para `12.34`, e
    converter depois já seria tarde — o erro entra na primeira conversão.
    """
    payload = _loads(b'{"amount": 12.34, "balance": 2500.75, "count": 3}')

    assert payload["amount"] == Decimal("12.34")
    assert isinstance(payload["amount"], Decimal)
    assert not isinstance(payload["balance"], float)
    # Inteiro continua inteiro: só o que era fracionário vira Decimal.
    assert payload["count"] == 3


def _fake_jwt(payload: dict) -> str:
    """JWT sem assinatura válida — o cliente lê o `exp` sem verificar nada."""
    segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"cabecalho.{segment}.assinatura"


def test_api_key_expiry_comes_from_the_token_itself() -> None:
    """`POST /auth` devolve só `apiKey`, sem campo de validade — mas a chave é um
    JWT, e o `exp` dele é a verdade sobre quando ela para de funcionar.

    Ler daí é melhor que confiar no "~2 horas" da doc: se a Pluggy encurtar a
    validade, o cliente acompanha em vez de passar a tomar 401 no meio dos syncs.
    """
    expira = datetime(2026, 8, 11, 20, 0, 0, tzinfo=UTC)
    assert _jwt_expiry(_fake_jwt({"exp": int(expira.timestamp())})) == expira


def test_a_key_that_is_not_a_jwt_falls_back_silently() -> None:
    """Best-effort: qualquer sinal de formato inesperado devolve None, e aí vale o
    `_API_KEY_TTL`. Nunca levanta — isso derrubaria a autenticação inteira."""
    assert _jwt_expiry("chave-opaca-qualquer") is None
    assert _jwt_expiry("a.b.c") is None
    assert _jwt_expiry(_fake_jwt({"sub": "sem exp"})) is None
    assert _jwt_expiry(_fake_jwt({"exp": "não é número"})) is None
