import os
import secrets
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from functools import wraps
from sqlalchemy import text

load_dotenv()

app = Flask(__name__)
CORS(app)

# ============= CONFIGURAÇÕES =============
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:password@localhost/demandas_db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,  # testa a conexão antes de usar; reconecta se o Neon já tiver derrubado
    'pool_recycle': 280,    # recicla conexões periodicamente, antes do banco fechar por ociosidade
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', secrets.token_urlsafe(32))
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=30)

# Email configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', True)
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', 'noreply@demandasapp.com')

db = SQLAlchemy(app)
jwt = JWTManager(app)
mail = Mail(app)

# ============= MODELOS =============
class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.now)
    is_active = db.Column(db.Boolean, default=True)
    reset_token = db.Column(db.String(255), unique=True, nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)
    access_verified = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)
    
    # Relacionamentos
    demands = db.relationship('Demand', backref='user', lazy=True, cascade='all, delete-orphan')
    demand_history = db.relationship('DemandHistory', backref='user', lazy=True, cascade='all, delete-orphan')
    work_groups = db.relationship('WorkGroup', backref='owner', lazy=True, cascade='all, delete-orphan')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def generate_reset_token(self):
        self.reset_token = secrets.token_urlsafe(32)
        self.reset_token_expires = datetime.now() + timedelta(hours=24)
        db.session.commit()
        return self.reset_token
    
    def verify_reset_token(self, token):
        if self.reset_token != token or self.reset_token_expires < datetime.now():
            return False
        return True
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'full_name': self.full_name,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'accessVerified': self.access_verified,
            'isAdmin': self.is_admin
        }

class WorkGroup(db.Model):
    __tablename__ = 'work_groups'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    emoji = db.Column(db.String(10), nullable=True)
    color = db.Column(db.String(7), default='#3b82f6')
    description = db.Column(db.String(255))
    order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # Relacionamento
    demands = db.relationship('Demand', backref='work_group', lazy=True)
    
    __table_args__ = (db.UniqueConstraint('user_id', 'name', name='unique_user_group_name'),)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'emoji': self.emoji,
            'color': self.color,
            'description': self.description,
            'order': self.order,
            'demandsCount': len([d for d in self.demands if d.status != 'concluido'])
        }

class Demand(db.Model):
    __tablename__ = 'demands'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    work_group_id = db.Column(db.Integer, db.ForeignKey('work_groups.id'), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    activity = db.Column(db.String(255), nullable=False)
    context = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='nao-iniciado')
    priority = db.Column(db.String(20), default='media')
    due_date = db.Column(db.Date, nullable=True)
    assigned_to = db.Column(db.String(100))
    created_date = db.Column(db.Date, default=date.today)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    reminder_at = db.Column(db.DateTime, nullable=True)
    reminder_sent = db.Column(db.Boolean, default=False)
    
    def to_dict(self):
        return {
            'id': self.id,
            'workGroupId': self.work_group_id,
            'workGroupName': self.work_group.name if self.work_group else None,
            'location': self.location,
            'activity': self.activity,
            'context': self.context,
            'status': self.status,
            'priority': self.priority,
            'dueDate': self.due_date.isoformat() if self.due_date else None,
            'assignedTo': self.assigned_to,
            'createdDate': self.created_date.isoformat() if self.created_date else None,
            'reminderAt': self.reminder_at.isoformat() if self.reminder_at else None
        }

class DemandHistory(db.Model):
    __tablename__ = 'demand_history'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    work_group_id = db.Column(db.Integer, db.ForeignKey('work_groups.id'), nullable=True)
    demand_id = db.Column(db.Integer, nullable=True)  # vínculo real com a demanda de origem (registros novos)
    location = db.Column(db.String(100), nullable=False)
    activity = db.Column(db.String(255), nullable=False)
    context = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False)
    status_change_date = db.Column(db.Date, nullable=False)
    created_date = db.Column(db.Date, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    
    def to_dict(self):
        return {
            'id': self.id,
            'demandId': self.demand_id,
            'workGroupId': self.work_group_id,
            'location': self.location,
            'activity': self.activity,
            'context': self.context,
            'status': self.status,
            'statusChangeDate': self.status_change_date.isoformat() if self.status_change_date else None,
            'createdDate': self.created_date.isoformat() if self.created_date else None
        }

class Note(db.Model):
    __tablename__ = 'notes'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    checklist = db.Column(db.JSON, default=list)  # [{"text": "...", "checked": false}, ...]
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'subject': self.subject,
            'description': self.description,
            'checklist': self.checklist or [],
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'updatedAt': self.updated_at.isoformat() if self.updated_at else None
        }

class AccessKey(db.Model):
    __tablename__ = 'access_keys'

    id = db.Column(db.Integer, primary_key=True)
    key_value = db.Column(db.String(64), unique=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    used_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        used_by_user = User.query.get(self.used_by) if self.used_by else None
        if self.used_by:
            status = 'used'
        elif not self.is_active:
            status = 'revoked'
        else:
            status = 'available'
        return {
            'id': self.id,
            'key': self.key_value,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'usedBy': used_by_user.username if used_by_user else None,
            'usedAt': self.used_at.isoformat() if self.used_at else None,
            'status': status
        }

class StatusConfig(db.Model):
    """Status de demanda configurável por usuário (substitui o conjunto fixo antigo)."""
    __tablename__ = 'status_configs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    key = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(7), nullable=False, default='#9aa0a7')
    emoji = db.Column(db.String(10), nullable=True)
    order = db.Column(db.Integer, default=0)
    is_completed = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint('user_id', 'key', name='unique_user_status_key'),)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'label': self.label,
            'color': self.color,
            'emoji': self.emoji,
            'order': self.order,
            'isCompleted': self.is_completed
        }

