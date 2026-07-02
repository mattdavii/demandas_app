# Configuração do Gunicorn para o Painel de Bordo
# Coloque este arquivo na raiz do projeto (mesmo lugar que o app.py)

workers = 1
timeout = 120       # Neon serverless pode levar até 60s pra acordar após inatividade
keepalive = 5
loglevel = 'info'
