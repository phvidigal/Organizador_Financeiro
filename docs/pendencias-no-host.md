# Pendências na máquina do app

O código é escrito em sessões na nuvem, que não alcançam a máquina onde o app de
fato roda — Docker Desktop, o Ollama na GPU e o banco com o extrato real. Tudo
o que só pode ser executado **lá** entra aqui, em ordem, com o motivo.

Item novo vai no fim. Feito, sai da lista (o histórico fica no git).

---

## 1. Atualização do Next para 15.5.28

**Quando:** depois do merge do PR #3 no `master`.

```bash
git checkout master && git pull
docker compose up -d --build --renew-anon-volumes frontend
```

O `--renew-anon-volumes` não é opcional. O `docker-compose.yml` monta
`/app/node_modules` como volume anônimo, e o compose reaproveita esse volume ao
recriar o container: sem a flag, a imagem nova sobe com os módulos antigos e o
frontend continua no 15.1.3, sem erro nenhum.

Conferir:

```bash
docker compose exec frontend npx next --version
```

Tem de responder `15.5.28`. Depois, abrir <http://localhost:3000> e confirmar que
o hot reload ainda dispara ao editar um arquivo — no Docker Desktop do Windows
ele depende do polling do webpack, que não pôde ser testado na nuvem.

## 2. Números da fila de revisão (informativo)

**Quando:** a qualquer momento. Não bloqueia nada.

```bash
docker compose exec db psql -U postgres -d finance \
  -c "SELECT categorization_status, count(*) FROM transactions WHERE deleted_at IS NULL GROUP BY 1;" \
  -c "SELECT count(*) AS revisoes, count(*) FILTER (WHERE previous_category_id = new_category_id) AS confirmou_llm FROM categorization_reviews;"
```

Responde quanto da fila de `NEEDS_REVIEW` já foi respondido e quantas respostas
existem para calibrar `LOW_CONFIDENCE = 0.70`. Em agosto eram 137 na fila e zero
respostas.

## 3. Painel (`/painel`)

**Quando:** junto com o item 1 — o painel está no mesmo PR #3. Sem migration e
sem dependência nova; o comando do item 1 já recria o frontend, falta o backend:

```bash
docker compose restart backend
```

O `restart` é por garantia: o código chega por bind mount do Windows, e o
`--reload` do uvicorn não é garantido de perceber arquivo novo por ali.

**Conferir com o extrato real** — é a parte que a nuvem não alcança; lá o painel
só foi visto com dados sintéticos:

1. Abrir <http://localhost:3000/painel> e ir até um mês fechado (agosto).
2. Comparar **Despesas** com o extrato e a fatura do mês. Duas diferenças são
   esperadas: o pagamento da fatura fica fora (é `TRANSFER`), e a compra no cartão
   conta na data da compra, não na do pagamento.
3. Ler o aviso de "sem decisão": é quanto do total ainda é palpite. Respondê-lo é
   a fila de `/revisao`.
4. Se aparecer o aviso de outra moeda, anotar quantos lançamentos são — decide se
   vale converter em vez de deixar fora.
5. Opcional, para fechar a questão do índice com o volume real:

   ```bash
   docker compose exec db psql -U postgres -d finance -c "EXPLAIN ANALYZE SELECT kind, category_id, categorization_status, sum(amount), count(*) FROM transactions WHERE tenant_id = '00000000-0000-0000-0000-000000000001' AND deleted_at IS NULL AND date BETWEEN '2026-08-01' AND '2026-08-31' GROUP BY 1, 2, 3;"
   ```

   O `postgres` é owner e ignora o RLS, por isso o `tenant_id` explícito. Com
   dado sintético deu 0,7 ms; qualquer coisa abaixo de alguns milissegundos
   confirma que não há índice a criar.

O que divergir no passo 2 é o que precisa voltar para a próxima sessão.