class PriorityConfig(db.Model):
    """Prioridade de demanda configurável por usuário."""
    __tablename__ = 'priority_configs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    key = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(7), nullable=False, default='#9aa0a7')
    order = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('user_id', 'key', name='unique_user_priority_key'),)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'label': self.label,
            'color': self.color,
            'order': self.order
        }

def seed_default_status_and_priority(user_id):
    """Cria o conjunto padrão de status/prioridade pra uma conta (nova ou já existente sem nenhum configurado)."""
    if StatusConfig.query.filter_by(user_id=user_id).first() is None:
        defaults = [
            {'key': 'agendado', 'label': 'Agendado', 'color': '#ff9f43', 'emoji': '🟠', 'order': 0, 'is_completed': False},
            {'key': 'nao-iniciado', 'label': 'Não Iniciado', 'color': '#9aa0a7', 'emoji': '⚪', 'order': 1, 'is_completed': False},
            {'key': 'andamento', 'label': 'Em Andamento', 'color': '#f5a623', 'emoji': '🟡', 'order': 2, 'is_completed': False},
            {'key': 'aguardando', 'label': 'Aguardando', 'color': '#4fc3f7', 'emoji': '🔵', 'order': 3, 'is_completed': False},
            {'key': 'aprovacao', 'label': 'Aprovação', 'color': '#b388ff', 'emoji': '🟣', 'order': 4, 'is_completed': False},
            {'key': 'concluido', 'label': 'Concluído', 'color': '#3ddc84', 'emoji': '🟢', 'order': 5, 'is_completed': True},
        ]
        for d in defaults:
            db.session.add(StatusConfig(user_id=user_id, **d))

    if PriorityConfig.query.filter_by(user_id=user_id).first() is None:
        defaults = [
            {'key': 'baixa', 'label': 'Baixa', 'color': '#5b6168', 'order': 0},
            {'key': 'media', 'label': 'Média', 'color': '#4fc3f7', 'order': 1},
            {'key': 'alta', 'label': 'Alta', 'color': '#f5a623', 'order': 2},
            {'key': 'urgente', 'label': 'Urgente', 'color': '#ff5b5b', 'order': 3},
        ]
        for d in defaults:
            db.session.add(PriorityConfig(user_id=user_id, **d))

    db.session.commit()

