# Configuração do Gunicorn para o Painel de Bordo
# Coloque este arquivo na raiz do projeto (mesmo lugar que o app.py)

workers = 1
timeout = 60        # 60s: suficiente pra qualquer query normal; mata rápido se o Neon travar
keepalive = 5
loglevel = 'info'
