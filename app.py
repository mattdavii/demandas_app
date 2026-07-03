import os
import secrets
import json
import re
import threading
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
from pywebpush import webpush, WebPushException

load_dotenv()

app = Flask(__name__)
CORS(app)

# ============= CONFIGURAÇÕES =============
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:password@localhost/demandas_db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 240,
    'pool_size': 1,
    'max_overflow': 2,
    'pool_timeout': 8,
    'connect_args': {'connect_timeout': 8},
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
app.config['MAIL_DEFAULT_SENDER'] = ("Painel de Bordo", os.getenv('MAIL_USERNAME', 'noreply@demandasapp.com'))

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
    theme = db.Column(db.String(30), default='bancada')
    last_login = db.Column(db.DateTime, nullable=True)
    
    # Relacionamentos
    demands = db.relationship('Demand', foreign_keys='Demand.user_id', backref='user', lazy=True, cascade='all, delete-orphan')
    demand_history = db.relationship('DemandHistory', foreign_keys='DemandHistory.user_id', backref='user', lazy=True, cascade='all, delete-orphan')
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
            'isAdmin': self.is_admin,
            'theme': self.theme or 'bancada',
            'lastLogin': self.last_login.isoformat() if self.last_login else None
        }

class Workspace(db.Model):
    """Espaço de dados compartilhado. Toda demanda/grupo/status/etc pertence a um
    workspace, não diretamente a uma conta — isso é o que permite várias pessoas
    trabalharem nos mesmos dados (time), mantendo conta de uso individual sozinho
    (workspace com um único membro) funcionando exatamente como hoje."""
    __tablename__ = 'workspaces'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    logo_url = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'logoUrl': self.logo_url,
            'createdAt': self.created_at.isoformat() if self.created_at else None
        }

class WorkspaceMember(db.Model):
    """Vínculo entre uma conta e um workspace, com papel de permissão e cargo
    (cargo é só rótulo descritivo, não afeta permissão — quem decide isso é role)."""
    __tablename__ = 'workspace_members'

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='member')  # 'admin' | 'member'
    cargo = db.Column(db.String(100), nullable=True)
    joined_at = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (db.UniqueConstraint('workspace_id', 'user_id', name='unique_workspace_user'),)

    def to_dict(self):
        user = User.query.get(self.user_id)
        return {
            'id': self.id,
            'workspaceId': self.workspace_id,
            'userId': self.user_id,
            'username': user.username if user else None,
            'fullName': user.full_name if user else None,
            'email': user.email if user else None,
            'role': self.role,
            'cargo': self.cargo,
            'joinedAt': self.joined_at.isoformat() if self.joined_at else None
        }

def ws_filter(model, user_id, workspace_id, extra=None):
    """Filtro padrão que inclui registros do workspace E registros com workspace_id NULL
    do próprio user (registros criados antes da migração de workspace). Aceita filtros
    adicionais via dicionário `extra` (repassados como keyword args ao filter_by)."""
    base = db.or_(
        model.workspace_id == workspace_id,
        db.and_(model.workspace_id == None, model.user_id == user_id)
    )
    q = model.query.filter(base)
    if extra:
        q = q.filter_by(**extra)
    return q

def get_user_workspace_id(user_id):
    """Resolve o workspace de uma conta. Hoje cada conta pertence a exatamente um
    workspace (o próprio, ou aquele pra que foi convidada) — não há troca de
    workspace ainda, então sempre pega o primeiro vínculo encontrado."""
    membership = WorkspaceMember.query.filter_by(user_id=user_id).first()
    return membership.workspace_id if membership else None

def get_user_workspace_role(user_id, workspace_id=None):
    """Retorna 'admin' ou 'member' da conta dentro do workspace informado
    (ou do workspace dela, se nenhum for passado). None se não for membro."""
    query = WorkspaceMember.query.filter_by(user_id=user_id)
    if workspace_id is not None:
        query = query.filter_by(workspace_id=workspace_id)
    membership = query.first()
    return membership.role if membership else None

def is_workspace_admin(user_id, workspace_id=None):
    return get_user_workspace_role(user_id, workspace_id) == 'admin'

def create_personal_workspace(user):
    """Cria um workspace próprio pra uma conta nova e a torna admin dele.
    É o que acontece quando alguém usa uma chave PESSOAL (não convite de time)."""
    workspace_name = user.full_name or user.username
    workspace = Workspace(name=f"Espaço de {workspace_name}")
    db.session.add(workspace)
    db.session.flush()  # garante que workspace.id já existe antes do membro referenciar

    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.session.add(member)
    db.session.commit()
    return workspace

class WorkGroup(db.Model):
    __tablename__ = 'work_groups'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    emoji = db.Column(db.String(10), nullable=True)
    color = db.Column(db.String(7), default='#3b82f6')
    description = db.Column(db.String(255))
    order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # Relacionamento
    demands = db.relationship('Demand', backref='work_group', lazy=True)
    
    __table_args__ = (db.UniqueConstraint('workspace_id', 'name', name='unique_workspace_group_name'),)
    
    def to_dict(self, demands_count=None):
        return {
            'id': self.id,
            'name': self.name,
            'emoji': self.emoji,
            'color': self.color,
            'description': self.description,
            'order': self.order,
            'demandsCount': demands_count if demands_count is not None else 0
        }

class Demand(db.Model):
    __tablename__ = 'demands'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)
    work_group_id = db.Column(db.Integer, db.ForeignKey('work_groups.id'), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    activity = db.Column(db.String(255), nullable=False)
    context = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='nao-iniciado')
    priority = db.Column(db.String(20), default='media')
    due_date = db.Column(db.Date, nullable=True)
    assigned_to = db.Column(db.String(100))  # legado: texto livre (mantido p/ registros antigos)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # responsável real (membro do workspace)
    created_date = db.Column(db.Date, default=date.today)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    reminder_at = db.Column(db.DateTime, nullable=True)
    reminder_sent = db.Column(db.Boolean, default=False)
    checklist = db.Column(db.JSON, default=list)  # [{"text": "...", "checked": false}, ...]
    is_recurring = db.Column(db.Boolean, default=False)
    recurrence_type = db.Column(db.String(20), nullable=True)
    previous_status = db.Column(db.String(50), nullable=True)
    rejection_note = db.Column(db.Text, nullable=True)  # 'weekly'|'biweekly'|'monthly'|'quarterly'|'semiannual'|'yearly'
    
    def to_dict(self, user_cache=None):
        if self.assigned_to_user_id:
            assigned_user = (user_cache or {}).get(self.assigned_to_user_id) or User.query.get(self.assigned_to_user_id)
        else:
            assigned_user = None
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
            'assignedToUserId': self.assigned_to_user_id,
            'assignedToName': (assigned_user.full_name or assigned_user.username) if assigned_user else None,
            'userId': self.user_id,
            'createdDate': self.created_date.isoformat() if self.created_date else None,
            'reminderAt': self.reminder_at.isoformat() if self.reminder_at else None,
            'checklist': self.checklist or [],
            'isRecurring': self.is_recurring or False,
            'recurrenceType': self.recurrence_type,
            'previousStatus': self.previous_status,
            'rejectionNote': self.rejection_note
        }

class DemandHistory(db.Model):
    __tablename__ = 'demand_history'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)
    work_group_id = db.Column(db.Integer, db.ForeignKey('work_groups.id'), nullable=True)
    demand_id = db.Column(db.Integer, nullable=True)  # vínculo real com a demanda de origem (registros novos)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # snapshot de quem era responsável ao concluir
    location = db.Column(db.String(100), nullable=False)
    activity = db.Column(db.String(255), nullable=False)
    context = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False)
    priority = db.Column(db.String(20), nullable=True)  # snapshot da prioridade no momento da mudança (registros novos)
    checklist = db.Column(db.JSON, nullable=True)  # snapshot do checklist da demanda ao concluir
    status_change_date = db.Column(db.Date, nullable=False)
    created_date = db.Column(db.Date, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    
    def to_dict(self):
        return {
            'id': self.id,
            'demandId': self.demand_id,
            'workGroupId': self.work_group_id,
            'assignedToUserId': self.assigned_to_user_id,
            'userId': self.user_id,
            'priority': self.priority,
            'location': self.location,
            'activity': self.activity,
            'context': self.context,
            'status': self.status,
            'statusChangeDate': self.status_change_date.isoformat() if self.status_change_date else None,
            'createdDate': self.created_date.isoformat() if self.created_date else None,
            'checklist': self.checklist  # None = dado não disponível (registro antigo); [] = nova demanda sem checklist
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
    key_type = db.Column(db.String(20), nullable=False, default='personal')  # 'personal' | 'team_invite'
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)  # só usado em 'team_invite'
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    used_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        used_by_user = User.query.get(self.used_by) if self.used_by else None
        # Para team_invite multi-uso, conta quantos membros entraram via esta chave
        if self.key_type == 'team_invite' and self.workspace_id:
            uses_count = WorkspaceMember.query.filter_by(workspace_id=self.workspace_id).count()
        else:
            uses_count = 1 if self.used_by else 0

        if not self.is_active:
            status = 'revoked'
        elif self.key_type == 'personal' and self.used_by:
            status = 'used'
        else:
            status = 'available'

        return {
            'id': self.id,
            'key': self.key_value,
            'type': self.key_type,
            'workspaceId': self.workspace_id,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'usedBy': used_by_user.username if used_by_user else None,
            'usedAt': self.used_at.isoformat() if self.used_at else None,
            'usesCount': uses_count,
            'status': status
        }

class StatusConfig(db.Model):
    """Status de demanda configurável por workspace (compartilhado entre os membros)."""
    __tablename__ = 'status_configs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)
    key = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(7), nullable=False, default='#9aa0a7')
    emoji = db.Column(db.String(10), nullable=True)
    order = db.Column(db.Integer, default=0)
    is_completed = db.Column(db.Boolean, default=False)
    is_approval = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint('workspace_id', 'key', name='unique_workspace_status_key'),)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'label': self.label,
            'color': self.color,
            'emoji': self.emoji,
            'order': self.order,
            'isCompleted': self.is_completed,
            'isApproval': self.is_approval or False
        }

