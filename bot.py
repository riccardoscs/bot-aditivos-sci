"""
Autorizacao unica - Bot de Aditivos Santa Cruz
-------------------------------------------------
Rode este script UMA VEZ no seu computador. Ele autoriza o bot a
enviar fotos e audios para o SEU Google Drive, usando a sua propria
conta Google (que tem espaco de armazenamento de verdade) em vez da
conta de servico (que nao tem).

Antes de rodar, instale a dependencia (uma vez so):

    pip3 install google-auth-oauthlib

Depois rode:

    python3 autorizar.py

O script vai pedir o Client ID e o Client Secret (voce pega no Google
Cloud Console, em Credenciais, no OAuth Client ID do tipo "Desktop
app" que voce criou). Em seguida abre uma aba no navegador - faca
login com a MESMA conta Google que e dona da planilha e da pasta do
Drive, e autorize o acesso.

No final, ele mostra um "refresh token" - copie esse valor e cole na
variavel GOOGLE_OAUTH_REFRESH_TOKEN no Railway (junto com o Client ID
e o Client Secret que voce usou aqui).
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def main() -> None:
    client_id = input("Cole aqui o Client ID: ").strip()
    client_secret = input("Cole aqui o Client Secret: ").strip()

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n" + "=" * 64)
    print("Autorizacao concluida!")
    print("Copie o valor abaixo e cole na variavel GOOGLE_OAUTH_REFRESH_TOKEN")
    print("no Railway (junto com GOOGLE_OAUTH_CLIENT_ID e")
    print("GOOGLE_OAUTH_CLIENT_SECRET, com os mesmos valores que voce usou")
    print("agora):\n")
    print(creds.refresh_token)
    print("=" * 64)


if __name__ == "__main__":
    main()

"""
Bot de captura de aditivos - Santa Cruz Instalações
-----------------------------------------------------
Recebe texto, foto e áudio direto do Telegram (enviados da obra),
registra tudo de forma estruturada numa planilha Google Sheets e
guarda os arquivos brutos (fotos/áudios) organizados por obra no
Google Drive. Também oferece uma sugestão rápida de preço com base
na faixa de R$/m² que a empresa já pratica.

Veja o README.md deste pacote para o passo a passo completo de
configuração (Telegram, Google Cloud, planilha, pasta no Drive) e
de deploy (Railway).
"""

import os
import io
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.service_account import Credentials
import gspread

# ------------------------------------------------------------------
# Configuração (tudo vem de variáveis de ambiente - ver .env.example)
# ------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
DRIVE_ROOT_FOLDER_ID = os.environ["DRIVE_ROOT_FOLDER_ID"]

# Opcional: restringe o bot a determinados usuários do Telegram
# (ids numéricos separados por vírgula). Deixe em branco para
# liberar para qualquer pessoa que tiver o link do bot.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}

TZ = ZoneInfo("America/Sao_Paulo")

# Faixa de preço por m² que a Santa Cruz já pratica hoje.
# PRECO_MIN_M2 = ambiente 100% seco / PRECO_MAX_M2 = 100% área molhada.
PRECO_MIN_M2 = float(os.environ.get("PRECO_MIN_M2", "950"))
PRECO_MAX_M2 = float(os.environ.get("PRECO_MAX_M2", "1400"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("bot-aditivos-sci")

HEADERS = ["Data/Hora", "Obra", "Tipo", "Descrição", "Autor", "Link"]


# ------------------------------------------------------------------
# Autenticação Google (uma única conta de serviço para Sheets + Drive)
# ------------------------------------------------------------------

def get_credentials() -> Credentials:
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


def get_drive_credentials():
    """
    Contas de serviço não têm cota de armazenamento no Google Drive
    (só conseguem editar arquivos já existentes, como a planilha, ou
    criar pastas, que não ocupam espaço). Para enviar fotos/áudios de
    verdade, é preciso usar as credenciais OAuth da sua própria conta
    Google (que tem espaço de sobra) - veja autorizar.py.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if client_id and client_secret and refresh_token:
        from google.oauth2.credentials import Credentials as UserCredentials

        return UserCredentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
    logger.warning(
        "GOOGLE_OAUTH_* não configurado - uploads de foto/áudio para o "
        "Drive vão falhar (contas de serviço não têm cota de "
        "armazenamento). Rode autorizar.py para configurar."
    )
    return CREDS


CREDS = get_credentials()
GC = gspread.authorize(CREDS)
DRIVE_CREDS = get_drive_credentials()
DRIVE = build("drive", "v3", credentials=DRIVE_CREDS)
SHEET = GC.open_by_key(SPREADSHEET_ID).sheet1