# ============= CRIAR TABELAS NA INICIALIZAÇÃO =============
with app.app_context():
    db.create_all()
    # Migração leve para colunas novas em bancos já existentes
    # (db.create_all() só cria tabelas que não existem, não altera as existentes)
    try:
        db.session.execute(text("ALTER TABLE demands ADD COLUMN IF NOT EXISTS reminder_at TIMESTAMP"))
        db.session.execute(text("ALTER TABLE demands ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE"))
        db.session.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS access_verified BOOLEAN DEFAULT TRUE"))
        db.session.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"))
        db.session.execute(text("ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS demand_id INTEGER"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Aviso: migração de colunas de lembrete não aplicada (provável SQLite local, ignorar): {e}")

    # Promove automaticamente a conta mais antiga a administrador, caso ainda não haja nenhum
    # (garante que o sistema de chaves tenha um admin de partida, sem passo manual)
    try:
        if User.query.filter_by(is_admin=True).first() is None:
            first_user = User.query.order_by(User.id.asc()).first()
            if first_user:
                first_user.is_admin = True
                first_user.access_verified = True
                db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Aviso: não foi possível definir admin automático: {e}")

    # Garante que toda conta já existente (criada antes desta atualização) tenha
    # o conjunto padrão de status/prioridade, já que esses dados não existiam antes
    try:
        for existing_user in User.query.all():
            seed_default_status_and_priority(existing_user.id)
    except Exception as e:
        db.session.rollback()
        print(f"Aviso: não foi possível popular status/prioridade padrão: {e}")

# ============= ROTAS DE PÁGINA =============
@app.route('/')
def index():
    """Servir página principal"""
    return render_template('index.html')

@app.route('/reset')
def reset_page():
    """Servir página de reset de senha"""
    return render_template('reset.html')

# ============= PWA: MANIFEST, SERVICE WORKER, FAVICON =============
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.ico', mimetype='image/vnd.microsoft.icon')

# ============= ROTAS DE AUTENTICAÇÃO =============
@app.route('/api/auth/register', methods=['POST'])
def register():
    """Registrar novo usuário"""
    data = request.get_json()
    
    if not data or not data.get('username') or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Dados incompletos'}), 400
    
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Usuário já existe'}), 409
    
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email já registrado'}), 409
    
    user = User(
        username=data['username'],
        email=data['email'],
        full_name=data.get('full_name', ''),
        access_verified=False
    )
    user.set_password(data['password'])
    
    db.session.add(user)
    db.session.commit()

    seed_default_status_and_priority(user.id)
    
    # Criar grupos padrão
    default_groups = [
        {'name': 'BACKOFFICE', 'emoji': '👨🏻‍💻', 'order': 1},
        {'name': 'ATENDIMENTOS', 'emoji': '👨🏼‍🔧', 'order': 2}
    ]
    
    for group_data in default_groups:
        group = WorkGroup(
            user_id=user.id,
            name=group_data['name'],
            emoji=group_data['emoji'],
            order=group_data['order']
        )
        db.session.add(group)

    db.session.commit()
    
    access_token = create_access_token(identity=str(user.id))

    # E-mail de boas-vindas
    if app.config['MAIL_USERNAME']:
        try:
            frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:5000')
            if user.access_verified:
                body_html = f'''
                <p>Olá {user.full_name or user.username},</p>
                <p>Sua conta no <strong>Painel de Bordo</strong> foi criada com sucesso!</p>
                <p>Você já pode acessar a plataforma normalmente:</p>
                <p><a href="{frontend_url}">Acessar Painel de Bordo</a></p>
                '''
            else:
                body_html = f'''
                <p>Olá {user.full_name or user.username},</p>
                <p>Sua conta no <strong>Painel de Bordo</strong> foi criada com sucesso!</p>
                <p>Antes de começar a usar a plataforma, você precisa de uma <strong>chave de acesso</strong> fornecida pelo administrador.</p>
                <p>Entre em contato com quem te convidou para receber sua chave e liberar o acesso completo.</p>
                '''
            msg = Message(
                'Bem-vindo ao Painel de Bordo!',
                recipients=[user.email],
                html=body_html
            )
            mail.send(msg)
        except Exception as e:
            print(f'Erro ao enviar email de boas-vindas: {e}')

    return jsonify({
        'message': 'Usuário registrado com sucesso',
        'user': user.to_dict(),
        'access_token': access_token
    }), 201

@app.route('/api/auth/login', methods=['POST'])
def login():
    """Login do usuário"""
    data = request.get_json()
    
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Usuário ou senha não fornecidos'}), 400
    
    user = User.query.filter_by(username=data['username']).first()
    
    if not user or not user.check_password(data['password']):
        return jsonify({'error': 'Usuário ou senha incorretos'}), 401
    
    if not user.is_active:
        return jsonify({'error': 'Usuário desativado'}), 403
    
    access_token = create_access_token(identity=str(user.id))
    return jsonify({
        'message': 'Login realizado com sucesso',
        'user': user.to_dict(),
        'access_token': access_token
    }), 200

@app.route('/api/auth/request-reset', methods=['POST'])
def request_reset():
    """Solicitar reset de senha"""
    data = request.get_json()
    
    if not data or not data.get('email'):
        return jsonify({'error': 'Email não fornecido'}), 400
    
    user = User.query.filter_by(email=data['email']).first()
    
    if not user:
        return jsonify({'message': 'Se o email existe, você receberá um link de reset'}), 200
    
    reset_token = user.generate_reset_token()
    reset_url = f"{os.getenv('FRONTEND_URL', 'http://localhost:5000')}/reset?token={reset_token}"
    
    if app.config['MAIL_USERNAME']:
        try:
            msg = Message(
                'Reset de Senha - Painel de Bordo',
                recipients=[user.email],
                html=f'''
                <p>Olá {user.full_name or user.username},</p>
                <p>Para resetar sua senha, clique no link abaixo:</p>
                <p><a href="{reset_url}">Resetar Senha</a></p>
                <p>Este link expira em 24 horas.</p>
                <p>Se você não solicitou isso, ignore este email.</p>
                '''
            )
            mail.send(msg)
        except Exception as e:
            print(f'Erro ao enviar email: {e}')
    
    return jsonify({'message': 'Link de reset enviado para seu email'}), 200

@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    """Resetar senha com token"""
    data = request.get_json()
    
    if not data or not data.get('token') or not data.get('password'):
        return jsonify({'error': 'Token ou senha não fornecidos'}), 400
    
    user = User.query.filter_by(reset_token=data['token']).first()
    
    if not user or not user.verify_reset_token(data['token']):
        return jsonify({'error': 'Token inválido ou expirado'}), 400
    
    user.set_password(data['password'])
    user.reset_token = None
    user.reset_token_expires = None
    db.session.commit()
    
    return jsonify({'message': 'Senha resetada com sucesso'}), 200

@app.route('/api/auth/me', methods=['GET'])
@jwt_required()
def get_current_user():
    """Obter dados do usuário atual"""
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404
    
    return jsonify(user.to_dict()), 200

@app.route('/api/auth/verify-key', methods=['POST'])
@jwt_required()
def verify_access_key():
    """Libera o acesso da conta usando uma chave de convite válida (uso único)"""
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)

    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    if user.access_verified:
        return jsonify({'message': 'Acesso já liberado', 'user': user.to_dict()}), 200

    data = request.get_json()
    key_value = (data or {}).get('key', '').strip()

    if not key_value:
        return jsonify({'error': 'Chave não fornecida'}), 400

    access_key = AccessKey.query.filter_by(key_value=key_value).first()

    if not access_key:
        return jsonify({'error': 'Chave inválida'}), 404
    if not access_key.is_active:
        return jsonify({'error': 'Chave revogada'}), 403
    if access_key.used_by is not None:
        return jsonify({'error': 'Chave já utilizada'}), 409

    access_key.used_by = user.id
    access_key.used_at = datetime.now()
    user.access_verified = True
    db.session.commit()

    if app.config['MAIL_USERNAME']:
        try:
            frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:5000')
            msg = Message(
                'Acesso liberado - Painel de Bordo',
                recipients=[user.email],
                html=f'''
                <p>Olá {user.full_name or user.username},</p>
                <p>Seu acesso ao <strong>Painel de Bordo</strong> foi liberado com sucesso!</p>
                <p>Você já pode entrar e começar a usar a plataforma:</p>
                <p><a href="{frontend_url}">Acessar Painel de Bordo</a></p>
                '''
            )
            mail.send(msg)
        except Exception as e:
            print(f'Erro ao enviar email de acesso liberado: {e}')

    return jsonify({'message': 'Acesso liberado com sucesso', 'user': user.to_dict()}), 200

def admin_required(user_id):
    user = User.query.get(user_id)
    return user is not None and user.is_admin

@app.route('/api/admin/keys', methods=['GET'])
@jwt_required()
def list_access_keys():
    """Listar todas as chaves de acesso (somente admin)"""
    user_id = int(get_jwt_identity())
    if not admin_required(user_id):
        return jsonify({'error': 'Acesso negado'}), 403

    keys = AccessKey.query.order_by(AccessKey.created_at.desc()).all()
    return jsonify([k.to_dict() for k in keys]), 200

@app.route('/api/admin/keys', methods=['POST'])
@jwt_required()
def create_access_key():
    """Gerar nova chave de acesso de uso único (somente admin)"""
    user_id = int(get_jwt_identity())
    if not admin_required(user_id):
        return jsonify({'error': 'Acesso negado'}), 403

    new_key = AccessKey(
        key_value=secrets.token_hex(6),
        created_by=user_id
    )
    db.session.add(new_key)
    db.session.commit()

    return jsonify(new_key.to_dict()), 201

@app.route('/api/admin/keys/<int:key_id>', methods=['DELETE'])
@jwt_required()
def revoke_access_key(key_id):
    """Revogar uma chave ainda não utilizada (somente admin)"""
    user_id = int(get_jwt_identity())
    if not admin_required(user_id):
        return jsonify({'error': 'Acesso negado'}), 403

    key = AccessKey.query.get_or_404(key_id)
    if key.used_by is not None:
        return jsonify({'error': 'Não é possível revogar uma chave já utilizada'}), 400

    db.session.delete(key)
    db.session.commit()

    return jsonify({'message': 'Chave revogada'}), 200

@app.route('/api/auth/change-password', methods=['POST'])
@jwt_required()
def change_password():
    """Alterar senha do usuário logado (requer senha atual)"""
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)

    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    data = request.get_json()

    if not data or not data.get('current_password') or not data.get('new_password'):
        return jsonify({'error': 'Senha atual e nova senha são obrigatórias'}), 400

    if not user.check_password(data['current_password']):
        return jsonify({'error': 'Senha atual incorreta'}), 401

    if len(data['new_password']) < 6:
        return jsonify({'error': 'A nova senha deve ter pelo menos 6 caracteres'}), 400

    user.set_password(data['new_password'])
    db.session.commit()

    return jsonify({'message': 'Senha alterada com sucesso'}), 200

