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
    'pool_pre_ping': True,    # reconecta automaticamente se conexão morreu
    'pool_recycle': 240,      # recicla antes dos 300s que o Neon fecha ociosas
    'pool_size': 4,           # 1 conexão por thread (4 threads no gthread)
    'max_overflow': 2,        # margem extra em picos
    'pool_timeout': 10,       # espera até 10s por conexão disponível
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
            'isActive': self.is_active if self.is_active is not None else True,
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



class DemandType(db.Model):
    """Tipos de demanda configuráveis por workspace (ex: Corretiva, Preventiva, Projeto)."""
    __tablename__ = 'demand_types'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspaces.id'), nullable=True)
    name         = db.Column(db.String(60), nullable=False)
    emoji        = db.Column(db.String(10), default='📌')
    color        = db.Column(db.String(20), default='#9aa0a7')
    order        = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('workspace_id', 'name', name='_workspace_type_uc'),)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'emoji': self.emoji or '📌',
            'color': self.color or '#9aa0a7',
            'order': self.order or 0,
            'workspaceId': self.workspace_id,
        }


class DemandNote(db.Model):
    """Anotações timestampadas dentro de uma demanda."""
    __tablename__ = 'demand_notes'
    id         = db.Column(db.Integer, primary_key=True)
    demand_id  = db.Column(db.Integer, db.ForeignKey('demands.id', ondelete='CASCADE'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    username   = db.Column(db.String(80), nullable=True)  # snapshot do autor
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'demandId': self.demand_id,
            'userId': self.user_id,
            'username': self.username,
            'content': self.content,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
        }

class MaintenanceNotice(db.Model):
    """Aviso de manutenção programada. Apenas um registro ativo por vez."""
    __tablename__ = 'maintenance_notices'
    id           = db.Column(db.Integer, primary_key=True)
    message      = db.Column(db.String(300), nullable=False)
    starts_at    = db.Column(db.DateTime, nullable=False)
    ends_at      = db.Column(db.DateTime, nullable=False)
    notify_at    = db.Column(db.DateTime, nullable=True)   # quando enviar a notificação push
    notify_sent  = db.Column(db.Boolean, default=False)
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'message': self.message,
            'startsAt': self.starts_at.isoformat() if self.starts_at else None,
            'endsAt': self.ends_at.isoformat() if self.ends_at else None,
            'notifyAt': self.notify_at.isoformat() if self.notify_at else None,
            'notifySent': self.notify_sent,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
        }


class AgentToken(db.Model):
    """Token de acesso pessoal para agentes de IA externos (Custom GPTs, n8n, etc.)"""
    __tablename__ = 'agent_tokens'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name       = db.Column(db.String(100), nullable=False)   # ex: "Meu Custom GPT"
    token      = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_used  = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'token': self.token,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'lastUsed': self.last_used.isoformat() if self.last_used else None,
        }


class AppLog(db.Model):
    """Log de erros e eventos do frontend, enviados pelos clientes em tempo real."""
    __tablename__ = 'app_logs'
    id         = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    username   = db.Column(db.String(80), nullable=True)   # snapshot para não perder se user for deletado
    level      = db.Column(db.String(10), nullable=False, default='error')  # error | warn | info
    category   = db.Column(db.String(50), nullable=True)   # js_error | api_error | init | etc.
    message    = db.Column(db.Text, nullable=False)
    details    = db.Column(db.Text, nullable=True)          # JSON extra (stack trace, etc.)

    def to_dict(self):
        return {
            'id': self.id,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'userId': self.user_id,
            'username': self.username,
            'level': self.level,
            'category': self.category,
            'message': self.message,
            'details': self.details,
        }

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
    group_type = db.Column(db.String(50), nullable=True)  # Trabalho, Pessoal, Freelancer, Estudos, etc.
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
            'groupType': self.group_type,
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
    type_id = db.Column(db.Integer, db.ForeignKey('demand_types.id', ondelete='SET NULL'), nullable=True)
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
            'typeId': self.type_id,
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
    action_type     = db.Column(db.String(30), nullable=True)   # 'approved' | null (mudança normal)
    type_id         = db.Column(db.Integer, nullable=True)       # snapshot do tipo ao concluir
    notes_snapshot  = db.Column(db.JSON, nullable=True)          # snapshot das anotações ao concluir
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
            'checklist': self.checklist,
            'actionType': self.action_type,
            'typeId': self.type_id,
            'notesSnapshot': self.notes_snapshot or []
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



@app.route('/whiteboard')
def whiteboard():
    """Whiteboard — serve o arquivo estático whiteboard.html do diretório static."""
    return send_from_directory('static', 'whiteboard.html')


@app.route('/')
def index():
    """Servir página principal"""
    return render_template('index.html')