def ensure_headers() -> None:
    values = SHEET.row_values(1)
    if values != HEADERS:
        SHEET.update("A1", [HEADERS])


# ------------------------------------------------------------------
# Estado simples: qual é a "obra ativa" de cada conversa do Telegram
# ------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


STATE = load_state()


def get_obra_ativa(chat_id: int):
    return STATE.get(str(chat_id))


def set_obra_ativa(chat_id: int, obra: str) -> None:
    STATE[str(chat_id)] = obra
    save_state(STATE)


# ------------------------------------------------------------------
# Google Drive: uma pasta por obra, dentro da pasta raiz configurada
# ------------------------------------------------------------------

def get_or_create_folder(name: str, parent_id: str) -> str:
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    res = DRIVE.files().list(q=query, fields="files(id, name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = DRIVE.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def get_obra_folder(obra: str) -> str:
    return get_or_create_folder(obra, DRIVE_ROOT_FOLDER_ID)


def upload_bytes(file_bytes: bytes, filename: str, mimetype: str, folder_id: str) -> str:
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=False)
    metadata = {"name": filename, "parents": [folder_id]}
    file = (
        DRIVE.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return file.get("webViewLink", "")


# ------------------------------------------------------------------
# Registro na planilha
# ------------------------------------------------------------------

def registrar(obra: str, tipo: str, descricao: str, autor: str, link: str = "") -> None:
    ensure_headers()
    agora = datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
    SHEET.append_row(
        [agora, obra, tipo, descricao, autor, link], value_input_option="USER_ENTERED"
    )


def autorizado(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS


# ------------------------------------------------------------------
# Handlers dos comandos e mensagens do Telegram
# ------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Oi! Eu sou o bot de aditivos da Santa Cruz Instalações.\n\n"
        "1) Use /obra <nome da obra> para definir a obra ativa deste chat.\n"
        "2) Depois é só mandar texto, foto ou áudio - eu registro tudo "
        "organizado na planilha e no Drive, na pasta da obra.\n"
        "3) Use /preco <m2> <% área molhada> para uma sugestão de valor "
        "baseada na faixa que vocês já praticam.\n\n"
        "Use /ajuda a qualquer momento para ver esses comandos de novo."
    )


async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def cmd_obra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    if not context.args:
        atual = get_obra_ativa(update.effective_chat.id)
        msg = f"Obra ativa: {atual}" if atual else "Nenhuma obra ativa. Use /obra <nome>."
        await update.message.reply_text(msg)
        return
    nome = " ".join(context.args)
    set_obra_ativa(update.effective_chat.id, nome)
    get_obra_folder(nome)  # já garante que a pasta existe no Drive
    await update.message.reply_text(
        f"Obra ativa definida: {nome}\nAgora é só mandar texto, foto ou áudio."
    )


async def cmd_preco(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    try:
        m2 = float(context.args[0].replace(",", "."))
        pct_molhada = float(context.args[1].replace(",", "."))
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Uso: /preco <m2> <% área molhada, de 0 a 100>\nEx: /preco 120 35"
        )
        return

    pct = max(0.0, min(100.0, pct_molhada)) / 100.0
    preco_m2 = PRECO_MIN_M2 + (PRECO_MAX_M2 - PRECO_MIN_M2) * pct
    total = preco_m2 * m2

    await update.message.reply_text(
        "Sugestão de valor para o aditivo:\n"
        f"R$ {preco_m2:,.2f}/m² x {m2:.1f} m² = R$ {total:,.2f}\n\n"
        f"(faixa base R$ {PRECO_MIN_M2:.0f}-{PRECO_MAX_M2:.0f}/m² conforme % de área molhada, "
        "ajuste manualmente se o caso tiver alguma particularidade)"
    )

    obra = get_obra_ativa(update.effective_chat.id) or "Sem obra definida"
    registrar(
        obra,
        "Sugestão de preço",
        f"{m2}m², {pct_molhada}% molhada -> R$ {total:,.2f}",
        update.effective_user.first_name,
    )


async def texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    obra = get_obra_ativa(update.effective_chat.id)
    if not obra:
        await update.message.reply_text("Defina a obra ativa primeiro com /obra <nome>.")
        return
    registrar(obra, "Texto", update.message.text, update.effective_user.first_name)
    await update.message.reply_text("Registrado ✅")


async def foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    obra = get_obra_ativa(update.effective_chat.id)
    if not obra:
        await update.message.reply_text("Defina a obra ativa primeiro com /obra <nome>.")
        return

    tg_file = await update.message.photo[-1].get_file()
    file_bytes = await tg_file.download_as_bytearray()
    filename = f"foto_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
    folder_id = get_obra_folder(obra)
    link = upload_bytes(bytes(file_bytes), filename, "image/jpeg", folder_id)

    legenda = update.message.caption or ""
    registrar(obra, "Foto", legenda, update.effective_user.first_name, link)
    await update.message.reply_text("Foto salva na pasta da obra ✅")


async def audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    obra = get_obra_ativa(update.effective_chat.id)
    if not obra:
        await update.message.reply_text("Defina a obra ativa primeiro com /obra <nome>.")
        return

    voice_or_audio = update.message.voice or update.message.audio
    tg_file = await voice_or_audio.get_file()
    file_bytes = await tg_file.download_as_bytearray()
    filename = f"audio_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.ogg"
    folder_id = get_obra_folder(obra)
    link = upload_bytes(bytes(file_bytes), filename, "audio/ogg", folder_id)

    registrar(obra, "Áudio", "", update.effective_user.first_name, link)
    await update.message.reply_text("Áudio salvo na pasta da obra ✅")


def main() -> None:
    ensure_headers()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("obra", cmd_obra))
    app.add_handler(CommandHandler("preco", cmd_preco))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto))
    app.add_handler(MessageHandler(filters.PHOTO, foto))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, audio))

    logger.info("Bot iniciado, aguardando mensagens...")
    app.run_polling()