# ============= ROTAS DE STATUS CONFIGURÁVEL =============
@app.route('/api/status-configs', methods=['GET'])
@jwt_required()
def get_status_configs():
    user_id = int(get_jwt_identity())
    seed_default_status_and_priority(user_id)  # garante que sempre haja pelo menos o padrão
    configs = StatusConfig.query.filter_by(user_id=user_id).order_by(StatusConfig.order.asc()).all()
    return jsonify([c.to_dict() for c in configs]), 200

@app.route('/api/status-configs', methods=['POST'])
@jwt_required()
def create_status_config():
    user_id = int(get_jwt_identity())
    data = request.get_json()

    if not data or not data.get('key') or not data.get('label'):
        return jsonify({'error': 'Chave e nome são obrigatórios'}), 400

    if StatusConfig.query.filter_by(user_id=user_id, key=data['key']).first():
        return jsonify({'error': 'Já existe um status com essa chave'}), 409

    max_order = db.session.query(db.func.max(StatusConfig.order)).filter_by(user_id=user_id).scalar() or 0

    config = StatusConfig(
        user_id=user_id,
        key=data['key'],
        label=data['label'],
        color=data.get('color', '#9aa0a7'),
        emoji=data.get('emoji', ''),
        order=data.get('order', max_order + 1),
        is_completed=data.get('isCompleted', False)
    )
    db.session.add(config)
    db.session.commit()

    return jsonify(config.to_dict()), 201

@app.route('/api/status-configs/<int:config_id>', methods=['PUT'])
@jwt_required()
def update_status_config(config_id):
    user_id = int(get_jwt_identity())
    config = StatusConfig.query.get_or_404(config_id)

    if config.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()

    if 'label' in data:
        config.label = data['label']
    if 'color' in data:
        config.color = data['color']
    if 'emoji' in data:
        config.emoji = data['emoji']
    if 'order' in data:
        config.order = data['order']
    if 'isCompleted' in data:
        # impede remover o último status marcado como conclusivo, senão "Concluir" para de funcionar
        if not data['isCompleted']:
            other_completed = StatusConfig.query.filter(
                StatusConfig.user_id == user_id,
                StatusConfig.is_completed == True,
                StatusConfig.id != config.id
            ).first()
            if config.is_completed and not other_completed:
                return jsonify({'error': 'Precisa existir ao menos um status marcado como conclusivo'}), 400
        config.is_completed = data['isCompleted']

    db.session.commit()
    return jsonify(config.to_dict()), 200

