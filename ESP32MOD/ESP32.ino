#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h> 
#include <LiquidCrystal_I2C.h>
#include <Preferences.h> 
#include <ArduinoJson.h>
#include <WiFiManager.h> 
#include <HTTPUpdate.h>
#include <WiFiClientSecure.h>

// ============================================================================
// CONFIGURAÇÕES DE SERVIDOR E MÁQUINA
// ============================================================================
const char* servidorAPI = "https://api-overpag.onrender.com";
char idMaquina[64] = "";  // ID único da máquina (configurável via WiFiManager)

// URLs construídas dinamicamente
String urlVerificarPagamento;

// ============================================================================
// CONFIGURAÇÕES DE PINOS
// ============================================================================
const int PINO_RELAY_BOMBA = 4;      
const int PINO_ILUMINACAO = 19;      
const int PINO_LED_STATUS = 2; 
const int PINO_BUZZER = 5; 
const int PINO_MOEDEIRO = 23;

// Configurações do PWM para LED (ESP32 usa LEDC)
const int LEDC_CHANNEL = 0;
const int LEDC_FREQ = 5000;
const int LEDC_RESOLUTION = 8; // 0-255

// ============================================================================
// CONFIGURAÇÕES DE TEMPO E VALOR (Dinâmicos)
// ============================================================================
float precoPixAtual = 2.00;            
unsigned long tempoBaseSegundos = 240; 

const long TIMEOUT_MOEDA_MS = 5000;    // 5 segundos de espera após a última moeda
const long TEMPO_LUZ_MS = 360000;      // 6 Minutos exatos
long intervaloHTTP = 5000;             // 5 segundos entre requisições (ajustável via API)
const long INTERVALO_TELA = 3000;      // 3 segundos alternando display
const long TIMEOUT_HTTP_MS = 8000;     // Timeout máximo para requisição HTTP

// ============================================================================
// VARIÁVEIS GLOBAIS
// ============================================================================
volatile int contadorMoedas = 0;           
volatile unsigned long ultimoTempoMoeda = 0; 
unsigned long ultimoCheckHTTP = 0;

unsigned long inicioLuz = 0;
unsigned long duracaoLuz = 0;
bool luzLigada = false;

Preferences preferencias;
unsigned int contadorTotal = 0;  
unsigned long tempoUltimaTrocaTela = 0;
bool mostrandoValor = true;

// Controle de estado do último PIX processado (evita uso múltiplo)
int ultimoIdPixProcessado = -1;
unsigned long tempoUltimoPixProcessado = 0;
const long TEMPO_EXPIRACAO_PIX_MS = 300000; // 5 minutos - PIX expira após este tempo

// Flag para indicar quando bomba está ligada (não consulta API durante uso)
bool bombaEmOperacao = false;

LiquidCrystal_I2C lcd(0x27, 16, 2);

// ============================================================================
// FUNÇÕES AUXILIARES
// ============================================================================
void escreverCentralizado(String texto, int linha) {
  int tamanho = texto.length();
  int coluna = (16 - tamanho) / 2; 
  if (coluna < 0) coluna = 0; 
  lcd.setCursor(coluna, linha);
  lcd.print(texto);
}

void IRAM_ATTR contarMoeda() {
  unsigned long tempoAtual = millis();
  if (tempoAtual - ultimoTempoMoeda > 150) { 
    contadorMoedas++;
    ultimoTempoMoeda = tempoAtual;
  }
}

// Constrói URL com o ID da máquina
void construirURLs() {
  urlVerificarPagamento = String(servidorAPI) + "/verificar_pagamento/" + String(idMaquina);
  Serial.println("URL de verificação: " + urlVerificarPagamento);
}

// Salva estado do PIX processado na memória não volátil
void salvarPixProcessado(int idPix) {
  ultimoIdPixProcessado = idPix;
  tempoUltimoPixProcessado = millis();
  preferencias.putInt("ultimo_pix", idPix);
  preferencias.putULong("tempo_ultimo_pix", tempoUltimoPixProcessado);
  Serial.println("PIX " + String(idPix) + " marcado como processado");
}

