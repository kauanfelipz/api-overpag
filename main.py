import os
import time
import requests
import mercadopago
from fastapi import FastAPI, Request
from supabase import create_client, Client
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =====================================================================
# MODELOS DE DADOS
# =====================================================================
class MoedaPayload(BaseModel):
    valor: float

# =====================================================================
# ROTA 1: WEBHOOK INTELIGENTE (AGORA COM RASTREADORES)
# =====================================================================
@app.post("/webhook/{id_conta_principal}")
async def receber_webhook(id_conta_principal: str, request: Request):
    
    print(f"🚪 1. Webhook acionado! URL terminada em: {id_conta_principal}")
    
    id_pagamento = request.query_params.get("id")
    if not id_pagamento:
        try:
            dados = await request.json()
            id_pagamento = dados.get("data", {}).get("id") or dados.get("id")
        except:
            pass

    print(f"📦 2. ID do PIX extraído: {id_pagamento}")

    if id_pagamento:
        # ATENÇÃO: Se a sua URL no MP for /global, mude 'id_posto' para 'id_cliente' se você tiver essa coluna
        # Se você ainda não tem a coluna id_cliente, a gente procura direto o token global da primeira máquina que achar!
        resposta_cliente = supabase.table("postos").select("access_token").limit(1).execute()
        
        if not resposta_cliente.data:
            print(f"❌ 3. ERRO: Tabela 'postos' está vazia ou não consegui ler!")
            return {"status": "erro", "mensagem": "Banco vazio"}
            
        token_do_cliente = resposta_cliente.data[0]["access_token"]
        print(f"🔑 3. Token encontrado no banco de dados!")
        
        sdk_cliente = mercadopago.SDK(token_do_cliente)
        resposta = sdk_cliente.payment().get(id_pagamento)
        pagamento = resposta.get("response", {})

        status_pix = pagamento.get("status")
        print(f"🔍 4. Status do PIX no Mercado Pago: {status_pix}")

        if status_pix == "approved":
            valor = pagamento.get("transaction_amount")
            
            id_bruto = pagamento.get("external_reference")
            
            # Se a external_reference estiver vazia (não veio do adesivo), abortamos pra não dar erro
            if not id_bruto:
                print("⚠️ 5. PIX aprovado, mas não veio de um Adesivo/QR Code nosso. Ignorando.")
                return {"status": "ok"}
                
            id_maquina_real = id_bruto.split("_")[0] 
            print(f"🎯 5. PIX destinado para a máquina: {id_maquina_real}")
            
            try:
                supabase.table("pagamentos").insert({
                    "id_pix": int(id_pagamento),
                    "valor": float(valor),
                    "status": "approved",
                    "processado": False,
                    "id_maquina": id_maquina_real
                }).execute()
                print(f"✅ 6. SUCESSO ABSOLUTO: PIX salvo no banco!")
                
                # ==========================================================
                # O REARME AUTOMÁTICO TURBINADO
                # ==========================================================
                resp_user = requests.get("https://api.mercadopago.com/users/me", headers={"Authorization": f"Bearer {token_do_cliente}"})
                user_id = resp_user.json().get("id")
                
                if user_id:
                    url_rearme = f"https://api.mercadopago.com/instore/orders/qr/seller/collectors/{user_id}/pos/{id_maquina_real}/qrs"
                    id_unico_pedido = f"{id_maquina_real}_{int(time.time())}"
                    
                    pedido_rearme = {
                        "external_reference": id_unico_pedido, 
                        "title": "Calibrador Automotivo",
                        "description": "Ficha de 2 Reais para o Calibrador",
                        "expiration_date": "2035-12-31T23:59:59.000-03:00",
                        "total_amount": 2.00, # <-- Lembre de ajustar para o valor real depois dos testes
                        "items": [
                            {
                                "title": "Tempo de Calibrador",
                                "unit_price": 2.00,
                                "quantity": 1,
                                "unit_measure": "unit",
                                "total_amount": 2.00
                            }
                        ]
                    }
                    resposta_rearme = requests.put(url_rearme, json=pedido_rearme, headers={"Authorization": f"Bearer {token_do_cliente}"})
                    print(f"🔄 7. Rearme concluído com código: {resposta_rearme.status_code}")
                # ==========================================================
                
            except Exception as e:
                print(f"❌ ERRO CRÍTICO no banco de dados: {e}")

    return {"status": "ok"}

# =====================================================================
# ROTA 2: ESP32 PERGUNTA AO SERVIDOR (PIX)
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

# =====================================================================
# ROTA 3: REGISTRAR MOEDAS FÍSICAS (Vindo da ESP32)
# =====================================================================
@app.post("/registrar_moeda/{id_maquina}")
async def registrar_moeda(id_maquina: str, payload: MoedaPayload):
    try:
        # Monta o registro da moeda para o banco de dados
        # Note que não enviamos "id_pix" porque moedas não geram ID do Mercado Pago
        dados_moeda = {
            "id_maquina": id_maquina,
            "valor": payload.valor,
            "tipo_pagamento": "moeda",
            "status": "approved",
            "processado": True # Já entra como processado pra não liberar a bomba via PIX acidentalmente
        }
        
        # Envia para a tabela 'pagamentos' no Supabase
        supabase.table("pagamentos").insert(dados_moeda).execute()
        
        print(f"🪙 SUCESSO: Moeda de R$ {payload.valor} salva na máquina {id_maquina}!")
        return {"status": "sucesso", "mensagem": "Moeda gravada com sucesso"}

    except Exception as e:
        print(f"❌ Erro ao salvar moeda: {e}")
        return {"status": "erro", "mensagem": str(e)}