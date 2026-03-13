# DigitalDegenX Dashboard Deployment

## Production Shape

- Run the Python API privately on `127.0.0.1:8080`
- Run the Next.js dashboard privately on `127.0.0.1:3000`
- Put Nginx in front and serve `digitaldegenx.online` and `www.digitaldegenx.online`
- Keep the bot API key server-side only

The dashboard already proxies browser calls through `/api/*`, so the backend key does not need to be exposed to client-side code.

## Files Added For You

- Nginx vhost: [/Users/rosalindjames/DigitalDegenX_Bot/deploy/nginx-digitaldegenx.conf](/Users/rosalindjames/DigitalDegenX_Bot/deploy/nginx-digitaldegenx.conf)
- FastAPI service: [/Users/rosalindjames/DigitalDegenX_Bot/deploy/digitaldegenx-api.service](/Users/rosalindjames/DigitalDegenX_Bot/deploy/digitaldegenx-api.service)
- Dashboard service: [/Users/rosalindjames/DigitalDegenX_Bot/deploy/digitaldegenx-dashboard.service](/Users/rosalindjames/DigitalDegenX_Bot/deploy/digitaldegenx-dashboard.service)
- Production env example: [/Users/rosalindjames/DigitalDegenX_Bot/deploy/production.env.example](/Users/rosalindjames/DigitalDegenX_Bot/deploy/production.env.example)

## DNS

Point your domain to the server hosting the dashboard:

- `A` record for `digitaldegenx.online` to your server IP
- `CNAME` record for `www.digitaldegenx.online` to `digitaldegenx.online`

## Build And Run

Build the dashboard:

```bash
cd /Users/rosalindjames/DigitalDegenX_Bot/nextjs
/opt/homebrew/bin/npm run build
```

Start the Python API:

```bash
cd /Users/rosalindjames/DigitalDegenX_Bot
python3 -m uvicorn api_server:app --host 127.0.0.1 --port 8080
```

Start the dashboard:

```bash
cd /Users/rosalindjames/DigitalDegenX_Bot/nextjs
BOT_API_BASE_URL=http://127.0.0.1:8080 BOT_API_KEY=your-real-api-key /opt/homebrew/bin/npm run start -- --hostname 127.0.0.1 --port 3000
```

## Nginx

Copy the included config into your Nginx sites config and reload Nginx. Then issue TLS certificates:

```bash
sudo certbot --nginx -d digitaldegenx.online -d www.digitaldegenx.online
```

## Recommended Hardening

- Replace the local placeholder API key in [config.py](/Users/rosalindjames/DigitalDegenX_Bot/config.py) with a long random secret before public deployment.
- Keep FastAPI bound to `127.0.0.1` only.
- Only expose Nginx publicly.
- Consider basic auth or a login layer before exposing portfolio and trading controls on the public internet.
