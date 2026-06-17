# SuperOdds Bot 🤖

Bot de Telegram que lê print de bilhete de aposta (via IA), pergunta o valor e a casa,
e cadastra automaticamente como **pendente** no mesmo Firestore usado pelo dashboard SuperOdds.

## Como funciona

1. Você manda um print do bilhete pro bot
2. Ele lê com IA (Claude) e identifica: esporte, jogo/mercado, odd
3. Pergunta quanto você apostou
4. Pergunta em qual casa
5. Salva no Firestore como `PENDENTE` — aparece automaticamente no dashboard

## O que você precisa antes de configurar

- [ ] Token do bot do Telegram (via @BotFather)
- [ ] Seu Chat ID pessoal (via @userinfobot)
- [ ] Uma API key da Anthropic (console.anthropic.com → API Keys)
- [ ] Seu UID do Firebase (o mesmo que aparece no badge do dashboard)
- [ ] Uma chave de serviço (service account) do Firebase

## Passo a passo

### 1. Gerar a chave de serviço do Firebase

1. Acesse o [console do Firebase](https://console.firebase.google.com) → seu projeto
2. ⚙️ Configurações do projeto → aba **Contas de serviço**
3. Clique em **Gerar nova chave privada** → confirma → baixa um arquivo `.json`
4. Esse arquivo tem todas as credenciais — guarde com segurança, nunca suba pro GitHub

### 2. Subir esse código no GitHub

1. Crie um repositório novo (ex: `superodds-bot`), separado do dashboard
2. Faça upload de `bot.py`, `requirements.txt` e `.gitignore`
3. **NÃO** suba o arquivo de credenciais do Firebase — ele vai direto numa variável de ambiente no Render

### 3. Criar o Web Service no Render

1. Acesse [render.com](https://render.com) → crie conta gratuita (sem cartão)
2. **New** → **Web Service** → conecte ao repositório `superodds-bot`
3. Configurações:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** Free

### 4. Configurar as variáveis de ambiente no Render

Na aba **Environment** do seu Web Service, adicione:

| Nome | Valor |
|---|---|
| `TELEGRAM_TOKEN` | o token do @BotFather |
| `ANTHROPIC_API_KEY` | sua API key da Anthropic |
| `ALLOWED_CHAT_ID` | seu Chat ID (número) |
| `FIREBASE_UID` | seu UID do Firebase |
| `FIREBASE_CREDENTIALS_JSON` | **todo o conteúdo** do arquivo `.json` da chave de serviço, colado como uma linha só |

### 5. Deploy

Clique em **Create Web Service** — o Render builda e inicia o bot automaticamente.
Depois de pronto, manda `/start` pro seu bot no Telegram pra testar!

## ⚠️ Importante sobre o plano gratuito do Render

O serviço "dorme" depois de 15 minutos sem uso. Isso é normal e gratuito — só significa
que a primeira mensagem depois de um tempo parado pode demorar ~1 minuto pra responder
(ele está "acordando"). Depois disso funciona normal até dormir de novo.

## Segurança

- O bot só responde ao `ALLOWED_CHAT_ID` configurado — nenhuma outra pessoa pode usá-lo
  mesmo que descubra o link, porque ele ignora qualquer chat diferente do seu
- A chave de serviço do Firebase nunca deve ser commitada no GitHub — sempre via variável
  de ambiente