@app.route('/api/status-configs/<int:config_id>', methods=['DELETE'])
@jwt_required()
def delete_status_config(config_id):
    user_id = int(get_jwt_identity())
    config = StatusConfig.query.get_or_404(config_id)

    if config.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403

    if StatusConfig.query.filter_by(user_id=user_id).count() <= 1:
        return jsonify({'error': 'Não é possível remover o último status restante'}), 400

    in_use = Demand.query.filter_by(user_id=user_id, status=config.key).first()
    if in_use:
        return jsonify({'error': 'Existem demandas usando esse status. Mude o status delas antes de remover.'}), 400

    if config.is_completed:
        other_completed = StatusConfig.query.filter(
            StatusConfig.user_id == user_id,
            StatusConfig.is_completed == True,
            StatusConfig.id != config.id
        ).first()
        if not other_completed:
            return jsonify({'error': 'Precisa existir ao menos um status marcado como conclusivo'}), 400

    db.session.delete(config)
    db.session.commit()

    return jsonify({'message': 'Status removido'}), 200

# ============= ROTAS DE PRIORIDADE CONFIGURÁVEL =============
@app.route('/api/priority-configs', methods=['GET'])
@jwt_required()
def get_priority_configs():
    user_id = int(get_jwt_identity())
    seed_default_status_and_priority(user_id)
    configs = PriorityConfig.query.filter_by(user_id=user_id).order_by(PriorityConfig.order.asc()).all()
    return jsonify([c.to_dict() for c in configs]), 200

@app.route('/api/priority-configs', methods=['POST'])
@jwt_required()
def create_priority_config():
    user_id = int(get_jwt_identity())
    data = request.get_json()

    if not data or not data.get('key') or not data.get('label'):
        return jsonify({'error': 'Chave e nome são obrigatórios'}), 400

    if PriorityConfig.query.filter_by(user_id=user_id, key=data['key']).first():
        return jsonify({'error': 'Já existe uma prioridade com essa chave'}), 409

    max_order = db.session.query(db.func.max(PriorityConfig.order)).filter_by(user_id=user_id).scalar() or 0

    config = PriorityConfig(
        user_id=user_id,
        key=data['key'],
        label=data['label'],
        color=data.get('color', '#9aa0a7'),
        order=data.get('order', max_order + 1)
    )
    db.session.add(config)
    db.session.commit()

    return jsonify(config.to_dict()), 201

@app.route('/api/priority-configs/<int:config_id>', methods=['PUT'])
@jwt_required()
def update_priority_config(config_id):
    user_id = int(get_jwt_identity())
    config = PriorityConfig.query.get_or_404(config_id)

    if config.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()

    if 'label' in data:
        config.label = data['label']
    if 'color' in data:
        config.color = data['color']
    if 'order' in data:
        config.order = data['order']

    db.session.commit()
    return jsonify(config.to_dict()), 200

@app.route('/api/priority-configs/<int:config_id>', methods=['DELETE'])
@jwt_required()
def delete_priority_config(config_id):
    user_id = int(get_jwt_identity())
    config = PriorityConfig.query.get_or_404(config_id)

    if config.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403

    if PriorityConfig.query.filter_by(user_id=user_id).count() <= 1:
        return jsonify({'error': 'Não é possível remover a última prioridade restante'}), 400

    in_use = Demand.query.filter_by(user_id=user_id, priority=config.key).first()
    if in_use:
        return jsonify({'error': 'Existem demandas usando essa prioridade. Mude a prioridade delas antes de remover.'}), 400

    db.session.delete(config)
    db.session.commit()

    return jsonify({'message': 'Prioridade removida'}), 200

# ============= ROTAS DE GRUPOS DE TRABALHO =============
@app.route('/api/work-groups', methods=['GET'])
@jwt_required()
def get_work_groups():
    """Listar grupos de trabalho do usuário"""
    user_id = int(get_jwt_identity())
    groups = WorkGroup.query.filter_by(user_id=user_id, is_active=True).order_by(WorkGroup.order).all()
    return jsonify([g.to_dict() for g in groups]), 200

@app.route('/api/work-groups', methods=['POST'])
@jwt_required()
def create_work_group():
    """Criar novo grupo de trabalho"""
    user_id = int(get_jwt_identity())
    data = request.get_json()
    
    if not data or not data.get('name'):
        return jsonify({'error': 'Nome do grupo é obrigatório'}), 400
    
    group = WorkGroup(
        user_id=user_id,
        name=data['name'],
        emoji=data.get('emoji', '📌'),
        color=data.get('color', '#3b82f6'),
        description=data.get('description', ''),
        order=data.get('order', 0)
    )
    
    db.session.add(group)
    db.session.commit()
    
    return jsonify(group.to_dict()), 201

@app.route('/api/work-groups/<int:group_id>', methods=['PUT'])
@jwt_required()
def update_work_group(group_id):
    """Atualizar grupo de trabalho"""
    user_id = int(get_jwt_identity())
    group = WorkGroup.query.get_or_404(group_id)
    
    if group.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    data = request.get_json()
    
    if 'name' in data:
        group.name = data['name']
    if 'emoji' in data:
        group.emoji = data['emoji']
    if 'color' in data:
        group.color = data['color']
    if 'description' in data:
        group.description = data['description']
    if 'order' in data:
        group.order = data['order']
    
    db.session.commit()
    return jsonify(group.to_dict()), 200

