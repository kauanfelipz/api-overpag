import os
import mercadopago
from fastapi import FastAPI, Request
from supabase import create_client, Client
from dotenv import load_dotenv

# Carrega variáveis de ambiente (do Render)
load_dotenv()

app = FastAPI()

# Conexão com o Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =====================================================================
# ROTA 1: MERCADO PAGO AVISA O SERVIDOR (WEBHOOK MULTI-CLIENTE)
# =====================================================================
@app.post("/webhook/{id_posto}")
async def receber_webhook(id_posto: str, request: Request):
    
    # 1. Pegar o ID do pagamento
    id_pagamento = request.query_params.get("id")
    if not id_pagamento:
        try:
            dados = await request.json()
            id_pagamento = dados.get("data", {}).get("id") or dados.get("id")
        except:
            pass

    if id_pagamento:
        # 2. PROCURAR O CLIENTE NO SUPABASE (Busca o Token do dono do posto)
        resposta_cliente = supabase.table("postos").select("access_token").eq("id_posto", id_posto).execute()
        
        if not resposta_cliente.data:
            print(f"ERRO: Posto '{id_posto}' não encontrado no banco de dados.")
            return {"status": "erro", "mensagem": "Posto desconhecido"}
            
        # Pega o token específico deste cliente
        token_do_cliente = resposta_cliente.data[0]["access_token"]
        
        # 3. INICIA O MERCADO PAGO COM O TOKEN DESTE CLIENTE
        sdk_cliente = mercadopago.SDK(token_do_cliente)
        resposta = sdk_cliente.payment().get(id_pagamento)
        pagamento = resposta.get("response", {})

        # 4. SALVA O PAGAMENTO ASSOCIADO A ESTE POSTO
        if pagamento.get("status") == "approved":
            valor = pagamento.get("transaction_amount")
            
            try:
                supabase.table("pagamentos").insert({
                    "id_pix": int(id_pagamento),
                    "valor": float(valor),
                    "status": "approved",
                    "processado": False,
                    "id_maquina": id_posto  # <-- Registra de quem é o dinheiro
                }).execute()
                print(f"✅ SUCESSO: PIX de R${valor} para a máquina '{id_posto}' gravado!")
            except Exception as e:
                print(f"Aviso: Erro ao gravar no banco: {e}")

    return {"status": "ok"}


# =====================================================================
# ROTA 2: ESP32 PERGUNTA AO SERVIDOR SE TEM PIX NOVO
# =====================================================================
@app.get("/verificar_pagamento/{id_maquina}")
def verificar_pagamento(id_maquina: str):
    try:
        # 1. Procura PIX não processado APENAS para esta máquina
        resposta = supabase.table("pagamentos") \
            .select("*") \
            .eq("processado", False) \
            .eq("id_maquina", id_maquina) \
            .limit(1) \
            .execute()
        
        # 2. Se encontrou PIX novo para ela
        if resposta.data and len(resposta.data) > 0:
            pagamento = resposta.data[0]
            
            # 3. Marca como "processado = True" para não atracar o relé duas vezes
            supabase.table("pagamentos") \
                .update({"processado": True}) \
                .eq("id_pix", pagamento["id_pix"]) \
                .execute()
            
            print(f"🚀 BOMBA LIBERADA! Máquina: {id_maquina} | Valor: R$ {pagamento['valor']}")
            
            # 4. Devolve o "aprovado" para o ESP32
            return {
                "status": "aprovado", 
                "id_pix": pagamento["id_pix"], 
                "valor": pagamento["valor"]
            }
            
        # 5. Se não tem nada novo, manda aguardar
        return {"status": "pendente"}
        
    except Exception as e:
        print(f"Erro ao verificar máquina {id_maquina}: {e}")
        return {"status": "erro", "mensagem": str(e)}