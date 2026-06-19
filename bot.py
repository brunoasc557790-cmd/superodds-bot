"""
SuperOdds Bot — recebe prints de apostas no Telegram, lê via IA (Claude),
pergunta valor/casa e grava direto no Firestore (mesma estrutura do dashboard).

Usa WEBHOOK em vez de polling: o Telegram manda as mensagens diretamente
pra uma URL HTTP nossa, em vez do bot ficar perguntando "tem mensagem nova?"
sem parar. Isso é mais compatível com o modelo de Web Service do Render
e evita os reinícios aleatórios que acontecem com polling de longa duração.
"""

import os
import json
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)

from groq import Groq
import base64
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO — lida de variáveis de ambiente (configuradas no Render)
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])     # seu chat id pessoal — só você usa o bot
FIREBASE_UID = os.environ["FIREBASE_UID"]                 # seu UID do Google no Firebase
FIREBASE_CREDENTIALS_JSON = os.environ["FIREBASE_CREDENTIALS_JSON"]  # conteúdo do service-account.json
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]   # ex: https://superodds-bot.onrender.com (o Render já preenche essa automaticamente)

# inicializa Firebase Admin
cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

groq_client = Groq(api_key=GROQ_API_KEY)

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
    """Manda o print pro Groq/Llama 4 e extrai os dados estruturados da aposta."""

    img_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = f"""Analise este print de bilhete de aposta esportiva e extraia os dados em JSON.

Se houver múltiplas seleções no bilhete (aposta múltipla), trate como UMA única aposta combinada e descreva todas as seleções juntas no campo "jogo_ou_aposta", usando a odd TOTAL da combinação.

Responda APENAS com um único objeto JSON válido (nunca uma lista), sem nenhum texto antes ou depois, exatamente neste formato:
{{
  "esporte": "um destes: {', '.join(SPORTS)}",
  "jogo_ou_aposta": "descrição curta do jogo/mercado, ex: 'Flamengo x Vasco - Over 2.5 gols'",
  "odd": 1.85
}}

Se não conseguir identificar algum campo com confiança, use null nesse campo. A resposta deve ser um objeto único {{...}}, nunca uma lista [...]."""

    resp = groq_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{img_b64}"}},
                {"type": "text", "text": prompt}
            ]
        }],
        max_tokens=500,
    )

    text = resp.choices[0].message.content.strip()
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


def buscar_pendentes(apenas_hoje: bool = True):
    """Retorna lista de (id, dados) das apostas pendentes, mais recentes primeiro.
    Por padrão, filtra só as de hoje (mesma data local do servidor)."""
    bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
    docs = bets_col.where("res", "==", "PENDENTE").stream()
    pendentes = [(doc.id, doc.to_dict()) for doc in docs]

    if apenas_hoje:
        hoje = datetime.now().strftime("%Y-%m-%d")
        pendentes = [(bid, b) for bid, b in pendentes if b.get("dat") == hoje]

    # ordena pela data da aposta, mais recente primeiro (fallback se não tiver "dat")
    pendentes.sort(key=lambda x: x[1].get("dat", ""), reverse=True)
    return pendentes


def resolver_aposta(bet_id: str, resultado: str):
    bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
    bets_col.document(bet_id).update({"res": resultado})


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
# RESOLVER PENDENTES POR TEXTO — "green", "red" ou "void"
# ══════════════════════════════════════════════════════════════════
EMOJI_ESPORTE = {
    "Futebol": "⚽", "Basquete": "🏀", "Tênis": "🎾",
    "MMA": "🥊", "Vôlei": "🏐", "E-sports": "🎮", "Outros": "🎲",
}


def formatar_resumo_aposta(b: dict, max_ap: int = 28) -> str:
    emoji = EMOJI_ESPORTE.get(b.get("esp"), "🎲")
    ap = b.get("ap", "—")
    odd = str(b.get("odd", "—"))
    casa = b.get("casa", "")
    if len(ap) > max_ap:
        ap = ap[:max_ap - 1] + "…"
    casa_str = f" {casa}" if casa else ""
    return f"{emoji} {ap}{casa_str} @{odd}"