if __name__ == "__main__":
    main()

# Bot de Aditivos - Santa Cruz Instalações

Bot de Telegram para capturar, direto da obra, informações sobre
aditivos (texto, foto, áudio) e organizar tudo automaticamente:

- Cada mensagem vira uma linha numa planilha Google Sheets central
  (data/hora, obra, tipo, descrição, autor, link do arquivo).
- Cada foto/áudio é salvo no Google Drive, numa pasta própria da obra.
- O comando `/preco` dá uma sugestão rápida de valor com base na
  faixa de R$/m² que a empresa já pratica (R$950-1.400/m², conforme
  % de área molhada).

Este README é o passo a passo completo, do zero, para colocar o bot
no ar. Nenhuma etapa exige conhecimento técnico avançado - é só
seguir na ordem.

---

## Visão geral do que você vai criar

1. Um bot no Telegram (grátis, via BotFather).
2. Uma "conta de serviço" no Google Cloud (grátis) - é como um
   usuário robô que tem permissão para escrever na planilha e na
   pasta do Drive.
3. Uma planilha Google Sheets e uma pasta no Google Drive,
   compartilhadas com esse usuário robô.
4. Uma hospedagem gratuita/barata (Railway) para o bot ficar rodando
   24 horas por dia.

---

## Passo 1 - Criar o bot no Telegram

1. Abra o Telegram e procure por **@BotFather**.
2. Envie `/newbot`.
3. Escolha um nome de exibição (ex: "Aditivos Santa Cruz") e um
   username terminado em "bot" (ex: `sci_aditivos_bot`).
