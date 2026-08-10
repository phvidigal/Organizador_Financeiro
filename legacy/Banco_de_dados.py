import sqlite3
import os

#Nome do banco de dados
DB_NAME = "finanças.db"

#Conectar com o banco de dados
def conectar():
   return sqlite3.connect(DB_NAME)

#Criar as tabelas do app
def inicializar_banco():
    conn = conectar()
    cursor = conn.cursor()

    print("Banco Inicializado")

    #Tabela 1: Transações reais
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            banco TEXT,
            data TEXT,
            descricao_bruta TEXT,
            descricao_limpa TEXT,
            categoria TEXT,
            valor REAL,
            tipo TEXT,
            status TEXT
        )
        ''')
    
    print("Tabela 'transações' verificada!")
    
    #Tabela 2: Planejamento e esboço
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS planejamento_orcamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mes_referencia TEXT,
            descricao TEXT,
            valor_planejado REAL,
            tipo_registro TEXT
        )
    ''')

    print("Tabela 'planejamento' verificada!")

    #Salvando e fechando o banco de dados
    conn.commit()
    conn.close()

    print("Banco de dados pronto para uso! Arquivo salvo como:", os.path.abspath(DB_NAME))


# Se você rodar este arquivo diretamente, ele vai executar a função abaixo:
if __name__ == "__main__":
    inicializar_banco()