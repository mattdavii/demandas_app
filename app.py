import os
import secrets
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from functools import wraps

load_dotenv()

app = Flask(__name__)
CORS(app)

# ============= CONFIGURAÇÕES =============
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///demandas.db')

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
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
            'createdAt': self.created_at.isoformat() if self.created_at else None
        }

class WorkGroup(db.Model):
    __tablename__ = 'work_groups'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)  # Ex: BACKOFFICE, ATENDIMENTOS
    emoji = db.Column(db.String(10), nullable=True)  # Ex: 👨🏻‍💻
    color = db.Column(db.String(7), default='#3b82f6')  # Cor em hex
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
    priority = db.Column(db.String(20), default='media')  # baixa, media, alta, urgente
    due_date = db.Column(db.Date, nullable=True)
    assigned_to = db.Column(db.String(100))  # Nome de quem será responsável
    created_date = db.Column(db.Date, default=date.today)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
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
            'createdDate': self.created_date.isoformat() if self.created_date else None
        }

class DemandHistory(db.Model):
    __tablename__ = 'demand_history'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    work_group_id = db.Column(db.Integer, db.ForeignKey('work_groups.id'), nullable=True)
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
            'workGroupId': self.work_group_id,
            'location': self.location,
            'activity': self.activity,
            'context': self.context,
            'status': self.status,
            'statusChangeDate': self.status_change_date.isoformat() if self.status_change_date else None,
            'createdDate': self.created_date.isoformat() if self.created_date else None
        }

# ============= ROTAS DE PÁGINA =============
@app.route('/')
def index():
    """Servir página principal"""
    return render_template('index.html')

