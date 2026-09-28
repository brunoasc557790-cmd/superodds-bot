"""
SuperOdds Bot — recebe prints de apostas no Telegram, lê via IA,
pergunta valor/casa e grava direto no Firestore (mesma estrutura do dashboard).

NESTA VERSÃO:
  • Funciona no seu PRIVADO (uso solo do dia a dia, igual antes)
    E dentro de um GRUPO (várias pessoas planilhando no mesmo dashboard).
  • Detecta ESCADA: quando o print tem a mesma aposta em várias linhas
    (ex: Mais de 1.5 / 2.5 / 3.5), cada linha vira uma aposta separada,
    todas com o mesmo `grupo` pra o dashboard agrupar.
  • Cada aposta guarda o AUTOR (quem mandou o print).
  • NOVO: detecta apostas feitas em CRIPTO (LTC, BTC, ETH, USDT — casas
    tipo TrustDice usam Ł, ₿, Ξ). Busca a cotação atual em BRL (CoinGecko)
    no momento do registro e grava o valor JÁ CONVERTIDO no campo `stake`
    (o dashboard continua só em reais, sem precisar mudar nada). O valor
    original na cripto e a cotação usada ficam guardados como referência.

Usa WEBHOOK em vez de polling.
"""

import os
import json
import uuid
import logging
import base64
import requests
from math import prod
from datetime import datetime, timezone, timedelta

# fuso horário de Brasília (UTC-3)
BRT = timezone(timedelta(hours=-3))

def agora() -> datetime:
    return datetime.now(BRT)

def hoje_local() -> str:
    return agora().strftime("%Y-%m-%d")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from openai import OpenAI
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO — variáveis de ambiente (Render)
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# seu chat pessoal (privado com o bot) — uso solo do dia a dia
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])

# id do GRUPO (número NEGATIVO, ex: -1001234567890). Opcional.
# Se não definir, o bot só funciona no seu privado.
_grp = os.environ.get("ALLOWED_GROUP_ID", "").strip()
ALLOWED_GROUP_ID = int(_grp) if _grp else None

# lista opcional de usuários liberados (ids separados por vírgula).
# Vazio = qualquer um do grupo pode planilhar.
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}

FIREBASE_UID = os.environ["FIREBASE_UID"]
FIREBASE_CREDENTIALS_JSON = os.environ["FIREBASE_CREDENTIALS_JSON"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]

# para onde mandar as confirmações vindas do webapp (resolver apostas):
# se tem grupo, avisa no grupo; senão, no seu privado.
CHAT_NOTIFICACAO = ALLOWED_GROUP_ID if ALLOWED_GROUP_ID is not None else ALLOWED_CHAT_ID

# inicializa Firebase Admin
cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

openrouter_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

SPORTS = ['Futebol', 'Basquete', 'Tênis', 'MMA', 'Vôlei', 'E-sports', 'Outros']

# estados da conversa
AGUARDANDO_VALOR, AGUARDANDO_CASA, AGUARDANDO_DATA = range(3)

# ══════════════════════════════════════════════════════════════════
# CRIPTO → BRL — cotação em tempo real (CoinGecko, sem chave)
# ══════════════════════════════════════════════════════════════════
CRYPTO_IDS = {
    "LTC": "litecoin",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "tether",
}

def obter_cotacao_brl(moeda: str) -> float | None:
    """Busca a cotação atual de uma cripto em BRL. Retorna None se falhar."""
    coingecko_id = CRYPTO_IDS.get((moeda or "").upper())
    if not coingecko_id:
        return None
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coingecko_id, "vs_currencies": "brl"},
            timeout=6,
        )
        r.raise_for_status()
        return float(r.json()[coingecko_id]["brl"])
    except Exception:
        log.exception(f"Erro ao buscar cotação {moeda}/BRL")
        return None

def fmt_cripto(valor: float) -> str:
    """Formata um valor cripto sem casas decimais desnecessárias (até 8 casas)."""
    s = f"{float(valor):.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