class PriorityConfig(db.Model):
    """Prioridade de demanda configurável por workspace (compartilhada entre os membros)."""
    __tablename__ = 'priority_configs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)
    key = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(7), nullable=False, default='#9aa0a7')
    order = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('workspace_id', 'key', name='unique_workspace_priority_key'),)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'label': self.label,
            'color': self.color,
            'order': self.order
        }

class PushSubscription(db.Model):
    """Inscrição de um dispositivo/navegador pra receber notificações push."""
    __tablename__ = 'push_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    endpoint = db.Column(db.String(500), unique=True, nullable=False)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def to_subscription_info(self):
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth}
        }

def send_push_notification(user_id, title, body, url='/'):
    """Envia uma notificação push pra todos os dispositivos inscritos de um usuário.
    Remove automaticamente inscrições que o navegador já invalidou (erro 410)."""
    vapid_private_key = os.getenv('VAPID_PRIVATE_KEY')
    if not vapid_private_key:
        return  # push não configurado neste ambiente, ignora silenciosamente

    subscriptions = PushSubscription.query.filter_by(user_id=user_id).all()
    vapid_claims = {"sub": "mailto:" + os.getenv('MAIL_USERNAME', 'admin@example.com')}

    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub.to_subscription_info(),
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims.copy(),
                timeout=8  # não travar o worker se o serviço push não responder
            )
        except WebPushException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 410):
                # inscrição expirada/inválida — remove pra não tentar de novo
                db.session.delete(sub)
                db.session.commit()
            print(f"Erro ao enviar push pro usuário {user_id}: {e}")
        except Exception as e:
            print(f"Erro inesperado ao enviar push pro usuário {user_id}: {e}")

def seed_default_status_and_priority(user_id, workspace_id):
    """Cria o conjunto padrão de status/prioridade pra um workspace (novo ou já
    existente sem nenhum configurado). user_id aqui é só quem fica registrado
    como criador do registro — o que importa pra escopo é workspace_id."""
    if StatusConfig.query.filter(db.or_(StatusConfig.workspace_id == workspace_id, db.and_(StatusConfig.workspace_id == None, StatusConfig.user_id == user_id))).first() is None:
        defaults = [
            {'key': 'agendado', 'label': 'Agendado', 'color': '#ff9f43', 'emoji': '🟠', 'order': 0, 'is_completed': False},
            {'key': 'nao-iniciado', 'label': 'Não Iniciado', 'color': '#9aa0a7', 'emoji': '⚪', 'order': 1, 'is_completed': False},
            {'key': 'andamento', 'label': 'Em Andamento', 'color': '#f5a623', 'emoji': '🟡', 'order': 2, 'is_completed': False},
            {'key': 'aguardando', 'label': 'Aguardando', 'color': '#4fc3f7', 'emoji': '🔵', 'order': 3, 'is_completed': False},
            {'key': 'aprovacao', 'label': 'Aguardando Aprovação', 'color': '#b388ff', 'emoji': '🟣', 'order': 4, 'is_completed': False, 'is_approval': True},
            {'key': 'concluido', 'label': 'Concluído', 'color': '#3ddc84', 'emoji': '🟢', 'order': 5, 'is_completed': True},
        ]
        for d in defaults:
            db.session.add(StatusConfig(user_id=user_id, workspace_id=workspace_id, **d))

    if PriorityConfig.query.filter(db.or_(PriorityConfig.workspace_id == workspace_id, db.and_(PriorityConfig.workspace_id == None, PriorityConfig.user_id == user_id))).first() is None:
        defaults = [
            {'key': 'baixa', 'label': 'Baixa', 'color': '#5b6168', 'order': 0},
            {'key': 'media', 'label': 'Média', 'color': '#4fc3f7', 'order': 1},
            {'key': 'alta', 'label': 'Alta', 'color': '#f5a623', 'order': 2},
            {'key': 'urgente', 'label': 'Urgente', 'color': '#ff5b5b', 'order': 3},
        ]
        for d in defaults:
            db.session.add(PriorityConfig(user_id=user_id, workspace_id=workspace_id, **d))

    db.session.commit()

# ============= CRIAR TABELAS NA INICIALIZAÇÃO =============
with app.app_context():
    db.create_all()


# ============= ROTAS DE PÁGINA =============
@app.route('/')
def index():
    """Servir página principal"""
    return render_template('index.html')

@app.route('/ping')
def ping():
    """Health check leve. Também aplica migrations pendentes na primeira chamada."""
    _approval_cols = [
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS previous_status VARCHAR(50)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP",
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS rejection_note TEXT",
        "ALTER TABLE status_configs ADD COLUMN IF NOT EXISTS is_approval BOOLEAN DEFAULT FALSE",
        "UPDATE status_configs SET is_approval = TRUE WHERE key = 'aprovacao' AND (is_approval IS NULL OR is_approval = FALSE)",
    ]
    for _s in _approval_cols:
        try:
            db.session.execute(text(_s))
            db.session.commit()
        except Exception:
            db.session.rollback()
    return 'pong', 200