// Verifica se um PIX já foi usado
bool pixJaProcessado(int idPix) {
  // Verifica se é o mesmo ID e não expirou
  if (idPix == ultimoIdPixProcessado) {
    if (millis() - tempoUltimoPixProcessado < TEMPO_EXPIRACAO_PIX_MS) {
      return true;
    }
  }
  return false;
}

// Carrega configurações salvas
void carregarConfiguracoesSalvas() {
  // Carrega ID da máquina
  String idSalvo = preferencias.getString("id_maquina", "");
  if (idSalvo.length() > 0) {
    strncpy(idMaquina, idSalvo.c_str(), sizeof(idMaquina) - 1);
    idMaquina[sizeof(idMaquina) - 1] = '\0';
    Serial.println("ID da máquina carregado: " + String(idMaquina));
  } else {
    strncpy(idMaquina, "maquina_padrao", sizeof(idMaquina) - 1);
    Serial.println("Usando ID padrão: maquina_padrao");
  }
  
  // Carrega último PIX processado
  ultimoIdPixProcessado = preferencias.getInt("ultimo_pix", -1);
  tempoUltimoPixProcessado = preferencias.getULong("tempo_ultimo_pix", 0);
}

// ============================================================================
// FUNÇÃO DE ATUALIZAÇÃO VIA WIFI (OTA)
// ============================================================================
void realizarAtualizacaoOTA(String urlArquivoBin) {
  Serial.println("Iniciando atualizacao OTA...");
  
  lcd.clear();
  escreverCentralizado("Atualizando...", 0);
  escreverCentralizado("Aguarde...", 1);

  WiFiClientSecure client;
  client.setInsecure(); // Ignora verificação de certificado SSL
  client.setTimeout(30); // 30 segundos de timeout

  t_httpUpdate_return ret = httpUpdate.update(client, urlArquivoBin);

  switch (ret) {
    case HTTP_UPDATE_FAILED:
      Serial.printf("Erro no OTA (%d): %s\n", httpUpdate.getLastError(), httpUpdate.getLastErrorString().c_str());
      lcd.clear();
      escreverCentralizado("Erro ao Baixar", 0);
      escreverCentralizado("Reiniciando...", 1);
      delay(3000);
      lcd.clear();
      break;

    case HTTP_UPDATE_NO_UPDATES:
      Serial.println("Nenhuma atualizacao encontrada.");
      break;

    case HTTP_UPDATE_OK:
      Serial.println("Sucesso! Reiniciando...");
      delay(2000);
      ESP.restart();
      break;
  }
}