# ══════════════════════════════════════════════════════════════════
# SEGURANÇA — libera privado do dono + grupo autorizado
# ══════════════════════════════════════════════════════════════════
def chat_liberado(chat_id: int) -> bool:
    if chat_id == ALLOWED_CHAT_ID:
        return True
    return ALLOWED_GROUP_ID is not None and chat_id == ALLOWED_GROUP_ID

def autorizado(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or not chat_liberado(chat.id):
        return False
    # se houver lista de liberados, o usuário precisa estar nela
    if ALLOWED_USER_IDS and (user is None or user.id not in ALLOWED_USER_IDS):
        return False
    return True

def nome_autor(update: Update) -> tuple[str, int]:
    u = update.effective_user
    if u is None:
        return ("desconhecido", 0)
    nome = u.username or u.full_name or str(u.id)
    return (nome, u.id)

# ══════════════════════════════════════════════════════════════════
# LEITURA DO PRINT VIA IA (visão) — devolve LISTA de seleções + tipo + moeda
# ══════════════════════════════════════════════════════════════════
def _extrair_json(text: str):
    """Extrai o primeiro objeto JSON do texto, aguentando chaves aninhadas."""
    text = (text or "").strip().replace("```json", "").replace("```", "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        text = text[i:j + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import ast
        try:
            return ast.literal_eval(text)
        except Exception:
            return None

def extrair_dados_print(image_bytes: bytes, media_type: str) -> dict:
    """Manda o print pra IA e devolve {esporte, tipo, moeda, odd_total, selecoes:[...]}."""
    img_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = f"""Analise este print de aposta esportiva e devolva TODAS as seleções.

CLASSIFIQUE O TIPO da aposta:
- Se CADA seleção tem a SUA PRÓPRIA caixa de valor/stake E o SEU PRÓPRIO
  "Retorno Potencial", então são APOSTAS SIMPLES SEPARADAS (NÃO é múltipla).
  Cada linha é independente.
    - Se, além disso, forem o MESMO jogo + MESMO jogador/mercado, mudando só a
      linha/handicap (ex: Mais de 1.5, Mais de 2.5, Mais de 3.5) -> tipo = "escada".
    - Caso contrário -> tipo = "simples".
- Se houver VÁRIAS seleções mas UMA ÚNICA caixa de valor e UM ÚNICO retorno total
  no rodapé, é uma MÚLTIPLA (acumulada) -> tipo = "multipla" e a odd é a odd TOTAL.
- Se houver só uma seleção -> tipo = "simples".

DETECTE A MOEDA da aposta pelo símbolo do valor apostado:
- "R$" -> moeda = "BRL"
- "Ł" -> moeda = "LTC" (Litecoin)
- "₿" -> moeda = "BTC" (Bitcoin)
- "Ξ" -> moeda = "ETH" (Ethereum)
- "$" sozinho em casa de cripto (sem "R$") -> moeda = "USDT"
- Se não conseguir identificar nenhum símbolo -> moeda = "BRL"
A moeda vale pra aposta inteira (mesma carteira). Extraia o valor de stake
SEMPRE no formato numérico original (ex: 0.34300000), sem converter nada.

Para CADA seleção extraia:
  descricao (jogo + mercado), odd, stake (valor apostado, no formato/moeda original, se visível), retorno (se visível, na moeda original).

Responda APENAS com um único JSON válido, sem texto antes ou depois:
{{
  "esporte": "um destes: {', '.join(SPORTS)}",
  "tipo": "simples | escada | multipla",
  "moeda": "BRL | LTC | BTC | ETH | USDT",
  "odd_total": null,
  "selecoes": [
    {{"descricao": "Vasco x Cruzeiro - Santiago Sosa - Faltas Mais de 1.5", "odd": 2.25, "stake": 1.50, "retorno": 3.37}}
  ]
}}
Use null quando não conseguir ler um campo. Seja tolerante com formatos diferentes."""

    resp = openrouter_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {"role": "system", "content": "You are a JSON extraction assistant. Respond ONLY with valid JSON, no thinking, no explanation, no markdown."},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{img_b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ],
        max_tokens=900,
    )

    vazio = {"esporte": None, "tipo": "simples", "moeda": "BRL", "odd_total": None, "selecoes": []}
    if not resp or not resp.choices:
        return vazio

    parsed = _extrair_json(resp.choices[0].message.content or "")
    if not isinstance(parsed, dict):
        return vazio

    parsed.setdefault("esporte", None)
    parsed.setdefault("tipo", "simples")
    parsed.setdefault("moeda", "BRL")
    parsed.setdefault("odd_total", None)
    parsed.setdefault("selecoes", [])
    if not isinstance(parsed["selecoes"], list):
        parsed["selecoes"] = []
    if not parsed.get("moeda"):
        parsed["moeda"] = "BRL"
    parsed["moeda"] = str(parsed["moeda"]).upper()
    return parsed

# ══════════════════════════════════════════════════════════════════
# PARSING FLEXÍVEL DE DATA
# ══════════════════════════════════════════════════════════════════
def parsear_data(texto: str):
    texto = (texto or "").strip().lower()
    hoje = agora()
    if texto in ("hoje", "h"):
        return hoje.strftime("%Y-%m-%d")
    if texto in ("ontem", "o"):
        return (hoje - timedelta(days=1)).strftime("%Y-%m-%d")
    for sep in ("/", "-"):
        if sep in texto:
            partes = texto.split(sep)
            if len(partes) == 2:
                dia, mes = partes
                ano = agora().year
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
# FIRESTORE
# ══════════════════════════════════════════════════════════════════
def gravar_aposta(esp, ap, odd, stake, casa, dat, autor, autor_id, grupo=None, tipo="simples",
                   moeda="BRL", stake_original=None, cotacao=None) -> str:
    bet = {
        "dat": dat,
        "esp": esp or "Outros",
        "casa": casa,
        "ap": ap,
        "odd": odd,
        "stake": stake,  # SEMPRE em BRL — dashboard e P&L continuam sem mudar
        "res": "PENDENTE",
        "autor": autor,
        "autor_id": autor_id,
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    if grupo:
        bet["grupo"] = grupo
        bet["tipo"] = tipo
    if moeda and moeda != "BRL":
        bet["moeda"] = moeda
        bet["stake_original"] = stake_original
        bet["cotacao"] = cotacao  # cotação BRL usada no momento do registro
    ref = db.collection("users").document(FIREBASE_UID).collection("bets").add(bet)
    return ref[1].id

def buscar_pendentes(apenas_hoje: bool = True):
    bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
    docs = bets_col.where("res", "==", "PENDENTE").stream()
    pendentes = [(doc.id, doc.to_dict()) for doc in docs]
    if apenas_hoje:
        hoje = hoje_local()
        pendentes = [(bid, b) for bid, b in pendentes if b.get("dat") == hoje]
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
        "Me manda o print do bilhete que eu cadastro como pendente.\n"
        "Se for uma escada (mesma aposta em várias linhas), eu detecto e "
        "cadastro cada linha separada automaticamente.\n"
        "Também entendo apostas em cripto (Ł, ₿, Ξ) — converto pra R$ na "
        "cotação do momento."
    )

async def receber_print(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    await update.message.reply_text("🔎 Lendo o print...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    try:
        dados = extrair_dados_print(bytes(image_bytes), "image/jpeg")
    except Exception as e:
        log.exception("Erro ao ler print")
        await update.message.reply_text(f"⚠ Não consegui ler o print: {e}\nTenta um print mais nítido.")
        return ConversationHandler.END

    selecoes = [s for s in dados.get("selecoes", []) if isinstance(s, dict) and s.get("odd")]
    if not selecoes:
        await update.message.reply_text(
            "⚠ Não identifiquei os dados nesse print.\n"
            "Manda um print mais nítido, mostrando o jogo e a odd."
        )
        return ConversationHandler.END

    esp = dados.get("esporte") or "Outros"
    tipo = (dados.get("tipo") or "simples").lower()
    moeda = (dados.get("moeda") or "BRL").upper()
    autor, autor_id = nome_autor(update)

    context.user_data["esp"] = esp
    context.user_data["autor"] = autor
    context.user_data["autor_id"] = autor_id
    context.user_data["moeda"] = moeda

    # se for cripto, já busca a cotação agora — usa pra tudo que vier depois
    cotacao = None
    if moeda != "BRL":
        cotacao = obter_cotacao_brl(moeda)
        if cotacao is None:
            await update.message.reply_text(
                f"⚠ Identifiquei uma aposta em {moeda}, mas não consegui buscar a "
                f"cotação agora. Tenta reenviar o print em instantes."
            )
            return ConversationHandler.END
        context.user_data["cotacao"] = cotacao

    escada = tipo in ("escada", "simples") and len(selecoes) >= 2
    todas_tem_stake = all(s.get("stake") not in (None, "", 0) for s in selecoes)

    # ---- CAMINHO 1: escada / várias simples com valores no print ----
    if escada and todas_tem_stake:
        context.user_data["modo"] = "grupo"
        context.user_data["tipo"] = tipo

        # converte cada linha pra BRL se for cripto, mantendo o valor original
        for s in selecoes:
            s["stake"] = float(s["stake"])
            s["stake_brl"] = round(s["stake"] * cotacao, 2) if moeda != "BRL" else s["stake"]
        context.user_data["selecoes"] = selecoes

        if moeda != "BRL":
            linhas = "\n".join(
                f"• {s['descricao']} @{s['odd']} — {fmt_cripto(s['stake'])} {moeda} (≈ R$ {s['stake_brl']:.2f})"
                for s in selecoes
            )
            total_nativo = sum(s["stake"] for s in selecoes)
            total_brl = sum(s["stake_brl"] for s in selecoes)
            rodape = (f"💱 Cotação usada: R$ {cotacao:.2f} / {moeda}\n"
                      f"Total: {fmt_cripto(total_nativo)} {moeda} ≈ R$ {total_brl:.2f}")
        else:
            linhas = "\n".join(
                f"• {s['descricao']} @{s['odd']} — R$ {s['stake']:.2f}" for s in selecoes
            )
            total = sum(s["stake"] for s in selecoes)
            rodape = f"Stake total: R$ {total:.2f}"

        await update.message.reply_text(
            f"🪜 Identifiquei uma *{tipo}* com {len(selecoes)} linhas:\n\n"
            f"{linhas}\n\n{rodape}\n\n🏦 Em qual casa de apostas?",
            parse_mode="Markdown",
        )
        return AGUARDANDO_CASA

    # ---- caso escada mas sem os valores por linha: não dá pra inferir ----
    if escada and not todas_tem_stake:
        await update.message.reply_text(
            "🪜 Parece uma escada (mesma aposta em várias linhas), mas não "
            "consegui ler o valor de CADA linha.\n"
            "Manda um print onde apareça o valor apostado em cada seleção."
        )
        return ConversationHandler.END

    # ---- CAMINHO 2: aposta única (simples de 1 linha ou múltipla) ----
    context.user_data["modo"] = "unico"
    if tipo == "multipla" and len(selecoes) >= 2:
        odd = dados.get("odd_total") or round(prod(float(s["odd"]) for s in selecoes), 2)
        ap = " + ".join(s["descricao"] for s in selecoes)
        context.user_data["tipo"] = "multipla"
    else:
        odd = float(selecoes[0]["odd"])
        ap = selecoes[0]["descricao"]
        context.user_data["tipo"] = "simples"

    context.user_data["ap"] = ap
    context.user_data["odd"] = float(odd)

    stake_no_print = selecoes[0].get("stake")
    resumo_base = (
        f"✅ Identifiquei:\n\n"
        f"🏅 Esporte: {esp}\n"
        f"🎯 Aposta: {ap}\n"
        f"📈 Odd: {context.user_data['odd']}\n"
    )

    if stake_no_print not in (None, "", 0):
        # o valor já veio no print — não precisa perguntar
        stake_original = float(stake_no_print)
        if moeda != "BRL":
            stake_brl = round(stake_original * cotacao, 2)
            context.user_data["stake"] = stake_brl
            context.user_data["stake_original"] = stake_original
            resumo_base += (
                f"💰 Valor: {fmt_cripto(stake_original)} {moeda} ≈ R$ {stake_brl:.2f} "
                f"(cotação R$ {cotacao:.2f})\n"
            )
        else:
            context.user_data["stake"] = stake_original
            resumo_base += f"💰 Valor: R$ {stake_original:.2f}\n"
        await update.message.reply_text(resumo_base + "\n🏦 Em qual casa de apostas?")
        return AGUARDANDO_CASA

    # valor não veio no print — pergunta, já na moeda certa
    if moeda != "BRL":
        resumo_base += f"\n💰 Quanto você apostou? (em {moeda}, ex: 0.05)"
    else:
        resumo_base += "\n💰 Quanto você apostou? (só o número, ex: 50)"
    await update.message.reply_text(resumo_base)
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

    moeda = context.user_data.get("moeda", "BRL")
    if moeda != "BRL":
        cotacao = context.user_data.get("cotacao") or obter_cotacao_brl(moeda)
        if cotacao is None:
            await update.message.reply_text(
                f"⚠ Não consegui buscar a cotação de {moeda} agora. Tenta de novo em instantes."
            )
            return ConversationHandler.END
        context.user_data["cotacao"] = cotacao
        stake_brl = round(valor * cotacao, 2)
        context.user_data["stake"] = stake_brl
        context.user_data["stake_original"] = valor
        await update.message.reply_text(
            f"💱 {fmt_cripto(valor)} {moeda} ≈ R$ {stake_brl:.2f} (cotação R$ {cotacao:.2f})\n\n"
            f"🏦 Em qual casa de apostas?"
        )
    else:
        context.user_data["stake"] = valor
        await update.message.reply_text("🏦 Em qual casa de apostas?")
    return AGUARDANDO_CASA

async def receber_casa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    context.user_data["casa"] = update.message.text.strip()
    hoje_fmt = agora().strftime("%d/%m")
    await update.message.reply_text(
        f"📅 Qual o dia da aposta?\nManda 'hoje', 'ontem', ou a data (ex: {hoje_fmt})"
    )
    return AGUARDANDO_DATA

async def receber_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    dat = parsear_data(update.message.text)
    if not dat:
        await update.message.reply_text(
            "⚠ Não entendi a data. Manda 'hoje', 'ontem', ou dd/mm (ex: 17/06)"
        )
        return AGUARDANDO_DATA

    d = context.user_data
    d["dat"] = dat
    data_fmt = datetime.strptime(dat, "%Y-%m-%d").strftime("%d/%m/%Y")
    moeda = d.get("moeda", "BRL")

    try:
        if d.get("modo") == "grupo":
            # ESCADA: cada linha vira uma aposta, todas com o mesmo grupo
            grupo = str(uuid.uuid4())[:8]
            linhas_txt = []
            for s in d["selecoes"]:
                gravar_aposta(
                    d["esp"], s["descricao"], float(s["odd"]), float(s["stake_brl"]),
                    d["casa"], dat, d["autor"], d["autor_id"],
                    grupo=grupo, tipo=d["tipo"],
                    moeda=moeda,
                    stake_original=float(s["stake"]) if moeda != "BRL" else None,
                    cotacao=d.get("cotacao") if moeda != "BRL" else None,
                )
                if moeda != "BRL":
                    linhas_txt.append(
                        f"• {s['descricao']} @{s['odd']} — {fmt_cripto(s['stake'])} {moeda} (R$ {s['stake_brl']:.2f})"
                    )
                else:
                    linhas_txt.append(f"• {s['descricao']} @{s['odd']} — R$ {s['stake_brl']:.2f}")

            total_brl = sum(s["stake_brl"] for s in d["selecoes"])
            rodape = f"💰 Stake total: R$ {total_brl:.2f}"
            if moeda != "BRL":
                total_nativo = sum(s["stake"] for s in d["selecoes"])
                rodape = (f"💰 Stake total: {fmt_cripto(total_nativo)} {moeda} "
                          f"≈ R$ {total_brl:.2f} (cotação R$ {d['cotacao']:.2f})")

            await update.message.reply_text(
                f"🎉 Escada cadastrada ({len(d['selecoes'])} linhas) como PENDENTE!\n\n"
                + "\n".join(linhas_txt)
                + f"\n\n{rodape}\n🏦 {d['casa']}\n📅 {data_fmt}\n👤 {d['autor']}\n\n"
                f"Resolve cada linha no dashboard (elas ganham/perdem separadas)."
            )
        else:
            # aposta única (simples/múltipla)
            bet_id = gravar_aposta(
                d["esp"], d["ap"], d["odd"], d["stake"], d["casa"], dat,
                d["autor"], d["autor_id"], tipo=d.get("tipo", "simples"),
                moeda=moeda,
                stake_original=d.get("stake_original") if moeda != "BRL" else None,
                cotacao=d.get("cotacao") if moeda != "BRL" else None,
            )
            valor_linha = f"💰 R$ {d['stake']:.2f}"
            if moeda != "BRL":
                valor_linha = (f"💰 {fmt_cripto(d['stake_original'])} {moeda} "
                               f"≈ R$ {d['stake']:.2f} (cotação R$ {d['cotacao']:.2f})")
            botao = [[InlineKeyboardButton("✏️ Editar aposta", callback_data=f"editar|{bet_id}")]]
            await update.message.reply_text(
                f"🎉 Aposta cadastrada como PENDENTE!\n\n"
                f"🏅 {d['esp']}\n🎯 {d['ap']}\n📈 Odd {d['odd']}\n"
                f"{valor_linha}\n🏦 {d['casa']}\n📅 {data_fmt}\n👤 {d['autor']}\n\n"
                f"Resolve ela (Green/Red/Void) no dashboard.",
                reply_markup=InlineKeyboardMarkup(botao),
            )
    except Exception as e:
        log.exception("Erro ao gravar no Firestore")
        await update.message.reply_text(f"⚠ Erro ao salvar no dashboard: {e}")
        return ConversationHandler.END

    context.user_data.clear()
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Cadastro cancelado.")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════
# EDITAR APOSTA (só no privado — no grupo, edite pelo dashboard)
# ══════════════════════════════════════════════════════════════════
CAMPOS_EDITAVEIS = {
    "esp": "🏅 Esporte", "ap": "🎯 Aposta", "odd": "📈 Odd",
    "stake": "💰 Valor (R$)", "casa": "🏦 Casa", "dat": "📅 Data",
}

async def callback_editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not chat_liberado(query.message.chat.id):
        return
    await query.answer()
    _, bet_id = query.data.split("|", 1)
    context.user_data["edit_bet_id"] = bet_id
    botoes = [[InlineKeyboardButton(label, callback_data=f"campo|{campo}")]
              for campo, label in CAMPOS_EDITAVEIS.items()]
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="campo|cancelar")])
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("✏️ O que você quer editar?", reply_markup=InlineKeyboardMarkup(botoes))

