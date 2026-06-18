"""
SuperOdds Bot — recebe prints de apostas no Telegram, lê via IA (Claude),
pergunta valor/casa e grava direto no Firestore (mesma estrutura do dashboard).
"""

import os
import json
import logging
import base64
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters
)

import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# SERVIDOR HTTP "FAKE" — só pra satisfazer o health check do Render.
# O Render (Web Service) espera algo respondendo numa porta HTTP;
# como o bot só conversa com o Telegram via polling, sem isso o
# Render entende que o serviço travou e reinicia sozinho.
# ══════════════════════════════════════════════════════════════════
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SuperOdds Bot rodando!")

    def log_message(self, format, *args):
        pass  # silencia o log padrão do http.server pra não poluir os logs do bot


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    log.info(f"Servidor de health check rodando na porta {port}")
    server.serve_forever()

# ══════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO — lida de variáveis de ambiente (configuradas no Render)
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])     # seu chat id pessoal — só você usa o bot
FIREBASE_UID = os.environ["FIREBASE_UID"]                 # seu UID do Google no Firebase
FIREBASE_CREDENTIALS_JSON = os.environ["FIREBASE_CREDENTIALS_JSON"]  # conteúdo do service-account.json

# inicializa Firebase Admin
cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

SPORTS = ['Futebol', 'Basquete', 'Tênis', 'MMA', 'Vôlei', 'E-sports', 'Outros']

# estados da conversa
AGUARDANDO_VALOR, AGUARDANDO_CASA, AGUARDANDO_DATA = range(3)


# ══════════════════════════════════════════════════════════════════
# SEGURANÇA — só responde ao seu chat pessoal
# ══════════════════════════════════════════════════════════════════
def autorizado(update: Update) -> bool:
    return update.effective_chat.id == ALLOWED_CHAT_ID


# ══════════════════════════════════════════════════════════════════
# LEITURA DO PRINT VIA GEMINI (visão, gratuito)
# ══════════════════════════════════════════════════════════════════
def extrair_dados_print(image_bytes: bytes, media_type: str) -> dict:
    """Manda o print pro Gemini e pede pra extrair os dados estruturados da aposta."""

    prompt = f"""Analise este print de bilhete de aposta esportiva e extraia os dados em JSON.

Se houver múltiplas seleções no bilhete (aposta múltipla), trate como UMA única aposta combinada e descreva todas as seleções juntas no campo "jogo_ou_aposta", usando a odd TOTAL da combinação.

Responda APENAS com um único objeto JSON válido (nunca uma lista), sem nenhum texto antes ou depois, exatamente neste formato:
{{
  "esporte": "um destes: {', '.join(SPORTS)}",
  "jogo_ou_aposta": "descrição curta do jogo/mercado, ex: 'Flamengo x Vasco - Over 2.5 gols'",
  "odd": 1.85
}}

Se não conseguir identificar algum campo com confiança, use null nesse campo. A resposta deve ser um objeto único {{...}}, nunca uma lista [...]."""

    image_part = {"mime_type": media_type, "data": image_bytes}
    resp = gemini_model.generate_content([prompt, image_part])

    text = resp.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(text)

    # defesa extra: se o modelo devolver uma lista (ex: várias seleções
    # separadas), usa o primeiro item e combina a descrição das demais
    if isinstance(parsed, list):
        if not parsed:
            return {"esporte": None, "jogo_ou_aposta": None, "odd": None}
        primeiro = parsed[0]
        if len(parsed) > 1:
            descricoes = [item.get("jogo_ou_aposta", "") for item in parsed if isinstance(item, dict)]
            primeiro["jogo_ou_aposta"] = " + ".join(d for d in descricoes if d)
        parsed = primeiro

    if not isinstance(parsed, dict):
        return {"esporte": None, "jogo_ou_aposta": None, "odd": None}

    return parsed


# ══════════════════════════════════════════════════════════════════
# PARSING FLEXÍVEL DE DATA — aceita "hoje", "ontem", ou dd/mm[/aaaa]
# ══════════════════════════════════════════════════════════════════
def parsear_data(texto: str) -> str | None:
    """Retorna a data no formato YYYY-MM-DD, ou None se não conseguir entender."""
    texto = texto.strip().lower()
    hoje = datetime.now()

    if texto in ("hoje", "h"):
        return hoje.strftime("%Y-%m-%d")
    if texto in ("ontem", "o"):
        from datetime import timedelta
        return (hoje - timedelta(days=1)).strftime("%Y-%m-%d")

    # aceita dd/mm ou dd/mm/aaaa ou dd-mm ou dd-mm-aaaa
    for sep in ("/", "-"):
        if sep in texto:
            partes = texto.split(sep)
            if len(partes) == 2:
                dia, mes = partes
                ano = hoje.year
            elif len(partes) == 3:
                dia, mes, ano = partes
                ano = int(ano) if len(ano) == 4 else 2000 + int(ano)
            else:
                continue
            try:
                d = datetime(int(ano), int(mes), int(dia))
                return d.strftime("%Y-%m-%d")
            except ValueError:
                return None
    return None


