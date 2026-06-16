# Hosting Backup

Strumento per il backup automatico di hosting condivisi basati su cPanel. Richiede il backup completo tramite API cPanel, lo scarica via FTP con resume automatico, lo archivia in locale e rimuove il file remoto al termine.

> **README in English (default):** [README.md](README.md)

## Caratteristiche

- Backup completo (`fullbackup_to_homedir`) via API cPanel UAPI
- Download FTP con resume automatico in caso di interruzione
- Attesa intelligente: polling finché la dimensione del file si stabilizza
- Pulizia automatica dei backup locali scaduti (retention configurabile per host)
- **Multi-host**: ogni hosting ha la propria configurazione e cron schedule indipendente
- **Web UI**: dashboard con stato in tempo reale e avvio manuale
- **CLI**: backup, pulizia e avvio server da riga di comando
- **Notifiche**: Telegram, SMTP e Resend — configurabili dal file JSON
- **Docker-ready**: `docker compose up` e sei operativo

## Struttura del progetto

```
backup/
  config.py     — dataclass HostConfig, caricamento ftp_config.json
  cpanel.py     — richiesta backup via API cPanel
  ftp.py        — connessione, polling, download con resume, cancellazione remota
  cleaner.py    — pulizia backup locali scaduti
  runner.py     — orchestrazione completa per un host; scrive status.json
  notify.py     — notifiche Telegram / SMTP / Resend
web/
  app.py        — FastAPI: dashboard, trigger manuale, scheduler APScheduler
  templates/
    index.html  — tabella stato, badge animati, pulsante avvio
main.py         — entry point CLI
Dockerfile
docker-compose.yml
ftp_config_sample.json
```

## Configurazione

Copia `ftp_config_sample.json` in `ftp_config.json` e inserisci i tuoi dati. Il file non è incluso nel repo (`.gitignore`).

```json
{
    "notifications": { ... },
    "cpanel1": {
        "host": "dominio.com",
        "backup_local_dest_folder": "/backups",
        "cpanel_api_token": "IL_TUO_TOKEN",
        "cpanel_username": "admin",
        "ftp_password": "la_tua_password",
        "ftp_username": "backup@dominio.com",
        "mail_to_notify": "tu@dominio.com",
        "time_to_wait": 60,
        "retention_days": 30,
        "schedule": "0 2 * * *"
    }
}
```

### Riferimento campi host

| Campo | Tipo | Default | Descrizione |
|---|---|---|---|
| `host` | string | — | Hostname FTP (con o senza `ftp.`) |
| `ftp_username` | string | — | Username FTP |
| `ftp_password` | string | — | Password FTP |
| `cpanel_username` | string | — | Username cPanel |
| `cpanel_api_token` | string | — | API token cPanel (vedi sotto) |
| `backup_local_dest_folder` | string | — | Cartella radice per i backup locali (`/backups` in Docker) |
| `mail_to_notify` | string | — | Email che cPanel usa per notificare il completamento del backup |
| `time_to_wait` | int | `60` | Secondi tra un controllo e l'altro durante la generazione del backup |
| `retention_days` | int | `30` | Giorni di retention dei backup locali |
| `schedule` | string | — | Cron expression per lo scheduler automatico (timezone via `TZ`); ometti per solo manuale. I numeri standard sono supportati (0 e 7 = domenica, 1 = lunedì … 6 = sabato) |
| `request_after_download` | bool | `true` | Se sul server FTP è già presente un backup precedente e viene scaricato, richiede automaticamente un nuovo backup tramite API cPanel al termine. Evita buchi nella storia dei backup se il cron gira mentre un file vecchio è ancora sul server |

La chiave di primo livello (`cpanel1`, `website`, ecc.) è il nome usabile da CLI e nell'URL della web UI.

I backup vengono salvati in `backup_local_dest_folder/<hostname>/backup-*.tar.gz`.

### Generare un API token cPanel

1. Accedi a cPanel → **Manage API Tokens**
2. Crea un nuovo token con nome descrittivo (es. `backup-script`)
3. Incolla il valore nel campo `cpanel_api_token`

## Notifiche

Tutti i canali di notifica si configurano nella sezione `notifications` del `ftp_config.json`. Puoi abilitarne più di uno contemporaneamente — la notifica viene inviata a tutti quelli con `"enabled": true`.

### Telegram