// ============================================================================
// FUNÇÃO DE REQUISIÇÃO HTTP COM TRATAMENTO DE ERROS
// ============================================================================
bool consultarServidor() {
  if (bombaEmOperacao) {
    return false; // Não consulta durante operação da bomba
  }

  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(TIMEOUT_HTTP_MS / 1000);

  HTTPClient http;
  
  Serial.println("Consultando: " + urlVerificarPagamento);
  
  if (!http.begin(client, urlVerificarPagamento)) {
    Serial.println("Falha ao iniciar HTTP");
    return false;
  }

  http.setTimeout(TIMEOUT_HTTP_MS);
  
  int httpCode = http.GET();

  if (httpCode == HTTP_CODE_OK) {
    String payload = http.getString();
    Serial.println("Resposta: " + payload);
    
    StaticJsonDocument<1536> doc; // Aumentado para suportar mais campos
    DeserializationError error = deserializeJson(doc, payload);
    
    if (!error) {
      // Atualiza configurações dinâmicas
      if (doc.containsKey("preco_pix")) {
        float novoPreco = doc["preco_pix"].as<float>();
        if (novoPreco > 0) {
          precoPixAtual = novoPreco;
        }
      }
      
      if (doc.containsKey("tempo_segundos")) {
        int novoTempo = doc["tempo_segundos"].as<int>();
        if (novoTempo > 0) {
          tempoBaseSegundos = novoTempo;
        }
      }

      // Intervalo HTTP ajustável via API
      if (doc.containsKey("intervalo_http")) {
        int novoIntervalo = doc["intervalo_http"].as<int>();
        if (novoIntervalo >= 2000 && novoIntervalo <= 60000) {
          intervaloHTTP = novoIntervalo;
        }
      }

      // Gatilho de atualização OTA
      if (doc.containsKey("url_ota")) {
        String urlNova = doc["url_ota"].as<String>();
        if (urlNova.length() > 10 && urlNova.startsWith("http")) { 
          Serial.println("Nova versão disponível: " + urlNova);
          http.end();
          realizarAtualizacaoOTA(urlNova);
          return true;
        }
      }

      // Processa status do pagamento
      if (doc.containsKey("status")) {
        String status = doc["status"].as<String>();
        
        if (status == "aprovado") {
          int tempoLiberadoSec = doc["tempo_liberado"].as<int>();
          int idPix = doc.containsKey("id_pix") ? doc["id_pix"].as<int>() : 0;
          
          // Verifica se este PIX já foi processado
          if (idPix > 0 && pixJaProcessado(idPix)) {
            Serial.println("PIX " + String(idPix) + " já processado, ignorando");
            http.end();
            return true;
          }
          
          Serial.println("PIX Aprovado! Tempo liberado: " + String(tempoLiberadoSec) + "s");
          
          // Marca como processado ANTES de ligar a bomba
          if (idPix > 0) {
            salvarPixProcessado(idPix);
          }
          
          http.end();
          
          // Liga bomba (fora do bloqueio HTTP)
          bombaEmOperacao = true;
          ligarBombaComTimer(tempoLiberadoSec * 1000UL);
          bombaEmOperacao = false;
          
          lcd.clear();
          return true;
        }
        else if (status == "valor_insuficiente") {
          float valorPago = doc.containsKey("valor_pago") ? doc["valor_pago"].as<float>() : 0;
          Serial.println("Valor insuficiente: R$ " + String(valorPago));
        }
        else if (status == "pendente") {
          // Sem pagamento pendente - estado normal
        }
        else if (status == "erro") {
          String mensagem = doc.containsKey("mensagem") ? doc["mensagem"].as<String>() : "Erro desconhecido";
          Serial.println("Erro do servidor: " + mensagem);
        }
      }
    } else {
      Serial.print("Erro ao parsear JSON: ");
      Serial.println(error.c_str());
    }
  } else {
    Serial.println("Erro HTTP: " + String(httpCode));
    
    // Tratamento de erros específicos
    if (httpCode == HTTP_CODE_NOT_FOUND) {
      Serial.println("Máquina não configurada no servidor");
      lcd.clear();
      escreverCentralizado("Configurar", 0);
      escreverCentralizado("ID no Server", 1);
    }
    else if (httpCode == HTTP_CODE_SERVICE_UNAVAILABLE || 
             httpCode == HTTP_CODE_GATEWAY_TIMEOUT) {
      Serial.println("Servidor indisponível temporariamente");
    }
  }
  
  http.end();
  return false;
}