@app.route('/ping')
def ping():
    """Health check leve. Também aplica migrations pendentes na primeira chamada."""
    _approval_cols = [
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS previous_status VARCHAR(50)",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS notes_snapshot JSON",
        "ALTER TABLE work_groups ADD COLUMN IF NOT EXISTS group_type VARCHAR(50)",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS action_type VARCHAR(30)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP",
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS rejection_note TEXT",
        "ALTER TABLE status_configs ADD COLUMN IF NOT EXISTS is_approval BOOLEAN DEFAULT FALSE",
        "UPDATE status_configs SET is_approval = TRUE WHERE key = 'aprovacao' AND (is_approval IS NULL OR is_approval = FALSE)",
        """CREATE TABLE IF NOT EXISTS agent_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            token VARCHAR(64) UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            last_used TIMESTAMP
        )""",
    ]
    for _s in _approval_cols:
        try:
            db.session.execute(text(_s))
            db.session.commit()
        except Exception:
            db.session.rollback()
    # Limpeza de logs antigos (>7 dias)
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=7)
        AppLog.query.filter(AppLog.created_at < cutoff).delete()
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

    demand_types = ws_filter(DemandType, user_id, workspace_id).order_by(DemandType.order.asc()).all()

    return jsonify({
        'user': user.to_dict() if user else None,
        'demandTypes': [t.to_dict() for t in demand_types],
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

    if app.config.get('MAIL_USERNAME'):
        import threading as _t
        _uid, _uname, _uemail, _verified = user.id, user.full_name or user.username, user.email, user.access_verified
        def _send_welcome():
            with app.app_context():
                try:
                    frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:5000')
                    body_html = f'''
                    <p>Olá {_uname},</p>
                    <p>Sua conta no <strong>Painel de Bordo</strong> foi criada com sucesso!</p>
                    ''' + (f'<p><a href="{frontend_url}">Acessar Painel de Bordo</a></p>' if _verified else
                    '<p>Antes de começar, você precisará de uma <strong>chave de acesso</strong> fornecida pelo administrador.</p>')
                    mail.send(Message('Bem-vindo ao Painel de Bordo!', recipients=[_uemail], html=body_html))
                    print(f'[email] Boas-vindas enviado para {_uemail}')
                except Exception as e:
                    print(f'[email] ERRO ao enviar boas-vindas para {_uemail}: {type(e).__name__}: {e}')
        _t.Thread(target=_send_welcome, daemon=True).start()
    else:
        print('[email] MAIL_USERNAME não configurado — e-mail de boas-vindas não enviado')

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
        # Chave pessoal: cria um workspace novo e independente com
        # status/prioridade padrão. Grupos ficam em branco — o usuário
        # cria os próprios conforme sua necessidade.
        workspace = create_personal_workspace(user)
        seed_default_status_and_priority(user.id, workspace.id)
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


@app.route('/api/workspace', methods=['PUT'])
@jwt_required()
def update_workspace():
    """Atualiza nome do workspace. Restrito a admin do workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    if not is_workspace_admin(user_id, workspace_id):
        return jsonify({'error': 'Apenas administradores podem editar o workspace'}), 403
    workspace = Workspace.query.get_or_404(workspace_id)
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Nome não pode ser vazio'}), 400
    if len(name) > 80:
        return jsonify({'error': 'Nome deve ter no máximo 80 caracteres'}), 400
    workspace.name = name
    db.session.commit()
    return jsonify(workspace.to_dict()), 200

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
    if 'group_type' in data:
        group.group_type = data['group_type'] or None
    
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

    # Garante coluna type_id (adicionada com os tipos de demanda)
    for _col in [
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS type_id INTEGER",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS type_id INTEGER",
    ]:
        try:
            db.session.execute(text(_col))
            db.session.commit()
        except Exception:
            db.session.rollback()

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
        reminder_at=parse_reminder_at(data.get('reminder_at')),
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
            demand.reminder_at = parse_reminder_at(data['reminder_at'])
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
        assigned_to_user_id=demand.assigned_to_user_id or demand.user_id,
        priority=demand.priority,
        checklist=demand.checklist or [],
        notes_snapshot=get_demand_notes_snapshot(demand.id),
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


@app.route('/api/notes/<int:note_id>', methods=['GET'])
@jwt_required()
def get_note(note_id):
    """Retorna uma nota específica (usada para restaurar gadgets fixados)"""
    user_id = int(get_jwt_identity())
    note = Note.query.filter_by(id=note_id, user_id=user_id).first_or_404()
    return jsonify(note.to_dict()), 200

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
    """Verifica e dispara lembretes vencidos do usuário logado.
    Push é enviado em thread separada mas a lista de disparados é retornada imediatamente."""
    user_id = int(get_jwt_identity())
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'triggered': []}), 200

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
        triggered = []
        for demand in due_demands:
            # Push em thread separada para não bloquear
            import threading as _th
            _d, _u = demand, user
            _th.Thread(target=lambda d=_d, u=_u: send_push_in_context(d, u), daemon=True).start()
            demand.reminder_sent = True
            triggered.append({
                'id': demand.id,
                'location': demand.location,
                'activity': demand.activity,
                'status': demand.status,
            })

        if due_demands:
            db.session.commit()

        return jsonify({'triggered': triggered}), 200

    except Exception as e:
        db.session.rollback()
        print(f'[reminders] erro: {e}')
        return jsonify({'triggered': [], 'error': str(e)}), 200


def send_push_in_context(demand, user):
    """Envia push + email de lembrete em contexto de app Flask."""
    with app.app_context():
        send_reminder_notification(demand, user)

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

    # Garante colunas novas que podem não existir ainda no banco
    for _col in [
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS type_id INTEGER",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS action_type VARCHAR(30)",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS notes_snapshot JSON",
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS type_id INTEGER",
    ]:
        try:
            db.session.execute(text(_col))
            db.session.commit()
        except Exception:
            db.session.rollback()

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

    history = query.order_by(DemandHistory.status_change_date.desc()).limit(500).all()
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
    """Feed de atividades recentes do workspace."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    limit = min(int(request.args.get('limit', 50)), 200)

    # Garante colunas novas que podem não existir ainda no banco
    for _col in [
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS action_type VARCHAR(30)",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS type_id INTEGER",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS notes_snapshot JSON",
    ]:
        try:
            db.session.execute(text(_col))
            db.session.commit()
        except Exception:
            db.session.rollback()

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
            'action': h.action_type or 'status_change',
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
            action_type='approved',
            notes_snapshot=get_demand_notes_snapshot(demand.id),
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
    try:
        # 1. Tabelas com user_id NOT NULL — precisam ser deletadas antes
        StatusConfig.query.filter_by(user_id=target_id).delete()
        PriorityConfig.query.filter_by(user_id=target_id).delete()
        Note.query.filter_by(user_id=target_id).delete()
        PushSubscription.query.filter_by(user_id=target_id).delete()
        WorkspaceMember.query.filter_by(user_id=target_id).delete()
        # work_groups, demands, demand_history têm cascade no relacionamento User

        # 2. Colunas nullable que referenciam o usuário — nullificar (preserva histórico)
        Demand.query.filter_by(assigned_to_user_id=target_id).update({'assigned_to_user_id': None})
        DemandHistory.query.filter_by(assigned_to_user_id=target_id).update({'assigned_to_user_id': None})
        AccessKey.query.filter_by(created_by=target_id).update({'created_by': None})
        AccessKey.query.filter_by(used_by=target_id).update({'used_by': None})

        db.session.flush()  # aplica as operações acima antes do delete final
        db.session.delete(target)
        db.session.commit()
        return jsonify({'message': f'Usuário {username} excluído com sucesso'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Erro ao excluir: {str(e)[:300]}'}), 500


# ============= MANUTENÇÃO DO SISTEMA =============
@app.route('/api/maintenance', methods=['GET'])
def get_maintenance():
    """Retorna o aviso de manutenção ativo ou futuro (sem autenticação — todos precisam ver)."""
    now = datetime.now()
    notice = MaintenanceNotice.query.filter(
        MaintenanceNotice.ends_at > now
    ).order_by(MaintenanceNotice.starts_at.asc()).first()
    return jsonify(notice.to_dict() if notice else None), 200


@app.route('/api/admin/maintenance', methods=['POST'])
@jwt_required()
def set_maintenance():
    """Cria ou substitui o aviso de manutenção. Restrito ao admin da plataforma."""
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403

    data = request.get_json()
    try:
        starts_at = datetime.fromisoformat(data['starts_at'])
        ends_at   = datetime.fromisoformat(data['ends_at'])
        notify_at = datetime.fromisoformat(data['notify_at']) if data.get('notify_at') else None
    except (KeyError, ValueError) as e:
        return jsonify({'error': f'Datas inválidas: {e}'}), 400

    if ends_at <= starts_at:
        return jsonify({'error': 'Fim deve ser depois do início'}), 400

    # Remove avisos anteriores e cria novo
    MaintenanceNotice.query.delete()
    notice = MaintenanceNotice(
        message=data.get('message', 'O sistema passará por manutenção programada.'),
        starts_at=starts_at,
        ends_at=ends_at,
        notify_at=notify_at,
        notify_sent=False,
        created_by=user_id
    )
    db.session.add(notice)
    db.session.commit()
    return jsonify(notice.to_dict()), 201


@app.route('/api/admin/maintenance', methods=['DELETE'])
@jwt_required()
def cancel_maintenance():
    """Cancela o aviso de manutenção atual."""
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403
    MaintenanceNotice.query.delete()
    db.session.commit()
    return jsonify({'message': 'Aviso cancelado'}), 200


@app.route('/api/cron/check-maintenance-notify', methods=['GET', 'POST'])
def cron_maintenance_notify():
    """Verifica se é hora de enviar a notificação de manutenção programada.
    Chamar via cron-job.org a cada 5 minutos."""
    secret = request.args.get('key') or request.headers.get('X-Cron-Key')
    if not secret or secret != os.getenv('CRON_SECRET_KEY'):
        return jsonify({'error': 'Não autorizado'}), 403

    now = datetime.now()
    notice = MaintenanceNotice.query.filter(
        MaintenanceNotice.notify_at != None,
        MaintenanceNotice.notify_at <= now,
        MaintenanceNotice.notify_sent == False,
        MaintenanceNotice.ends_at > now
    ).first()

    if not notice:
        return jsonify({'message': 'Nenhuma notificação pendente'}), 200

    # Envia push para todos os usuários
    users = User.query.filter_by(is_active=True).all()
    sent = 0
    for u in users:
        try:
            send_push_notification(
                u.id,
                '🔧 Manutenção Programada',
                f'{notice.message} — {notice.starts_at.strftime("%d/%m às %H:%M")} até {notice.ends_at.strftime("%H:%M")}',
                '/'
            )
            sent += 1
        except Exception:
            pass

    notice.notify_sent = True
    db.session.commit()
    return jsonify({'message': f'Notificação enviada para {sent} usuários'}), 200




_agent_schema_ok = False

def parse_reminder_at(value):
    """Parse de reminder_at enviado pelo frontend.
    Aceita múltiplos formatos:
      "2026-07-08T17:00"          (datetime-local sem timezone — legado)
      "2026-07-08T20:00:00.000Z"  (ISO UTC com Z — novo formato)
      "2026-07-08T20:00:00+00:00" (ISO UTC com offset)
    Sempre retorna datetime naive em UTC (para comparar com datetime.now() no servidor UTC).
    """
    if not value:
        return None
    try:
        # Remove sufixo Z e substitui por offset +00:00 para fromisoformat funcionar
        s = str(value).strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        # Remove milissegundos se presentes (ex: .000)
        import re as _re
        s = _re.sub(r'\.\d+(\+)', r'\1', s)
        s = _re.sub(r'\.\d+$', '', s)
        dt = datetime.fromisoformat(s)
        # Se tem timezone, converte para UTC naive
        if dt.tzinfo is not None:
            import datetime as _dt
            utc = _dt.timezone.utc
            dt = dt.astimezone(utc).replace(tzinfo=None)
        return dt
    except Exception:
        # Fallback para formato legado sem timezone
        try:
            return datetime.strptime(value[:16], '%Y-%m-%dT%H:%M')
        except Exception:
            return None


def get_demand_notes_snapshot(demand_id):
    """Captura snapshot das anotações de uma demanda ao ser concluída.
    Cria a tabela se não existir e retorna [] em caso de qualquer erro."""
    try:
        # Garante que a tabela existe antes de consultar
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS demand_notes (
                id SERIAL PRIMARY KEY,
                demand_id INTEGER NOT NULL REFERENCES demands(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                username VARCHAR(80),
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )"""))
        db.session.commit()
        notes = DemandNote.query.filter_by(demand_id=demand_id)\
            .order_by(DemandNote.created_at.asc()).all()
        return [{'username': n.username, 'content': n.content,
                 'createdAt': n.created_at.isoformat() if n.created_at else None}
                for n in notes]
    except Exception as e:
        db.session.rollback()
        print(f'[demand_notes] erro ao capturar snapshot: {e}')
        return []


def ensure_agent_schema():
    """Garante colunas novas nas tabelas usadas pelos endpoints do agente.
    Roda apenas uma vez por startup do servidor (flag em memória)."""
    global _agent_schema_ok
    if _agent_schema_ok:
        return
    migrations = [
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS type_id INTEGER",
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS previous_status VARCHAR(50)",
        "ALTER TABLE demands ADD COLUMN IF NOT EXISTS rejection_note TEXT",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS type_id INTEGER",
        "ALTER TABLE demand_history ADD COLUMN IF NOT EXISTS action_type VARCHAR(30)",
        "ALTER TABLE status_configs ADD COLUMN IF NOT EXISTS is_approval BOOLEAN DEFAULT FALSE",
        "ALTER TABLE work_groups ADD COLUMN IF NOT EXISTS group_type VARCHAR(50)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP",
    ]
    for sql in migrations:
        try:
            db.session.execute(text(sql))
            db.session.commit()
        except Exception:
            db.session.rollback()
    _agent_schema_ok = True

# ============= AGENTE IA (ACESSO EXTERNO VIA TOKEN) =============
def get_agent_user(token_str):
    """Autentica um agente via token pessoal e retorna (user, workspace_id) ou None."""
    tok = AgentToken.query.filter_by(token=token_str).first()
    if not tok:
        return None, None
    tok.last_used = datetime.now()
    db.session.commit()
    workspace_id = get_user_workspace_id(tok.user_id)
    return User.query.get(tok.user_id), workspace_id


@app.route('/api/agent/summary', methods=['GET'])
def agent_summary():
    """Resumo das demandas para agentes de IA externos.
    Auth: ?token=SEU_TOKEN  ou  Authorization: Bearer SEU_TOKEN"""
    token_str = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', '').strip())
    user, workspace_id = get_agent_user(token_str)
    if not user:
        return jsonify({'error': 'Token inválido ou expirado'}), 401

    ensure_agent_schema()  # garante colunas novas antes de qualquer query ORM

    demands  = ws_filter(Demand, user.id, workspace_id).all()
    terminal = [s.key for s in ws_filter(StatusConfig, user.id, workspace_id, {'is_completed': True}).all()]
    approval = [s.key for s in ws_filter(StatusConfig, user.id, workspace_id, {'is_approval': True}).all()]
    active   = [d for d in demands if d.status not in terminal]
    overdue  = [d for d in active if d.due_date and str(d.due_date) < str(date.today()) and d.status not in approval]
    today_d  = [d for d in active if d.due_date and str(d.due_date) == str(date.today())]
    pending_appr = [d for d in active if d.status in approval]

    # SQL direto — evita falha se coluna nova (group_type) ainda não existe no banco
    rows = db.session.execute(text(
        "SELECT id, name, emoji, color FROM work_groups WHERE workspace_id = :ws OR (workspace_id IS NULL AND user_id = :uid)"
    ), {'ws': workspace_id, 'uid': user.id}).fetchall()
    groups = {r[0]: {'name': r[1], 'emoji': r[2] or '', 'color': r[3] or '#f5a623'} for r in rows}

    def fmt(d):
        return {
            'local': d.location,
            'atividade': d.activity,
            'status': d.status,
            'prioridade': d.priority,
            'grupo': (groups.get(d.work_group_id) or {}).get('name', '—'),
            'vencimento': str(d.due_date) if d.due_date else None,
        }

    workspace = Workspace.query.get(workspace_id)

    return jsonify({
        'usuario': user.full_name or user.username,
        'workspace': workspace.name if workspace else '',
        'logoUrl': workspace.logo_url if workspace else None,
        'data_hoje': str(date.today()),
        'totais': {
            'ativas': len(active),
            'atrasadas': len(overdue),
            'para_hoje': len(today_d),
            'aguardando_aprovacao': len(pending_appr),
        },
        'atrasadas': [fmt(d) for d in overdue],
        'para_hoje': [fmt(d) for d in today_d],
        'aguardando_aprovacao': [fmt(d) for d in pending_appr],
    }), 200


@app.route('/api/agent/demands', methods=['GET'])
def agent_demands():
    """Lista completa de demandas ativas para agentes de IA.
    Parâmetros opcionais: ?grupo=NOME&status=STATUS&prioridade=ALTA&local=TEXTO"""
    token_str = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', '').strip())
    user, workspace_id = get_agent_user(token_str)
    if not user:
        return jsonify({'error': 'Token inválido'}), 401

    ensure_agent_schema()  # garante colunas novas antes de qualquer query ORM

    terminal = [s.key for s in ws_filter(StatusConfig, user.id, workspace_id, {'is_completed': True}).all()]
    demands  = ws_filter(Demand, user.id, workspace_id).filter(~Demand.status.in_(terminal)).all()

    # SQL direto — evita falha se coluna nova (group_type) ainda não existe no banco
    rows = db.session.execute(text(
        "SELECT id, name, emoji, color FROM work_groups WHERE workspace_id = :ws OR (workspace_id IS NULL AND user_id = :uid)"
    ), {'ws': workspace_id, 'uid': user.id}).fetchall()
    groups = {r[0]: {'name': r[1], 'emoji': r[2] or '', 'color': r[3] or '#f5a623'} for r in rows}

    # Filtros opcionais
    grupo_q   = (request.args.get('grupo')      or '').lower()
    status_q  = (request.args.get('status')     or '').lower()
    prior_q   = (request.args.get('prioridade') or '').lower()
    local_q   = (request.args.get('local')      or '').lower()

    result = []
    for d in demands:
        g_info = groups.get(d.work_group_id) or {}
        nome_grupo = g_info.get('name', '') if isinstance(g_info, dict) else str(g_info)
        if grupo_q  and grupo_q  not in nome_grupo.lower(): continue
        if status_q and status_q not in d.status.lower():   continue
        if prior_q  and prior_q  not in (d.priority or '').lower(): continue
        if local_q  and local_q  not in (d.location or '').lower(): continue
        result.append({
            'id': d.id,
            'local': d.location,
            'atividade': d.activity,
            'contexto': d.context,
            'status': d.status,
            'prioridade': d.priority,
            'grupo': nome_grupo,
            'grupoEmoji': g_info.get('emoji', '') if isinstance(g_info, dict) else '',
            'grupoColor': g_info.get('color', '#f5a623') if isinstance(g_info, dict) else '#f5a623',
            'checklist': d.checklist or [],
            'vencimento': str(d.due_date) if d.due_date else None,
            'atrasada': bool(d.due_date and str(d.due_date) < str(date.today())),
        })

    return jsonify({'total': len(result), 'demandas': result}), 200


@app.route('/api/agent/history', methods=['GET'])
def agent_history():
    """Histórico de demandas concluídas recentes para agentes de IA.
    Parâmetro opcional: ?dias=30 (padrão: 30 dias)"""
    token_str = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', '').strip())
    user, workspace_id = get_agent_user(token_str)
    if not user:
        return jsonify({'error': 'Token inválido'}), 401

    ensure_agent_schema()

    days   = min(int(request.args.get('dias', 30)), 90)
    cutoff = date.today() - __import__('datetime').timedelta(days=days)
    records = ws_filter(DemandHistory, user.id, workspace_id).filter(
        DemandHistory.status_change_date >= cutoff
    ).order_by(DemandHistory.timestamp.desc()).limit(100).all()

    return jsonify({'periodo_dias': days, 'total': len(records), 'concluidas': [
        {'local': h.location, 'atividade': h.activity, 'status': h.status,
         'data': str(h.status_change_date), 'aprovada': h.action_type == 'approved'}
        for h in records
    ]}), 200


# ── Gerenciamento de tokens pelo usuário ──────────────────────────────────────
@app.route('/api/agent/tokens', methods=['GET'])
@jwt_required()
def list_agent_tokens():
    user_id = int(get_jwt_identity())
    tokens = AgentToken.query.filter_by(user_id=user_id).order_by(AgentToken.created_at.desc()).all()
    return jsonify([t.to_dict() for t in tokens]), 200


@app.route('/api/agent/tokens', methods=['POST'])
@jwt_required()
def create_agent_token():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Dê um nome para identificar seu agente'}), 400
    if AgentToken.query.filter_by(user_id=user_id).count() >= 10:
        return jsonify({'error': 'Limite de 10 tokens por conta'}), 400
    token = AgentToken(user_id=user_id, name=name, token=secrets.token_urlsafe(40))
    db.session.add(token)
    db.session.commit()
    return jsonify(token.to_dict()), 201


@app.route('/api/agent/tokens/<int:tok_id>', methods=['DELETE'])
@jwt_required()
def delete_agent_token(tok_id):
    user_id = int(get_jwt_identity())
    tok = AgentToken.query.filter_by(id=tok_id, user_id=user_id).first_or_404()
    db.session.delete(tok)
    db.session.commit()
    return jsonify({'message': 'Token revogado'}), 200


# ============= LOG DO SISTEMA =============
@app.route('/api/log', methods=['POST'])
@jwt_required()
def post_log():
    """Recebe entradas de log do frontend. Rate limit simples: max 20 por minuto por usuário."""
    user_id = int(get_jwt_identity())
    data = request.get_json() or {}

    level    = (data.get('level') or 'error')[:10]
    category = (data.get('category') or '')[:50]
    message  = (data.get('message') or '')[:2000]
    details  = data.get('details')
    if details and not isinstance(details, str):
        details = json.dumps(details)[:4000]

    if not message:
        return jsonify({'ok': True}), 200

    # Rate limit: não salvar mais de 20 logs por minuto por usuário
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(minutes=1)
    recent = AppLog.query.filter(
        AppLog.user_id == user_id,
        AppLog.created_at >= cutoff
    ).count()
    if recent >= 20:
        return jsonify({'ok': True, 'skipped': True}), 200

    user = User.query.get(user_id)
    entry = AppLog(
        user_id=user_id,
        username=user.full_name or user.username if user else str(user_id),
        level=level,
        category=category,
        message=message,
        details=details
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'ok': True}), 201


@app.route('/api/admin/logs', methods=['GET'])
@jwt_required()
def admin_get_logs():
    """Retorna logs do sistema. Restrito ao admin da plataforma."""
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403

    limit     = min(int(request.args.get('limit', 200)), 500)
    level_f   = request.args.get('level')
    user_f    = request.args.get('username')
    category_f = request.args.get('category')

    q = AppLog.query.order_by(AppLog.created_at.desc())
    if level_f:    q = q.filter(AppLog.level == level_f)
    if user_f:     q = q.filter(AppLog.username.ilike(f'%{user_f}%'))
    if category_f: q = q.filter(AppLog.category == category_f)

    logs = q.limit(limit).all()
    return jsonify([l.to_dict() for l in logs]), 200


@app.route('/api/admin/logs', methods=['DELETE'])
@jwt_required()
def admin_clear_logs():
    """Limpa todos os logs. Restrito ao admin da plataforma."""
    user_id = int(get_jwt_identity())
    requester = User.query.get(user_id)
    if not requester or not requester.is_admin:
        return jsonify({'error': 'Acesso restrito'}), 403
    deleted = AppLog.query.delete()
    db.session.commit()
    return jsonify({'deleted': deleted}), 200


# ============= TIPOS DE DEMANDA =============
@app.route('/api/demand-types', methods=['GET'])
@jwt_required()
def get_demand_types():
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    types = ws_filter(DemandType, user_id, workspace_id).order_by(DemandType.order.asc()).all()
    return jsonify([t.to_dict() for t in types]), 200


@app.route('/api/demand-types', methods=['POST'])
@jwt_required()
def create_demand_type():
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Nome obrigatório'}), 400
    if DemandType.query.filter(
        db.or_(DemandType.workspace_id == workspace_id,
               db.and_(DemandType.workspace_id == None, DemandType.user_id == user_id)),
        DemandType.name == name
    ).first():
        return jsonify({'error': 'Já existe um tipo com este nome'}), 409
    max_order = db.session.query(db.func.max(DemandType.order)).filter(
        db.or_(DemandType.workspace_id == workspace_id,
               db.and_(DemandType.workspace_id == None, DemandType.user_id == user_id))
    ).scalar() or 0
    dt = DemandType(user_id=user_id, workspace_id=workspace_id,
                    name=name, emoji=data.get('emoji','📌'),
                    color=data.get('color','#9aa0a7'), order=max_order+1)
    db.session.add(dt)
    db.session.commit()
    return jsonify(dt.to_dict()), 201


@app.route('/api/demand-types/<int:type_id>', methods=['PUT'])
@jwt_required()
def update_demand_type(type_id):
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    dt = ws_filter(DemandType, user_id, workspace_id, {'id': type_id}).first_or_404()
    data = request.get_json()
    if 'name' in data and data['name'].strip():
        dt.name = data['name'].strip()
    if 'emoji' in data:  dt.emoji = data['emoji'] or '📌'
    if 'color' in data:  dt.color = data['color'] or '#9aa0a7'
    if 'order' in data:  dt.order = int(data['order'])
    db.session.commit()
    return jsonify(dt.to_dict()), 200


@app.route('/api/demand-types/<int:type_id>', methods=['DELETE'])
@jwt_required()
def delete_demand_type(type_id):
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    dt = ws_filter(DemandType, user_id, workspace_id, {'id': type_id}).first_or_404()
    # Desvincula demandas que usavam este tipo
    Demand.query.filter_by(type_id=type_id).update({'type_id': None})
    db.session.delete(dt)
    db.session.commit()
    return jsonify({'message': 'Tipo removido'}), 200


@app.route('/api/agent/notes', methods=['GET'])
def agent_notes():
    """Retorna notas do usuário para o Whiteboard (autenticado via token de agente)."""
    token_str = request.args.get('token') or (request.headers.get('Authorization','').replace('Bearer ','').strip())
    user, workspace_id = get_agent_user(token_str)
    if not user:
        return jsonify({'error': 'Token inválido'}), 401
    ids_param = request.args.get('ids', '')
    query = Note.query.filter_by(user_id=user.id)
    if ids_param:
        ids = [int(i) for i in ids_param.split(',') if i.strip().isdigit()]
        if ids:
            query = query.filter(Note.id.in_(ids))
    notes = query.order_by(Note.updated_at.desc()).all()
    return jsonify([n.to_dict() for n in notes]), 200


@app.route('/api/agent/demands/<int:demand_id>', methods=['PATCH'])
def agent_update_demand(demand_id):
    """Atualiza campos de uma demanda via token de agente (Whiteboard)."""
    token_str = request.args.get('token') or (request.headers.get('Authorization','').replace('Bearer ','').strip())
    user, workspace_id = get_agent_user(token_str)
    if not user:
        return jsonify({'error': 'Token inválido'}), 401
    demand = ws_filter(Demand, user.id, workspace_id, {'id': demand_id}).first()
    if not demand:
        return jsonify({'error': 'Demanda não encontrada'}), 404
    data = request.get_json() or {}
    terminal_keys = [s.key for s in ws_filter(StatusConfig, user.id, workspace_id, {'is_completed': True}).all()]
    if 'status' in data:
        new_status = data['status']
        if new_status in terminal_keys and demand.status not in terminal_keys:
            history = DemandHistory(
                user_id=user.id, workspace_id=workspace_id,
                work_group_id=demand.work_group_id, demand_id=demand.id,
                assigned_to_user_id=demand.assigned_to_user_id or demand.user_id,
                priority=demand.priority, checklist=demand.checklist or [],
                type_id=demand.type_id, location=demand.location,
                activity=demand.activity, context=demand.context,
                status=new_status, status_change_date=date.today(),
                notes_snapshot=get_demand_notes_snapshot(demand.id),
                created_date=demand.created_date
            )
            db.session.add(history)
            db.session.delete(demand)
            db.session.commit()
            return jsonify({'message': 'Demanda concluída', 'archived': True}), 200
        demand.status = new_status
    if 'priority' in data: demand.priority = data['priority']
    if 'due_date'  in data: demand.due_date = data['due_date'] or None
    if 'context'   in data: demand.context  = data['context']
    demand.updated_at = datetime.now()
    db.session.commit()
    return jsonify(demand.to_dict()), 200


@app.route('/api/agent/notes/<int:note_id>', methods=['PATCH'])
def agent_update_note(note_id):
    """Atualiza uma nota via token de agente (Whiteboard)."""
    token_str = request.args.get('token') or (request.headers.get('Authorization','').replace('Bearer ','').strip())
    user, workspace_id = get_agent_user(token_str)
    if not user:
        return jsonify({'error': 'Token inválido'}), 401
    note = Note.query.filter_by(id=note_id, user_id=user.id).first()
    if not note:
        return jsonify({'error': 'Nota não encontrada'}), 404
    data = request.get_json() or {}
    if 'subject'     in data: note.subject     = data['subject']
    if 'description' in data: note.description = data['description']
    if 'checklist'   in data: note.checklist   = data['checklist']
    note.updated_at = datetime.now()
    db.session.commit()
    return jsonify(note.to_dict()), 200



# ============= ANOTAÇÕES DE DEMANDA =============
@app.route('/api/demands/export-notes', methods=['GET'])
@jwt_required()
def export_demand_notes():
    """Retorna todas as notas de um conjunto de demandas (para exportação HTML)."""
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({}), 200
    ids = [int(i) for i in ids_param.split(',') if i.strip().isdigit()]
    if not ids:
        return jsonify({}), 200
    try:
        db.session.execute(text("CREATE TABLE IF NOT EXISTS demand_notes (id SERIAL PRIMARY KEY, demand_id INTEGER NOT NULL REFERENCES demands(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, username VARCHAR(80), content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    notes = DemandNote.query.filter(DemandNote.demand_id.in_(ids)).order_by(DemandNote.demand_id, DemandNote.created_at.asc()).all()
    # Agrupar por demand_id
    result = {}
    for n in notes:
        key = str(n.demand_id)
        if key not in result:
            result[key] = []
        result[key].append({'username': n.username, 'content': n.content,
                             'createdAt': n.created_at.isoformat() if n.created_at else None})
    return jsonify(result), 200


@jwt_required()
def get_demand_notes(demand_id):
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    demand = ws_filter(Demand, user_id, workspace_id, {'id': demand_id}).first_or_404()
    # Garante que a tabela demand_notes existe
    try:
        db.session.execute(text("CREATE TABLE IF NOT EXISTS demand_notes (id SERIAL PRIMARY KEY, demand_id INTEGER NOT NULL REFERENCES demands(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, username VARCHAR(80), content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    notes = DemandNote.query.filter_by(demand_id=demand_id).order_by(DemandNote.created_at.asc()).all()
    return jsonify([n.to_dict() for n in notes]), 200


@app.route('/api/demands/<int:demand_id>/notes', methods=['GET'])
@jwt_required()
def get_demand_notes(demand_id):
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    ws_filter(Demand, user_id, workspace_id, {'id': demand_id}).first_or_404()
    try:
        db.session.execute(text("CREATE TABLE IF NOT EXISTS demand_notes (id SERIAL PRIMARY KEY, demand_id INTEGER NOT NULL REFERENCES demands(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, username VARCHAR(80), content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    notes = DemandNote.query.filter_by(demand_id=demand_id).order_by(DemandNote.created_at.asc()).all()
    return jsonify([n.to_dict() for n in notes]), 200


@app.route('/api/demands/<int:demand_id>/notes', methods=['POST'])
@jwt_required()
def add_demand_note(demand_id):
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    demand = ws_filter(Demand, user_id, workspace_id, {'id': demand_id}).first_or_404()
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'Conteúdo obrigatório'}), 400
    # Garante que a tabela demand_notes existe antes de inserir
    try:
        db.session.execute(text("CREATE TABLE IF NOT EXISTS demand_notes (id SERIAL PRIMARY KEY, demand_id INTEGER NOT NULL REFERENCES demands(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, username VARCHAR(80), content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    user = User.query.get(user_id)
    note = DemandNote(
        demand_id=demand_id,
        user_id=user_id,
        username=user.full_name or user.username if user else str(user_id),
        content=content
    )
    db.session.add(note)
    db.session.commit()
    return jsonify(note.to_dict()), 201


@app.route('/api/demands/<int:demand_id>/notes/<int:note_id>', methods=['DELETE'])
@jwt_required()
def delete_demand_note(demand_id, note_id):
    user_id = int(get_jwt_identity())
    workspace_id = get_user_workspace_id(user_id)
    ws_filter(Demand, user_id, workspace_id, {'id': demand_id}).first_or_404()
    note = DemandNote.query.filter_by(id=note_id, demand_id=demand_id).first_or_404()
    # Só o autor ou admin pode deletar
    user = User.query.get(user_id)
    if note.user_id != user_id and not (user and user.is_admin):
        return jsonify({'error': 'Sem permissão'}), 403
    db.session.delete(note)
    db.session.commit()
    return jsonify({'message': 'Nota removida'}), 200

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