Crea un bot con [@BotFather](https://t.me/BotFather) per ottenere il token. Per il `chat_id` usa [@userinfobot](https://t.me/userinfobot) o l'ID del canale/gruppo (prefisso `-100`).

```json
"notifications": {
    "telegram": {
        "enabled": true,
        "bot_token": "123456789:AABBcc...",
        "chat_id": "-100123456789"
    }
}
```

### SMTP

Funziona con qualsiasi server SMTP. Per Gmail usa una [App Password](https://myaccount.google.com/apppasswords) con `port: 587` e `use_ssl: false` (STARTTLS). Per SSL nativo usa `port: 465` e `use_ssl: true`.

```json
"smtp": {
    "enabled": true,
    "host": "smtp.gmail.com",
    "port": 587,
    "use_ssl": false,
    "username": "tu@gmail.com",
    "password": "app-password",
    "from": "Hosting Backup <tu@gmail.com>",
    "to": "destinatario@dominio.com"
}
```

### Resend

Alternativa SMTP cloud. Registrati su [resend.com](https://resend.com), verifica il dominio mittente e crea un API key.

```json
"resend": {
    "enabled": true,
    "api_key": "re_xxxx...",
    "from": "Hosting Backup <backup@tuodominio.com>",
    "to": "destinatario@dominio.com"
}
```

## Utilizzo

### Docker (consigliato)

```bash
cp ftp_config_sample.json ftp_config.json
# modifica ftp_config.json con le tue credenziali
docker compose up -d
```

La web UI è disponibile su `http://localhost:8080`.

I backup finiscono nel volume Docker `backups`. Per salvarli su un path fisso, modifica `docker-compose.yml`:

```yaml
volumes:
  - ./ftp_config.json:/app/ftp_config.json:ro
  - /mnt/disco_esterno:/backups        # path locale
  - data:/data
```

### Portainer

Con Portainer non hai una cartella di progetto locale, quindi il file di configurazione va creato manualmente sull'host Docker prima di fare il deploy dello stack.

**Step 1 — crea il file di configurazione sull'host** (SSH sulla macchina che esegue Docker):

```bash
mkdir -p /opt/hostingbackup
cp /path/to/ftp_config_sample.json /opt/hostingbackup/ftp_config.json
nano /opt/hostingbackup/ftp_config.json   # inserisci le tue credenziali
```

**Step 2 — build dell'immagine sull'host** (clona il repo una volta sola):

```bash
git clone https://github.com/gioxx/HostingBackup.git /opt/hostingbackup/src
docker build -t hostingbackup:latest /opt/hostingbackup/src
```

**Step 3 — crea un nuovo stack in Portainer** (Stacks → Add stack → Web editor) e incolla:

```yaml
services:
  hostingbackup:
    image: hostingbackup:latest
    ports:
      - "8080:8080"
    volumes:
      - /opt/hostingbackup/ftp_config.json:/app/ftp_config.json:ro
      - hostingbackup_backups:/backups
      - hostingbackup_data:/data
    environment:
      STATUS_FILE: /data/status.json
      CONFIG_FILE: /app/ftp_config.json
    restart: unless-stopped

volumes:
  hostingbackup_backups:
  hostingbackup_data:
```

I named volume (`hostingbackup_backups`, `hostingbackup_data`) vengono creati automaticamente e sono visibili in Portainer sotto **Volumes**. Se vuoi i backup su un path specifico dell'host, sostituisci il named volume con un bind mount:

```yaml
    volumes:
      - /opt/hostingbackup/ftp_config.json:/app/ftp_config.json:ro
      - /mnt/disco_esterno:/backups
      - hostingbackup_data:/data
```

Per aggiornare l'immagine dopo una modifica al codice, ricostruiscila sull'host e rideploya lo stack in Portainer:

```bash
cd /opt/hostingbackup/src && git pull
docker build -t hostingbackup:latest .
```

Poi in Portainer: **Stacks → hostingbackup → Redeploy**.

### Locale

```bash
pip install -r requirements.txt
cp ftp_config_sample.json ftp_config.json
# modifica ftp_config.json

# Web UI + scheduler automatico
python main.py serve

# Backup manuale di un singolo host
python main.py backup cpanel1

# Backup di tutti gli host in sequenza
python main.py backup --all

# Anteprima pulizia backup scaduti (senza cancellare)
python main.py clean cpanel1 --dry-run

# Pulizia effettiva di tutti gli host
python main.py clean --all
```

### API REST

Quando la web UI è in esecuzione:

| Metodo | Path | Descrizione |
|---|---|---|
| `GET` | `/` | Dashboard HTML |
| `GET` | `/api/status` | Stato JSON di tutti gli host |
| `GET` | `/api/hosts` | Lista host configurati |
| `POST` | `/backup/<nome>` | Avvia backup in background |

```bash
# Trigger da script bash
curl -X POST http://localhost:8080/backup/cpanel1

# Stato in JSON
curl http://localhost:8080/api/status
```

## Note

- Il file di backup viene cancellato dal server FTP solo dopo che il download locale è andato a buon fine.
- Se sul server esiste già un backup da una sessione precedente, viene scaricato direttamente senza richiederne uno nuovo.
- Lo scheduler usa timezone UTC; adatta le cron expression di conseguenza.
- I log vanno su stdout; con Docker usa `docker compose logs -f`.
