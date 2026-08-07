FROM python:3.11-slim

WORKDIR /app

# Fontes TrueType usadas pelo !perfilcla (card do clã, desenhado com
# Pillow). A imagem "slim" não vem com nenhuma fonte instalada — sem isso,
# _carregar_fonte_perfil() nunca encontra os caminhos que procura
# (/usr/share/fonts/truetype/dejavu/...) e cai sempre no fallback de fonte
# pequena/embutida do Pillow, deixando o card feio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Diretório de dados persistentes do bot (registros, clãs, vendas, admins,
# doações, etc. — ver DATA_DIR em main.py). Declarado como VOLUME para que,
# mesmo se alguém rodar o container sem -v, o Docker ainda crie um volume
# anônimo aqui em vez de gravar só na camada gravável do container (que
# some no rebuild). Em produção, sempre rode com -v apontando pra um volume
# nomeado ou uma pasta do host, como no exemplo abaixo, para os dados
# sobreviverem entre containers.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Gunicorn no lugar do servidor de desenvolvimento do Flask (app.run):
# o servidor embutido do Flask não foi feito pra produção (sem gestão de
# múltiplos workers de verdade, sem graceful reload, warnings de segurança
# no próprio log). O loop automático de guerra/raide/CWL é iniciado dentro
# de main.py na importação do módulo (iniciar_ciclos_automaticos()), então
# funciona igual rodando via Gunicorn.
#
# ⚠️ MANTENHA --workers 1: cada worker do Gunicorn é um PROCESSO separado.
# Mais de um worker faria cada processo iniciar seu próprio loop de fundo,
# duplicando relatórios/avisos enviados no grupo — o estado do bot (JSON +
# threading.Lock em memória) não foi feito pra coordenar múltiplos processos.
# --threads dá concorrência suficiente pra requests do webhook dentro do
# mesmo processo, sem esse problema.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT:-5000} main:app"]