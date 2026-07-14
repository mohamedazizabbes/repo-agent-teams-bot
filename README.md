# repo-agent-teams-bot

Microsoft Teams channel adapter for Repo Agent. Receives messages in Teams, queries the RAG backend, and posts answers back.

## Setup

1. Register a bot in the [Azure Portal](https://portal.azure.com):
   - Create an Azure AD app registration
   - Add a Bot Framework registration under "Channels" → "Microsoft Teams"
   - Note the **App ID** and **Client Secret**

2. Configure environment variables (see `.env.example`):
   ```
   MICROSOFT_APP_ID=<your-app-id>
   MICROSOFT_APP_PASSWORD=<your-client-secret>
   RAG_BACKEND_URL=https://code-explainer-c8g2.onrender.com
   ```

3. Install and run:
   ```bash
   pip install -r requirements.txt
   python main.py
   ```

4. Expose the webhook (e.g. via ngrok for local dev):
   ```
   ngrok http 3978
   ```

5. Set the messaging endpoint in Azure to `https://<your-domain>/api/messages`

## Usage

In any Teams chat where the bot is installed:

```
/code-explainer What does the auth module do?
```

Or just message the bot directly with the repo name prefix. After the first query, you can omit the `/repo_name` and the bot remembers it for that conversation.
