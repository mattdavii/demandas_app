#!/usr/bin/env python
"""
Script de Setup - Cria tabelas e usuário de teste

Uso: python setup.py
"""

import os
from app import app, db, User, WorkGroup

def setup_database():
    """Criar todas as tabelas"""
    with app.app_context():
        print("\n📦 Criando tabelas do banco de dados...")
        db.create_all()
        print("✅ Tabelas criadas com sucesso!")

def create_test_user():
    """Criar usuário de teste"""
    with app.app_context():
        # Verificar se usuário já existe
        if User.query.filter_by(username='teste').first():
            print("ℹ️  Usuário 'teste' já existe")
            return
        
        print("\n👤 Criando usuário de teste...")
        
        user = User(
            username='teste',
            email='teste@example.com',
            full_name='Usuário Teste'
        )
        user.set_password('senha123')
        
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
        
        print("✅ Usuário de teste criado!")
        print(f"   Usuário: teste")
        print(f"   Senha: senha123")
        print(f"   Email: teste@example.com")

def main():
    print("\n" + "="*50)
    print("🚀 SETUP - Gestor de Demandas")
    print("="*50)
    
    try:
        setup_database()
        create_test_user()
        
        print("\n" + "="*50)
        print("✅ Setup concluído com sucesso!")
        print("="*50)
        print("\n📝 Próximas etapas:")
        print("1. Rode: python app.py")
        print("2. Acesse: http://localhost:5000")
        print("3. Faça login com: teste / senha123")
        print("\n")
        
    except Exception as e:
        print(f"\n❌ Erro durante setup: {e}")
        print("\nDicas:")
        print("- Verifique se DATABASE_URL está configurada no .env")
        print("- Verifique se o PostgreSQL/SQLite está rodando")
        print("- Tente novamente")

if __name__ == '__main__':
    main()
