"""Exceções da categorização por LLM.

A hierarquia tem exatamente um propósito, e ele é a diferença entre um backlog
recuperável e um backlog perdido: **separar falha de infraestrutura de resposta
ruim do modelo**.

`OllamaUnavailableError` significa "o Ollama está fora do ar". Marcar as
transações como FAILED nesse caso encheria a tabela de linhas que precisariam de
um reset manual para voltar à fila; o job aborta e as deixa PENDING.

`OllamaResponseError` significa "o Ollama respondeu, mas não dá para usar". Aí o
problema é daquela transação, o job segue em frente, e a linha vai para
NEEDS_REVIEW — que é a fila de revisão humana, não a de erro.
"""


class OllamaError(Exception):
    """Base de tudo que o cliente do Ollama levanta."""


class OllamaUnavailableError(OllamaError):
    """Rede, timeout ou 5xx que sobreviveu às tentativas.

    Infraestrutura. Quem chama deve parar, não marcar a transação como falha.
    """


class OllamaResponseError(OllamaError):
    """Resposta recebida, mas com corpo inesperado ou que não é JSON válido.

    Carrega um trecho curto do que veio — o suficiente para depurar um modelo que
    ignora o `format`, sem despejar uma geração inteira no log.
    """