async def cmd_resolver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre o Mini App para resolver apostas pendentes de hoje."""
    if not autorizado(update):
        return
    pendentes = buscar_pendentes()
    n = len(pendentes)
    if not n:
        await update.message.reply_text("✅ Nenhuma aposta pendente hoje!")
        return
    from telegram import WebAppInfo
    webapp_url = f"{RENDER_EXTERNAL_URL}/webapp?token={TELEGRAM_TOKEN}"
    botao = [[InlineKeyboardButton(
        f"⚡ Resolver apostas ({n} hoje)",
        web_app=WebAppInfo(url=webapp_url)
    )]]
    await update.message.reply_text(
        f"📋 *{n} aposta{'s' if n!=1 else ''} pendente{'s' if n!=1 else ''} hoje*\nAbra o menu para resolver:",
        reply_markup=InlineKeyboardMarkup(botao),
        parse_mode="Markdown",
    )


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o resumo do dia diretamente no chat."""
    if not autorizado(update):
        return

    hoje = datetime.now().strftime("%Y-%m-%d")
    hoje_fmt = datetime.now().strftime("%d/%m")

    bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
    docs = bets_col.where("dat", "==", hoje).stream()
    apostas_hoje = [doc.to_dict() for doc in docs]

    if not apostas_hoje:
        await update.message.reply_text(f"📊 Nenhuma aposta registrada hoje ({hoje_fmt}).")
        return

    green  = [b for b in apostas_hoje if b.get("res") == "GREEN"]
    red    = [b for b in apostas_hoje if b.get("res") == "RED"]
    void   = [b for b in apostas_hoje if b.get("res") == "VOID"]
    pend   = [b for b in apostas_hoje if not b.get("res") or b.get("res") == "PENDENTE"]

    lucro = 0.0
    stake_total = 0.0
    for b in apostas_hoje:
        s = float(b.get("stake", 0) or 0)
        o = float(b.get("odd", 0) or 0)
        stake_total += s
        if b.get("res") == "GREEN":
            lucro += s * (o - 1)
        elif b.get("res") == "RED":
            lucro -= s

    stake_pend = sum(float(b.get("stake", 0) or 0) for b in pend)
    roi = (lucro / stake_total * 100) if stake_total > 0 else 0
    resolvidas = len(green) + len(red)
    acerto = (len(green) / resolvidas * 100) if resolvidas > 0 else 0

    sinal = "+" if lucro >= 0 else ""
    cor_lucro = "🟢" if lucro > 0 else "🔴" if lucro < 0 else "⚪"

    linhas = [
        f"📊 *Resumo de hoje — {hoje_fmt}*",
        "",
        f"✅ Green: *{len(green)}*  ❌ Red: *{len(red)}*  ⚪ Void: *{len(void)}*",
        f"{cor_lucro} Lucro: *{sinal}R$ {lucro:.2f}*",
        f"📈 ROI: *{sinal}{roi:.1f}%*",
    ]

    if resolvidas > 0:
        linhas.append(f"🎯 Acerto: *{acerto:.0f}%*")

    if pend:
        linhas.append(f"⏳ Pendentes: *{len(pend)}* (R$ {stake_pend:.2f} em risco)")

    linhas += [
        "",
        f"📋 Total apostas hoje: *{len(apostas_hoje)}*",
        f"💵 Stake total: *R$ {stake_total:.2f}*",
    ]

    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def callback_resolver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disparado quando o usuário clica num dos botões de aposta pendente."""
    query = update.callback_query
    if query.message.chat.id != ALLOWED_CHAT_ID:
        return

    await query.answer()

    _, resultado, bet_id = query.data.split("|")

    bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
    doc = bets_col.document(bet_id).get()
    b = doc.to_dict() if doc.exists else {}

    try:
        resolver_aposta(bet_id, resultado)
    except Exception as e:
        log.exception("Erro ao resolver aposta")
        await query.edit_message_text(f"⚠ Erro ao marcar a aposta: {e}")
        return

    cor = {"GREEN": "🟢", "RED": "🔴", "VOID": "⚪"}.get(resultado, "")
    resumo = formatar_resumo_aposta(b) if b else ""

    texto = f"{cor} *{resultado}*\n{resumo}" if resumo else f"{cor} Aposta marcada como {resultado}!"

    # calcula o retorno/lucro pra mostrar na confirmação, quando possível
    try:
        stake = float(b.get("stake", 0))
        odd = float(b.get("odd", 0))
        if resultado == "GREEN":
            lucro = stake * (odd - 1)
            texto += f"\n💰 Lucro: +R$ {lucro:.2f}"
        elif resultado == "RED":
            texto += f"\n💸 Prejuízo: -R$ {stake:.2f}"
        elif resultado == "VOID":
            texto += f"\n↩️ Stake devolvida: R$ {stake:.2f}"
    except (TypeError, ValueError):
        pass

    await query.edit_message_text(texto, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════
# MAIN — servidor webhook manual com aiohttp (evita o start_webhook()
# interno da biblioteca, que tem um bug de incompatibilidade com
# versões recentes do Python no ambiente do Render)
# ══════════════════════════════════════════════════════════════════
async def run_bot():
    from aiohttp import web

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
    app.add_handler(CommandHandler("resolver", cmd_resolver))
    app.add_handler(CommandHandler("resumo", cmd_resumo))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_resolver, pattern=r"^resolve\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagem_nao_reconhecida))

    port = int(os.environ.get("PORT", 10000))
    webhook_path = "/" + TELEGRAM_TOKEN  # usa o próprio token como "segredo" da URL
    webhook_url = f"{RENDER_EXTERNAL_URL}{webhook_path}"

    async def handle_webhook(request: web.Request) -> web.Response:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response()

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(text="SuperOdds Bot rodando!")

    async def handle_webapp(request: web.Request) -> web.Response:
        """Serve o HTML do Mini App."""
        import os as _os
        html_path = _os.path.join(_os.path.dirname(__file__), "webapp.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html", headers={
            "Access-Control-Allow-Origin": "*",
        })

    async def handle_pendentes(request: web.Request) -> web.Response:
        """API: retorna apostas pendentes de hoje em JSON."""
        if request.rel_url.query.get("token") != TELEGRAM_TOKEN:
            return web.Response(status=403, text="Forbidden")
        hoje = datetime.now().strftime("%Y-%m-%d")
        bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
        docs = bets_col.where("res", "==", "PENDENTE").stream()
        pendentes = []
        for doc in docs:
            d = doc.to_dict()
            if d.get("dat") == hoje:
                d["id"] = doc.id
                # remove campos não serializáveis (como SERVER_TIMESTAMP)
                d.pop("createdAt", None)
                pendentes.append(d)
        pendentes.sort(key=lambda x: x.get("dat", ""), reverse=True)
        return web.json_response(pendentes, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_resolve(request: web.Request) -> web.Response:
        """API: resolve uma aposta e retorna o resultado com lucro/prejuízo."""
        if request.rel_url.query.get("token") != TELEGRAM_TOKEN:
            return web.Response(status=403, text="Forbidden")
        bet_id = request.rel_url.query.get("bet_id")
        resultado = request.rel_url.query.get("res", "").upper()
        if not bet_id or resultado not in ("GREEN", "RED", "VOID"):
            return web.Response(status=400, text="Parâmetros inválidos")
        bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
        doc = bets_col.document(bet_id).get()
        b = doc.to_dict() if doc.exists else {}
        resolver_aposta(bet_id, resultado)
        # monta mensagem de confirmação para o chat
        cor = {"GREEN": "🟢", "RED": "🔴", "VOID": "⚪"}.get(resultado, "")
        resumo = formatar_resumo_aposta(b) if b else ""
        msg = f"{cor} *{resultado}*\n{resumo}" if resumo else f"{cor} Aposta marcada como {resultado}!"
        try:
            stake = float(b.get("stake", 0))
            odd = float(b.get("odd", 0))
            if resultado == "GREEN":
                lucro = stake * (odd - 1)
                msg += f"\n💰 Lucro: +R$ {lucro:.2f}"
            elif resultado == "RED":
                msg += f"\n💸 Prejuízo: -R$ {stake:.2f}"
            elif resultado == "VOID":
                msg += f"\n↩️ Stake devolvida: R$ {stake:.2f}"
        except (TypeError, ValueError):
            pass
        # envia confirmação no chat do Telegram
        try:
            await app.bot.send_message(ALLOWED_CHAT_ID, msg, parse_mode="Markdown")
        except Exception:
            pass
        return web.json_response({"ok": True, "msg": f"{cor} {resultado}!"}, headers={
            "Access-Control-Allow-Origin": "*",
        })

    async def handle_resolve_multi(request: web.Request) -> web.Response:
        """API: resolve múltiplas apostas de uma vez."""
        if request.rel_url.query.get("token") != TELEGRAM_TOKEN:
            return web.Response(status=403, text="Forbidden")
        ids_str = request.rel_url.query.get("ids", "")
        resultado = request.rel_url.query.get("res", "").upper()
        if not ids_str or resultado not in ("GREEN", "RED", "VOID"):
            return web.Response(status=400, text="Parâmetros inválidos")
        ids = [i.strip() for i in ids_str.split(",") if i.strip()]
        bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
        total_lucro = 0.0
        cor = {"GREEN": "🟢", "RED": "🔴", "VOID": "⚪"}.get(resultado, "")
        for bet_id in ids:
            doc = bets_col.document(bet_id).get()
            b = doc.to_dict() if doc.exists else {}
            resolver_aposta(bet_id, resultado)
            try:
                stake = float(b.get("stake", 0))
                odd = float(b.get("odd", 0))
                if resultado == "GREEN": total_lucro += stake * (odd - 1)
                elif resultado == "RED": total_lucro -= stake
            except (TypeError, ValueError):
                pass
        # monta mensagem de confirmação
        n = len(ids)
        if resultado == "GREEN":
            msg = f"🟢 *{n} aposta{'s' if n>1 else ''} marcada{'s' if n>1 else ''} como GREEN*\n💰 Lucro total: +R$ {total_lucro:.2f}"
        elif resultado == "RED":
            msg = f"🔴 *{n} aposta{'s' if n>1 else ''} marcada{'s' if n>1 else ''} como RED*\n💸 Prejuízo total: -R$ {abs(total_lucro):.2f}"
        else:
            msg = f"⚪ *{n} aposta{'s' if n>1 else ''} marcada{'s' if n>1 else ''} como VOID*\n↩️ Stakes devolvidas"
        try:
            await app.bot.send_message(ALLOWED_CHAT_ID, msg, parse_mode="Markdown")
        except Exception:
            pass
        return web.json_response({"ok": True, "msg": f"{cor} {n} apostas salvas!"}, headers={
            "Access-Control-Allow-Origin": "*",
        })

    web_app = web.Application()
    web_app.router.add_post(webhook_path, handle_webhook)
    web_app.router.add_get("/", handle_health)
    web_app.router.add_get("/webapp", handle_webapp)
    web_app.router.add_get("/pendentes", handle_pendentes)
    web_app.router.add_get("/resolve", handle_resolve)
    web_app.router.add_get("/resolve-multi", handle_resolve_multi)

    runner = web.AppRunner(web_app)

    async with app:
        await app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        await app.start()

        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        log.info(f"Bot iniciado via webhook em {webhook_url}")

        import asyncio
        try:
            await asyncio.Event().wait()  # mantém o processo rodando pra sempre
        finally:
            await runner.cleanup()
            await app.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot())



