# Configuração do Gunicorn para o Painel de Bordo
# Coloque este arquivo na raiz do projeto (mesmo lugar que o app.py)

workers = 1          # free tier do Render tem 1 CPU — mais workers desperdiçam RAM
timeout = 120        # 2 minutos: suficiente para envio de emails em lote
keepalive = 5
loglevel = 'info'
