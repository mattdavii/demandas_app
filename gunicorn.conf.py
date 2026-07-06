# Configuração do Gunicorn — Painel de Bordo
# gthread: 1 processo + N threads — suporta requisições concorrentes sem bloquear
# Quando o Neon acorda (cold start 3-10s), outras requisições continuam sendo atendidas

workers      = 1
worker_class = 'gthread'   # threads assíncronas dentro do mesmo processo
threads      = 4            # até 4 requisições simultâneas
timeout      = 90           # reduzido: Neon raramente passa de 60s
keepalive    = 5
loglevel     = 'info'
