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
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
SPREADSHEET = GC.open_by_key(SPREADSHEET_ID)
SHEET = SPREADSHEET.sheet1


def ensure_headers() -> None:
    values = SHEET.row_values(1)
    if values != HEADERS:
        SHEET.update("A1", [HEADERS])


# ------------------------------------------------------------------
# Estado simples: qual é a "obra ativa" de cada conversa do Telegram.
#
# Fica guardado numa aba própria da mesma planilha (não num arquivo no
# servidor), porque o Railway apaga o disco a cada novo deploy - se
# guardássemos num arquivo local, toda atualização do bot faria ele
# "esquecer" a obra ativa de cada conversa.
# ------------------------------------------------------------------

ESTADO_ABA = "estado_bot"


def get_estado_sheet():
    try:
        return SPREADSHEET.worksheet(ESTADO_ABA)
    except gspread.exceptions.WorksheetNotFound:
        aba = SPREADSHEET.add_worksheet(title=ESTADO_ABA, rows=200, cols=2)
        aba.update("A1", [["chat_id", "obra_ativa"]])
        return aba


ESTADO_SHEET = get_estado_sheet()


def load_state() -> dict:
    try:
        linhas = ESTADO_SHEET.get_all_values()[1:]  # pula o cabeçalho
        return {linha[0]: linha[1] for linha in linhas if len(linha) >= 2 and linha[0]}
    except Exception:
        logger.exception("Falha ao carregar estado da planilha, começando vazio.")
        return {}


STATE = load_state()


def get_obra_ativa(chat_id: int):
    return STATE.get(str(chat_id))


def set_obra_ativa(chat_id: int, obra: str) -> None:
    chat_id_str = str(chat_id)
    STATE[chat_id_str] = obra
    cell = ESTADO_SHEET.find(chat_id_str, in_column=1)
    if cell:
        ESTADO_SHEET.update_cell(cell.row, 2, obra)
    else:
        ESTADO_SHEET.append_row([chat_id_str, obra], value_input_option="USER_ENTERED")


# ------------------------------------------------------------------
# Orçamento de material: rascunho de itens em andamento por chat, e
# percentuais sugeridos (o percentual que o material representa do
# valor total do aditivo).
# ------------------------------------------------------------------

ORCAMENTOS_ABERTOS: dict = {}  # chat_id -> lista de itens
ORCAMENTOS_PENDENTES: dict = {}  # chat_id -> {"itens": [...], "total_material": float}

PERCENTUAIS_SUGERIDOS = [15, 20, 25, 30, 35, 40, 45, 50]


def parse_numero(texto: str) -> float:
    """Converte '200m', 'R$ 2,50', '1.250,00', '4.500' etc. num float."""
    limpo = re.sub(r"(?i)r\$", "", texto).strip()
    limpo = re.sub(r"[^0-9.,]", "", limpo)
    if "," in limpo and "." in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    elif "," in limpo:
        limpo = limpo.replace(",", ".")
    elif "." in limpo:
        # Só ponto, sem vírgula: ambíguo entre decimal (4.50) e
        # separador de milhar ao estilo BR (4.500). Se o último
        # grupo depois do ponto tiver 3 dígitos, tratamos como
        # milhar; se tiver 1-2, como parte decimal.
        ultimo_grupo = limpo.rsplit(".", 1)[-1]
        if len(ultimo_grupo) == 3:
            limpo = limpo.replace(".", "")
    return float(limpo)


def formatar_moeda(valor: float) -> str:
    return f"R$ {valor:,.2f}"


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
        "2) Depois é só mandar texto, foto, áudio ou documento (PDF, Excel "
        "etc.) - eu registro tudo organizado na planilha e no Drive, na "
        "pasta da obra.\n"
        "3) Use /preco <m2> <% área molhada> para uma sugestão de valor "
        "baseada na faixa que vocês já praticam.\n"
        "4) Use /orcamento para montar o valor do aditivo a partir dos "
        "itens de material (eu calculo o total pedindo o % que o "
        "material representa do aditivo).\n\n"
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


async def cmd_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    obra = get_obra_ativa(update.effective_chat.id)
    if not obra:
        await update.message.reply_text("Defina a obra ativa primeiro com /obra <nome>.")
        return
    chat_id = update.effective_chat.id
    ORCAMENTOS_ABERTOS[chat_id] = []
    await update.message.reply_text(
        "Ok, vamos montar o orçamento de material da obra "
        f"'{obra}'.\n\n"
        "Manda os itens, um por mensagem, neste formato:\n"
        "descrição - quantidade - valor unitário\n\n"
        "Exemplo: Fio 2,5mm - 200m - 2,50\n\n"
        "Quando terminar de mandar os itens, use /fechar_orcamento "
        "(ou /cancelar_orcamento para desistir)."
    )


async def cmd_fechar_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    chat_id = update.effective_chat.id
    itens = ORCAMENTOS_ABERTOS.get(chat_id)
    if not itens:
        await update.message.reply_text(
            "Não há itens de material registrados ainda. Use /orcamento "
            "para começar."
        )
        return

    total_material = sum(item["total"] for item in itens)
    linhas = "\n".join(
        f"- {item['desc']}: {item['qtd']:g} x {formatar_moeda(item['valor_unit'])} "
        f"= {formatar_moeda(item['total'])}"
        for item in itens
    )
    ORCAMENTOS_PENDENTES[chat_id] = {"itens": itens, "total_material": total_material}
    del ORCAMENTOS_ABERTOS[chat_id]

    botoes = [
        InlineKeyboardButton(f"{p}%", callback_data=f"orcpct:{p}")
        for p in PERCENTUAIS_SUGERIDOS
    ]
    teclado = InlineKeyboardMarkup([botoes[i : i + 4] for i in range(0, len(botoes), 4)])

    await update.message.reply_text(
        f"Itens do material:\n{linhas}\n\n"
        f"Total de material: {formatar_moeda(total_material)}\n\n"
        "Esse valor de material representa quantos % do valor total do "
        "aditivo? Escolhe abaixo:",
        reply_markup=teclado,
    )