# ══════════════════════════════════════════════════════════════════
# FIRESTORE — grava a aposta no mesmo formato do dashboard
# ══════════════════════════════════════════════════════════════════
def gravar_aposta(esp: str, ap: str, odd: float, stake: float, casa: str, dat: str):
    bet = {
        "dat": dat,
        "esp": esp or "Outros",
        "casa": casa,
        "ap": ap,
        "odd": odd,
        "stake": stake,
        "res": "PENDENTE",
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    db.collection("users").document(FIREBASE_UID).collection("bets").add(bet)


# ══════════════════════════════════════════════════════════════════
# HANDLERS DO TELEGRAM
# ══════════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await update.message.reply_text(
        "🤖 SuperOdds Bot ativo!\n\n"
        "Me manda o print do bilhete da aposta que eu cadastro automaticamente como pendente."
    )


async def receber_print(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return

    await update.message.reply_text("🔎 Lendo o print...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    try:
        dados = extrair_dados_print(bytes(image_bytes), "image/jpeg")
    except Exception as e:
        log.exception("Erro ao ler print")
        await update.message.reply_text(f"⚠ Não consegui ler o print: {e}\nTenta de novo ou manda um print mais nítido.")
        return ConversationHandler.END

    if not dados.get("odd") or not dados.get("jogo_ou_aposta"):
        await update.message.reply_text(
            "⚠ Não consegui identificar os dados com certeza nesse print.\n"
            "Pode mandar um print mais nítido, mostrando claramente o jogo e a odd?"
        )
        return ConversationHandler.END

    context.user_data["esp"] = dados.get("esporte") or "Outros"
    context.user_data["ap"] = dados["jogo_ou_aposta"]
    context.user_data["odd"] = float(dados["odd"])

    resumo = (
        f"✅ Identifiquei:\n\n"
        f"🏅 Esporte: {context.user_data['esp']}\n"
        f"🎯 Aposta: {context.user_data['ap']}\n"
        f"📈 Odd: {context.user_data['odd']}\n\n"
        f"💰 Quanto você apostou? (só o número, ex: 50)"
    )
    await update.message.reply_text(resumo)
    return AGUARDANDO_VALOR


async def receber_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    texto = update.message.text.strip().replace(",", ".").replace("R$", "").strip()
    try:
        valor = float(texto)
    except ValueError:
        await update.message.reply_text("⚠ Manda só o número, ex: 50 ou 50.00")
        return AGUARDANDO_VALOR

    context.user_data["stake"] = valor
    await update.message.reply_text("🏦 Em qual casa de apostas?")
    return AGUARDANDO_CASA


async def receber_casa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    context.user_data["casa"] = update.message.text.strip()

    hoje_fmt = datetime.now().strftime("%d/%m")
    await update.message.reply_text(
        f"📅 Qual o dia da aposta?\n"
        f"Manda 'hoje', 'ontem', ou a data (ex: {hoje_fmt})"
    )
    return AGUARDANDO_DATA


async def receber_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    dat = parsear_data(update.message.text)
    if not dat:
        await update.message.reply_text(
            "⚠ Não entendi essa data. Manda 'hoje', 'ontem', ou no formato dd/mm (ex: 17/06)"
        )
        return AGUARDANDO_DATA

    d = context.user_data
    d["dat"] = dat

    try:
        gravar_aposta(d["esp"], d["ap"], d["odd"], d["stake"], d["casa"], d["dat"])
    except Exception as e:
        log.exception("Erro ao gravar no Firestore")
        await update.message.reply_text(f"⚠ Erro ao salvar no dashboard: {e}")
        return ConversationHandler.END

    data_fmt = datetime.strptime(dat, "%Y-%m-%d").strftime("%d/%m/%Y")
    await update.message.reply_text(
        f"🎉 Aposta cadastrada como PENDENTE!\n\n"
        f"🏅 {d['esp']}\n🎯 {d['ap']}\n📈 Odd {d['odd']}\n"
        f"💰 R$ {d['stake']:.2f}\n🏦 {d['casa']}\n📅 {data_fmt}\n\n"
        f"Resolve ela (Green/Red/Void) direto no dashboard quando o jogo acabar."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Cadastro cancelado.")
    return ConversationHandler.END


async def mensagem_nao_reconhecida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await update.message.reply_text("Me manda um print do bilhete da aposta pra eu cadastrar! 📸")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receber_print)],
        states={
            AGUARDANDO_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_valor)],
            AGUARDANDO_CASA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_casa)],
            AGUARDANDO_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_data)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagem_nao_reconhecida))

    log.info("Bot iniciado, aguardando mensagens...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # inicia o servidor HTTP fake numa thread separada, em paralelo ao bot
    threading.Thread(target=start_health_server, daemon=True).start()

    main()
