import os
import requests 
import mercadopago
from fastapi import FastAPI, Request
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =====================================================================
# ROTA 1: WEBHOOK INTELIGENTE (SALVA O PIX E REARMA A MÁQUINA)
# =====================================================================
@app.post("/webhook/{id_conta_principal}")
async def receber_webhook(id_conta_principal: str, request: Request):
    
    id_pagamento = request.query_params.get("id")
    if not id_pagamento:
        try:
            dados = await request.json()
            id_pagamento = dados.get("data", {}).get("id") or dados.get("id")
        except:
            pass

    if id_pagamento:
        # 1. Busca o Token do cliente
        resposta_cliente = supabase.table("postos").select("access_token").eq("id_posto", id_conta_principal).execute()
        
        if not resposta_cliente.data:
            return {"status": "erro", "mensagem": "Posto desconhecido"}
            
        token_do_cliente = resposta_cliente.data[0]["access_token"]
        
        # 2. Pergunta ao Mercado Pago os detalhes desse pagamento
        sdk_cliente = mercadopago.SDK(token_do_cliente)
        resposta = sdk_cliente.payment().get(id_pagamento)
        pagamento = resposta.get("response", {})

        if pagamento.get("status") == "approved":
            valor = pagamento.get("transaction_amount")
            id_maquina_real = pagamento.get("pos_id") or pagamento.get("external_reference") or id_conta_principal
            
            try:
                # 3. Grava no banco para a ESP32 ler
                supabase.table("pagamentos").insert({
                    "id_pix": int(id_pagamento),
                    "valor": float(valor),
                    "status": "approved",
                    "processado": False,
                    "id_maquina": id_maquina_real
                }).execute()
                print(f"✅ SUCESSO: PIX de R${valor} na máquina '{id_maquina_real}'!")
                
                # ==========================================================
                # 4. O REARME AUTOMÁTICO (A Mágica da Ficha Infinita)
                # ==========================================================
                # Descobre o User ID do dono do posto na hora
                resp_user = requests.get("https://api.mercadopago.com/users/me", headers={"Authorization": f"Bearer {token_do_cliente}"})
                user_id = resp_user.json().get("id")
                
                if user_id:
                    url_rearme = f"https://api.mercadopago.com/instore/orders/qr/seller/collectors/{user_id}/pos/{id_maquina_real}/qrs"
                    
                    pedido_rearme = {
                        "external_reference": id_maquina_real,
                        "title": "Aspirador Automotivo",
                        "description": "Ficha de 2 Reais para o Aspirador",
                        "expiration_date": "2035-12-31T23:59:59.000-03:00",
                        "total_amount": 2.00,
                        "items": [
                            {
                                "title": "Tempo de Aspirador",
                                "unit_price": 2.00,
                                "quantity": 1,
                                "unit_measure": "unit",
                                "total_amount": 2.00
                            }
                        ]
                    }
                    requests.put(url_rearme, json=pedido_rearme, headers={"Authorization": f"Bearer {token_do_cliente}"})
                    print(f"🔄 Máquina '{id_maquina_real}' rearmada para o próximo cliente!")
                # ==========================================================
                
            except Exception as e:
                print(f"Aviso: Erro ao gravar ou rearmar: {e}")

    return {"status": "ok"}


# =====================================================================
# ROTA 2: ESP32 PERGUNTA AO SERVIDOR
# =====================================================================
@app.get("/verificar_pagamento/{id_maquina}")
def verificar_pagamento(id_maquina: str):
    try:
        resposta_posto = supabase.table("postos").select("preco_pix, tempo_segundos, url_ota").eq("id_posto", id_maquina).execute()
            
        if not resposta_posto.data:
            return {"status": "erro", "mensagem": "Máquina não configurada no banco"}
            
        config = resposta_posto.data[0]
        preco_esperado = config.get("preco_pix", 2.0)
        tempo_liberado = config.get("tempo_segundos", 240)
        link_atualizacao = config.get("url_ota")

        resposta_esp = {
            "status": "pendente",
            "preco_pix": preco_esperado,
            "tempo_segundos": tempo_liberado
        }

        if link_atualizacao and len(link_atualizacao) > 10:
            resposta_esp["url_ota"] = link_atualizacao

        resposta = supabase.table("pagamentos").select("*").eq("processado", False).eq("id_maquina", id_maquina).limit(1).execute()
        
        if resposta.data and len(resposta.data) > 0:
            pagamento = resposta.data[0]
            valor_pago = pagamento["valor"]
            
            if valor_pago >= preco_esperado:
                supabase.table("pagamentos").update({"processado": True}).eq("id_pix", pagamento["id_pix"]).execute()
                print(f"🚀 BOMBA LIBERADA! Máquina: {id_maquina} | Pagou: R${valor_pago}")
                resposta_esp["status"] = "aprovado"
                resposta_esp["tempo_liberado"] = tempo_liberado
                return resposta_esp
            else:
                supabase.table("pagamentos").update({"processado": True}).eq("id_pix", pagamento["id_pix"]).execute()
                print(f"⚠️ GOLPE BLOQUEADO! Máquina: {id_maquina} | Pagou R${valor_pago} (Esperado R${preco_esperado})")
                return resposta_esp
            
        return resposta_esp
        
    except Exception as e:
        print(f"Erro ao verificar máquina {id_maquina}: {e}")
        return {"status": "erro", "mensagem": str(e)}