async def cmd_cancelar_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    chat_id = update.effective_chat.id
    ORCAMENTOS_ABERTOS.pop(chat_id, None)
    ORCAMENTOS_PENDENTES.pop(chat_id, None)
    await update.message.reply_text("Orçamento cancelado.")


async def cb_percentual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not autorizado(update):
        return

    chat_id = query.message.chat.id
    pendente = ORCAMENTOS_PENDENTES.get(chat_id)
    if not pendente:
        await query.edit_message_text(
            "Esse orçamento já foi encerrado ou expirou. Use /orcamento "
            "para começar um novo."
        )
        return

    pct = float(query.data.split(":")[1])
    total_material = pendente["total_material"]
    total_aditivo = total_material / (pct / 100.0)
    resto = total_aditivo - total_material

    resumo = (
        f"Material: {formatar_moeda(total_material)} ({pct:g}%)\n"
        f"Mão de obra / demais custos: {formatar_moeda(resto)} ({100 - pct:g}%)\n"
        f"Total estimado do aditivo: {formatar_moeda(total_aditivo)}"
    )
    await query.edit_message_text(f"Orçamento fechado ✅\n\n{resumo}")

    obra = get_obra_ativa(chat_id) or "Sem obra definida"
    linhas = "\n".join(
        f"- {item['desc']}: {item['qtd']:g} x {formatar_moeda(item['valor_unit'])} "
        f"= {formatar_moeda(item['total'])}"
        for item in pendente["itens"]
    )
    registrar(
        obra,
        "Orçamento",
        f"Itens:\n{linhas}\n\n{resumo}",
        query.from_user.first_name,
    )
    del ORCAMENTOS_PENDENTES[chat_id]


async def texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    obra = get_obra_ativa(update.effective_chat.id)
    if not obra:
        await update.message.reply_text("Defina a obra ativa primeiro com /obra <nome>.")
        return

    chat_id = update.effective_chat.id
    if ORCAMENTOS_ABERTOS.get(chat_id) is not None:
        partes = update.message.text.rsplit(" - ", 2)
        if len(partes) == 3:
            desc, qtd_txt, valor_txt = partes
            try:
                qtd = parse_numero(qtd_txt)
                valor_unit = parse_numero(valor_txt)
            except ValueError:
                partes = None
        else:
            partes = None

        if not partes:
            await update.message.reply_text(
                "Não entendi esse item. Manda no formato:\n"
                "descrição - quantidade - valor unitário\n"
                "Exemplo: Fio 2,5mm - 200m - 2,50\n\n"
                "Ou use /fechar_orcamento se já terminou, ou "
                "/cancelar_orcamento para desistir."
            )
            return

        item_total = qtd * valor_unit
        ORCAMENTOS_ABERTOS[chat_id].append(
            {"desc": desc.strip(), "qtd": qtd, "valor_unit": valor_unit, "total": item_total}
        )
        total_ate_agora = sum(i["total"] for i in ORCAMENTOS_ABERTOS[chat_id])
        await update.message.reply_text(
            f"Item adicionado: {desc.strip()} - {qtd:g} x "
            f"{formatar_moeda(valor_unit)} = {formatar_moeda(item_total)}\n"
            f"Total de material até agora: {formatar_moeda(total_ate_agora)}\n\n"
            "Manda mais itens ou use /fechar_orcamento para calcular o "
            "total do aditivo."
        )
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


async def documento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        return
    obra = get_obra_ativa(update.effective_chat.id)
    if not obra:
        await update.message.reply_text("Defina a obra ativa primeiro com /obra <nome>.")
        return

    doc = update.message.document
    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()
    filename = doc.file_name or f"documento_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}"
    mimetype = doc.mime_type or "application/octet-stream"
    folder_id = get_obra_folder(obra)
    link = upload_bytes(bytes(file_bytes), filename, mimetype, folder_id)

    legenda = update.message.caption or filename
    registrar(obra, "Documento", legenda, update.effective_user.first_name, link)
    await update.message.reply_text("Documento salvo na pasta da obra ✅")


def main() -> None:
    ensure_headers()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("obra", cmd_obra))
    app.add_handler(CommandHandler("preco", cmd_preco))
    app.add_handler(CommandHandler("orcamento", cmd_orcamento))
    app.add_handler(CommandHandler(["fechar_orcamento", "fechar"], cmd_fechar_orcamento))
    app.add_handler(CommandHandler(["cancelar_orcamento", "cancelar"], cmd_cancelar_orcamento))
    app.add_handler(CallbackQueryHandler(cb_percentual, pattern=r"^orcpct:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto))
    app.add_handler(MessageHandler(filters.PHOTO, foto))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, audio))
    app.add_handler(MessageHandler(filters.Document.ALL, documento))

    logger.info("Bot iniciado, aguardando mensagens...")
    app.run_polling()


if __name__ == "__main__":
    main()
