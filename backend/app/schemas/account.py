"""Schema de conta bancária."""

import uuid

from pydantic import BaseModel, ConfigDict

from app.schemas.common import Money


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bank_connection_id: uuid.UUID | None
    pluggy_account_id: uuid.UUID | None
    type: str
    subtype: str | None
    name: str
    marketing_name: str | None
    number: str | None
    balance: Money | None
    currency_code: str

    # `bank_data` / `credit_data` ficam de fora: são blocos crus, com forma que
    # varia por connector, e o que a interface precisa deles (limite do cartão,
    # vencimento da fatura) ainda não foi decidido. Expor cru agora viraria
    # contrato de API por acidente.
