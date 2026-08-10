FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Camada separada e antes do código: só invalida o cache quando as deps mudam.
# requirements-dev entra na imagem porque este Dockerfile é de desenvolvimento
# (a suíte roda com `docker compose exec backend pytest`).
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# O código real chega pelo bind mount do compose; este COPY existe para a imagem
# continuar funcionando sem o mount.
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