@app.route('/reset')
def reset_page():
    """Servir página de reset de senha"""
    return render_template('reset.html')

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
        full_name=data.get('full_name', '')
    )
    user.set_password(data['password'])
    
    db.session.add(user)
    db.session.commit()
    
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
    
    access_token = create_access_token(identity=user.id)
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
    
    access_token = create_access_token(identity=user.id)
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
        # Por segurança, não confirmamos se o email existe
        return jsonify({'message': 'Se o email existe, você receberá um link de reset'}), 200
    
    reset_token = user.generate_reset_token()
    reset_url = f"{os.getenv('FRONTEND_URL', 'http://localhost:5000')}/reset/{reset_token}"
    
    # Enviar email (se configurado)
    if app.config['MAIL_USERNAME']:
        try:
            msg = Message(
                'Reset de Senha - Demandas App',
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
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404
    
    return jsonify(user.to_dict()), 200

# ============= ROTAS DE GRUPOS DE TRABALHO =============
@app.route('/api/work-groups', methods=['GET'])
@jwt_required()
def get_work_groups():
    """Listar grupos de trabalho do usuário"""
    user_id = get_jwt_identity()
    groups = WorkGroup.query.filter_by(user_id=user_id, is_active=True).order_by(WorkGroup.order).all()
    return jsonify([g.to_dict() for g in groups]), 200

@app.route('/api/work-groups', methods=['POST'])
@jwt_required()
def create_work_group():
    """Criar novo grupo de trabalho"""
    user_id = get_jwt_identity()
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
    user_id = get_jwt_identity()
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
    user_id = get_jwt_identity()
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
    user_id = get_jwt_identity()
    demands = Demand.query.filter(
        Demand.user_id == user_id,
        Demand.status != 'concluido'
    ).all()
    return jsonify([d.to_dict() for d in demands]), 200

@app.route('/api/demands', methods=['POST'])
@jwt_required()
def create_demand():
    """Criar nova demanda"""
    user_id = get_jwt_identity()
    data = request.get_json()
    
    if not data or not data.get('work_group_id') or not data.get('location') or not data.get('activity'):
        return jsonify({'error': 'Dados incompletos'}), 400
    
    # Verificar se o grupo pertence ao usuário
    group = WorkGroup.query.get(data['work_group_id'])
    if not group or group.user_id != user_id:
        return jsonify({'error': 'Grupo inválido'}), 403
    
    demand = Demand(
        user_id=user_id,
        work_group_id=data['work_group_id'],
        location=data['location'],
        activity=data['activity'],
        context=data.get('context', ''),
        status=data.get('status', 'nao-iniciado'),
        priority=data.get('priority', 'media'),
        due_date=datetime.strptime(data['due_date'], '%Y-%m-%d').date() if data.get('due_date') else None,
        assigned_to=data.get('assigned_to', '')
    )
    
    db.session.add(demand)
    db.session.commit()
    
    return jsonify(demand.to_dict()), 201

@app.route('/api/demands/<int:demand_id>', methods=['PUT'])
@jwt_required()
def update_demand(demand_id):
    """Atualizar demanda"""
    user_id = get_jwt_identity()
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
    
    db.session.commit()
    return jsonify(demand.to_dict()), 200

@app.route('/api/demands/<int:demand_id>', methods=['DELETE'])
@jwt_required()
def delete_demand(demand_id):
    """Deletar demanda"""
    user_id = get_jwt_identity()
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
    user_id = get_jwt_identity()
    demand = Demand.query.get_or_404(demand_id)
    
    if demand.user_id != user_id:
        return jsonify({'error': 'Acesso negado'}), 403
    
    data = request.get_json()
    new_status = data.get('status')
    
    if not new_status:
        return jsonify({'error': 'Status não fornecido'}), 400
    
    # Adicionar ao histórico
    history = DemandHistory(
        user_id=user_id,
        work_group_id=demand.work_group_id,
        location=demand.location,
        activity=demand.activity,
        context=demand.context,
        status=new_status,
        status_change_date=date.today(),
        created_date=demand.created_date
    )
    db.session.add(history)
    
    # Se mudou para concluído, remove de demandas
    if new_status == 'concluido':
        db.session.delete(demand)
    else:
        demand.status = new_status
    
    db.session.commit()
    return jsonify({'message': 'Status atualizado'}), 200

# ============= ROTAS DE HISTÓRICO =============
@app.route('/api/history', methods=['GET'])
@jwt_required()
def get_history():
    """Listar histórico de demandas"""
    user_id = get_jwt_identity()
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
    user_id = get_jwt_identity()
    today = date.today().strftime('%d/%m/%Y')
    output = f"_*{today}*_\n"
    
    # Agrupar demandas por grupo de trabalho
    demands = Demand.query.filter(Demand.user_id == user_id, Demand.status != 'concluido').all()
    history = DemandHistory.query.filter(
        DemandHistory.user_id == user_id,
        DemandHistory.status_change_date == date.today(),
        DemandHistory.status == 'concluido'
    ).all()
    
    groups = WorkGroup.query.filter_by(user_id=user_id, is_active=True).order_by(WorkGroup.order).all()
    
    STATUS_EMOJI = {
        'concluido': '🟢',
        'andamento': '🟡',
        'nao-iniciado': '⚪',
        'aguardando': '🔵',
        'aprovacao': '🟣'
    }
    
    for group in groups:
        group_demands = [d for d in demands if d.work_group_id == group.id]
        group_history = [h for h in history if h.work_group_id == group.id]
        
        if group_demands or group_history:
            output += f"_*{group.emoji} {group.name}:*_\n"
            
            for d in group_demands:
                emoji = STATUS_EMOJI.get(d.status, '⚪')
                output += f"> *{d.location}:* {d.activity}"
                if d.context:
                    output += f" _({d.context})_"
                output += f"; _*{d.status.upper()} {emoji}*_\n"
            
            for h in group_history:
                output += f"> *{h.location}:* {h.activity}"
                if h.context:
                    output += f" _({h.context})_"
                output += f"; _*CONCLUÍDO 🟢*_\n"
            
            output += "\n"
    
    return jsonify({'text': output}), 200

# ============= ROTAS DE BACKUP =============
@app.route('/api/export', methods=['GET'])
@jwt_required()
def export_data():
    """Exportar todos os dados do usuário"""
    user_id = get_jwt_identity()
    
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
    user_id = get_jwt_identity()
    data = request.get_json()
    
    try:
        # Importar grupos de trabalho
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
        
        # Importar demandas
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
        
        # Importar histórico
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
    with app.app_context():
        db.create_all()
    
    port = int(os.getenv('PORT', 5000))
    app.run(debug=os.getenv('FLASK_ENV') == 'development', host='0.0.0.0', port=port)