async def callback_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not chat_liberado(query.message.chat.id):
        return
    await query.answer()
    campo = query.data.split("|", 1)[1]
    if campo == "cancelar":
        await query.edit_message_text("Edição cancelada.")
        context.user_data.pop("edit_bet_id", None)
        return
    context.user_data["edit_campo"] = campo
    label = CAMPOS_EDITAVEIS.get(campo, campo)
    await query.edit_message_text(f"✏️ Novo valor para *{label}*:", parse_mode="Markdown")

async def _aplicar_edicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bet_id = context.user_data.pop("edit_bet_id")
    campo = context.user_data.pop("edit_campo")
    novo = update.message.text.strip()
    try:
        if campo == "odd":
            novo = float(novo.replace(",", "."))
        elif campo == "stake":
            # edição de stake é sempre em R$ direto (valor já congelado no dashboard)
            novo = float(novo.replace(",", ".").replace("R$", "").strip())
        elif campo == "dat":
            novo = parsear_data(novo)
            if not novo:
                await update.message.reply_text("⚠ Data inválida. Tenta 'hoje', 'ontem' ou dd/mm.")
                return
    except ValueError:
        await update.message.reply_text("⚠ Valor inválido. Tenta de novo.")
        return
    try:
        bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
        bets_col.document(bet_id).update({campo: novo})
        await update.message.reply_text(f"✅ *{CAMPOS_EDITAVEIS.get(campo, campo)}* atualizado!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠ Erro ao atualizar: {e}")

