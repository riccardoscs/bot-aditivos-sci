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