@app.route('/api/work-groups/<int:group_id>', methods=['DELETE'])
@jwt_required()
def delete_work_group(group_id):
    """Deletar grupo de trabalho"""
    user_id = int(get_jwt_identity())
    group = WorkGroup.query.get_or_404(group_id)
    
    if group.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    group.is_active = False
    db.session.commit()
    
    return jsonify({'message': 'Grupo deletado'}), 200

# ============= ROTAS DE DEMANDAS =============
@app.route('/api/demands', methods=['GET'])
@jwt_required()
def get_demands():
    """Listar demandas pendentes do usuário"""
    user_id = int(get_jwt_identity())
    terminal_keys = [s.key for s in StatusConfig.query.filter_by(user_id=user_id, is_completed=True).all()]
    query = Demand.query.filter(Demand.user_id == user_id)
    if terminal_keys:
        query = query.filter(~Demand.status.in_(terminal_keys))
    demands = query.all()
    return jsonify([d.to_dict() for d in demands]), 200

@app.route('/api/demands', methods=['POST'])
@jwt_required()
def create_demand():
    """Criar nova demanda"""
    user_id = int(get_jwt_identity())
    data = request.get_json()
    
    if not data or not data.get('work_group_id') or not data.get('location') or not data.get('activity'):
        return jsonify({'error': 'Dados incompletos'}), 400
    
    group = WorkGroup.query.get(data['work_group_id'])
    if not group or group.user_id != user_id:
        return jsonify({'error': 'Grupo inválido'}), 403

    default_status = data.get('status')
    if not default_status:
        first_status = StatusConfig.query.filter_by(user_id=user_id, is_completed=False).order_by(StatusConfig.order.asc()).first()
        default_status = first_status.key if first_status else 'nao-iniciado'

    default_priority = data.get('priority')
    if not default_priority:
        media_priority = PriorityConfig.query.filter_by(user_id=user_id, key='media').first()
        if media_priority:
            default_priority = media_priority.key
        else:
            any_priority = PriorityConfig.query.filter_by(user_id=user_id).order_by(PriorityConfig.order.asc()).first()
            default_priority = any_priority.key if any_priority else 'media'

    demand = Demand(
        user_id=user_id,
        work_group_id=data['work_group_id'],
        location=data['location'],
        activity=data['activity'],
        context=data.get('context', ''),
        status=default_status,
        priority=default_priority,
        due_date=datetime.strptime(data['due_date'], '%Y-%m-%d').date() if data.get('due_date') else None,
        assigned_to=data.get('assigned_to', ''),
        reminder_at=datetime.strptime(data['reminder_at'], '%Y-%m-%dT%H:%M') if data.get('reminder_at') else None
    )
    
    db.session.add(demand)
    db.session.commit()
    
    return jsonify(demand.to_dict()), 201

@app.route('/api/demands/<int:demand_id>', methods=['PUT'])
@jwt_required()
def update_demand(demand_id):
    """Atualizar demanda"""
    user_id = int(get_jwt_identity())
    demand = Demand.query.get_or_404(demand_id)
    
    if demand.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    data = request.get_json()
    
    if 'location' in data:
        demand.location = data['location']
    if 'activity' in data:
        demand.activity = data['activity']
    if 'context' in data:
        demand.context = data['context']
    if 'status' in data:
        demand.status = data['status']
    if 'priority' in data:
        demand.priority = data['priority']
    if 'due_date' in data:
        demand.due_date = datetime.strptime(data['due_date'], '%Y-%m-%d').date() if data['due_date'] else None
    if 'assigned_to' in data:
        demand.assigned_to = data['assigned_to']
    if 'reminder_at' in data:
        if data['reminder_at']:
            demand.reminder_at = datetime.strptime(data['reminder_at'], '%Y-%m-%dT%H:%M')
            demand.reminder_sent = False  # rearma o lembrete se a data/hora mudou
        else:
            demand.reminder_at = None
            demand.reminder_sent = False
    
    db.session.commit()
    return jsonify(demand.to_dict()), 200

@app.route('/api/demands/<int:demand_id>', methods=['DELETE'])
@jwt_required()
def delete_demand(demand_id):
    """Deletar demanda"""
    user_id = int(get_jwt_identity())
    demand = Demand.query.get_or_404(demand_id)
    
    if demand.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    db.session.delete(demand)
    db.session.commit()
    
    return jsonify({'message': 'Demanda deletada'}), 200

@app.route('/api/demands/<int:demand_id>/status', methods=['POST'])
@jwt_required()
def update_demand_status(demand_id):
    """Atualizar status de demanda e mover para histórico se concluído"""
    user_id = int(get_jwt_identity())
    demand = Demand.query.get_or_404(demand_id)
    
    if demand.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    data = request.get_json()
    new_status = data.get('status')
    
    if not new_status:
        return jsonify({'error': 'Status não fornecido'}), 400
    
    history = DemandHistory(
        user_id=user_id,
        work_group_id=demand.work_group_id,
        demand_id=demand.id,
        location=demand.location,
        activity=demand.activity,
        context=demand.context,
        status=new_status,
        status_change_date=date.today(),
        created_date=demand.created_date
    )
    db.session.add(history)

    status_config = StatusConfig.query.filter_by(user_id=user_id, key=new_status).first()
    is_terminal = status_config.is_completed if status_config else (new_status == 'concluido')

    if is_terminal:
        db.session.delete(demand)
    else:
        demand.status = new_status
    
    db.session.commit()
    return jsonify({'message': 'Status atualizado'}), 200

