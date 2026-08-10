FROM node:22-alpine

WORKDIR /app

# Camada de dependências separada do código: só reinstala quando o manifesto muda.
# `npm ci` e não `npm install`: instala exatamente as versões do lock, então o
# build é reprodutível e não muda sozinho entre uma máquina e outra.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
