# DigitalDegenX Dashboard Deployment

## Best Production Shape

- Keep the Python API private on `127.0.0.1:8080`
- Build the Next.js dashboard into a Docker image once
- Let Traefik route `digitaldegenx.online` and `www.digitaldegenx.online` to that container
- Protect the dashboard with Traefik Basic Auth before the app even loads

This is cleaner than rebuilding the dashboard on every container restart.

## Files Added

- Docker image definition: [/Users/rosalindjames/DigitalDegenX_Bot/nextjs/Dockerfile](/Users/rosalindjames/DigitalDegenX_Bot/nextjs/Dockerfile)
- Docker build ignore: [/Users/rosalindjames/DigitalDegenX_Bot/nextjs/.dockerignore](/Users/rosalindjames/DigitalDegenX_Bot/nextjs/.dockerignore)
- Traefik compose template: [/Users/rosalindjames/DigitalDegenX_Bot/deploy/traefik-dashboard.compose.yml](/Users/rosalindjames/DigitalDegenX_Bot/deploy/traefik-dashboard.compose.yml)
- Traefik env example: [/Users/rosalindjames/DigitalDegenX_Bot/deploy/traefik-dashboard.env.example](/Users/rosalindjames/DigitalDegenX_Bot/deploy/traefik-dashboard.env.example)

## Why This Is Better

- The container starts with `node server.js` from Next standalone output
- No `npm install` on every boot
- No `npm run build` on every boot
- Docker healthcheck can tell if the dashboard is actually healthy
- Traefik can restart-route to a healthy container more predictably

## Password Protection

Traefik Basic Auth is the simplest safe first layer.

Generate a bcrypt password hash on the VPS:

```bash
docker run --rm httpd:2.4-alpine htpasswd -nbB admin 'choose-a-strong-password'
```

That prints something like:

```text
admin:$2y$05$...
```

Put that whole value into the env file as `DASHBOARD_BASIC_AUTH`.

## VPS Setup

Create the deployment folder:

```bash
mkdir -p /docker/digitaldegenx-dashboard
```

Copy the repo templates into place:

```bash
cp /root/meme-trade-bot/deploy/traefik-dashboard.compose.yml /docker/digitaldegenx-dashboard/docker-compose.yml
cp /root/meme-trade-bot/deploy/traefik-dashboard.env.example /docker/digitaldegenx-dashboard/.env
```

Edit `/docker/digitaldegenx-dashboard/.env` and set:

- `BOT_API_KEY`
- `DASHBOARD_BASIC_AUTH`

## Start Or Refresh

```bash
cd /docker/digitaldegenx-dashboard
docker compose build --pull
docker compose up -d
```

## Update After Code Changes

After pulling new app code:

```bash
cd /root/meme-trade-bot
git pull --no-rebase origin main
cd /docker/digitaldegenx-dashboard
docker compose build
docker compose up -d
```

## API Hardening

Your bot API should only listen on localhost:

```bash
python3 -m uvicorn api_server:app --host 127.0.0.1 --port 8080
```

That keeps it private while the dashboard talks to it over localhost using host networking.

## Recommended Follow-Up

- Rotate the exposed API key
- Replace the placeholder dashboard password
- Add real app authentication later if multiple users will access the site