@app.route('/api/init')
@jwt_required()
def get_init_data():
    """Retorna configs, workspace, membros e grupos — sem demands e sem COUNT.
    Demands são buscadas em paralelo pelo frontend via /api/demands, que é mais rápida
    (2 queries) e pode renderizar o Kanban independentemente das queries de configuração."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    user = User.query.get(user_id)
    workspace = Workspace.query.get(workspace_id) if workspace_id else None
    members_raw = WorkspaceMember.query.filter_by(workspace_id=workspace_id).all() if workspace_id else []

    status_configs = ws_filter(StatusConfig, user_id, workspace_id).order_by(StatusConfig.order.asc()).all()
    priority_configs = ws_filter(PriorityConfig, user_id, workspace_id).order_by(PriorityConfig.order.asc()).all()

    groups = WorkGroup.query.filter(
        db.or_(
            db.and_(WorkGroup.workspace_id == workspace_id, WorkGroup.is_active == True),
            db.and_(WorkGroup.workspace_id == None, WorkGroup.user_id == user_id, WorkGroup.is_active == True)
        )
    ).order_by(WorkGroup.order).all()

    member_users = {u.id: u for u in User.query.filter(
        User.id.in_([m.user_id for m in members_raw])
    ).all()} if members_raw else {}

    def member_dict(m):
        u = member_users.get(m.user_id)
        return {
            'userId': m.user_id,
            'role': m.role,
            'cargo': getattr(m, 'cargo', None),
            'fullName': (u.full_name or u.username) if u else str(m.user_id),
            'email': u.email if u else None,
        }

    return jsonify({
        'user': user.to_dict() if user else None,
        'workspace': {'id': workspace.id, 'name': workspace.name, 'logoUrl': workspace.logo_url} if workspace else None,
        'members': [member_dict(m) for m in members_raw],
        'statusConfigs': [s.to_dict() for s in status_configs],
        'priorityConfigs': [p.to_dict() for p in priority_configs],
        'workGroups': [g.to_dict(demands_count=0) for g in groups],  # contagem calculada no frontend
    }), 200


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

    # Status, prioridades e grupos padrão só são criados quando o workspace da conta
    # é determinado (ao validar a chave de acesso) — uma conta recém-registrada ainda
    # não pertence a nenhum workspace, então não há onde anexar esses dados ainda.
    
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
    """Login do usuário (aceita username ou e-mail no mesmo campo, já que quem é
    convidado pro time só recebe o e-mail, nunca escolhe um username)"""
    data = request.get_json()
    
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Usuário ou senha não fornecidos'}), 400
    
    login_input = data['username']
    user = User.query.filter(
        (User.username == login_input) | (User.email == login_input)
    ).first()
    
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
    
    # Registra último acesso com throttle de 10 minutos
    now = datetime.now()
    if not user.last_login or (now - user.last_login).total_seconds() > 600:
        user.last_login = now
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    result = user.to_dict()
    if user.access_verified:
        workspace_id = get_user_workspace_id(user_id)
        result['workspaceId'] = workspace_id
        result['workspaceRole'] = get_user_workspace_role(user_id, workspace_id)
    return jsonify(result), 200

@app.route('/api/auth/theme', methods=['POST'])
@jwt_required()
def set_theme():
    """Salva a preferência de tema visual do usuário (sincroniza entre dispositivos)"""
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)

    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    data = request.get_json()
    theme = (data or {}).get('theme', '').strip()

    valid_themes = {'bancada', 'claro', 'meianoite', 'violeta'}
    if theme not in valid_themes:
        return jsonify({'error': 'Tema inválido'}), 400

    user.theme = theme
    db.session.commit()

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

    # Chaves pessoais são de uso único — chaves de convite são multi-uso (vários
    # colegas podem usar o mesmo link) e só ficam com used_by do primeiro usuário
    # pra rastreabilidade, mas continuam ativas pra novos usos.
    if access_key.key_type == 'personal' and access_key.used_by is not None:
        return jsonify({'error': 'Chave já utilizada'}), 409

    # Registra quem usou (primeiro uso) e quando, mas não bloqueia reuso em team_invite
    if access_key.used_by is None:
        access_key.used_by = user.id
        access_key.used_at = datetime.now()

    user.access_verified = True
    db.session.commit()

    if access_key.key_type == 'team_invite':
        # Entra como membro comum de um workspace que já existe — não recria nada,
        # já que o time já tem seus próprios grupos/status/prioridades configurados.
        db.session.add(WorkspaceMember(workspace_id=access_key.workspace_id, user_id=user.id, role='member'))
        db.session.commit()
    else:
        # Chave pessoal: cria um workspace novo e independente, com o conjunto
        # padrão de status/prioridade e os grupos iniciais, como sempre foi.
        workspace = create_personal_workspace(user)
        seed_default_status_and_priority(user.id, workspace.id)

        default_groups = [
            {'name': 'BACKOFFICE', 'emoji': '👨🏻‍💻', 'order': 1},
            {'name': 'ATENDIMENTOS', 'emoji': '👨🏼‍🔧', 'order': 2}
        ]
        for group_data in default_groups:
            db.session.add(WorkGroup(
                user_id=user.id,
                workspace_id=workspace.id,
                name=group_data['name'],
                emoji=group_data['emoji'],
                order=group_data['order']
            ))
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

# ============= ROTAS DE WORKSPACE E MEMBROS =============
def generate_unique_username(base_text):
    """Gera um username único a partir de um nome ou e-mail (parte local), evitando colisão."""
    base = re.sub(r'[^a-z0-9]+', '.', base_text.lower()).strip('.') or 'usuario'
    candidate = base
    counter = 2
    while User.query.filter_by(username=candidate).first() is not None:
        candidate = f"{base}{counter}"
        counter += 1
    return candidate

@app.route('/api/workspace', methods=['GET'])
@jwt_required()
def get_workspace_info():
    """Dados do workspace atual (nome, logo) — qualquer membro pode ver."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    workspace = Workspace.query.get(workspace_id) if workspace_id else None
    if not workspace:
        return jsonify({'error': 'Workspace não encontrado'}), 404
    result = workspace.to_dict()
    result['myRole'] = get_user_workspace_role(user_id, workspace_id)
    return jsonify(result), 200

@app.route('/api/workspace/logo', methods=['PUT'])
@jwt_required()
def set_workspace_logo():
    """Define a logo do workspace (imagem em base64 data-URI, já redimensionada
    no navegador). Restrito a admin do workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem alterar a logo'}), 403

    data = request.get_json()
    logo_url = (data or {}).get('logoUrl')

    if logo_url and len(logo_url) > 400000:  # ~300KB de imagem em base64
        return jsonify({'error': 'Imagem muito grande. Use uma imagem menor.'}), 400

    workspace = Workspace.query.get_or_404(workspace_id)
    workspace.logo_url = logo_url or None
    db.session.commit()

    return jsonify(workspace.to_dict()), 200

@app.route('/api/workspace/members', methods=['GET'])
@jwt_required()
def list_workspace_members():
    """Lista os membros do workspace atual — qualquer membro pode ver o time."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    members = WorkspaceMember.query.filter_by(workspace_id=workspace_id).order_by(WorkspaceMember.joined_at.asc()).all()
    return jsonify([m.to_dict() for m in members]), 200

@app.route('/api/workspace/invite', methods=['POST'])
@jwt_required()
def invite_workspace_member():
    """Convida uma pessoa nova pro workspace: cria a conta dela (ainda sem senha
    utilizável), o vínculo de membro, e envia um e-mail com link de definição de
    senha (reaproveita o mesmo mecanismo de token do reset de senha). Restrito a
    admin do workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem convidar membros'}), 403

    data = request.get_json()
    name = (data or {}).get('name', '').strip()
    cargo = (data or {}).get('cargo', '').strip()
    email = (data or {}).get('email', '').strip().lower()
    role = (data or {}).get('role', 'member')

    if not name or not email:
        return jsonify({'error': 'Nome e e-mail são obrigatórios'}), 400
    if role not in ('admin', 'member'):
        role = 'member'

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Já existe uma conta com esse e-mail'}), 409

    username = generate_unique_username(email.split('@')[0])

    new_user = User(
        username=username,
        email=email,
        full_name=name,
        access_verified=True  # convite direto do admin já libera o acesso, sem precisar de chave
    )
    new_user.set_password(secrets.token_urlsafe(24))  # senha provisória inutilizável; ela define a própria no 1º acesso
    db.session.add(new_user)
    db.session.flush()  # garante new_user.id antes de referenciar

    db.session.add(WorkspaceMember(workspace_id=workspace_id, user_id=new_user.id, role=role, cargo=cargo or None))

    reset_token = new_user.generate_reset_token()
    frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:5000')
    reset_url = f"{frontend_url}/reset?token={reset_token}"

    db.session.commit()

    email_sent = False
    if app.config['MAIL_USERNAME']:
        try:
            workspace = Workspace.query.get(workspace_id)
            msg = Message(
                f'Você foi convidado pro time no Painel de Bordo!',
                recipients=[email],
                html=f'''
                <p>Olá {name},</p>
                <p>Você foi adicionado ao workspace <strong>{workspace.name if workspace else "do time"}</strong> no Painel de Bordo{f" como {cargo}" if cargo else ""}.</p>
                <p>Pra começar a usar, defina sua senha clicando no link abaixo:</p>
                <p><a href="{reset_url}">Definir minha senha</a></p>
                <p>Esse link expira em 24 horas. Depois de definir a senha, você já pode entrar com seu e-mail (<strong>{email}</strong>) normalmente.</p>
                '''
            )
            mail.send(msg)
            email_sent = True
        except Exception as e:
            print(f'Erro ao enviar email de convite: {e}')

    return jsonify({
        'message': 'Membro convidado com sucesso',
        'emailSent': email_sent,
        'resetUrl': reset_url,  # admin pode compartilhar manualmente se o e-mail falhar/não estiver configurado
        'member': WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=new_user.id).first().to_dict()
    }), 201

@app.route('/api/workspace/members/<int:member_id>', methods=['PUT'])
@jwt_required()
def update_workspace_member(member_id):
    """Atualiza papel e/ou cargo de um membro. Restrito a admin do workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem editar membros'}), 403

    member = WorkspaceMember.query.get_or_404(member_id)
    if member.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()

    if 'role' in data:
        if data['role'] not in ('admin', 'member'):
            return jsonify({'error': 'Papel inválido'}), 400
        if member.role == 'admin' and data['role'] != 'admin':
            other_admin = WorkspaceMember.query.filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == 'admin',
                WorkspaceMember.id != member.id
            ).first()
            if not other_admin:
                return jsonify({'error': 'Precisa existir ao menos um administrador no workspace'}), 400
        member.role = data['role']

    if 'cargo' in data:
        member.cargo = data['cargo'] or None

    db.session.commit()
    return jsonify(member.to_dict()), 200

