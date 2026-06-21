#!/usr/bin/env python
"""
Script de migração para importar dados do localStorage JSON para o banco de dados PostgreSQL.
Use este script para transferir seus dados do widget anterior para o novo app.

Exemplo de uso:
  python migrate.py dados_export.json
"""

import json
import sys
import os
from datetime import datetime, date
from app import app, db, Demand, DemandHistory

def migrate_data(json_file):
    """
    Importa dados de um arquivo JSON (exportado do localStorage)
    para o banco de dados PostgreSQL.
    
    Formato esperado do JSON:
    {
        "items": [
            {
                "id": 123,
                "section": "BACKOFFICE" ou "ATENDIMENTOS",
                "location": "Local",
                "activity": "Atividade",
                "context": "Contexto opcional",
                "status": "concluido",
                "createdDate": "2026-06-18"
            }
        ],
        "history": [
            {
                "section": "BACKOFFICE",
                "location": "Local",
                "activity": "Atividade",
                "context": "Contexto",
                "status": "concluido",
                "statusChangeDate": "2026-06-18"
            }
        ]
    }
    """
    
    if not json_file:
        print("❌ Nenhum arquivo especificado!")
        print("\nUso: python migrate.py <arquivo.json>")
        print("\nExemplo:")
        print("  python migrate.py dados_backup.json")
        return False
    
    if not os.path.exists(json_file):
        print(f"❌ Arquivo não encontrado: {json_file}")
        return False
    
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Erro ao ler JSON: {e}")
        return False
    
    with app.app_context():
        try:
            # Backup - não limpa dados existentes, apenas adiciona
            items_count = 0
            history_count = 0
            
            print("\n📥 Iniciando migração de dados...")
            print(f"   Total de items: {len(data.get('items', []))}")
            print(f"   Total de históricos: {len(data.get('history', []))}")
            
            # Importa demands (items pendentes)
            print("\n⏳ Importando items pendentes...")
            for item in data.get('items', []):
                try:
                    # Normaliza seção
                    section = item.get('section', 'ATENDIMENTOS')
                    if isinstance(section, str) and 'BACKOFFICE' in section.upper():
                        section = 'BACKOFFICE'
                    else:
                        section = 'ATENDIMENTOS'
                    
                    # Parse de data
                    created_date = item.get('createdDate', str(date.today()))
                    if isinstance(created_date, str):
                        try:
                            created_date = datetime.strptime(created_date, '%Y-%m-%d').date()
                        except:
                            created_date = date.today()
                    
                    demand = Demand(
                        section=section,
                        location=item.get('location', 'N/A'),
                        activity=item.get('activity', 'N/A'),
                        context=item.get('context', ''),
                        status=item.get('status', 'nao-iniciado'),
                        created_date=created_date
                    )
                    db.session.add(demand)
                    items_count += 1
                except Exception as e:
                    print(f"   ⚠️  Erro ao importar item: {e}")
                    continue
            
            db.session.commit()
            print(f"   ✅ {items_count} items importados com sucesso!")
            
            # Importa histórico
            print("\n⏳ Importando histórico...")
            for hist in data.get('history', []):
                try:
                    # Normaliza seção
                    section = hist.get('section', 'ATENDIMENTOS')
                    if isinstance(section, str) and 'BACKOFFICE' in section.upper():
                        section = 'BACKOFFICE'
                    else:
                        section = 'ATENDIMENTOS'
                    
                    # Parse de datas
                    status_change_date = hist.get('statusChangeDate', str(date.today()))
                    if isinstance(status_change_date, str):
                        try:
                            status_change_date = datetime.strptime(status_change_date, '%Y-%m-%d').date()
                        except:
                            status_change_date = date.today()
                    
                    created_date = hist.get('createdDate')
                    if isinstance(created_date, str):
                        try:
                            created_date = datetime.strptime(created_date, '%Y-%m-%d').date()
                        except:
                            created_date = None
                    
                    history = DemandHistory(
                        section=section,
                        location=hist.get('location', 'N/A'),
                        activity=hist.get('activity', 'N/A'),
                        context=hist.get('context', ''),
                        status=hist.get('status', 'nao-iniciado'),
                        status_change_date=status_change_date,
                        created_date=created_date
                    )
                    db.session.add(history)
                    history_count += 1
                except Exception as e:
                    print(f"   ⚠️  Erro ao importar histórico: {e}")
                    continue
            
            db.session.commit()
            print(f"   ✅ {history_count} históricos importados com sucesso!")
            
            print(f"\n✅ Migração concluída!")
            print(f"   Total: {items_count} items + {history_count} históricos")
            return True
            
        except Exception as e:
            db.session.rollback()
            print(f"\n❌ Erro durante migração: {e}")
            return False

def main():
    if len(sys.argv) < 2:
        print("=== Script de Migração de Dados ===\n")
        print("Este script importa dados do localStorage JSON para o banco de dados.\n")
        print("Uso: python migrate.py <arquivo.json>\n")
        print("Exemplos:")
        print("  python migrate.py dados_backup.json")
        print("  python migrate.py /caminho/para/demandas_2026-06-18.json")
        print("\nO arquivo JSON deve conter a estrutura:")
        print("{")
        print('  "items": [{...}],')
        print('  "history": [{...}]')
        print("}\n")
        sys.exit(0)
    
    json_file = sys.argv[1]
    success = migrate_data(json_file)
    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
