import os
import mercadopago
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from supabase import create_client, Client

# 1. Carrega as variáveis
load_dotenv()

# Configuração Mercado Pago
TOKEN_MP = os.getenv("MP_ACCESS_TOKEN")
if not TOKEN_MP:
    raise ValueError("ERRO: Token MP não encontrado!")
sdk = mercadopago.SDK(TOKEN_MP)

# Configuração Supabase
URL_SUPABASE = os.getenv("SUPABASE_URL")
CHAVE_SUPABASE = os.getenv("SUPABASE_KEY")
if not URL_SUPABASE or not CHAVE_SUPABASE:
    raise ValueError("ERRO: Credenciais do Supabase não encontradas!")
supabase: Client = create_client(URL_SUPABASE, CHAVE_SUPABASE)

app = FastAPI()

# ==========================================================
# ROTA 1: O Mercado Pago avisa aqui quando o PIX cai (Webhook)
# ==========================================================
@app.post("/webhook")
async def receber_webhook(request: Request):
    dados = await request.json()
    
    # Verifica se a notificação é de pagamento
    if dados.get("type") == "payment" or dados.get("topic") == "payment":
        id_pagamento = dados.get("data", {}).get("id")
        
        if id_pagamento:
            # Segurança: Confirma com o MP se o pagamento é real e foi aprovado
            resposta = sdk.payment().get(id_pagamento)
            pagamento = resposta.get("response", {})
            
            if pagamento.get("status") == "approved":
                valor = pagamento.get("transaction_amount")
                
                # Salva no Banco de Dados!
                try:
                    supabase.table("pagamentos").insert({
                        "id_pix": int(id_pagamento),
                        "valor": float(valor),
                        "status": "approved",
                        "processado": False # Avisa que a bomba ainda não ligou
                    }).execute()
                    print(f"✅ PIX Salvo! ID: {id_pagamento} - R$ {valor}")
                except Exception as e:
                    # Se der erro, é porque o id_pix já existe (evita duplicidade)
                    pass
                    
    # Responde 200 OK para o Mercado Pago parar de enviar a notificação
    return {"status": "ok"}


# ==========================================================
# ROTA 2: O ESP32 consulta aqui a cada 3 segundos
# ==========================================================
@app.get("/verificar_pagamento") # Use o mesmo nome que você colocou no Arduino
def verificar_pagamento():
    # Busca 1 pagamento que esteja aprovado e que a bomba ainda não processou
    resposta = supabase.table("pagamentos").select("*").eq("processado", False).limit(1).execute()
    
    # Se achou algum PIX novo...
    if len(resposta.data) > 0:
        pagamento = resposta.data[0]
        
        # 1. Muda no banco para "processado = True" para não ligar a bomba duas vezes
        supabase.table("pagamentos").update({"processado": True}).eq("id_pix", pagamento["id_pix"]).execute()
        
        # 2. Responde para o ESP32 ligar a bomba!
        return {"status": "aprovado", "id_pix": pagamento["id_pix"], "valor": pagamento["valor"]}
        
    # Se não achou nada, manda o ESP32 esperar
    return {"status": "pendente"}