@app.route('/api/workspace/members/<int:member_id>', methods=['DELETE'])
@jwt_required()
def remove_workspace_member(member_id):
    """Remove um membro do workspace. Restrito a admin. Demandas atribuídas a essa
    pessoa ficam sem responsável (não são apagadas). A conta removida volta pra
    tela de bloqueio de acesso, já que deixa de pertencer a qualquer workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem remover membros'}), 403

    member = WorkspaceMember.query.get_or_404(member_id)
    if member.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403

    if member.role == 'admin':
        other_admin = WorkspaceMember.query.filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == 'admin',
            WorkspaceMember.id != member.id
        ).first()
        if not other_admin:
            return jsonify({'error': 'Precisa existir ao menos um administrador no workspace'}), 400

    Demand.query.filter_by(workspace_id=workspace_id, assigned_to_user_id=member.user_id).update({'assigned_to_user_id': None})

    removed_user = User.query.get(member.user_id)
    if removed_user:
        removed_user.access_verified = False

    db.session.delete(member)
    db.session.commit()

    return jsonify({'message': 'Membro removido'}), 200


# ============= ROTAS DE CHAVES DE CONVITE (WORKSPACE ADMIN) =============
@app.route('/api/workspace/invite-keys', methods=['GET'])
@jwt_required()
def list_invite_keys():
    """Lista as chaves de convite do workspace. Restrito a admin do workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem ver as chaves de convite'}), 403

    keys = AccessKey.query.filter_by(
        workspace_id=workspace_id,
        key_type='team_invite'
    ).order_by(AccessKey.created_at.desc()).all()

    return jsonify([k.to_dict() for k in keys]), 200


@app.route('/api/workspace/invite-keys', methods=['POST'])
@jwt_required()
def create_invite_key():
    """Gera uma nova chave de convite para o workspace. Restrito a admin do workspace.
    Chaves de convite são reutilizáveis (multi-uso) até serem revogadas — diferentes
    das chaves pessoais que são de uso único."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem gerar chaves de convite'}), 403

    # Gera chave legível (6 chars hex), única no banco
    while True:
        key_value = secrets.token_hex(4).upper()  # ex: A3F9C2E1
        if not AccessKey.query.filter_by(key_value=key_value).first():
            break

    new_key = AccessKey(
        key_value=key_value,
        key_type='team_invite',
        workspace_id=workspace_id,
        created_by=user_id,
        is_active=True
    )
    db.session.add(new_key)
    db.session.commit()

    return jsonify(new_key.to_dict()), 201


@app.route('/api/workspace/invite-keys/<int:key_id>', methods=['DELETE'])
@jwt_required()
def revoke_invite_key(key_id):
    """Revoga (desativa) uma chave de convite do workspace. Restrito a admin."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem revogar chaves de convite'}), 403

    key = AccessKey.query.get_or_404(key_id)

    if key.workspace_id != workspace_id or key.key_type != 'team_invite':
        return jsonify({'error': 'Chave não encontrada neste workspace'}), 404

    key.is_active = False
    db.session.commit()

    return jsonify({'message': 'Chave revogada com sucesso'}), 200

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
    workspace_id = get_user_workspace_id(user_id)

    # Fallback: inclui registros com workspace_id NULL do próprio user (migração incompleta)
    existing = StatusConfig.query.filter(
        db.or_(
            StatusConfig.workspace_id == workspace_id,
            db.and_(StatusConfig.workspace_id == None, StatusConfig.user_id == user_id)
        )
    ).first()
    if not existing:
        seed_default_status_and_priority(user_id, workspace_id)

    configs = StatusConfig.query.filter(
        db.or_(
            StatusConfig.workspace_id == workspace_id,
            db.and_(StatusConfig.workspace_id == None, StatusConfig.user_id == user_id)
        )
    ).order_by(StatusConfig.order.asc()).all()
    return jsonify([c.to_dict() for c in configs]), 200

