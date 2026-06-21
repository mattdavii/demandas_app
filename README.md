# 📊 Gestor de Demandas Diárias - App Web PWA

Aplicação web moderna para gerenciar demandas diárias com suporte para mobile e desktop. Desenvolvida em Flask com PostgreSQL (Neon) e pronta para deployment no Render.

## ✨ Funcionalidades

- ✅ **PWA (Progressive Web App)** - Instale como app nativo em mobile e desktop
- ✅ **Offline First** - Funciona offline com Service Worker
- ✅ **Responsivo** - Interface adaptada para mobile, tablet e desktop
- ✅ **Demandas por Grupo** - Organize por BACKOFFICE e ATENDIMENTOS
- ✅ **Histórico** - Rastreie todas as demandas concluídas
- ✅ **Texto para WhatsApp** - Gere relatórios formatados automaticamente
- ✅ **Importar/Exportar** - Backup de dados em JSON
- ✅ **Google Drive Sync** - Sincronize com Google Drive (opcional)

## 📋 Requisitos

- Python 3.8+
- Git
- Conta no Render (gratuita)
- Banco de dados PostgreSQL no Neon (gratuito)
- Node.js (opcional, para desenvolvimento)

## 🚀 Quick Start Local

### 1. Clonar/Copiar repositório

```bash
# Crie uma pasta para o projeto
mkdir demandas-app
cd demandas-app

# Copie todos os arquivos fornecidos
# app.py, requirements.txt, index.html, sw.js, etc.
```

### 2. Configurar ambiente virtual

```bash
# Criar ambiente virtual
python -m venv venv

# Ativar (Linux/Mac)
source venv/bin/activate

# Ativar (Windows)
venv\Scripts\activate
```

### 3. Instalar dependências

```bash
pip install -r requirements.txt
```

### 4. Configurar banco de dados

Crie um arquivo `.env`:

```bash
DATABASE_URL=postgresql://user:password@localhost/demandas_db
FLASK_ENV=development
PORT=5000
```

**Opção 1: PostgreSQL Local**
```bash
# Criar banco de dados
createdb demandas_db

# No .env usar:
DATABASE_URL=postgresql://localhost/demandas_db
```

