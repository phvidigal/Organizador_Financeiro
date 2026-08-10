# legacy/ — protótipo SQLite

Código do primeiro protótipo, preservado apenas como referência histórica.
**Não faz parte da aplicação** e não é executado por nada em `backend/` ou `frontend/`.

- `Banco_de_dados.py` — script que criava `transacoes` e `planejamento_orcamento` em SQLite.
- `finanças.db` — banco gerado por esse script.

Substituído pelo schema PostgreSQL em [`backend/app/models/`](../backend/app/models/),
com migrations versionadas via Alembic.

Duas diferenças que motivaram a reescrita e valem registro:

- `valor REAL` virou `amount NUMERIC(18,2)`. Ponto flutuante binário não representa
  `0,10` exatamente, e o erro acumula em somas de extrato.
- Não havia chaves estrangeiras nem constraint de unicidade, então qualquer
  reimportação duplicava transações.