# ============= ROTAS DE NOTAS =============
@app.route('/api/notes', methods=['GET'])
@jwt_required()
def get_notes():
    """Listar notas do usuário (mais recentes primeiro)"""
    user_id = int(get_jwt_identity())
    notes = Note.query.filter_by(user_id=user_id).order_by(Note.updated_at.desc()).all()
    return jsonify([n.to_dict() for n in notes]), 200

@app.route('/api/notes', methods=['POST'])
@jwt_required()
def create_note():
    """Criar nova nota"""
    user_id = int(get_jwt_identity())
    data = request.get_json()

    if not data or not data.get('subject'):
        return jsonify({'error': 'Assunto é obrigatório'}), 400

    note = Note(
        user_id=user_id,
        subject=data['subject'],
        description=data.get('description', ''),
        checklist=data.get('checklist', [])
    )
    db.session.add(note)
    db.session.commit()

    return jsonify(note.to_dict()), 201

@app.route('/api/notes/<int:note_id>', methods=['PUT'])
@jwt_required()
def update_note(note_id):
    """Atualizar nota (assunto, descrição e/ou checklist)"""
    user_id = int(get_jwt_identity())
    note = Note.query.get_or_404(note_id)

    if note.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Dados não fornecidos'}), 400

    if 'subject' in data:
        if not data['subject']:
            return jsonify({'error': 'Assunto é obrigatório'}), 400
        note.subject = data['subject']
    if 'description' in data:
        note.description = data['description']
    if 'checklist' in data:
        note.checklist = data['checklist']

    db.session.commit()
    return jsonify(note.to_dict()), 200

@app.route('/api/notes/<int:note_id>', methods=['DELETE'])
@jwt_required()
def delete_note(note_id):
    """Deletar nota"""
    user_id = int(get_jwt_identity())
    note = Note.query.get_or_404(note_id)

    if note.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403

    db.session.delete(note)
    db.session.commit()

    return jsonify({'message': 'Nota deletada'}), 200

# ============= ROTAS DE LEMBRETE =============
@app.route('/api/reminders/check', methods=['POST'])
@jwt_required()
def check_reminders():
    """Verifica lembretes vencidos do usuário, envia email e marca como enviados.
    Chamada pelo frontend ao abrir o app (verificação best-effort, sem cron)."""
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)

    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    now = datetime.now()
    due_demands = Demand.query.filter(
        Demand.user_id == user_id,
        Demand.reminder_at.isnot(None),
        Demand.reminder_at <= now,
        Demand.reminder_sent == False,
        Demand.status != 'concluido'
    ).all()

    triggered = []
    for demand in due_demands:
        if app.config['MAIL_USERNAME']:
            try:
                msg = Message(
                    f'Lembrete: {demand.activity}',
                    recipients=[user.email],
                    html=f'''
                    <p>Olá {user.full_name or user.username},</p>
                    <p>Lembrete da demanda:</p>
                    <p><strong>{demand.location}:</strong> {demand.activity}</p>
                    {f"<p>{demand.context}</p>" if demand.context else ""}
                    <p>Status atual: {demand.status}</p>
                    '''
                )
                mail.send(msg)
            except Exception as e:
                print(f'Erro ao enviar email de lembrete: {e}')

        demand.reminder_sent = True
        triggered.append(demand.to_dict())

    if due_demands:
        db.session.commit()

    return jsonify({'triggered': triggered}), 200

@app.route('/api/locations/merge', methods=['POST'])
@jwt_required()
def merge_locations():
    """Renomeia todos os registros (ativos e histórico) de um local pro nome de outro.
    Útil pra unificar variações de digitação do mesmo local (ex: 'IBIAPINA 1' -> 'IBIAPINA 01')."""
    user_id = int(get_jwt_identity())
    data = request.get_json()

    from_location = (data or {}).get('from', '').strip()
    to_location = (data or {}).get('to', '').strip()

    if not from_location or not to_location:
        return jsonify({'error': 'Informe os dois locais (origem e destino)'}), 400
    if from_location == to_location:
        return jsonify({'error': 'Os locais devem ser diferentes'}), 400

    demands_updated = Demand.query.filter_by(user_id=user_id, location=from_location).update(
        {Demand.location: to_location}
    )
    history_updated = DemandHistory.query.filter_by(user_id=user_id, location=from_location).update(
        {DemandHistory.location: to_location}
    )
    db.session.commit()

    return jsonify({
        'message': 'Locais mesclados com sucesso',
        'demandsUpdated': demands_updated,
        'historyUpdated': history_updated
    }), 200

# ============= ROTAS DE HISTÓRICO =============
@app.route('/api/history', methods=['GET'])
@jwt_required()
def get_history():
    """Listar histórico de demandas"""
    user_id = int(get_jwt_identity())
    location = request.args.get('location', '')
    activity = request.args.get('activity', '')
    
    query = DemandHistory.query.filter_by(user_id=user_id)
    
    if location:
        query = query.filter(DemandHistory.location.ilike(f'%{location}%'))
    if activity:
        query = query.filter(DemandHistory.activity.ilike(f'%{activity}%'))
    
    history = query.order_by(DemandHistory.status_change_date.desc()).all()
    return jsonify([h.to_dict() for h in history]), 200