// ============================================================================
// SETUP
// ============================================================================
void setup() {
  Serial.begin(115200);
  
  pinMode(PINO_RELAY_BOMBA, OUTPUT);
  pinMode(PINO_ILUMINACAO, OUTPUT); 
  pinMode(PINO_LED_STATUS, OUTPUT);
  pinMode(PINO_BUZZER, OUTPUT); 
  
  // Configura PWM para LED (ESP32 usa LEDC, não analogWrite)
  ledcSetup(LEDC_CHANNEL, LEDC_FREQ, LEDC_RESOLUTION);
  ledcAttachPin(PINO_LED_STATUS, LEDC_CHANNEL);
  ledcWrite(LEDC_CHANNEL, 0); // LED desligado inicialmente
  
  pinMode(PINO_MOEDEIRO, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PINO_MOEDEIRO), contarMoeda, FALLING);
  
  digitalWrite(PINO_RELAY_BOMBA, LOW); 
  digitalWrite(PINO_ILUMINACAO, LOW); 
  noTone(PINO_BUZZER); 

  preferencias.begin("maquina", false); 
  contadorTotal = preferencias.getUInt("total", 0); 
  
  // Carrega configurações salvas (ID da máquina, último PIX, etc)
  carregarConfiguracoesSalvas();

  lcd.init(); 
  lcd.backlight();
  escreverCentralizado("Iniciando...", 0);

  WiFi.mode(WIFI_STA);
  WiFiManager wifiManager;
  wifiManager.setConfigPortalTimeout(180); 
  
  // Configurações personalizadas do WiFiManager
  WiFiManagerParameter param_id_maquina("id_maquina", "ID da Máquina", idMaquina, 63);
  wifiManager.addParameter(&param_id_maquina);
  
  // wifiManager.resetSettings(); // DESCOMENTE APENAS PARA FORÇAR A TELA DE CONFIGURAÇÃO

  bool conectado = wifiManager.autoConnect("OverPag_Configurar", "admin123");

  if (!conectado) {
    lcd.clear();
    escreverCentralizado("Falha WiFi!", 0);
    escreverCentralizado("Reiniciando...", 1);
    delay(2000);
    ESP.restart();
  }
  
  Serial.println("\nWiFi OK!");
  
  // Atualiza ID da máquina com valor digitado no portal (se houver)
  if (strlen(param_id_maquina.getValue()) > 0) {
    strncpy(idMaquina, param_id_maquina.getValue(), sizeof(idMaquina) - 1);
    idMaquina[sizeof(idMaquina) - 1] = '\0';
    preferencias.putString("id_maquina", String(idMaquina));
    Serial.println("ID da máquina atualizado: " + String(idMaquina));
  }
  
  // Constrói URLs com o ID da máquina
  construirURLs();
  
  lcd.clear();
  escreverCentralizado("Conectado!", 0);
  escreverCentralizado("ID: " + String(idMaquina), 1);
  delay(2000);
  lcd.clear();
}

// ============================================================================
// FUNÇÃO PRINCIPAL (LIGA BOMBA E LUZES)
// ============================================================================
void ligarBombaComTimer(unsigned long tempoTotalMillis) {
  Serial.println("Ligando Bomba...");
  
  contadorTotal++;
  preferencias.putUInt("total", contadorTotal);

  luzLigada = true;
  inicioLuz = millis();         
  duracaoLuz = TEMPO_LUZ_MS;    
  digitalWrite(PINO_ILUMINACAO, HIGH); 

  digitalWrite(PINO_RELAY_BOMBA, HIGH);
  
  unsigned long tempoInicio = millis();
  unsigned long ultimaAtualizacaoLCD = 0;
  
  lcd.clear();
  escreverCentralizado("BOMBA LIGADA!", 0);

  while ((millis() - tempoInicio) < tempoTotalMillis) {
    
    unsigned long tempoPassado = millis() - tempoInicio;
    unsigned long tempoRestante = tempoTotalMillis - tempoPassado;

    // LED com efeito respiração
    float angulo = (float)millis() / 500.0; 
    int brilho = 128 + 127 * sin(angulo);   
    ledcWrite(LEDC_CHANNEL, brilho);   

    // Atualiza display a cada segundo
    if (millis() - ultimaAtualizacaoLCD >= 1000) {
      ultimaAtualizacaoLCD = millis();
      int minutos = (tempoRestante / 1000) / 60;
      int segundos = (tempoRestante / 1000) % 60;

      lcd.setCursor(0, 1);
      char buf[20]; // Buffer aumentado para segurança
      snprintf(buf, sizeof(buf), "Tempo: %02d:%02d     ", minutos, segundos);
      escreverCentralizado(String(buf), 1);
    }

    // Alerta sonoro nos últimos 10 segundos
    if (tempoRestante <= 10000 && (millis() / 1000) % 2 == 0) {
      tone(PINO_BUZZER, 1000); 
    } else {
      noTone(PINO_BUZZER);
    }
    
    delay(10); 
  }

  digitalWrite(PINO_RELAY_BOMBA, LOW);  
  ledcWrite(LEDC_CHANNEL, 0); // Desliga LED via PWM
  noTone(PINO_BUZZER);                
  
  lcd.clear();
  escreverCentralizado(" TEMPO ACABOU! ", 0);
  delay(3000);
  lcd.clear();
}