@app.route('/api/status-configs', methods=['POST'])
@jwt_required()
def create_status_config():
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem criar status'}), 403
    data = request.get_json()

    if not data or not data.get('key') or not data.get('label'):
        return jsonify({'error': 'Chave e nome são obrigatórios'}), 400

    if StatusConfig.query.filter_by(workspace_id=workspace_id, key=data['key']).first():
        return jsonify({'error': 'Já existe um status com essa chave'}), 409

    max_order = db.session.query(db.func.max(StatusConfig.order)).filter_by(workspace_id=workspace_id).scalar() or 0

    config = StatusConfig(
        user_id=user_id,
        workspace_id=workspace_id,
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
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem editar status'}), 403
    config = StatusConfig.query.get_or_404(config_id)

    if config.workspace_id != workspace_id is not None and config.workspace_id.workspace_id != workspace_id:
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
                StatusConfig.workspace_id == workspace_id,
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
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem remover status'}), 403
    config = StatusConfig.query.get_or_404(config_id)

    if config.workspace_id != workspace_id is not None and config.workspace_id.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403

    if StatusConfig.query.filter(db.or_(StatusConfig.workspace_id == workspace_id, db.and_(StatusConfig.workspace_id == None, StatusConfig.user_id == user_id))).count() <= 1:
        return jsonify({'error': 'Não é possível remover o último status restante'}), 400

    in_use = ws_filter(Demand, user_id, workspace_id, {'status': config.key}).first()
    if in_use:
        return jsonify({'error': 'Existem demandas usando esse status. Mude o status delas antes de remover.'}), 400

    if config.is_completed:
        other_completed = StatusConfig.query.filter(
            StatusConfig.workspace_id == workspace_id,
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
    workspace_id = get_user_workspace_id(user_id)

    existing = PriorityConfig.query.filter(
        db.or_(
            PriorityConfig.workspace_id == workspace_id,
            db.and_(PriorityConfig.workspace_id == None, PriorityConfig.user_id == user_id)
        )
    ).first()
    if not existing:
        seed_default_status_and_priority(user_id, workspace_id)

    configs = PriorityConfig.query.filter(
        db.or_(
            PriorityConfig.workspace_id == workspace_id,
            db.and_(PriorityConfig.workspace_id == None, PriorityConfig.user_id == user_id)
        )
    ).order_by(PriorityConfig.order.asc()).all()
    return jsonify([c.to_dict() for c in configs]), 200

@app.route('/api/priority-configs', methods=['POST'])
@jwt_required()
def create_priority_config():
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem criar prioridades'}), 403
    data = request.get_json()

    if not data or not data.get('key') or not data.get('label'):
        return jsonify({'error': 'Chave e nome são obrigatórios'}), 400

    if PriorityConfig.query.filter_by(workspace_id=workspace_id, key=data['key']).first():
        return jsonify({'error': 'Já existe uma prioridade com essa chave'}), 409

    max_order = db.session.query(db.func.max(PriorityConfig.order)).filter_by(workspace_id=workspace_id).scalar() or 0

    config = PriorityConfig(
        user_id=user_id,
        workspace_id=workspace_id,
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
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem editar prioridades'}), 403
    config = PriorityConfig.query.get_or_404(config_id)

    if config.workspace_id != workspace_id is not None and config.workspace_id.workspace_id != workspace_id:
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
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem remover prioridades'}), 403
    config = PriorityConfig.query.get_or_404(config_id)

    if config.workspace_id != workspace_id is not None and config.workspace_id.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403

    if PriorityConfig.query.filter(db.or_(PriorityConfig.workspace_id == workspace_id, db.and_(PriorityConfig.workspace_id == None, PriorityConfig.user_id == user_id))).count() <= 1:
        return jsonify({'error': 'Não é possível remover a última prioridade restante'}), 400

    in_use = ws_filter(Demand, user_id, workspace_id, {'priority': config.key}).first()
    if in_use:
        return jsonify({'error': 'Existem demandas usando essa prioridade. Mude a prioridade delas antes de remover.'}), 400

    db.session.delete(config)
    db.session.commit()

    return jsonify({'message': 'Prioridade removida'}), 200

# ============= ROTAS DE GRUPOS DE TRABALHO =============
@app.route('/api/work-groups', methods=['GET'])
@jwt_required()
def get_work_groups():
    """Listar grupos de trabalho do workspace"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    groups = WorkGroup.query.filter(
        db.or_(
            db.and_(WorkGroup.workspace_id == workspace_id, WorkGroup.is_active == True),
            db.and_(WorkGroup.workspace_id == None, WorkGroup.user_id == user_id, WorkGroup.is_active == True)
        )
    ).order_by(WorkGroup.order).all()

    # Contar demandas ativas por grupo numa query só (evita N+1 lazy-load)
    terminal_keys = [s.key for s in ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': True}).all()]
    q = db.session.query(Demand.work_group_id, db.func.count(Demand.id)).filter(
        db.or_(Demand.workspace_id == workspace_id, db.and_(Demand.workspace_id == None, Demand.user_id == user_id))
    )
    if terminal_keys:
        q = q.filter(~Demand.status.in_(terminal_keys))
    counts = {row[0]: row[1] for row in q.group_by(Demand.work_group_id).all()}

    return jsonify([g.to_dict(demands_count=counts.get(g.id, 0)) for g in groups]), 200

@app.route('/api/work-groups', methods=['POST'])
@jwt_required()
def create_work_group():
    """Criar novo grupo de trabalho (restrito a admin)"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem criar grupos'}), 403
    data = request.get_json()
    
    if not data or not data.get('name'):
        return jsonify({'error': 'Nome do grupo é obrigatório'}), 400
    
    group = WorkGroup(
        user_id=user_id,
        workspace_id=workspace_id,
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
    """Atualizar grupo de trabalho (restrito a admin)"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem editar grupos'}), 403
    group = WorkGroup.query.get_or_404(group_id)
    
    if group.workspace_id is not None and group.workspace_id != workspace_id:
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
    """Deletar grupo de trabalho (restrito a admin)"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem remover grupos'}), 403
    group = WorkGroup.query.get_or_404(group_id)
    
    if group.workspace_id is not None and group.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    group.is_active = False
    db.session.commit()
    
    return jsonify({'message': 'Grupo deletado'}), 200

# ============= ROTAS DE DEMANDAS =============
@app.route('/api/demands', methods=['GET'])
@jwt_required()
def get_demands():
    """Listar demandas pendentes do workspace"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    terminal_keys = [s.key for s in ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': True}).all()]
    query = ws_filter(Demand, user_id, workspace_id)
    if terminal_keys:
        query = query.filter(~Demand.status.in_(terminal_keys))
    demands = query.all()

    # Preload de usuários responsáveis numa query só (evita N+1 por demanda)
    assignee_ids = {d.assigned_to_user_id for d in demands if d.assigned_to_user_id}
    user_cache = {u.id: u for u in User.query.filter(User.id.in_(assignee_ids)).all()} if assignee_ids else {}

    return jsonify([d.to_dict(user_cache=user_cache) for d in demands]), 200

@app.route('/api/demands', methods=['POST'])
@jwt_required()
def create_demand():
    """Criar nova demanda"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    data = request.get_json()
    
    if not data or not data.get('work_group_id') or not data.get('location') or not data.get('activity'):
        return jsonify({'error': 'Dados incompletos'}), 400
    
    group = WorkGroup.query.get(data['work_group_id'])
    if not group or (group.workspace_id is not None and group.workspace_id != workspace_id):
        return jsonify({'error': 'Grupo inválido'}), 403

    default_status = data.get('status')
    if not default_status:
        first_status = ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': False}).order_by(StatusConfig.order.asc()).first()
        default_status = first_status.key if first_status else 'nao-iniciado'

    default_priority = data.get('priority')
    if not default_priority:
        media_priority = ws_filter(PriorityConfig, user_id, workspace_id, {'key': 'media'}).first()
        if media_priority:
            default_priority = media_priority.key
        else:
            any_priority = ws_filter(PriorityConfig, user_id, workspace_id).order_by(PriorityConfig.order.asc()).first()
            default_priority = any_priority.key if any_priority else 'media'

    assigned_to_user_id = data.get('assigned_to_user_id')
    if not is_workspace_member_user(workspace_id, assigned_to_user_id):
        return jsonify({'error': 'Responsável inválido: precisa ser membro do workspace'}), 400

    demand = Demand(
        user_id=user_id,
        workspace_id=workspace_id,
        work_group_id=data['work_group_id'],
        location=data['location'],
        activity=data['activity'],
        context=data.get('context', ''),
        status=default_status,
        priority=default_priority,
        due_date=datetime.strptime(data['due_date'], '%Y-%m-%d').date() if data.get('due_date') else None,
        assigned_to=data.get('assigned_to', ''),
        assigned_to_user_id=assigned_to_user_id,
        reminder_at=datetime.strptime(data['reminder_at'], '%Y-%m-%dT%H:%M') if data.get('reminder_at') else None,
        checklist=data.get('checklist', []),
        is_recurring=bool(data.get('is_recurring', False)),
        recurrence_type=data.get('recurrence_type') or None
    )
    
    db.session.add(demand)
    db.session.commit()

    if assigned_to_user_id:
        notify_assignment(demand, User.query.get(assigned_to_user_id), user_id)
    
    return jsonify(demand.to_dict()), 201

@app.route('/api/demands/<int:demand_id>', methods=['PUT'])
@jwt_required()
def update_demand(demand_id):
    """Atualizar demanda"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    demand = Demand.query.get_or_404(demand_id)
    
    if demand.workspace_id != workspace_id is not None and demand.workspace_id.workspace_id != workspace_id:
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
    if 'assigned_to_user_id' in data:
        new_assignee_id = data['assigned_to_user_id']
        if not is_workspace_member_user(workspace_id, new_assignee_id):
            return jsonify({'error': 'Responsável inválido: precisa ser membro do workspace'}), 400
        assignee_changed = new_assignee_id != demand.assigned_to_user_id
        demand.assigned_to_user_id = new_assignee_id
        if assignee_changed and new_assignee_id:
            notify_assignment(demand, User.query.get(new_assignee_id), user_id)
    if 'reminder_at' in data:
        if data['reminder_at']:
            demand.reminder_at = datetime.strptime(data['reminder_at'], '%Y-%m-%dT%H:%M')
            demand.reminder_sent = False  # rearma o lembrete se a data/hora mudou
        else:
            demand.reminder_at = None
            demand.reminder_sent = False
    if 'checklist' in data:
        demand.checklist = data['checklist']
    if 'is_recurring' in data:
        demand.is_recurring = bool(data['is_recurring'])
    if 'recurrence_type' in data:
        demand.recurrence_type = data['recurrence_type'] or None
    
    db.session.commit()
    return jsonify(demand.to_dict()), 200

@app.route('/api/demands/<int:demand_id>', methods=['DELETE'])
@jwt_required()
def delete_demand(demand_id):
    """Deletar demanda"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    demand = Demand.query.get_or_404(demand_id)
    
    if demand.workspace_id != workspace_id is not None and demand.workspace_id.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    db.session.delete(demand)
    db.session.commit()
    
    return jsonify({'message': 'Demanda deletada'}), 200


def add_months(d, months):
    """Desloca uma data por N meses sem depender de dateutil."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)

def next_due_date(due_date, recurrence_type):
    """Calcula a próxima data de vencimento deslocando pela periodicidade."""
    from datetime import timedelta
    base = due_date or date.today()
    if recurrence_type == 'weekly':      return base + timedelta(weeks=1)
    if recurrence_type == 'biweekly':    return base + timedelta(weeks=2)
    if recurrence_type == 'monthly':     return add_months(base, 1)
    if recurrence_type == 'quarterly':   return add_months(base, 3)
    if recurrence_type == 'semiannual':  return add_months(base, 6)
    if recurrence_type == 'yearly':      return add_months(base, 12)
    return None

@app.route('/api/demands/<int:demand_id>/status', methods=['POST'])
@jwt_required()
def update_demand_status(demand_id):
    """Atualizar status de demanda e mover para histórico se concluído"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    demand = Demand.query.get_or_404(demand_id)

    if demand.workspace_id is not None and demand.workspace_id != workspace_id:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()
    new_status = data.get('status')
    old_status = demand.status  # salvar antes de qualquer alteração

    if not new_status:
        return jsonify({'error': 'Status não fornecido'}), 400
    
    history = DemandHistory(
        user_id=user_id,
        workspace_id=workspace_id,
        work_group_id=demand.work_group_id,
        demand_id=demand.id,
        assigned_to_user_id=demand.assigned_to_user_id or demand.user_id,  # snapshot do responsável (ou criador se sem responsável)
        priority=demand.priority,
        checklist=demand.checklist or [],  # snapshot do checklist — preservado mesmo após deletar a demanda
        location=demand.location,
        activity=demand.activity,
        context=demand.context,
        status=new_status,
        status_change_date=date.today(),
        created_date=demand.created_date
    )
    db.session.add(history)

    status_config = ws_filter(StatusConfig, user_id, workspace_id, {'key': new_status}).first()
    is_terminal = status_config.is_completed if status_config else (new_status in ('concluido', 'concluído'))
    is_approval = status_config.is_approval if status_config else False

    # Fluxo de aprovação: salva o status anterior e notifica os admins
    if is_approval:
        demand.previous_status = old_status
        requester = User.query.get(user_id)
        requester_name = (requester.full_name or requester.username) if requester else 'Alguém'
        # Notifica após commit (dispara em background pra não atrasar a resposta)
        import threading as _t
        _d, _w, _r = demand, workspace_id, requester_name
        def _notify():
            with app.app_context():
                notify_admins_approval_pending(_d, _w, _r)
        _t.Thread(target=_notify, daemon=True).start()

    if is_terminal:
        if demand.is_recurring and demand.recurrence_type:
            # Auto-cria a próxima ocorrência com mesmos dados, checklist resetado e nova data
            new_due = next_due_date(demand.due_date, demand.recurrence_type)
            first_status = ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': False}).order_by(StatusConfig.order.asc()).first()
            initial_status = first_status.key if first_status else 'nao-iniciado'
            checklist_reset = [{'text': item['text'], 'checked': False} for item in (demand.checklist or [])]
            next_demand = Demand(
                user_id=demand.user_id,
                workspace_id=demand.workspace_id,
                work_group_id=demand.work_group_id,
                location=demand.location,
                activity=demand.activity,
                context=demand.context,
                status=initial_status,
                priority=demand.priority,
                due_date=new_due,
                assigned_to=demand.assigned_to,
                assigned_to_user_id=demand.assigned_to_user_id,
                is_recurring=True,
                recurrence_type=demand.recurrence_type,
                checklist=checklist_reset
            )
            db.session.add(next_demand)
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
def send_reminder_notification(demand, user):
    """Envia e-mail e push de lembrete pra uma demanda específica."""
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

    send_push_notification(user.id, f'🔔 {demand.location}', demand.activity, '/')

def is_workspace_member_user(workspace_id, candidate_user_id):
    """Confirma se candidate_user_id é de fato membro do workspace informado
    (evita atribuir uma demanda a alguém de outro workspace)."""
    if not candidate_user_id:
        return True  # None é válido — significa "sem responsável"
    return WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=candidate_user_id).first() is not None

def notify_assignment(demand, assigned_user, assigner_user_id):
    """Avisa (push + e-mail) quando uma demanda é atribuída a alguém — só dispara
    se a pessoa atribuída for diferente de quem fez a atribuição."""
    if not assigned_user or assigned_user.id == assigner_user_id:
        return

    if app.config['MAIL_USERNAME']:
        try:
            msg = Message(
                f'Nova demanda atribuída: {demand.activity}',
                recipients=[assigned_user.email],
                html=f'''
                <p>Olá {assigned_user.full_name or assigned_user.username},</p>
                <p>Uma demanda foi atribuída a você:</p>
                <p><strong>{demand.location}:</strong> {demand.activity}</p>
                {f"<p>{demand.context}</p>" if demand.context else ""}
                '''
            )
            mail.send(msg)
        except Exception as e:
            print(f'Erro ao enviar email de atribuição: {e}')

    send_push_notification(assigned_user.id, f'📋 Nova demanda atribuída', f'{demand.location}: {demand.activity}', '/')

@app.route('/api/reminders/check', methods=['POST'])
@jwt_required()
def check_reminders():
    """Verifica lembretes vencidos e os processa em BACKGROUND para não travar
    o único worker do Gunicorn. O push notification externo pode demorar; com isso
    a resposta é imediata e o worker fica livre para outras requisições."""
    user_id = int(get_jwt_identity())

    import threading as _th
    def _process():
        with app.app_context():
            try:
                user = User.query.get(user_id)
                if not user:
                    return
                workspace_id = get_user_workspace_id(user_id)
                terminal_keys = [s.key for s in ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': True}).all()]
                now = datetime.now()
                query = ws_filter(Demand, user_id, workspace_id).filter(
                    Demand.reminder_at.isnot(None),
                    Demand.reminder_at <= now,
                    Demand.reminder_sent == False
                )
                if terminal_keys:
                    query = query.filter(~Demand.status.in_(terminal_keys))
                due_demands = query.all()
                for demand in due_demands:
                    send_reminder_notification(demand, user)
                    demand.reminder_sent = True
                if due_demands:
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f'[reminders] erro: {e}')

    _th.Thread(target=_process, daemon=True).start()
    return jsonify({'triggered': []}), 200

@app.route('/api/cron/check-all-reminders', methods=['POST', 'GET'])
def cron_check_all_reminders():
    """Endpoint pra ser chamado por um serviço externo de cron (ex: cron-job.org) de tempos
    em tempos, garantindo que lembretes disparem (email + push) mesmo com o app fechado.
    Protegido por chave secreta (não usa JWT, já que não há usuário logado nesse contexto)."""
    secret = request.args.get('key') or request.headers.get('X-Cron-Key')
    if not secret or secret != os.getenv('CRON_SECRET_KEY'):
        return jsonify({'error': 'Não autorizado'}), 403

    now = datetime.now()
    due_demands = Demand.query.filter(
        Demand.reminder_at.isnot(None),
        Demand.reminder_at <= now,
        Demand.reminder_sent == False
    ).all()

    sent_count = 0
    for demand in due_demands:
        user = User.query.get(demand.user_id)
        if not user:
            continue

        status_config = StatusConfig.query.filter_by(workspace_id=demand.workspace_id, key=demand.status).first()
        if status_config and status_config.is_completed:
            continue  # defesa extra: não deveria existir demanda ativa com status conclusivo

        send_reminder_notification(demand, user)
        demand.reminder_sent = True
        sent_count += 1

    if sent_count:
        db.session.commit()

    return jsonify({'message': f'{sent_count} lembretes verificados e enviados'}), 200

# ============= ROTAS DE PUSH NOTIFICATIONS =============
@app.route('/api/push/vapid-public-key', methods=['GET'])
def get_vapid_public_key():
    return jsonify({'publicKey': os.getenv('VAPID_PUBLIC_KEY', '')}), 200

@app.route('/api/push/subscribe', methods=['POST'])
@jwt_required()
def push_subscribe():
    """Registra (ou atualiza) a inscrição push de um dispositivo/navegador pro usuário atual."""
    user_id = int(get_jwt_identity())
    data = request.get_json()

    endpoint = (data or {}).get('endpoint')
    keys = (data or {}).get('keys', {})

    if not endpoint or not keys.get('p256dh') or not keys.get('auth'):
        return jsonify({'error': 'Dados de inscrição incompletos'}), 400

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        existing.user_id = user_id
        existing.p256dh = keys['p256dh']
        existing.auth = keys['auth']
    else:
        db.session.add(PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=keys['p256dh'],
            auth=keys['auth']
        ))

    db.session.commit()
    return jsonify({'message': 'Inscrito com sucesso'}), 200

@app.route('/api/push/unsubscribe', methods=['POST'])
@jwt_required()
def push_unsubscribe():
    """Remove a inscrição push deste dispositivo/navegador."""
    user_id = int(get_jwt_identity())
    data = request.get_json()
    endpoint = (data or {}).get('endpoint')

    PushSubscription.query.filter_by(user_id=user_id, endpoint=endpoint).delete()
    db.session.commit()

    return jsonify({'message': 'Desinscrito com sucesso'}), 200

@app.route('/api/locations/merge', methods=['POST'])
@jwt_required()
def merge_locations():
    """Renomeia todos os registros (ativos e histórico) de um local pro nome de outro.
    Útil pra unificar variações de digitação do mesmo local (ex: 'IBIAPINA 1' -> 'IBIAPINA 01')."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    data = request.get_json()

    from_location = (data or {}).get('from', '').strip()
    to_location = (data or {}).get('to', '').strip()

    if not from_location or not to_location:
        return jsonify({'error': 'Informe os dois locais (origem e destino)'}), 400
    if from_location == to_location:
        return jsonify({'error': 'Os locais devem ser diferentes'}), 400

    demands_updated = Demand.query.filter(
        db.or_(Demand.workspace_id == workspace_id, db.and_(Demand.workspace_id == None, Demand.user_id == user_id)),
        Demand.location == from_location
    ).update({Demand.location: to_location}, synchronize_session=False)
    history_updated = DemandHistory.query.filter(
        db.or_(DemandHistory.workspace_id == workspace_id, db.and_(DemandHistory.workspace_id == None, DemandHistory.user_id == user_id)),
        DemandHistory.location == from_location
    ).update({DemandHistory.location: to_location}, synchronize_session=False)
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
    """Listar histórico de demandas do workspace"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    location = request.args.get('location', '')
    activity = request.args.get('activity', '')

    # Inclui registros do workspace E registros ainda com workspace_id NULL mas
    # pertencentes ao user_id — esses são registros antigos que a migração ainda não
    # conseguiu popular (pode acontecer se o ALTER TABLE e o UPDATE rodaram em restarts
    # diferentes). Isso garante que o histórico apareça mesmo antes do próximo restart
    # reexecutar a migração e popular o workspace_id neles.
    query = DemandHistory.query.filter(
        db.or_(
            DemandHistory.workspace_id == workspace_id,
            db.and_(
                DemandHistory.workspace_id == None,
                DemandHistory.user_id == user_id
            )
        )
    )

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
    workspace_id = get_user_workspace_id(user_id)
    today = date.today().strftime('%d/%m/%Y')
    output = f"_*{today}*_\n"

    terminal_keys = [s.key for s in ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': True}).all()]

    demands_query = ws_filter(Demand, user_id, workspace_id)
    if terminal_keys:
        demands_query = demands_query.filter(~Demand.status.in_(terminal_keys))
    demands = demands_query.all()

    history_query = ws_filter(DemandHistory, user_id, workspace_id).filter(
        DemandHistory.status_change_date == date.today()
    )
    if terminal_keys:
        history_query = history_query.filter(DemandHistory.status.in_(terminal_keys))
    else:
        history_query = history_query.filter(DemandHistory.status == 'concluido')
    history = history_query.all()
    
    groups = ws_filter(WorkGroup, user_id, workspace_id, {'is_active': True}).order_by(WorkGroup.order).all()
    
    STATUS_EMOJI = {
        s.key: s.emoji for s in ws_filter(StatusConfig, user_id, workspace_id).all()
    }
    STATUS_LABEL = {
        s.key: s.label for s in ws_filter(StatusConfig, user_id, workspace_id).all()
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


# ============= FEED DE ATIVIDADE =============
@app.route('/api/activity-feed', methods=['GET'])
@jwt_required()
def get_activity_feed():
    """Feed de atividades recentes do workspace: quem moveu o quê pra qual status e quando.
    Usa demand_history como fonte — user_id é quem fez a mudança, assigned_to_user_id é o responsável."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    limit = min(int(request.args.get('limit', 50)), 200)

    history = ws_filter(DemandHistory, user_id, workspace_id)         .order_by(DemandHistory.timestamp.desc())         .limit(limit).all()

    items = []
    for h in history:
        actor = User.query.get(h.user_id)
        responsible = User.query.get(h.assigned_to_user_id) if h.assigned_to_user_id else None
        group = WorkGroup.query.get(h.work_group_id) if h.work_group_id else None
        items.append({
            'id': h.id,
            'actorId': h.user_id,
            'actorName': (actor.full_name or actor.username) if actor else 'Alguém',
            'responsibleName': (responsible.full_name or responsible.username) if responsible else None,
            'action': 'status_change',
            'status': h.status,
            'demandId': h.demand_id,
            'location': h.location,
            'activity': h.activity,
            'workGroupId': h.work_group_id,
            'workGroupName': group.name if group else None,
            'workGroupEmoji': group.emoji if group else None,
            'workGroupColor': group.color if group else None,
            'timestamp': h.timestamp.isoformat() if h.timestamp else None,
            'statusChangeDate': h.status_change_date.isoformat() if h.status_change_date else None,
        })

    return jsonify(items), 200



# ============= FLUXO DE APROVAÇÃO =============
def get_approval_status(workspace_id):
    """Retorna o StatusConfig marcado como is_approval neste workspace.
    Fallback: procura pela key 'aprovacao' caso a coluna ainda não tenha sido populada."""
    s = StatusConfig.query.filter_by(workspace_id=workspace_id, is_approval=True).first()
    if not s:
        s = StatusConfig.query.filter_by(workspace_id=workspace_id, key='aprovacao').first()
    return s

def notify_admins_approval_pending(demand, workspace_id, requester_name):
    """Envia push notification + email pra todos os admins do workspace quando
    uma demanda entra em aprovação."""
    admins = WorkspaceMember.query.filter_by(workspace_id=workspace_id, role='admin').all()
    for admin_member in admins:
        admin_user = User.query.get(admin_member.user_id)
        if not admin_user:
            continue

        # Push notification
        subs = PushSubscription.query.filter_by(user_id=admin_user.id).all()
        msg_body = f'{requester_name} solicitou aprovação: {demand.location} · {demand.activity}'
        for sub in subs:
            try:
                from pywebpush import webpush, WebPushException
                webpush(
                    subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    data=json.dumps({"title": "⏳ Aprovação Pendente", "body": msg_body, "url": "/"}),
                    vapid_private_key=os.getenv('VAPID_PRIVATE_KEY'),
                    vapid_claims={"sub": f"mailto:{os.getenv('MAIL_USERNAME', 'admin@app.com')}"},
                    timeout=8
                )
            except Exception:
                pass

        # Email
        if admin_user.email and app.config.get('MAIL_USERNAME'):
            try:
                mail.send(Message(
                    '⏳ Aprovação Pendente — Painel de Bordo',
                    recipients=[admin_user.email],
                    html=f"""
                    <div style="font-family:Arial,sans-serif; background:#111; color:#e0e0e0; padding:2rem; max-width:500px; margin:auto;">
                        <div style="border-left:4px solid #b388ff; padding-left:1rem; margin-bottom:1.5rem;">
                            <div style="font-size:11px; text-transform:uppercase; letter-spacing:2px; color:#9aa0a7;">Painel de Bordo</div>
                            <h2 style="color:#b388ff; margin:4px 0;">⏳ Aprovação Pendente</h2>
                        </div>
                        <p style="color:#9aa0a7;"><strong style="color:#e0e0e0;">{requester_name}</strong> solicitou aprovação para:</p>
                        <div style="background:#1a1a1a; border-radius:6px; padding:1rem; margin:1rem 0;">
                            <div style="color:#e0e0e0; font-weight:700; margin-bottom:0.25rem;">{demand.activity}</div>
                            <div style="color:#9aa0a7; font-size:0.9rem;">📍 {demand.location}</div>
                        </div>
                        <a href="{os.getenv('FRONTEND_URL', '')}" style="display:inline-block; background:#b388ff; color:#1a0a2e; padding:0.75rem 1.5rem; border-radius:4px; text-decoration:none; font-weight:700;">Ver no Painel de Bordo</a>
                        <div style="margin-top:2rem; font-size:11px; color:#555; border-top:1px solid #222; padding-top:1rem;">Desenvolvido por MD Soluções Tecnológicas</div>
                    </div>"""
                ))
            except Exception:
                pass


@app.route('/api/demands/my-approvals', methods=['GET'])
@jwt_required()
def get_my_approvals():
    """Retorna demandas que o usuário enviou para aprovação — tanto pendentes quanto
    já resolvidas (aprovadas ou rejeitadas nos últimos 30 dias via demand_history)."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    # Demandas atualmente em aprovação enviadas por este usuário
    approval_status = get_approval_status(workspace_id)
    pending = []
    if approval_status:
        pending = ws_filter(Demand, user_id, workspace_id).filter(
            Demand.status == approval_status.key,
            db.or_(Demand.user_id == user_id, Demand.assigned_to_user_id == user_id)
        ).all()

    # Demandas recentemente resolvidas que passaram pelo fluxo de aprovação
    # (identificadas pelo rejection_note ou pelo histórico de status change a partir de approval)
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=30)
    rejected_demands = ws_filter(Demand, user_id, workspace_id).filter(
        Demand.rejection_note != None,
        db.or_(Demand.user_id == user_id, Demand.assigned_to_user_id == user_id)
    ).all()

    assignee_ids = {d.assigned_to_user_id for d in pending + rejected_demands if d.assigned_to_user_id}
    user_cache = {u.id: u for u in User.query.filter(User.id.in_(assignee_ids)).all()} if assignee_ids else {}

    return jsonify({
        'pending': [d.to_dict(user_cache=user_cache) for d in pending],
        'rejected': [d.to_dict(user_cache=user_cache) for d in rejected_demands],
    }), 200


@app.route('/api/demands/pending-approval', methods=['GET'])
@jwt_required()
def get_pending_approval():
    """Lista demandas aguardando aprovação no workspace. Restrito a admins."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem ver aprovações pendentes'}), 403

    approval_status = get_approval_status(workspace_id)
    if not approval_status:
        return jsonify([]), 200

    demands = ws_filter(Demand, user_id, workspace_id, {'status': approval_status.key}).all()
    assignee_ids = {d.assigned_to_user_id for d in demands if d.assigned_to_user_id}
    user_cache = {u.id: u for u in User.query.filter(User.id.in_(assignee_ids)).all()} if assignee_ids else {}
    return jsonify([d.to_dict(user_cache=user_cache) for d in demands]), 200


@app.route('/api/demands/<int:demand_id>/approve', methods=['POST'])
@jwt_required()
def approve_demand(demand_id):
    """Admin aprova a demanda: se o status destino for concluído, roda o fluxo completo
    (cria histórico, remove de ativas — igual ao update_demand_status). Se for um status
    intermediário, apenas atualiza o status da demanda ativa."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem aprovar demandas'}), 403

    demand = Demand.query.get_or_404(demand_id)
    data = request.get_json()
    new_status = data.get('status')

    if not new_status:
        return jsonify({'error': 'Informe o status de destino'}), 400

    status_config = ws_filter(StatusConfig, user_id, workspace_id, {'key': new_status}).first()
    if not status_config:
        return jsonify({'error': 'Status inválido'}), 400

    is_terminal = status_config.is_completed

    requester_id = demand.assigned_to_user_id or demand.user_id
    demand.previous_status = None
    demand.rejection_note = None

    if is_terminal:
        # Fluxo completo: cria registro no histórico e remove da tabela de ativas
        history = DemandHistory(
            user_id=user_id,
            workspace_id=workspace_id,
            work_group_id=demand.work_group_id,
            demand_id=demand.id,
            assigned_to_user_id=demand.assigned_to_user_id or demand.user_id,
            priority=demand.priority,
            checklist=demand.checklist or [],
            location=demand.location,
            activity=demand.activity,
            context=demand.context,
            status=new_status,
            status_change_date=date.today(),
            created_date=demand.created_date
        )
        db.session.add(history)

        # Demanda recorrente: cria próxima ocorrência
        if demand.is_recurring and demand.recurrence_type:
            new_due = next_due_date(demand.due_date, demand.recurrence_type)
            first_status = ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': False}).order_by(StatusConfig.order.asc()).first()
            initial_status = first_status.key if first_status else 'nao-iniciado'
            checklist_reset = [{'text': item['text'], 'checked': False} for item in (demand.checklist or [])]
            next_demand = Demand(
                user_id=demand.user_id,
                workspace_id=demand.workspace_id,
                work_group_id=demand.work_group_id,
                location=demand.location,
                activity=demand.activity,
                context=demand.context,
                status=initial_status,
                priority=demand.priority,
                due_date=new_due,
                assigned_to=demand.assigned_to,
                assigned_to_user_id=demand.assigned_to_user_id,
                is_recurring=True,
                recurrence_type=demand.recurrence_type,
                checklist=checklist_reset
            )
            db.session.add(next_demand)

        db.session.delete(demand)
    else:
        # Status intermediário: apenas atualiza
        demand.status = new_status

    db.session.commit()

    # Notifica o solicitante em background
    admin_user = User.query.get(user_id)
    admin_name = (admin_user.full_name or admin_user.username) if admin_user else 'Admin'
    status_label = status_config.label if status_config else new_status
    import threading as _t
    _rid, _act, _loc, _al, _sl = requester_id, demand.activity, demand.location, admin_name, status_label
    def _notify_approved():
        with app.app_context():
            send_push_notification(
                _rid,
                '✅ Demanda aprovada',
                f'{_loc} · {_act} → {_sl} (por {_al})',
                '/'
            )
    _t.Thread(target=_notify_approved, daemon=True).start()

    return jsonify({'message': 'Demanda aprovada'}), 200


@app.route('/api/demands/<int:demand_id>/reject', methods=['POST'])
@jwt_required()
def reject_demand(demand_id):
    """Admin rejeita a demanda: volta pro status anterior e salva a justificativa."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem rejeitar demandas'}), 403

    demand = Demand.query.get_or_404(demand_id)
    data = request.get_json()
    note = (data.get('note') or '').strip()

    if not note:
        return jsonify({'error': 'A justificativa é obrigatória'}), 400

    # Volta pro status anterior; se não houver, vai pro primeiro status não-concluído
    target_status = demand.previous_status
    if not target_status:
        first = ws_filter(StatusConfig, user_id, workspace_id, {'is_completed': False}).order_by(StatusConfig.order.asc()).first()
        target_status = first.key if first else 'nao-iniciado'

    demand.status = target_status
    demand.rejection_note = note
    demand.previous_status = None

    # Appenda a nota ao contexto pra o membro ver o motivo
    admin_user = User.query.get(user_id)
    admin_name = admin_user.full_name or admin_user.username if admin_user else 'Admin'
    from datetime import date as _date
    prefix = f'[Rejeitado em {_date.today().strftime("%d/%m/%Y")} por {admin_name}: {note}]'
    demand.context = f'{prefix}\n\n{demand.context}' if demand.context else prefix

    requester_id = demand.assigned_to_user_id or demand.user_id
    db.session.commit()

    # Notifica o solicitante em background
    import threading as _t
    _rid, _act, _loc, _n, _an = requester_id, demand.activity, demand.location, note, admin_name
    def _notify_rejected():
        with app.app_context():
            send_push_notification(
                _rid,
                f'❌ Demanda rejeitada',
                f'{_loc} · {_act} — {_an}: "{_n[:80]}"',
                '/'
            )
    _t.Thread(target=_notify_rejected, daemon=True).start()

    return jsonify({'message': 'Demanda rejeitada', 'demand': demand.to_dict()}), 200


# ── Interceptar update_demand_status para detectar entrada em aprovação ────────
# (injetado no update_demand_status existente via after_this_commit)



# ============= GESTÃO DE USUÁRIOS (ADMIN DA PLATAFORMA) =============
@app.route('/api/admin/users', methods=['GET'])
@jwt_required()
def admin_list_users():
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403
    users = User.query.order_by(User.id.asc()).all()
    result = []
    for u in users:
        member = WorkspaceMember.query.filter_by(user_id=u.id).first()
        workspace = Workspace.query.get(member.workspace_id) if member else None
        d = u.to_dict()
        d['workspaceName'] = workspace.name if workspace else None
        d['workspaceRole'] = member.role if member else None
        result.append(d)
    return jsonify(result), 200


@app.route('/api/admin/users/<int:target_id>', methods=['PUT'])
@jwt_required()
def admin_edit_user(target_id):
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403
    target = User.query.get_or_404(target_id)
    data = request.get_json()
    if data.get('full_name', '').strip():
        target.full_name = data['full_name'].strip()
    if 'email' in data:
        email = data['email'].strip().lower()
        if User.query.filter(User.email == email, User.id != target_id).first():
            return jsonify({'error': 'E-mail já em uso'}), 409
        target.email = email
    if 'username' in data:
        uname = data['username'].strip()
        if User.query.filter(User.username == uname, User.id != target_id).first():
            return jsonify({'error': 'Username já em uso'}), 409
        target.username = uname
    db.session.commit()
    return jsonify(target.to_dict()), 200


@app.route('/api/admin/users/<int:target_id>/toggle-active', methods=['POST'])
@jwt_required()
def admin_toggle_user(target_id):
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403
    if target_id == user_id:
        return jsonify({'error': 'Não é possível alterar sua própria conta aqui'}), 400
    target = User.query.get_or_404(target_id)
    target.is_active = not target.is_active
    db.session.commit()
    return jsonify({'isActive': target.is_active, 'username': target.username}), 200


@app.route('/api/admin/users/<int:target_id>', methods=['DELETE'])
@jwt_required()
def admin_delete_user(target_id):
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403
    if target_id == user_id:
        return jsonify({'error': 'Não é possível excluir sua própria conta'}), 400
    target = User.query.get_or_404(target_id)
    username = target.username
    db.session.delete(target)
    db.session.commit()
    return jsonify({'message': f'Usuário {username} excluído'}), 200

# ============= ROTAS DE BACKUP =============
@app.route('/api/export', methods=['GET'])
@jwt_required()
def export_data():
    """Exportar todos os dados do workspace"""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    
    demands = ws_filter(Demand, user_id, workspace_id).all()
    history = ws_filter(DemandHistory, user_id, workspace_id).all()
    groups = ws_filter(WorkGroup, user_id, workspace_id).all()
    
    return jsonify({
        'demands': [d.to_dict() for d in demands],
        'history': [h.to_dict() for h in history],
        'workGroups': [g.to_dict() for g in groups],
        'exportedAt': datetime.now().isoformat()
    }), 200

@app.route('/api/import', methods=['POST'])
@jwt_required()
def import_data():
    """Importar dados pro workspace (restaurar backup). Ação restrita a admin do
    workspace, já que escreve em massa em dados compartilhados por todo o time."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)

    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores do workspace podem restaurar backup'}), 403

    data = request.get_json()
    
    try:
        group_mapping = {}
        for group_data in data.get('workGroups', []):
            group = WorkGroup(
                user_id=user_id,
                workspace_id=workspace_id,
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
                workspace_id=workspace_id,
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
                workspace_id=workspace_id,
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