4. O BotFather vai te dar um **token** (algo como
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`). Guarde esse
   valor - ele vai no `TELEGRAM_BOT_TOKEN`.

(Opcional) Para descobrir o seu próprio ID numérico do Telegram, e
assim poder restringir o bot só para a sua equipe, fale com
**@userinfobot** - ele responde com o seu ID.

---

## Passo 2 - Criar a conta de serviço no Google Cloud

1. Acesse https://console.cloud.google.com/ e crie um projeto novo
   (ex: "SCI Aditivos").
2. No menu "APIs e serviços" > "Biblioteca", ative:
   - **Google Sheets API**
   - **Google Drive API**
3. Vá em "APIs e serviços" > "Credenciais" > "Criar credenciais" >
   **Conta de serviço**. Dê um nome (ex: `bot-aditivos`) e conclua.
4. Clique na conta de serviço criada > aba "Chaves" > "Adicionar
   chave" > "Criar nova chave" > formato **JSON**. Isso baixa um
   arquivo `.json` - é a credencial que o bot vai usar.
5. Anote o "e-mail" dessa conta de serviço (algo como
   `bot-aditivos@sci-aditivos.iam.gserviceaccount.com`) - você vai
   precisar dele no próximo passo.

Para colocar o conteúdo desse JSON na variável `GOOGLE_CREDENTIALS_JSON`,
abra o arquivo baixado e copie o conteúdo inteiro (ele já vem numa
linha só, sem quebras) - é isso que vai colado na variável de
ambiente.

---

## Passo 3 - Criar a planilha e a pasta no Drive

1. Crie uma planilha nova no Google Sheets (pode chamar
   "Aditivos - Central"). Pegue o ID dela na URL:
   `.../spreadsheets/d/ESTE-PEDACO-AQUI/edit` → isso é o
   `SPREADSHEET_ID`.
2. Compartilhe essa planilha com o e-mail da conta de serviço
   (Passo 2.5), com permissão de **Editor**.
3. Crie uma pasta no Google Drive (ex: "Aditivos - Obras"). Pegue o
   ID dela na URL: `.../drive/folders/ESTE-PEDACO-AQUI` → isso é o
   `DRIVE_ROOT_FOLDER_ID`.
4. Compartilhe essa pasta também com o e-mail da conta de serviço,
   com permissão de **Editor**.

O bot cria automaticamente uma subpasta para cada obra dentro dessa
pasta raiz, na primeira vez que você usar `/obra <nome>`.

---

## Passo 4 - Testar localmente (opcional, mas recomendado)

Se quiser testar no seu computador antes de colocar no ar:

```bash
cd bot-aditivos-sci
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edite o .env preenchendo os valores dos Passos 1-3
```

Depois, carregue as variáveis do `.env` (por exemplo com o pacote
`python-dotenv`, ou exportando manualmente) e rode:

```bash
python bot.py
```

Se tudo estiver certo, o bot fica escutando. Mande `/start` para ele
no Telegram.

---

## Passo 5 - Deploy no Railway (hospedagem 24h)

O Railway (https://railway.app) tem um plano de teste gratuito e é
simples de configurar - sem precisar mexer em servidor.

1. Crie uma conta em https://railway.app (dá pra entrar com GitHub).
2. Suba esta pasta (`bot-aditivos-sci`) para um repositório novo no
   seu GitHub.
3. No Railway, clique em "New Project" > "Deploy from GitHub repo" e
   escolha esse repositório.
4. O Railway detecta o `Procfile` e cria automaticamente um serviço
   do tipo **worker** (processo contínuo, sem precisar de site).
5. Vá em "Variables" e cadastre cada variável do `.env.example`
   com os valores reais que você anotou nos Passos 1-3:
   - `TELEGRAM_BOT_TOKEN`
   - `SPREADSHEET_ID`
   - `DRIVE_ROOT_FOLDER_ID`
   - `GOOGLE_CREDENTIALS_JSON` (cole o JSON inteiro)
   - `ALLOWED_USER_IDS` (opcional)
   - `PRECO_MIN_M2` / `PRECO_MAX_M2` (opcional, já vem com 950/1400)
6. O Railway faz o deploy automaticamente. Depois de alguns segundos,
   mande `/start` para o bot no Telegram - se responder, está no ar.

A partir daqui, qualquer atualização que você (ou eu, numa próxima
conversa) fizer no código e enviar pro GitHub, o Railway atualiza o
bot sozinho.

---

## Como usar no dia a dia

1. `/obra Gávea 60` - define a obra ativa deste chat (cria a pasta
   no Drive automaticamente).
2. Depois, é só mandar:
   - texto ("cliente pediu 2 tomadas a mais no quarto 3") → vira
     uma linha na planilha;
   - foto → sobe pro Drive na pasta da obra e loga na planilha
     (a legenda da foto, se tiver, também é salva);
   - áudio/mensagem de voz → mesma coisa, salvo como arquivo de
     áudio.
3. `/preco 120 35` - pede uma sugestão de valor para 120m² com 35%
   de área molhada, com base na faixa R$950-1.400/m². O resultado
   também é registrado na planilha, para ficar no histórico.

Depois, com os dados na planilha, é só me chamar (na conversa do
Claude) para montar a proposta de aditivo formal a partir desse
material - usando o modelo que já usamos para os outros aditivos.

---

## Próximos passos possíveis (quando quiser evoluir)

- Sugestão de preço mais precisa: hoje é um cálculo linear simples
  entre R$950 e R$1.400/m². Dá para refinar puxando a média de
  aditivos fechados anteriormente, já registrados na planilha.
- Transcrição automática de áudio (hoje o áudio só é salvo como
  arquivo; dá pra adicionar transcrição automática depois).
- Um comando `/resumo <obra>` que já traz tudo que foi registrado
  daquela obra, pronto para virar proposta.
- Trocar o `state.json` (que guarda a "obra ativa" de cada chat) por
  um banco de dados, para não depender do disco do servidor.

Qualquer uma dessas evoluções, é só pedir numa próxima conversa.

worker: python bot.py

python-telegram-bot==21.6
gspread==6.1.2
google-auth==2.34.0
google-api-python-client==2.146.0