async def texto_avulso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Texto solto FORA de conversa. Só age no privado, pra não poluir o grupo."""
    if not autorizado(update):
        return
    if update.effective_chat.type != "private":
        return
    if "edit_bet_id" in context.user_data and "edit_campo" in context.user_data:
        await _aplicar_edicao(update, context)
        return
    await update.message.reply_text("Me manda um print do bilhete pra eu cadastrar! 📸")

# ══════════════════════════════════════════════════════════════════
# RESOLVER PENDENTES (Mini App)
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
    if not autorizado(update):
        return
    pendentes = buscar_pendentes()
    n = len(pendentes)
    if not n:
        await update.message.reply_text("✅ Nenhuma aposta pendente hoje!")
        return
    from telegram import WebAppInfo
    webapp_url = f"{RENDER_EXTERNAL_URL}/webapp?token={TELEGRAM_TOKEN}"
    botao = [[InlineKeyboardButton(f"⚡ Resolver apostas ({n} hoje)", web_app=WebAppInfo(url=webapp_url))]]
    await update.message.reply_text(
        f"📋 *{n} aposta{'s' if n!=1 else ''} pendente{'s' if n!=1 else ''} hoje*\nAbra o menu para resolver:",
        reply_markup=InlineKeyboardMarkup(botao),
        parse_mode="Markdown",
    )

async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    from telegram import WebAppInfo
    webapp_url = f"{RENDER_EXTERNAL_URL}/dia-app"
    botao = [[InlineKeyboardButton("📊 Ver resumo do dia", web_app=WebAppInfo(url=webapp_url))]]
    await update.message.reply_text("📊 *Resumo do dia*", reply_markup=InlineKeyboardMarkup(botao), parse_mode="Markdown")

async def callback_resolver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not chat_liberado(query.message.chat.id):
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
    try:
        stake = float(b.get("stake", 0)); odd = float(b.get("odd", 0))
        if resultado == "GREEN":
            texto += f"\n💰 Lucro: +R$ {stake * (odd - 1):.2f}"
        elif resultado == "RED":
            texto += f"\n💸 Prejuízo: -R$ {stake:.2f}"
        elif resultado == "VOID":
            texto += f"\n↩️ Stake devolvida: R$ {stake:.2f}"
    except (TypeError, ValueError):
        pass
    await query.edit_message_text(texto, parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════
# MAIN — servidor webhook com aiohttp
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
        # per_user/per_chat True por padrão -> no grupo, cada pessoa tem
        # a sua própria conversa em paralelo, sem misturar.
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("resolver", cmd_resolver))
    app.add_handler(CommandHandler("resumo", cmd_resumo))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_resolver, pattern=r"^resolve\|"))
    app.add_handler(CallbackQueryHandler(callback_editar, pattern=r"^editar\|"))
    app.add_handler(CallbackQueryHandler(callback_campo, pattern=r"^campo\|"))
    # texto solto só no privado (edição / mensagem não reconhecida)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, texto_avulso))

    port = int(os.environ.get("PORT", 10000))
    webhook_path = "/" + TELEGRAM_TOKEN
    webhook_url = f"{RENDER_EXTERNAL_URL}{webhook_path}"

    async def handle_webhook(request: web.Request) -> web.Response:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response()

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(text="SuperOdds Bot rodando!")

    async def handle_webapp(request: web.Request) -> web.Response:
        import os as _os
        html_path = _os.path.join(_os.path.dirname(__file__), "webapp.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html", headers={"Access-Control-Allow-Origin": "*"})

    async def handle_pendentes(request: web.Request) -> web.Response:
        if request.rel_url.query.get("token") != TELEGRAM_TOKEN:
            return web.Response(status=403, text="Forbidden")
        hoje = hoje_local()
        bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
        docs = bets_col.where("res", "==", "PENDENTE").stream()
        pendentes = []
        for doc in docs:
            d = doc.to_dict()
            if d.get("dat") == hoje:
                d["id"] = doc.id
                d.pop("createdAt", None)
                pendentes.append(d)
        pendentes.sort(key=lambda x: x.get("dat", ""), reverse=True)
        return web.json_response(pendentes, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_resolve(request: web.Request) -> web.Response:
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
        cor = {"GREEN": "🟢", "RED": "🔴", "VOID": "⚪"}.get(resultado, "")
        resumo = formatar_resumo_aposta(b) if b else ""
        msg = f"{cor} *{resultado}*\n{resumo}" if resumo else f"{cor} Aposta marcada como {resultado}!"
        try:
            stake = float(b.get("stake", 0)); odd = float(b.get("odd", 0))
            if resultado == "GREEN":
                msg += f"\n💰 Lucro: +R$ {stake * (odd - 1):.2f}"
            elif resultado == "RED":
                msg += f"\n💸 Prejuízo: -R$ {stake:.2f}"
            elif resultado == "VOID":
                msg += f"\n↩️ Stake devolvida: R$ {stake:.2f}"
        except (TypeError, ValueError):
            pass
        try:
            await app.bot.send_message(CHAT_NOTIFICACAO, msg, parse_mode="Markdown")
        except Exception:
            pass
        return web.json_response({"ok": True, "msg": f"{cor} {resultado}!"}, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_resolve_multi_v2(request: web.Request) -> web.Response:
        if request.rel_url.query.get("token") != TELEGRAM_TOKEN:
            return web.Response(status=403, text="Forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="JSON inválido")
        bets_col = db.collection("users").document(FIREBASE_UID).collection("bets")
        green_lucro = red_lucro = 0.0
        green_n = red_n = void_n = 0
        for bet_id, resultado in data.items():
            if resultado not in ("GREEN", "RED", "VOID"):
                continue
            doc = bets_col.document(bet_id).get()
            b = doc.to_dict() if doc.exists else {}
            bets_col.document(bet_id).update({"res": resultado})
            try:
                stake = float(b.get("stake", 0) or 0); odd = float(b.get("odd", 0) or 0)
                if resultado == "GREEN":
                    green_lucro += stake * (odd - 1); green_n += 1
                elif resultado == "RED":
                    red_lucro -= stake; red_n += 1
                elif resultado == "VOID":
                    void_n += 1
            except (TypeError, ValueError):
                pass
        linhas = ["✅ *Apostas resolvidas!*"]
        if green_n: linhas.append(f"🟢 {green_n} GREEN · Lucro: +R$ {green_lucro:.2f}")
        if red_n: linhas.append(f"🔴 {red_n} RED · Prejuízo: -R$ {abs(red_lucro):.2f}")
        if void_n: linhas.append(f"⚪ {void_n} VOID")
        total = green_lucro + red_lucro
        if green_n or red_n:
            sinal = "+" if total >= 0 else ""
            linhas.append(f"\n💰 Resultado líquido: *{sinal}R$ {total:.2f}*")
        try:
            await app.bot.send_message(CHAT_NOTIFICACAO, "\n".join(linhas), parse_mode="Markdown")
        except Exception:
            pass
        return web.json_response(
            {"ok": True, "msg": f"✅ {green_n+red_n+void_n} apostas salvas!"},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def handle_dia_app(request: web.Request) -> web.Response:
        import os as _os
        html_path = _os.path.join(_os.path.dirname(__file__), "resumo.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html", headers={"Access-Control-Allow-Origin": "*"})

    web_app = web.Application()
    web_app.router.add_post(webhook_path, handle_webhook)
    web_app.router.add_get("/", handle_health)
    web_app.router.add_get("/webapp", handle_webapp)
    web_app.router.add_get("/dia-app", handle_dia_app)
    web_app.router.add_get("/pendentes", handle_pendentes)
    web_app.router.add_get("/resolve", handle_resolve)
    web_app.router.add_post("/resolve-multi-v2", handle_resolve_multi_v2)

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
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await app.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot())
