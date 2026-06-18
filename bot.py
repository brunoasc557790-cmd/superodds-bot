"""
SuperOdds Bot — recebe prints de apostas no Telegram, lê via IA (Claude),
pergunta valor/casa e grava direto no Firestore (mesma estrutura do dashboard).
"""

import os
import json
import logging
import base64
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters
)

import anthropic
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO — lida de variáveis de ambiente (configuradas no Render)
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])     # seu chat id pessoal — só você usa o bot
FIREBASE_UID = os.environ["FIREBASE_UID"]                 # seu UID do Google no Firebase
FIREBASE_CREDENTIALS_JSON = os.environ["FIREBASE_CREDENTIALS_JSON"]  # conteúdo do service-account.json

# inicializa Firebase Admin
cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SPORTS = ['Futebol', 'Basquete', 'Tênis', 'MMA', 'Vôlei', 'E-sports', 'Outros']

# estados da conversa
AGUARDANDO_VALOR, AGUARDANDO_CASA = range(2)


# ══════════════════════════════════════════════════════════════════
# SEGURANÇA — só responde ao seu chat pessoal
# ══════════════════════════════════════════════════════════════════
def autorizado(update: Update) -> bool:
    return update.effective_chat.id == ALLOWED_CHAT_ID


# ══════════════════════════════════════════════════════════════════
# LEITURA DO PRINT VIA CLAUDE (visão)
# ══════════════════════════════════════════════════════════════════
def extrair_dados_print(image_bytes: bytes, media_type: str) -> dict:
    """Manda o print pro Claude e pede pra extrair os dados estruturados da aposta."""
    img_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = f"""Analise este print de bilhete de aposta esportiva e extraia os dados em JSON.

Responda APENAS com um JSON válido, sem nenhum texto antes ou depois, no formato:
{{
  "esporte": "um destes: {', '.join(SPORTS)}",
  "jogo_ou_aposta": "descrição curta do jogo/mercado, ex: 'Flamengo x Vasco - Over 2.5 gols'",
  "odd": 1.85
}}

Se não conseguir identificar algum campo com confiança, use null nesse campo."""

    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )

    text = resp.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


# ══════════════════════════════════════════════════════════════════
# FIRESTORE — grava a aposta no mesmo formato do dashboard
# ══════════════════════════════════════════════════════════════════
def gravar_aposta(esp: str, ap: str, odd: float, stake: float, casa: str):
    hoje = datetime.now().strftime("%Y-%m-%d")
    bet = {
        "dat": hoje,
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

    casa = update.message.text.strip()
    d = context.user_data

    try:
        gravar_aposta(d["esp"], d["ap"], d["odd"], d["stake"], casa)
    except Exception as e:
        log.exception("Erro ao gravar no Firestore")
        await update.message.reply_text(f"⚠ Erro ao salvar no dashboard: {e}")
        return ConversationHandler.END

    await update.message.reply_text(
        f"🎉 Aposta cadastrada como PENDENTE!\n\n"
        f"🏅 {d['esp']}\n🎯 {d['ap']}\n📈 Odd {d['odd']}\n"
        f"💰 R$ {d['stake']:.2f}\n🏦 {casa}\n\n"
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
    main()