// ============================================================================
// LOOP PRINCIPAL
// ============================================================================
void loop() {
  
  // 0. VERIFICAÇÃO CONTÍNUA DA ILUMINAÇÃO
  if (luzLigada) {
    if (millis() - inicioLuz >= duracaoLuz) {
      luzLigada = false;
      digitalWrite(PINO_ILUMINACAO, LOW); 
      duracaoLuz = 0;
    }
  }

  if(WiFi.status() == WL_CONNECTED) {

    // ==========================================
    // 1. LÓGICA DO MOEDEIRO PROPORCIONAL
    // ==========================================
    if (contadorMoedas == 0) {
      escreverCentralizado("OverPag", 0); 

      if (millis() - tempoUltimaTrocaTela > INTERVALO_TELA) {
        mostrandoValor = !mostrandoValor; 
        tempoUltimaTrocaTela = millis();
        lcd.setCursor(0, 1);
        lcd.print("                "); 
      }

      if (mostrandoValor) {
        char bufPreco[17];
        sprintf(bufPreco, "Valor: R$ %.2f", precoPixAtual);
        escreverCentralizado(String(bufPreco), 1);
      } else {
        char bufferContador[17];
        sprintf(bufferContador, "Cont: %05u", contadorTotal);
        escreverCentralizado(String(bufferContador), 1);
      }
    } 
    else {
      unsigned long tempoEspera = millis() - ultimoTempoMoeda;

      float valorInserido = contadorMoedas * 1.00;
      unsigned long tempoProporcionalSegundos = (tempoBaseSegundos / precoPixAtual) * valorInserido;

      if (tempoEspera >= TIMEOUT_MOEDA_MS) {
        contadorMoedas = 0; 
        tone(PINO_BUZZER, 800);
        delay(200);
        noTone(PINO_BUZZER);
        
        ligarBombaComTimer(tempoProporcionalSegundos * 1000UL);
        lcd.clear(); 
      } 
      else {
        int segRestantes = (TIMEOUT_MOEDA_MS - tempoEspera) / 1000;
        
        char bufSaldo[20];
        snprintf(bufSaldo, sizeof(bufSaldo), "Saldo: R$ %.2f", valorInserido);
        escreverCentralizado(String(bufSaldo), 0);
        
        char bufTempo[20];
        snprintf(bufTempo, sizeof(bufTempo), "Inicia em %ds...", segRestantes);
        escreverCentralizado(String(bufTempo), 1);
      }
    }

    // ==========================================
    // 2. LÓGICA DO SERVIDOR HTTP (Nuvem)
    // ==========================================
    // Só consulta se não houver moedas sendo inseridas e intervalo atingido
    if (contadorMoedas == 0 && !bombaEmOperacao && (millis() - ultimoCheckHTTP >= intervaloHTTP)) {
      ultimoCheckHTTP = millis(); 
      consultarServidor();
    }
    
  } else {
    lcd.setCursor(0, 0);
    lcd.print("Sem WiFi...     ");
    
    // Tenta reconectar
    if (millis() % 10000 < 1000) {
      Serial.println("Tentando reconectar WiFi...");
      WiFi.reconnect();
    }
  }
  
  // Delay mínimo para estabilidade do watchdog e debouncing
  delay(10);
}
