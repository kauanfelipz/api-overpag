import mercadopago
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI

# 1. Carrega as variáveis
load_dotenv()
TOKEN_MP = os.getenv("MP_ACCESS_TOKEN")

if not TOKEN_MP:
    raise ValueError("ERRO: Token não encontrado no .env!")

app = FastAPI()
sdk = mercadopago.SDK(TOKEN_MP)

ARQUIVO_IDS = "ids_processados.txt"

def carregar_ids_usados():
    if not os.path.exists(ARQUIVO_IDS):
        return []
    with open(ARQUIVO_IDS, "r") as f:
        return [line.strip() for line in f.readlines()]

def salvar_id_novo(novo_id):
    with open(ARQUIVO_IDS, "a") as f:
        f.write(f"{novo_id}\n")

def buscar_pix_recente():
    print("\n--- Iniciando busca ---")
    ids_ja_usados = carregar_ids_usados()
    
    # Busca os últimos 10 pagamentos (IGUAL AO DEBUG)
    filters = {
        'sort': 'date_created',
        'criteria': 'desc',
        'limit': 10  # Pega os 10 últimos, independente da data
    }
    
    try:
        resultado = sdk.payment().search(filters)
        pagamentos = resultado.get("response", {}).get("results", [])
        
        if not pagamentos:
            print("📭 Nenhum pagamento recente encontrado na conta.")
            return False, None

        # Fuso horário UTC para comparação
        agora = datetime.now(timezone.utc)

        for pag in pagamentos:
            pid = str(pag.get("id"))
            status = pag.get("status")
            valor = float(pag.get("transaction_amount", 0))
            data_criacao_str = pag.get("date_created")
            
            # Converte string do MP para objeto data
            # O replace resolve o 'Z' que às vezes vem no final
            data_pag = datetime.fromisoformat(data_criacao_str.replace("Z", "+00:00"))
            
            print(f"🔎 Analisando ID {pid} | Status: {status} | Valor: {valor} | Data: {data_criacao_str}")

            # 1. Verifica se já foi usado
            if pid in ids_ja_usados:
                print(f"   -> ❌ Já processado antes.")
                continue

            # 2. Verifica se é aprovado
            if status != 'approved':
                print(f"   -> ❌ Status não é approved.")
                continue

            # 3. Verifica o valor (Aceita 2.0 ou 2)
            if valor != 2.0:
                print(f"   -> ❌ Valor incorreto (esperado 2.0).")
                continue

            # 4. Verifica se é recente (últimos 60 minutos)
            # Se a data do pagamento for maior que (agora - 60min)
            if data_pag > (agora - timedelta(minutes=60)):
                print(f"   -> ✅ SUCCESSO! Pagamento válido encontrado.")
                return True, pid
            else:
                print(f"   -> ❌ Muito antigo (mais de 60 min).")

    except Exception as e:
        print(f"❌ Erro ao consultar Mercado Pago: {e}")
        
    return False, None

@app.get("/verificar_pagamento")
def rota_esp32():
    tem_novo, id_pix = buscar_pix_recente()
    
    if tem_novo:
        print(f"🚀 LIBERANDO BOMBA! ID: {id_pix}")
        salvar_id_novo(id_pix)
        return {"ligar": True}
    
    return {"ligar": False}