**Opção 2: Neon (Recomendado para produção)**
- Acesse [neon.tech](https://neon.tech)
- Criar projeto grátis
- Copiar connection string para `.env`

### 5. Inicializar banco de dados

```bash
python app.py
```

A aplicação criará as tabelas automaticamente.

### 6. Rodar localmente

```bash
python app.py
```

Abra em seu navegador: http://localhost:5000

## 📥 Importar Dados Anteriores

### Exportar dados do widget anterior (localStorage)

No widget HTML anterior, na aba "Sincronizar" → "Exportar":
1. Clique em "Gerar dados para copiar"
2. Clique em "Copiar JSON"
3. Salve em um arquivo `dados_backup.json`

### Importar para o novo app

**Opção 1: Via script (recomendado)**
```bash
python migrate.py dados_backup.json
```

**Opção 2: Via interface do app**
1. Acesse http://localhost:5000
2. Vá para aba "⚙️ Sincronizar"
3. Seção "📤 Importar Dados"
4. Cole o JSON
5. Clique "✅ Importar Dados"

## 🌐 Deploy no Render

### 1. Preparar repositório GitHub

```bash
# Inicializar git
git init

# Criar .gitignore
# Adicione os arquivos
git add .
git commit -m "Initial commit"

# Criar repositório no GitHub
# (https://github.com/new)

# Fazer push
git remote add origin https://github.com/seu-usuario/demandas-app.git
git branch -M main
git push -u origin main
```

### 2. Criar banco de dados no Neon

1. Acesse [neon.tech](https://neon.tech)
2. Sign up grátis
3. Criar novo projeto
4. Copiar connection string `postgresql://...`

### 3. Deploy no Render

1. Acesse [render.com](https://render.com)
2. Sign up/Login
3. Clique "New +" → "Web Service"
4. Conecte seu repositório GitHub
5. Configure:
   - **Name**: demandas-app (ou seu nome)
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`

6. Adicione variáveis de ambiente:
   - **DATABASE_URL**: Cole sua URL do Neon
   - **FLASK_ENV**: `production`

7. Criar serviço (ele fará deploy automático)

### 4. Verificar deploy

Seu app estará em: `https://demandas-app.onrender.com`

## 🔄 Fluxo de Trabalho

### Para adicionar/editar demandas:

1. **Aba "Pendências"** - Veja todos os cards de demandas
2. **Aba "Hoje"** - Adicione novas demandas e gere texto WhatsApp
3. **Aba "Histórico"** - Veja tudo que foi concluído
4. **Aba "Sincronizar"** - Backup e restauração de dados

### Para gerar relatório WhatsApp:

1. Vá para aba "Hoje"
2. Adicione/edite demandas conforme necessário
3. Atualize o status de cada uma
4. Copie o texto formatado na seção "Texto para WhatsApp"
5. Cole no WhatsApp

## 📱 Instalar como App

### No Mobile (Android/iOS)

1. Abra o app no navegador
2. Clique no ícone de compartilhar/menu
3. Selecione "Instalar app" ou "Add to Home Screen"
4. Pronto! Terá um ícone na tela inicial

### No Desktop

1. Abra em um navegador Chromium (Chrome, Edge)
2. Clique no ícone de instalação (canto superior direito)
3. Clique "Instalar"
4. Terá um atalho no menu

## 🔒 Segurança

- Dados sensíveis em variáveis de ambiente (`.env`)
- Conexão HTTPS automática no Render
- Banco de dados criptografado no Neon
- CORS configurado apenas para seu domínio

## 🛠️ Troubleshooting

### Erro "DATABASE_URL not found"
```bash
# Verifique se .env está configurado
cat .env
```

### Erro de conexão ao Neon
- Verifique a URL (copie direto do painel Neon)
- Certifique-se de que `psycopg2-binary` está instalado

### App não carrega após deploy
1. Abra console do navegador (F12)
2. Veja os erros
3. Verifique logs no Render: Settings → Logs

### Service Worker não funciona
- Aplicação deve estar em HTTPS (Render fornece isso)
- Limpe cache do navegador (Ctrl+Shift+Del)

## 📊 Estrutura de Banco de Dados

### Tabela: demands
```
id (Integer, PK)
section (String) - BACKOFFICE ou ATENDIMENTOS
location (String) - Local do trabalho
activity (String) - Descrição da atividade
context (Text) - Observações
status (String) - nao-iniciado, andamento, aguardando, aprovacao
created_date (Date)
updated_date (DateTime)
```

### Tabela: demand_history
```
id (Integer, PK)
section (String)
location (String)
activity (String)
context (Text)
status (String)
status_change_date (Date)
created_date (Date)
timestamp (DateTime)
```

## 🔌 API Endpoints

- `GET /api/demands` - Lista demandas pendentes
- `POST /api/demands` - Criar demanda
- `PUT /api/demands/<id>` - Atualizar demanda
- `DELETE /api/demands/<id>` - Deletar demanda
- `GET /api/history` - Histórico de demandas
- `GET /api/whatsapp-text` - Texto formatado para WhatsApp
- `GET /api/export` - Exportar dados em JSON
- `POST /api/import` - Importar dados de JSON
- `POST /api/status-update/<id>` - Atualizar status

## 📞 Suporte

Para problemas ou dúvidas:
1. Verifique o console do navegador (F12)
2. Veja logs no Render
3. Tente executar localmente para isolar o problema

## 📝 Licença

MIT - Livre para usar e modificar

## 🎯 Próximas Melhorias

- [ ] Integração com Google Calendar
- [ ] Notificações push
- [ ] Relatórios em PDF
- [ ] Autenticação de usuários
- [ ] Dark mode
- [ ] Múltiplos usuários/equipes

---

**Desenvolvido com ❤️ para melhorar sua produtividade**
