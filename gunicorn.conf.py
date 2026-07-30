# Configuração do Gunicorn — Painel de Bordo
import os

workers      = 1
worker_class = 'gthread'
threads      = 4
timeout      = 90
keepalive    = 5
loglevel     = 'info'

# Railway define PORT automaticamente; Render usava 10000
bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