# ============= ROTAS DE RELATÓRIO =============
@app.route('/api/whatsapp-text', methods=['GET'])
@jwt_required()
def get_whatsapp_text():
    """Gera texto formatado para WhatsApp"""
    user_id = int(get_jwt_identity())
    today = date.today().strftime('%d/%m/%Y')
    output = f"_*{today}*_\n"

    terminal_keys = [s.key for s in StatusConfig.query.filter_by(user_id=user_id, is_completed=True).all()]

    demands_query = Demand.query.filter(Demand.user_id == user_id)
    if terminal_keys:
        demands_query = demands_query.filter(~Demand.status.in_(terminal_keys))
    demands = demands_query.all()

    history_query = DemandHistory.query.filter(
        DemandHistory.user_id == user_id,
        DemandHistory.status_change_date == date.today()
    )
    if terminal_keys:
        history_query = history_query.filter(DemandHistory.status.in_(terminal_keys))
    else:
        history_query = history_query.filter(DemandHistory.status == 'concluido')
    history = history_query.all()
    
    groups = WorkGroup.query.filter_by(user_id=user_id, is_active=True).order_by(WorkGroup.order).all()
    
    STATUS_EMOJI = {
        s.key: s.emoji for s in StatusConfig.query.filter_by(user_id=user_id).all()
    }
    STATUS_LABEL = {
        s.key: s.label for s in StatusConfig.query.filter_by(user_id=user_id).all()
    }
    
    for group in groups:
        group_demands = [d for d in demands if d.work_group_id == group.id]
        group_history = [h for h in history if h.work_group_id == group.id]
        
        if group_demands or group_history:
            output += f"_*{group.emoji} {group.name}:*_\n"
            
            for d in group_demands:
                emoji = STATUS_EMOJI.get(d.status, '⚪')
                d_label = STATUS_LABEL.get(d.status, d.status).upper()
                output += f"> *{d.location}:* {d.activity}"
                if d.context:
                    output += f" _({d.context})_"
                output += f"; _*{d_label} {emoji}*_\n"
            
            for h in group_history:
                output += f"> *{h.location}:* {h.activity}"
                if h.context:
                    output += f" _({h.context})_"
                h_label = STATUS_LABEL.get(h.status, 'Concluído').upper()
                h_emoji = STATUS_EMOJI.get(h.status, '🟢')
                output += f"; _*{h_label} {h_emoji}*_\n"
            
            output += "\n"
    
    return jsonify({'text': output}), 200

# ============= ROTAS DE BACKUP =============
@app.route('/api/export', methods=['GET'])
@jwt_required()
def export_data():
    """Exportar todos os dados do usuário"""
    user_id = int(get_jwt_identity())
    
    demands = Demand.query.filter_by(user_id=user_id).all()
    history = DemandHistory.query.filter_by(user_id=user_id).all()
    groups = WorkGroup.query.filter_by(user_id=user_id).all()
    
    return jsonify({
        'demands': [d.to_dict() for d in demands],
        'history': [h.to_dict() for h in history],
        'workGroups': [g.to_dict() for g in groups],
        'exportedAt': datetime.now().isoformat()
    }), 200

@app.route('/api/import', methods=['POST'])
@jwt_required()
def import_data():
    """Importar dados do usuário"""
    user_id = int(get_jwt_identity())
    data = request.get_json()
    
    try:
        group_mapping = {}
        for group_data in data.get('workGroups', []):
            group = WorkGroup(
                user_id=user_id,
                name=group_data['name'],
                emoji=group_data.get('emoji', '📌'),
                color=group_data.get('color', '#3b82f6'),
                description=group_data.get('description', ''),
                order=group_data.get('order', 0)
            )
            db.session.add(group)
            db.session.flush()
            group_mapping[group_data['id']] = group.id
        
        for demand_data in data.get('demands', []):
            demand = Demand(
                user_id=user_id,
                work_group_id=group_mapping.get(demand_data.get('workGroupId')),
                location=demand_data['location'],
                activity=demand_data['activity'],
                context=demand_data.get('context', ''),
                status=demand_data.get('status', 'nao-iniciado'),
                priority=demand_data.get('priority', 'media'),
                assigned_to=demand_data.get('assignedTo', ''),
                created_date=datetime.strptime(demand_data.get('createdDate', str(date.today())), '%Y-%m-%d').date()
            )
            db.session.add(demand)
        
        for history_data in data.get('history', []):
            history = DemandHistory(
                user_id=user_id,
                work_group_id=group_mapping.get(history_data.get('workGroupId')),
                location=history_data['location'],
                activity=history_data['activity'],
                context=history_data.get('context', ''),
                status=history_data['status'],
                status_change_date=datetime.strptime(history_data.get('statusChangeDate', str(date.today())), '%Y-%m-%d').date(),
                created_date=datetime.strptime(history_data.get('createdDate', str(date.today())), '%Y-%m-%d').date() if history_data.get('createdDate') else None
            )
            db.session.add(history)
        
        db.session.commit()
        
        return jsonify({
            'message': 'Dados importados com sucesso',
            'demands_count': len(data.get('demands', [])),
            'history_count': len(data.get('history', [])),
            'groups_count': len(data.get('workGroups', []))
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Erro ao importar: {str(e)}'}), 400

# ============= ERROR HANDLERS =============
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Não encontrado'}), 404

@app.errorhandler(500)
def server_error(error):
    db.session.rollback()
    return jsonify({'error': 'Erro interno do servidor'}), 500

@jwt.unauthorized_loader
def unauthorized(error):
    return jsonify({'error': 'Autenticação necessária'}), 401

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=os.getenv('FLASK_ENV') == 'development', host='0.0.0.0', port=port)
