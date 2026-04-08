# HAMSTER FAUCET BOT - TURBO v8.0 (Web Interface)

Interface web para o Hamster Faucet Bot Turbo v8.0 com terminal em tempo real via WebSocket.

## Funcionalidades

- Campo para inserir Refresh Token ou ID Token
- Menu completo com todas as opções do bot
- Terminal em tempo real com logs coloridos
- Painel de estatísticas ao vivo
- Tema hacker verde com efeito Matrix
- Suporte a WebSocket para comunicação em tempo real

## Deploy na Render

1. Acesse [render.com](https://render.com) e faça login
2. Clique em **New** → **Web Service**
3. Conecte o repositório `MadagascarMods/hamster-turbo-web`
4. Configure:
   - **Name**: `hamster-turbo-bot`
   - **Runtime**: `Python`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 1 --bind 0.0.0.0:$PORT app:app`
5. Clique em **Create Web Service**

## Execução Local

```bash
pip install -r requirements.txt
python app.py
```

Acesse: http://localhost:5000
